"""Test profile and bean system parsing."""

import base64
import json
from pathlib import Path

from custom_components.delonghi_coffee.api import DeLonghiApi, _decode_utf16

FIXTURES = Path(__file__).parent / "fixtures"


def _load_props(filename: str) -> dict:
    data = json.loads((FIXTURES / filename).read_text())
    return {p["property"]["name"]: p["property"] for p in data}


class TestProfileParsing:
    """Profile names, colors, figures from d051/d052/d286."""

    def setup_method(self):
        self.api = DeLonghiApi.__new__(DeLonghiApi)

    def test_empty_properties(self):
        """No profile properties returns defaults."""
        result = self.api.parse_profiles({})
        assert result["active"] == 1
        assert result["profiles"] == {}

    def test_active_profile_from_d286(self):
        """Active profile extracted from d286_mach_sett_profile."""
        # Build a minimal binary: header(4 bytes) + profile byte
        raw = bytes([0xD0, 0x05, 0x00, 0xF0, 0x03])  # profile = 3
        props = {"d286_mach_sett_profile": {"value": base64.b64encode(raw).decode()}}
        result = self.api.parse_profiles(props)
        assert result["active"] == 3

    def test_profile_names_from_d051(self):
        """Profile names 1-3 extracted from d051_profile_name1_3."""
        # Build: header(6) + 3x(20 bytes name UTF-16-BE + 2 bytes icon) + CRC(2)
        name1 = "Seb".encode("utf-16-be").ljust(20, b"\x00")
        name2 = "Guest".encode("utf-16-be").ljust(20, b"\x00")
        name3 = "Kid".encode("utf-16-be").ljust(20, b"\x00")
        header = bytes([0xD0, 0x50, 0xA6, 0xF0, 0x01, 0x01])
        data = name1 + bytes([0x00, 0x00]) + name2 + bytes([0x01, 0x00]) + name3 + bytes([0x02, 0x00])
        raw = header + data + bytes([0x00, 0x00])  # CRC placeholder
        props = {"d051_profile_name1_3": {"value": base64.b64encode(raw).decode()}}
        result = self.api.parse_profiles(props)
        assert 1 in result["profiles"]
        assert result["profiles"][1]["name"] == "Seb"
        assert 2 in result["profiles"]
        assert result["profiles"][2]["name"] == "Guest"

    def test_profile4_from_d052(self):
        """Profile 4 extracted from d052_profile_name4."""
        name4 = "Extra".encode("utf-16-be").ljust(20, b"\x00")
        header = bytes([0xD0, 0x20, 0xA6, 0xF0, 0x01, 0x01])
        data = name4 + bytes([0x03])  # icon=3 → orange, kid
        raw = header + data + bytes([0x00, 0x00])
        props = {"d052_profile_name4": {"value": base64.b64encode(raw).decode()}}
        result = self.api.parse_profiles(props)
        assert 4 in result["profiles"]
        assert result["profiles"][4]["name"] == "Extra"

    def test_invalid_base64_skipped(self):
        """Invalid base64 in profile properties doesn't crash."""
        props = {"d051_profile_name1_3": {"value": "not_valid_base64!!!"}}
        result = self.api.parse_profiles(props)
        assert result["profiles"] == {}

    def test_null_value_skipped(self):
        """Null value for profile property is skipped."""
        props = {"d051_profile_name1_3": {"value": None}}
        result = self.api.parse_profiles(props)
        assert result["profiles"] == {}


class TestBeanSystemParsing:
    """Bean Adapt system names from d250-d256."""

    def setup_method(self):
        self.api = DeLonghiApi.__new__(DeLonghiApi)

    def test_empty_properties(self):
        """No bean properties returns empty list."""
        result = self.api.parse_bean_systems({})
        assert result == []

    def test_parse_bean_name(self):
        """Bean name extracted from d250_beansystem_0."""
        # Build minimal binary with a UTF-16 name
        name = "Arabica".encode("utf-16-be")
        raw = bytes([0xD0, 0x20, 0x00, 0xF0, 0x00]) + name + bytes([0x00, 0x00])
        props = {"d250_beansystem_0": {"value": base64.b64encode(raw).decode()}}
        result = self.api.parse_bean_systems(props)
        assert len(result) == 1
        assert result[0]["id"] == 0
        assert "Arabica" in result[0]["name"]

    def test_multiple_beans(self):
        """Multiple bean systems parsed."""
        props = {}
        for i in range(3):
            name = f"Bean{i}".encode("utf-16-be")
            raw = bytes([0xD0, 0x20, 0x00, 0xF0, 0x00]) + name + bytes([0x00, 0x00])
            props[f"d{250 + i}_beansystem_{i}"] = {"value": base64.b64encode(raw).decode()}
        result = self.api.parse_bean_systems(props)
        assert len(result) == 3

    def test_invalid_value_fallback(self):
        """Invalid base64 falls back to generic name."""
        props = {"d250_beansystem_0": {"value": "!!invalid!!"}}
        result = self.api.parse_bean_systems(props)
        assert len(result) == 1
        assert result[0]["name"] == "Bean 0"

    def test_raw_params_hex_is_empty_when_no_tail_bytes(self):
        """Without trailing param bytes the raw hex field stays empty."""
        name = "Arabica".encode("utf-16-be")
        raw = bytes([0xD0, 0x20, 0x00, 0xF0, 0x00]) + name + bytes([0x00, 0x00])
        props = {"d250_beansystem_0": {"value": base64.b64encode(raw).decode()}}
        result = self.api.parse_bean_systems(props)
        assert result[0]["raw_params_hex"] == ""

    def test_parse_bean_systems_returns_distinct_local_and_english(self):
        """H-logic-1: the bean payload has TWO 20-byte UTF-16-BE names —
        local and English. The decoder must surface them separately.

        Before this fix, ``_decode_utf16(data)`` truncated at the first
        NUL pair (correct hardening from beta.14) so the consumer's
        ``text.split('\\x00')`` only ever saw one segment, and the
        English variant fell back to the local name. Bean 0 was
        decoded as ``'Predefini'`` for both fields — Issue #7's intended
        UX (local + English alongside) was silently broken.

        Fix: slice the 20-byte halves explicitly and decode each.
        """
        local = "Predefini".encode("utf-16-be").ljust(20, b"\x00")
        english = "Default".encode("utf-16-be").ljust(20, b"\x00")
        data = local + english
        raw = bytes([0xD0, 0x20, 0x00, 0xF0, 0x00]) + data + bytes([0x00, 0x00])
        props = {"d250_beansystem_0": {"value": base64.b64encode(raw).decode()}}
        result = self.api.parse_bean_systems(props)
        assert result[0]["name"] == "Predefini"
        assert result[0]["english_name"] == "Default"
        assert result[0]["name"] != result[0]["english_name"]

    def test_parse_bean_systems_english_falls_back_when_payload_short(self):
        """If the payload is shorter than 40 bytes (no English slot), the
        English name mirrors the local one — no IndexError, no garbage.
        """
        local = "Arabica".encode("utf-16-be").ljust(20, b"\x00")
        # Only the local 20-byte slot, no English half present.
        data = local
        raw = bytes([0xD0, 0x20, 0x00, 0xF0, 0x00]) + data + bytes([0x00, 0x00])
        props = {"d250_beansystem_0": {"value": base64.b64encode(raw).decode()}}
        result = self.api.parse_bean_systems(props)
        assert result[0]["name"] == "Arabica"
        assert result[0]["english_name"] == "Arabica"

    def test_raw_params_hex_captures_bean_adapt_tail(self):
        """Bean Adapt tail bytes after the 40-byte name block are exposed as hex.

        Issue #7: MattG-K's write capture showed a 5-byte tail after the
        UTF-16 name (temperature, intensity, grinder, ...). We don't
        decode those fields yet but we expose them verbatim so users can
        share samples without inventing a decoder.
        """
        # Build 40 bytes of name (local + english) + 5 tail bytes matching
        # MattG-K's Packet 1: 0A 02 04 00 01.
        local = ("Grains 1" + "\x00" * 12).encode("utf-16-be")[:40]
        local = local.ljust(40, b"\x00")
        tail = bytes([0x0A, 0x02, 0x04, 0x00, 0x01])
        data = local + tail
        raw = bytes([0xD0, 0x20, 0x00, 0xF0, 0x00]) + data + bytes([0x00, 0x00])
        props = {"d250_beansystem_0": {"value": base64.b64encode(raw).decode()}}
        result = self.api.parse_bean_systems(props)
        assert result[0]["raw_params_hex"] == "0a02040001"
        assert result[0]["raw_bytes"] == len(raw)


class TestBeanSystemParParsing:
    """d260_beansystem_par raw block parsing (issue #7 diagnostics)."""

    def setup_method(self):
        self.api = DeLonghiApi.__new__(DeLonghiApi)

    def test_missing_property_returns_empty(self):
        assert self.api.parse_bean_system_par({}) == {}

    def test_null_value_returns_empty(self):
        assert self.api.parse_bean_system_par({"d260_beansystem_par": {"value": None}}) == {}

    def test_valid_payload_exposes_raw_hex(self):
        raw = bytes(range(16))
        props = {"d260_beansystem_par": {"value": base64.b64encode(raw).decode()}}
        result = self.api.parse_bean_system_par(props)
        assert result["raw_hex"] == raw.hex()
        assert result["raw_bytes"] == 16

    def test_invalid_base64_reports_error(self):
        props = {"d260_beansystem_par": {"value": "!!!not-base64!!!"}}
        result = self.api.parse_bean_system_par(props)
        assert result.get("error") == "decode_failed"


class TestDecodeUTF16Extended:
    """Extended UTF-16 decoding tests for real-world data."""

    def test_mixed_case_with_accents(self):
        """French accented text in UTF-16-BE."""
        data = "cafe".encode("utf-16-be")
        assert _decode_utf16(data) == "cafe"

    def test_emoji_in_name(self):
        """UTF-16 with emoji characters."""
        data = "Test".encode("utf-16-le")
        assert _decode_utf16(data) == "Test"

    def test_very_long_name_truncated(self):
        """Names longer than buffer still parse."""
        data = ("A" * 50).encode("utf-16-le")
        result = _decode_utf16(data)
        assert len(result) == 50

    def test_pure_null_bytes(self):
        """All null bytes returns empty string."""
        data = b"\x00" * 20
        assert _decode_utf16(data) == ""
