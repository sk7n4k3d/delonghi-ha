"""Test ECAM command building — length bytes, format, CRC."""

from custom_components.delonghi_coffee.api import DeLonghiApi


class TestCommandLengthBytes:
    """Every ECAM command must have len = total_bytes - 1 at byte[1]."""

    def _build_and_check(self, body: bytes) -> bytes:
        cmd = body + DeLonghiApi._crc16(body)
        assert cmd[1] == len(cmd) - 1, (
            f"Length byte 0x{cmd[1]:02X} != total({len(cmd)}) - 1 = 0x{len(cmd)-1:02X}"
        )
        assert cmd[0] == 0x0D, "ECAM commands must start with 0x0D"
        return cmd

    def test_cancel_brew(self):
        """Cancel (0x8F): 5 bytes total, len=4."""
        body = bytes([0x0D, 0x04, 0x8F])
        cmd = self._build_and_check(body)
        assert len(cmd) == 5
        assert cmd[2] == 0x8F

    def test_sync_recipes_profile1(self):
        """Sync recipes (0xA9) profile 1: 6 bytes total, len=5."""
        body = bytes([0x0D, 0x05, 0xA9, 0x01])
        cmd = self._build_and_check(body)
        assert len(cmd) == 6
        assert cmd[2] == 0xA9
        assert cmd[3] == 0x01  # profile

    def test_sync_recipes_profile4(self):
        """Sync recipes profile 4."""
        body = bytes([0x0D, 0x05, 0xA9, 0x04])
        cmd = self._build_and_check(body)
        assert cmd[3] == 0x04

    def test_monitor_command(self):
        """Monitor (0x84): 8 bytes total, len=7."""
        cmd = bytes.fromhex("0d07840f03025640")
        assert cmd[1] == len(cmd) - 1

    def test_power_on(self):
        """Power on: 8 bytes total, len=7."""
        cmd = bytes.fromhex("0d07840f02015512")
        assert cmd[1] == len(cmd) - 1

    def test_power_off(self):
        """Power off: 8 bytes total, len=7."""
        cmd = bytes.fromhex("0d07840f01010041")
        assert cmd[1] == len(cmd) - 1


class TestRecipeToBrew:
    """Test recipe → brew command conversion."""

    def test_normal_drink_excludes_visible(self):
        """VISIBLE(25) must be excluded from brew command."""
        # Fake recipe: D0 len A6 F0 profile=1 bev_id=1 [params...] CRC
        # Params: TASTE(8)=3, VISIBLE(25)=1, TEMP(2)=1
        recipe = bytes([0xD0, 0x0C, 0xA6, 0xF0, 0x01, 0x01, 8, 3, 25, 1, 2, 1, 0x00, 0x00])
        cmd = DeLonghiApi._recipe_to_brew_command(recipe, profile=1)
        # VISIBLE(25) should NOT appear in brew params
        # Parse brew params (after header 6 bytes, before profile_save+CRC)
        params_start = 6
        params_end = len(cmd) - 3  # -1 profile_save -2 CRC
        params = cmd[params_start:params_end]
        param_ids = []
        i = 0
        big = {1, 9, 15}
        while i < len(params):
            pid = params[i]
            param_ids.append(pid)
            i += 3 if pid in big else 2
        assert 25 not in param_ids, "VISIBLE(25) must be excluded"
        assert 8 in param_ids, "TASTE(8) must be preserved"

    def test_passthrough_existing_brew(self):
        """If input already starts with 0x0D, return as-is."""
        brew = bytes.fromhex("0d1383f0010308000100281b010205270106") + b"\x51\xd7"
        assert DeLonghiApi._recipe_to_brew_command(brew) is brew

    def test_profile_save_byte(self):
        """profile_save = (profile << 2) | 2."""
        recipe = bytes([0xD0, 0x08, 0xA6, 0xF0, 0x01, 0x01, 8, 3, 0x00, 0x00])
        for profile in (1, 2, 3, 4):
            cmd = DeLonghiApi._recipe_to_brew_command(recipe, profile=profile)
            profile_save = cmd[-(2 + 1)]  # 1 byte before CRC
            expected = (profile << 2) | 2
            assert profile_save == expected, (
                f"Profile {profile}: got 0x{profile_save:02X}, expected 0x{expected:02X}"
            )

    def test_iced_adds_iced_param(self):
        """Iced drinks must append ICED(31)=0."""
        recipe = bytes([0xD0, 0x08, 0xA6, 0xF0, 0x01, 0x01, 8, 3, 0x00, 0x00])
        cmd = DeLonghiApi._recipe_to_brew_command(recipe, is_iced=True, profile=1)
        # Find ICED(31) in params
        params = cmd[6:-(1 + 2)]
        found = False
        i = 0
        big = {1, 9, 15}
        while i < len(params):
            pid = params[i]
            if pid == 31:
                assert params[i + 1] == 0, "ICED value should be 0 for iced drinks"
                found = True
            i += 3 if pid in big else 2
        assert found, "ICED(31) param not found in iced brew command"

    def test_cold_brew_adds_iced3_and_intensity(self):
        """Cold brew must append ICED(31)=3 + INTENSITY(38)."""
        recipe = bytes([0xD0, 0x08, 0xA6, 0xF0, 0x01, 0x01, 8, 3, 0x00, 0x00])
        cmd = DeLonghiApi._recipe_to_brew_command(
            recipe, is_cold_brew=True, intensity=2, profile=1
        )
        params = cmd[6:-(1 + 2)]
        iced_val = None
        intensity_val = None
        i = 0
        big = {1, 9, 15}
        while i < len(params):
            pid = params[i]
            if pid == 31:
                iced_val = params[i + 1]
            if pid == 38:
                intensity_val = params[i + 1]
            i += 3 if pid in big else 2
        assert iced_val == 3, f"ICED should be 3, got {iced_val}"
        assert intensity_val == 2, f"INTENSITY should be 2, got {intensity_val}"
