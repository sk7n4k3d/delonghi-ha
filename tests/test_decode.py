"""Test UTF-16 decoding for profile/recipe names."""

from custom_components.delonghi_coffee.api import _decode_utf16


class TestDecodeUTF16:
    """De'Longhi stores names in UTF-16 with variable endianness."""

    def test_utf16_le(self):
        """Standard UTF-16-LE ASCII text."""
        data = "Seb".encode("utf-16-le")
        assert _decode_utf16(data) == "Seb"

    def test_utf16_be(self):
        """UTF-16-BE text (some properties use this)."""
        data = "Profile".encode("utf-16-be")
        assert _decode_utf16(data) == "Profile"

    def test_with_null_padding(self):
        """Names often have trailing null bytes."""
        data = "Test".encode("utf-16-le") + b"\x00\x00\x00\x00"
        assert _decode_utf16(data) == "Test"

    def test_empty_data(self):
        """Empty bytes returns empty string."""
        assert _decode_utf16(b"") == ""

    def test_single_byte(self):
        """Single byte (too short for UTF-16)."""
        assert _decode_utf16(b"\x41") == ""

    def test_accented_characters(self):
        """French accented characters."""
        data = "Cafe Latte".encode("utf-16-le")
        assert _decode_utf16(data) == "Cafe Latte"

    def test_all_nulls(self):
        """All null bytes (empty name)."""
        result = _decode_utf16(b"\x00\x00\x00\x00")
        assert result == ""
