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


class TestDecodeUTF16FixedWidthBuffer:
    """v1.6.0-beta.14: when the caller passes a fixed-size buffer that
    contains a NUL-terminated UTF-16 string followed by trailing garbage
    from an adjacent struct field, the decoder must stop at the
    terminator instead of concatenating the garbage. Without this guard,
    bean profile names landed as 'Bean1Bean2…' on Bastien's machine."""

    def test_truncates_at_first_aligned_null_terminator_le(self):
        # 'Bean1' + UTF-16-LE NUL + garbage from next slot
        data = "Bean1".encode("utf-16-le") + b"\x00\x00" + "Bean2".encode("utf-16-le")
        assert _decode_utf16(data) == "Bean1"

    def test_truncates_at_first_aligned_null_terminator_be(self):
        data = "Profile".encode("utf-16-be") + b"\x00\x00" + "Junk".encode("utf-16-be")
        assert _decode_utf16(data) == "Profile"

    def test_strips_c0_control_chars_leaking_from_adjacent_field(self):
        """A length-prefix byte 0x01 just after the name slot used to
        leak in as a control character ('Bean1\\x01')."""
        data = "Bean1".encode("utf-16-le") + b"\x01\x00" + b"\x00\x00"
        # \x01 control char must be stripped
        assert _decode_utf16(data) == "Bean1"

    def test_keeps_tab_and_newline(self):
        data = "Line1\nLine2".encode("utf-16-le")
        assert _decode_utf16(data) == "Line1\nLine2"

    def test_real_bastien_bean_0_no_longer_leaks(self):
        """Regression on the exact pattern observed 2026-05-01:
        'Prédéfini' + null + 'Démarrer' + 0x01 byte + null
        used to render as 'PrédéfiniDémarrer\\x01Ā'."""
        # Reconstruct the leaky buffer with two UTF-16-LE strings glued together
        leaky = (
            "Prédéfini".encode("utf-16-le")
            + b"\x00\x00"  # explicit terminator after first name
            + "Démarrer".encode("utf-16-le")
            + b"\x01\x00"  # control byte from next struct field
        )
        result = _decode_utf16(leaky)
        # Only the first NUL-terminated name survives
        assert result == "Prédéfini"
        # The leaked second name is gone
        assert "Démarrer" not in result

    def test_buffer_without_terminator_decodes_full_content(self):
        """If the caller pre-trimmed the buffer to the exact name length
        (no terminator), the decoder must still produce the right result."""
        data = "Bean1".encode("utf-16-le")
        assert _decode_utf16(data) == "Bean1"

    def test_buffer_starting_with_null_returns_empty(self):
        """A name slot that's all zeros must yield an empty string."""
        data = b"\x00\x00\x00\x00\x00\x00"
        assert _decode_utf16(data) == ""
