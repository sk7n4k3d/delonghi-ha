"""Test MonitorDataV2 parsing."""

import base64

import pytest

from custom_components.delonghi_coffee.api import DeLonghiApi


class TestMonitorV2:
    """Parse binary monitor data from known machine states."""

    def _parse(self, hex_str: str) -> dict:
        raw = bytes.fromhex(hex_str)
        return DeLonghiApi._parse_monitor_v2(raw)

    def test_ready_no_alarms(self):
        """Machine ready (state=7), profile 1, no alarms."""
        # 14 bytes: header(4) + profile(1) + acc(2) + alarm0(1) + alarm1(1)
        #           + state(1) + sub(1) + extra(1) + alarm2(1) + alarm3(1)
        raw = "d0 0c a4 f0 01 02 00 00 00 07 00 00 00 00".replace(" ", "")
        result = self._parse(raw)
        assert result["machine_state"] == "Ready"
        assert result["profile"] == 1
        assert result["alarms"] == []
        assert result["alarm_word"] == 0

    def test_off_state(self):
        """Machine off (state=0)."""
        raw = "d0 0c a4 f0 02 00 00 00 00 00 00 00 00 00".replace(" ", "")
        result = self._parse(raw)
        assert result["machine_state"] == "Off"
        assert result["profile"] == 2

    def test_brewing_state(self):
        """Machine brewing (state=3)."""
        raw = "d0 0c a4 f0 01 02 00 00 00 03 00 00 00 00".replace(" ", "")
        result = self._parse(raw)
        assert result["machine_state"] == "Brewing"

    def test_water_tank_empty_alarm(self):
        """Alarm bit 0 = Water Tank Empty."""
        # alarm byte 0 = 0x01 (bit 0 set)
        raw = "d0 0c a4 f0 01 02 00 01 00 07 00 00 00 00".replace(" ", "")
        result = self._parse(raw)
        assert len(result["alarms"]) == 1
        assert result["alarms"][0]["bit"] == 0
        assert result["alarms"][0]["name"] == "Water Tank Empty"

    def test_multiple_alarms(self):
        """Multiple alarm bits set."""
        # alarm byte 0 = 0x03 (bits 0+1), byte 1 = 0x00
        raw = "d0 0c a4 f0 01 00 00 03 00 07 00 00 00 00".replace(" ", "")
        result = self._parse(raw)
        assert len(result["alarms"]) == 2
        bits = {a["bit"] for a in result["alarms"]}
        assert bits == {0, 1}

    def test_high_alarm_bytes(self):
        """Alarms in bytes [12] and [13] (bits 16+)."""
        # alarm byte 2 = 0x01 (bit 16 = Cleaning Needed)
        raw = "d0 0c a4 f0 01 00 00 00 00 07 00 00 01 00".replace(" ", "")
        result = self._parse(raw)
        assert len(result["alarms"]) == 1
        assert result["alarms"][0]["bit"] == 16

    def test_too_short_data(self):
        """Data shorter than 14 bytes returns defaults."""
        result = self._parse("d00ca4f001")
        assert result["machine_state"] == "Unknown"
        assert result["alarms"] == []

    def test_alarm_word_construction(self):
        """Verify 32-bit alarm word from 4 scattered bytes."""
        # byte[7]=0xFF, byte[8]=0x00, byte[12]=0x00, byte[13]=0x01
        raw = "d0 0c a4 f0 01 00 00 ff 00 07 00 00 00 01".replace(" ", "")
        result = self._parse(raw)
        expected = 0xFF | (0x00 << 8) | (0x00 << 16) | (0x01 << 24)
        assert result["alarm_word"] == expected

    def test_extended_operation_states(self):
        """Dispensing-phase codes >= 10 map to names instead of Unknown (issue #42)."""
        for code, name in [
            (10, "Preparing Milk"),
            (11, "Dispensing Hot Water"),
            (12, "Cleaning Milk System"),
            (14, "Rinsing Water Filter"),
            (16, "Preparing Chocolate"),
            (17, "Preparing Milk Alt"),
        ]:
            raw = f"d00ca4f00102000000{code:02x}00000000"
            result = self._parse(raw)
            assert result["machine_state"] == name, f"state {code}"

    def test_unknown_code_still_flagged(self):
        """Codes without a mapping keep the Unknown (N) rendering."""
        raw = "d00ca4f001020000001d00000000"  # 0x1d = 29
        result = self._parse(raw)
        assert result["machine_state"] == "Unknown (29)"

    def test_action_and_progress(self):
        """Bytes [10]/[11] are exposed as action/progress (cremalink naming)."""
        raw = "d0 0c a4 f0 01 02 00 00 00 0a 03 2a 00 00".replace(" ", "")
        result = self._parse(raw)
        assert result["machine_state"] == "Preparing Milk"
        assert result["action"] == 3
        assert result["progress"] == 0x2A

    def test_real_striker_frame(self):
        """Live frame captured from an Eletta Explore (state=7, Cleaning Needed)."""
        raw = "d012750f010100000007000001000000003ae46ad0aae1"
        result = self._parse(raw)
        assert result["machine_state"] == "Ready"
        assert result["action"] == 0
        assert result["progress"] == 0
        assert [a["name"] for a in result["alarms"]] == ["Cleaning Needed"]


class TestMonitorPayloadDecoding:
    """decode_monitor_payload / parse_monitor_hex / parse_monitor_value.

    The same MonitorDataV2 frame reaches us base64-encoded from the Ayla
    cloud and (per the issue #10 LAN logs) hex-encoded on the LAN socket, so
    the decoder has to tell the two apart — issue #50.
    """

    HEX_FRAME = "d012750f010100000007000001000000003ae46ad0aae1"
    B64_FRAME = "0BJ1DwEBAAAABwAAAQAAAAA65GrQquE="

    def test_hex_frame(self):
        result = DeLonghiApi.parse_monitor_hex(self.HEX_FRAME)
        assert result["machine_state"] == "Ready"
        assert result["monitor_raw"] == self.HEX_FRAME
        assert [a["name"] for a in result["alarms"]] == ["Cleaning Needed"]

    def test_base64_frame(self):
        result = DeLonghiApi.parse_monitor_hex(self.B64_FRAME)
        assert result["machine_state"] == "Ready"
        assert result["monitor_raw"] == self.HEX_FRAME

    def test_raw_bytes_pass_through(self):
        result = DeLonghiApi.parse_monitor_hex(bytes.fromhex(self.HEX_FRAME))
        assert result["machine_state"] == "Ready"

    def test_decode_prefers_the_monitor_looking_candidate(self):
        """A string that decodes both ways must yield the real frame."""
        assert DeLonghiApi.decode_monitor_payload(self.B64_FRAME) == bytes.fromhex(self.HEX_FRAME)
        assert DeLonghiApi.decode_monitor_payload(self.HEX_FRAME) == bytes.fromhex(self.HEX_FRAME)

    def test_decode_rejects_undersized_and_garbage(self):
        assert DeLonghiApi.decode_monitor_payload("AAA=") is None  # 1 byte
        assert DeLonghiApi.decode_monitor_payload("0d1784cafebabe") is None  # 7 bytes as hex
        assert DeLonghiApi.decode_monitor_payload("not a monitor") is None
        assert DeLonghiApi.decode_monitor_payload("") is None
        assert DeLonghiApi.decode_monitor_payload(None) is None
        assert DeLonghiApi.decode_monitor_payload(1234) is None

    def test_parse_monitor_hex_raises_on_garbage(self):
        with pytest.raises(ValueError):
            DeLonghiApi.parse_monitor_hex("zzzz")

    def test_parse_monitor_value_never_raises(self):
        assert DeLonghiApi.parse_monitor_value("zzzz") is None
        assert DeLonghiApi.parse_monitor_value(None) is None
        assert DeLonghiApi.parse_monitor_value(self.B64_FRAME)["machine_state"] == "Ready"


class TestMonitorTimestamp:
    """Issue #47 — the unix clock sitting in the MonitorDataV2 trailer.

    The power-on confirmation can only tell a fresh frame from a frozen cloud
    cache if the frame carries its own clock: ``[payload…][CRC-16 2][unix ts BE 4]``.

    Both *real* frames this suite carries prove the layout, and the CRC is the
    proof that it is a layout and not a coincidence — on each one,
    ``_crc16(raw[:-6])`` reproduces the two bytes at ``[-6:-4]`` exactly, and
    the four bytes after them are the day the frame was captured:

    * millcore ``d302_monitor`` (issues #3/#10 dump) → 2026-04-02;
    * live Eletta Explore frame → 2026-10-15.

    The second one is the answer to the open question of issue #47: the striker
    family really does stamp its monitor, so the confirmation can rely on the
    clock and not only on raw-change comparison.
    """

    MILLREAL_FRAME = "d012750f00000000000700000000000000d6a369ceb82b"
    STRIKER_FRAME = "d012750f010100000007000001000000003ae46ad0aae1"
    MILLREAL_TS = 1775155243  # 2026-04-02T18:40:43Z
    STRIKER_TS = 1792060129  # 2026-10-15T10:28:49Z

    def test_millcore_dump_is_stamped_with_its_capture_date(self):
        raw = bytes.fromhex(self.MILLREAL_FRAME)
        assert DeLonghiApi._crc16(raw[:-6]) == raw[-6:-4]
        assert DeLonghiApi.monitor_timestamp(raw) == self.MILLREAL_TS

    def test_striker_frame_is_stamped_too(self):
        """The Eletta Explore (the #47 machine) embeds the same trailer."""
        raw = bytes.fromhex(self.STRIKER_FRAME)
        assert DeLonghiApi._crc16(raw[:-6]) == raw[-6:-4]
        assert DeLonghiApi.monitor_timestamp(raw) == self.STRIKER_TS

    def test_short_or_absent_frame_has_no_timestamp(self):
        assert DeLonghiApi.monitor_timestamp(None) is None
        assert DeLonghiApi.monitor_timestamp(b"") is None
        # 14 bytes: parseable monitor, no room for CRC + clock.
        assert DeLonghiApi.monitor_timestamp(bytes.fromhex("d00ca4f001020000000700000000")) is None
        # The synthetic Eletta fixture of properties_eletta.json (15 bytes,
        # zero tail) proves nothing about the real machine — it is not a capture.
        assert DeLonghiApi.monitor_timestamp(base64.b64decode("0AwkpPABAgAAAQcAAAAA")) is None

    def test_filler_tails_are_not_dates(self):
        payload = bytes.fromhex("d012750f010100000007000001000000")  # 17 B, no trailer
        assert DeLonghiApi.monitor_timestamp(payload + b"\x00\x00\x00\x00\x00\x00") is None
        assert DeLonghiApi.monitor_timestamp(payload + b"\x00\x00\xff\xff\xff\xff") is None

    def test_counter_below_the_wallclock_floor_is_rejected(self):
        """An uptime counter (or any pre-2020 value) is not a date."""
        payload = bytes.fromhex("d012750f010100000007000001000000")
        assert DeLonghiApi.monitor_timestamp(payload + b"\x00\x00" + (12345).to_bytes(4, "big")) is None

    def test_app_signature_tail_shifts_the_clock(self):
        """Command-echo frames append the app id *after* the clock → read [-8:-4]."""
        payload = bytes.fromhex("d012750f0101000000070000010000")
        frame = payload + b"\x00\x00" + self.STRIKER_TS.to_bytes(4, "big") + bytes([0x20, 0x40, 0x35, 0xEF])
        assert DeLonghiApi.monitor_timestamp(frame) == self.STRIKER_TS

    def test_parse_monitor_v2_exposes_monitor_ts(self):
        assert DeLonghiApi._parse_monitor_v2(bytes.fromhex(self.STRIKER_FRAME))["monitor_ts"] == self.STRIKER_TS
        # The key always exists, even when there is nothing to parse.
        assert DeLonghiApi._parse_monitor_v2(bytes.fromhex("d00ca4f001"))["monitor_ts"] is None
        assert DeLonghiApi._parse_monitor_v2(bytes.fromhex("d00ca4f001020000000700000000"))["monitor_ts"] is None

    def test_parse_monitor_hex_carries_monitor_ts(self):
        """The LAN path (``parse_monitor_value``) gets the clock for free."""
        assert DeLonghiApi.parse_monitor_hex(self.STRIKER_FRAME)["monitor_ts"] == self.STRIKER_TS
        assert DeLonghiApi.parse_monitor_value(self.STRIKER_FRAME)["monitor_ts"] == self.STRIKER_TS
