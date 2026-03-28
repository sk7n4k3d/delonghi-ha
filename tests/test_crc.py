"""Test CRC-16/SPI-FUJITSU implementation."""

from custom_components.delonghi_coffee.api import DeLonghiApi


class TestCRC16:
    """Verify CRC against known ECAM commands from MITM captures."""

    def test_monitor_command(self):
        """Monitor command CRC from confirmed working capture."""
        body = bytes.fromhex("0d07840f0302")
        assert DeLonghiApi._crc16(body) == bytes.fromhex("5640")

    def test_power_on_command(self):
        """Power on CRC from const.py (credit: MattG-K)."""
        body = bytes.fromhex("0d07840f0201")
        assert DeLonghiApi._crc16(body) == bytes.fromhex("5512")

    def test_power_off_command(self):
        """Power off CRC from const.py (credit: MattG-K)."""
        body = bytes.fromhex("0d07840f0101")
        assert DeLonghiApi._crc16(body) == bytes.fromhex("0041")

    def test_brew_espresso_capture(self):
        """Brew espresso CRC from MITM capture."""
        # Full command: 0d 13 83 f0 01 03 08 00 01 00 28 1b 01 02 05 27 01 06 51 d7
        body = bytes.fromhex("0d1383f00103080001002801b0102052701060")
        # Rebuild from known payload
        body = bytes.fromhex("0d1383f001030800010028")
        full = bytes.fromhex("0d1383f0010308000100281b010205270106")
        assert DeLonghiApi._crc16(full) == bytes.fromhex("51d7")

    def test_empty_data(self):
        """CRC of empty data should be the init value."""
        assert DeLonghiApi._crc16(b"") == (0x1D0F).to_bytes(2, "big")

    def test_deterministic(self):
        """Same input always produces same output."""
        data = bytes([0x0D, 0x04, 0x8F])
        assert DeLonghiApi._crc16(data) == DeLonghiApi._crc16(data)
