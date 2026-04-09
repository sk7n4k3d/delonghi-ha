"""Tests for Bean Adapt write support (issue #7).

Opcodes shared by @MattG-K:
    185 | 0xB9 Select Bean System
    186 | 0xBA Read Bean System
    187 | 0xBB Write Bean System

Framing matches every other ECAM command already in use:
    [0x0D][len][opcode][payload][CRC-16/SPI-FUJITSU]

Tests build bodies, verify length byte, verify CRC round-trip, and
replay MattG-K's captured Write Bean System payload byte-for-byte
(name="Grains 1", temperature=high=0x0A, intensity=strong=0x02,
grinder level 5=0x04, flag1=0, flag2=1).
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from custom_components.delonghi_coffee.api import DeLonghiApi, DeLonghiApiError
from custom_components.delonghi_coffee.const import (
    BEAN_NAME_MAX_BYTES,
    OPCODE_READ_BEAN_SYSTEM,
    OPCODE_SELECT_BEAN_SYSTEM,
    OPCODE_WRITE_BEAN_SYSTEM,
)

# Raw payload MattG-K posted on issue #7 for Bean Adapt with name="Grains 1",
# temperature=high, intensity=strong, grinder setting=5. 46 bytes total:
# [1B slot][40B UTF-16-BE name][5B tail].
MATTGK_PAYLOAD = (
    bytes([0x01])
    + "Grains 1".encode("utf-16-be").ljust(40, b"\x00")
    + bytes([0x0A, 0x02, 0x04, 0x00, 0x01])
)


def _make_api() -> DeLonghiApi:
    """Build a DeLonghiApi instance with a mock session/token for unit tests."""
    api = DeLonghiApi.__new__(DeLonghiApi)
    api._email = "test@example.com"
    api._password = "password"
    api._session = MagicMock()
    api._ayla_token = "fake_token"
    api._ayla_refresh = None
    api._token_expires = time.time() + 86400
    api._ayla_app_id = "test_app_id"
    api._ayla_app_secret = "test_secret"
    api._ayla_user = "https://user.test.com"
    api._ayla_ads = "https://ads.test.com"
    api._oem_model = "DL-striker-cb"
    api._cmd_property = "app_data_request"
    api._ping_supported = None
    api._rate_tracker = MagicMock()
    return api


class TestSelectBeanSystemFrame:
    """0xB9 Select Bean System: [0x0D][0x05][0xB9][slot][CRC]."""

    def test_opcode_constant(self):
        assert OPCODE_SELECT_BEAN_SYSTEM == 0xB9

    def test_build_select_body_slot1(self):
        body = DeLonghiApi._build_bean_select_body(1)
        assert body == bytes([0x0D, 0x05, 0xB9, 0x01])
        assert body[0] == 0x0D
        assert body[2] == OPCODE_SELECT_BEAN_SYSTEM

    def test_build_select_body_slot7(self):
        body = DeLonghiApi._build_bean_select_body(7)
        assert body == bytes([0x0D, 0x05, 0xB9, 0x07])

    def test_select_frame_crc_and_length(self):
        """Full frame: len byte must equal total-1, CRC deterministic."""
        body = DeLonghiApi._build_bean_select_body(3)
        cmd = body + DeLonghiApi._crc16(body)
        assert len(cmd) == 6
        assert cmd[1] == len(cmd) - 1  # 0x05
        # CRC must be deterministic for the same body
        assert DeLonghiApi._crc16(body) == DeLonghiApi._crc16(body)

    def test_select_invalid_slot_zero(self):
        with pytest.raises(DeLonghiApiError):
            DeLonghiApi._build_bean_select_body(0)

    def test_select_invalid_slot_eight(self):
        with pytest.raises(DeLonghiApiError):
            DeLonghiApi._build_bean_select_body(8)

    def test_select_bean_system_sends_command(self):
        api = _make_api()
        api.send_command = MagicMock(return_value=True)  # type: ignore[method-assign]
        assert api.select_bean_system("DSN", 2) is True
        api.send_command.assert_called_once()
        _, sent = api.send_command.call_args[0]
        assert sent[0] == 0x0D
        assert sent[2] == 0xB9
        assert sent[3] == 0x02
        assert sent[1] == len(sent) - 1
        # Verify trailing CRC matches the body we'd expect
        assert sent[-2:] == DeLonghiApi._crc16(sent[:-2])


class TestReadBeanSystemFrame:
    """0xBA Read Bean System: [0x0D][0x05][0xBA][slot][CRC]."""

    def test_opcode_constant(self):
        assert OPCODE_READ_BEAN_SYSTEM == 0xBA

    def test_build_read_body_slot1(self):
        body = DeLonghiApi._build_bean_read_body(1)
        assert body == bytes([0x0D, 0x05, 0xBA, 0x01])

    def test_read_frame_length_byte(self):
        body = DeLonghiApi._build_bean_read_body(5)
        cmd = body + DeLonghiApi._crc16(body)
        assert cmd[1] == len(cmd) - 1
        assert cmd[2] == 0xBA
        assert cmd[3] == 0x05

    def test_read_invalid_slot(self):
        with pytest.raises(DeLonghiApiError):
            DeLonghiApi._build_bean_read_body(0)
        with pytest.raises(DeLonghiApiError):
            DeLonghiApi._build_bean_read_body(42)

    def test_read_bean_system_sends_command(self):
        api = _make_api()
        api.send_command = MagicMock(return_value=True)  # type: ignore[method-assign]
        assert api.read_bean_system("DSN", 4) is True
        _, sent = api.send_command.call_args[0]
        assert sent[2] == 0xBA
        assert sent[3] == 0x04
        assert sent[1] == len(sent) - 1


class TestWriteBeanSystemFrame:
    """0xBB Write Bean System: [0x0D][len][0xBB][slot][40B name][5B tail][CRC]."""

    def test_opcode_constant(self):
        assert OPCODE_WRITE_BEAN_SYSTEM == 0xBB

    def test_bean_name_max_bytes(self):
        assert BEAN_NAME_MAX_BYTES == 40

    def test_encode_bean_name_pads_to_40(self):
        """Short name is null-padded to 40 bytes UTF-16-BE."""
        encoded = DeLonghiApi._encode_bean_name("Grains 1")
        assert len(encoded) == 40
        # Name bytes + null padding
        assert encoded.startswith("Grains 1".encode("utf-16-be"))
        assert encoded.endswith(b"\x00" * 24)
        # Decode round-trip
        assert encoded.decode("utf-16-be").rstrip("\x00") == "Grains 1"

    def test_encode_bean_name_exactly_20_chars(self):
        """Exactly 20 UTF-16-BE code units = 40 bytes = no padding."""
        name = "A" * 20
        encoded = DeLonghiApi._encode_bean_name(name)
        assert len(encoded) == 40
        assert encoded == name.encode("utf-16-be")

    def test_encode_bean_name_too_long(self):
        """21 ASCII chars = 42 UTF-16-BE bytes > 40, must raise."""
        with pytest.raises(DeLonghiApiError, match="too long"):
            DeLonghiApi._encode_bean_name("A" * 21)

    def test_encode_bean_name_multibyte_boundary(self):
        """Emoji uses surrogate pairs — 11 emoji = 44 bytes, must raise."""
        with pytest.raises(DeLonghiApiError, match="too long"):
            DeLonghiApi._encode_bean_name("🫘" * 11)

    def test_write_body_layout(self):
        """Full body layout assertion for a known profile."""
        body = DeLonghiApi._build_bean_write_body(
            slot=1,
            name="Grains 1",
            temperature=0x0A,
            intensity=0x02,
            grinder=0x04,
            flag1=0,
            flag2=1,
        )
        # Header: 0x0D, len, 0xBB
        assert body[0] == 0x0D
        assert body[2] == 0xBB
        # Slot
        assert body[3] == 0x01
        # Name bytes 4..44
        assert body[4:44] == "Grains 1".encode("utf-16-be").ljust(40, b"\x00")
        # Tail
        assert body[44:49] == bytes([0x0A, 0x02, 0x04, 0x00, 0x01])
        # len = body + 2 CRC - 1, body is 49 bytes here → total = 51 → len = 50
        assert body[1] == 0x32
        assert len(body) == 49

    def test_write_body_matches_mattgk_capture(self):
        """Payload bytes must replay MattG-K's captured Write Bean System."""
        body = DeLonghiApi._build_bean_write_body(
            slot=1,
            name="Grains 1",
            temperature=0x0A,
            intensity=0x02,
            grinder=0x04,
            flag1=0,
            flag2=1,
        )
        # Strip the 3-byte ECAM header (0x0D, len, opcode) → pure payload
        assert body[3:] == MATTGK_PAYLOAD
        # Full expected body hex (header + payload, no CRC)
        expected_hex = (
            "0d32bb01"
            "0047007200610069006e0073002000310000000000000000000000000000000000000000000000000a02040001"
        )
        assert body.hex() == expected_hex

    def test_write_full_frame_round_trip(self):
        """Full ECAM frame: body + CRC, length byte consistent."""
        body = DeLonghiApi._build_bean_write_body(
            slot=1,
            name="Grains 1",
            temperature=0x0A,
            intensity=0x02,
            grinder=0x04,
        )
        cmd = body + DeLonghiApi._crc16(body)
        assert len(cmd) == 51
        assert cmd[1] == len(cmd) - 1  # 0x32
        # CRC re-computes from the body
        assert cmd[-2:] == DeLonghiApi._crc16(cmd[:-2])

    def test_write_invalid_slot(self):
        with pytest.raises(DeLonghiApiError, match="slot"):
            DeLonghiApi._build_bean_write_body(
                slot=0, name="X", temperature=0, intensity=0, grinder=0
            )
        with pytest.raises(DeLonghiApiError, match="slot"):
            DeLonghiApi._build_bean_write_body(
                slot=8, name="X", temperature=0, intensity=0, grinder=0
            )

    def test_write_invalid_flag(self):
        with pytest.raises(DeLonghiApiError, match="flag1"):
            DeLonghiApi._build_bean_write_body(
                slot=1,
                name="X",
                temperature=0,
                intensity=0,
                grinder=0,
                flag1=2,
            )
        with pytest.raises(DeLonghiApiError, match="flag2"):
            DeLonghiApi._build_bean_write_body(
                slot=1,
                name="X",
                temperature=0,
                intensity=0,
                grinder=0,
                flag2=9,
            )

    def test_write_invalid_byte_range(self):
        with pytest.raises(DeLonghiApiError):
            DeLonghiApi._build_bean_write_body(
                slot=1, name="X", temperature=300, intensity=0, grinder=0
            )
        with pytest.raises(DeLonghiApiError):
            DeLonghiApi._build_bean_write_body(
                slot=1, name="X", temperature=0, intensity=-1, grinder=0
            )

    def test_write_bean_system_sends_command(self):
        api = _make_api()
        api.send_command = MagicMock(return_value=True)  # type: ignore[method-assign]
        result = api.write_bean_system(
            "DSN",
            slot=1,
            name="Grains 1",
            temperature=0x0A,
            intensity=0x02,
            grinder=0x04,
            flag1=0,
            flag2=1,
        )
        assert result is True
        _, sent = api.send_command.call_args[0]
        assert sent[0] == 0x0D
        assert sent[2] == 0xBB
        # Payload after header equals MattG-K's capture
        assert sent[3 : 3 + len(MATTGK_PAYLOAD)] == MATTGK_PAYLOAD
        # CRC is at the tail
        assert sent[-2:] == DeLonghiApi._crc16(sent[:-2])
        # Length byte consistency
        assert sent[1] == len(sent) - 1
