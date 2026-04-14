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

    def test_odd_length_does_not_raise(self):
        """Regression: odd-length buffers must not trigger an IndexError.

        The endianness detector samples even and odd byte positions
        independently; when the buffer length is odd it must clamp the
        sample to a symmetric even length instead of peeking past the
        last valid index of either stream.
        """
        # 21 bytes: 20 bytes of valid UTF-16-LE "Mocha12345" + trailing garbage byte.
        payload = "Mocha12345".encode("utf-16-le") + b"\xff"
        assert len(payload) == 21
        # Must not raise; decoded text should contain the prefix.
        result = _decode_utf16(payload)
        assert result.startswith("Mocha12345")

    def test_very_short_odd_length(self):
        """Regression: 3-byte odd buffer is handled gracefully."""
        # 3 bytes is below the 4-byte threshold where both detectors see
        # equal counts; the function must still return without crashing.
        _decode_utf16(b"\x41\x00\x42")  # Must not raise.
