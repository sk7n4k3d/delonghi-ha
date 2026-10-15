"""Test MonitorDataV2 parsing."""

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
