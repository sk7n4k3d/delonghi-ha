"""Test API client methods — auth, send_command, packet building, rate tracking."""

import base64
import struct
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from custom_components.delonghi_coffee.api import (
    DeLonghiApi,
    DeLonghiApiError,
)


class TestPacketBuilding:
    """Test _build_packet for both models."""

    def setup_method(self):
        self.api = DeLonghiApi.__new__(DeLonghiApi)
        self.api._email = "test@example.com"
        self.api._password = "password"
        self.api._session = MagicMock()
        self.api._ayla_token = "fake_token"
        self.api._ayla_refresh = None
        self.api._token_expires = time.time() + 86400
        self.api._ayla_app_id = "test_app_id"
        self.api._ayla_app_secret = "test_secret"
        self.api._ayla_user = "https://user.test.com"
        self.api._ayla_ads = "https://ads.test.com"
        self.api._oem_model = ""
        self.api._cmd_property = None
        self.api._ping_supported = None
        self.api._rate_tracker = MagicMock()
        from custom_components.delonghi_coffee.const import APP_SIGNATURE

        self._app_sig = APP_SIGNATURE

    def test_with_app_id(self):
        """Packet with app_id includes 4-byte signature."""
        ecam = bytes([0x0D, 0x04, 0x8F, 0x00, 0x00])
        b64 = self.api._build_packet(ecam, include_app_id=True)
        raw = base64.b64decode(b64)
        assert raw[:5] == ecam
        # 4 bytes timestamp + 4 bytes app_id
        assert len(raw) == len(ecam) + 4 + 4
        assert raw[-4:] == self._app_sig

    def test_without_app_id(self):
        """Packet without app_id has only timestamp appended."""
        ecam = bytes([0x0D, 0x04, 0x8F, 0x00, 0x00])
        b64 = self.api._build_packet(ecam, include_app_id=False)
        raw = base64.b64decode(b64)
        assert raw[:5] == ecam
        # 4 bytes timestamp only
        assert len(raw) == len(ecam) + 4
        assert raw[-4:] != self._app_sig

    def test_timestamp_is_current(self):
        """Timestamp in packet should be within 2 seconds of now."""
        ecam = bytes([0x0D, 0x04, 0x8F])
        b64 = self.api._build_packet(ecam, include_app_id=False)
        raw = base64.b64decode(b64)
        ts = struct.unpack(">I", raw[3:7])[0]
        assert abs(ts - int(time.time())) < 2


class TestCommandPropertyRouting:
    """Test model-aware command property selection."""

    def test_primadonna_uses_data_request(self):
        """DL-pd-* models should use data_request."""
        api = DeLonghiApi.__new__(DeLonghiApi)
        api._oem_model = "DL-pd-soul"
        api._cmd_property = None
        if api._oem_model.startswith("DL-pd-"):
            api._cmd_property = "data_request"
        assert api._cmd_property == "data_request"

    def test_eletta_uses_app_data_request(self):
        """DL-striker-* models should use app_data_request."""
        api = DeLonghiApi.__new__(DeLonghiApi)
        api._oem_model = "DL-striker-cb"
        api._cmd_property = None
        if api._oem_model.startswith("DL-striker-"):
            api._cmd_property = "app_data_request"
        assert api._cmd_property == "app_data_request"

    def test_unknown_model_no_cache(self):
        """Unknown models should not have cached property."""
        api = DeLonghiApi.__new__(DeLonghiApi)
        api._oem_model = "DL-unknown-model"
        api._cmd_property = None
        if api._oem_model.startswith("DL-pd-"):
            api._cmd_property = "data_request"
        elif api._oem_model.startswith("DL-striker-"):
            api._cmd_property = "app_data_request"
        assert api._cmd_property is None


class TestBrewCustom:
    """Test custom brew command building."""

    def setup_method(self):
        self.api = DeLonghiApi.__new__(DeLonghiApi)
        self.api._email = "test@example.com"
        self.api._password = "password"
        self.api._session = MagicMock()
        self.api._ayla_token = "fake_token"
        self.api._ayla_refresh = None
        self.api._token_expires = time.time() + 86400
        self.api._ayla_app_id = "test_app_id"
        self.api._ayla_app_secret = "test_secret"
        self.api._ayla_user = "https://user.test.com"
        self.api._ayla_ads = "https://ads.test.com"
        self.api._oem_model = ""
        self.api._cmd_property = "app_data_request"
        self.api._ping_supported = False
        self.api._rate_tracker = MagicMock()
        self.api._custom_recipe_names = {}

    def test_unknown_beverage_raises(self):
        """Unknown beverage key raises DeLonghiApiError."""
        with pytest.raises(DeLonghiApiError, match="Unknown beverage"):
            self.api.brew_custom("DSN", "unicorn_latte")

    def test_invalid_profile_raises(self):
        """Profile outside 1-5 raises DeLonghiApiError."""
        with pytest.raises(DeLonghiApiError, match="Profile must be 1-5"):
            self.api.brew_custom("DSN", "espresso", profile=0)
        with pytest.raises(DeLonghiApiError, match="Profile must be 1-5"):
            self.api.brew_custom("DSN", "espresso", profile=6)


class TestPreBrewCheck:
    """Test pre-brew safety checks."""

    def setup_method(self):
        self.api = DeLonghiApi.__new__(DeLonghiApi)
        self.api._email = "test@example.com"
        self.api._password = "password"
        self.api._session = MagicMock()
        self.api._ayla_token = "fake_token"
        self.api._ayla_refresh = None
        self.api._token_expires = time.time() + 86400
        self.api._ayla_app_id = "test_app_id"
        self.api._ayla_app_secret = "test_secret"
        self.api._ayla_user = "https://user.test.com"
        self.api._ayla_ads = "https://ads.test.com"
        self.api._oem_model = ""
        self.api._cmd_property = None
        self.api._ping_supported = None
        self.api._rate_tracker = MagicMock()
        self.api._devices = []

    def test_machine_off_blocks_brew(self):
        """Cannot brew when machine is Off."""
        # Fake recipe
        recipe = bytes([0xD0, 0x08, 0xA6, 0xF0, 0x01, 0x01, 8, 3, 0x00, 0x00])
        with (
            patch.object(self.api, "get_status", return_value={"machine_state": "Off", "alarms": []}),
            pytest.raises(DeLonghiApiError, match="machine is off"),
        ):
            self.api._pre_brew_check("DSN", recipe, "espresso")

    def test_machine_brewing_blocks_brew(self):
        """Cannot brew when machine is already Brewing."""
        recipe = bytes([0xD0, 0x08, 0xA6, 0xF0, 0x01, 0x01, 8, 3, 0x00, 0x00])
        with (
            patch.object(self.api, "get_status", return_value={"machine_state": "Brewing", "alarms": []}),
            pytest.raises(DeLonghiApiError, match="already brewing"),
        ):
            self.api._pre_brew_check("DSN", recipe, "espresso")

    def test_water_tank_alarm_blocks_brew(self):
        """Water Tank Empty alarm blocks brewing."""
        recipe = bytes([0xD0, 0x08, 0xA6, 0xF0, 0x01, 0x01, 8, 3, 0x00, 0x00])
        status = {
            "machine_state": "Ready",
            "alarms": [{"bit": 0, "name": "Water Tank Empty"}],
        }
        with (
            patch.object(self.api, "get_status", return_value=status),
            pytest.raises(DeLonghiApiError, match="Water Tank Empty"),
        ):
            self.api._pre_brew_check("DSN", recipe, "espresso")

    def test_ready_machine_passes(self):
        """Ready machine with no alarms passes pre-brew check."""
        recipe = bytes([0xD0, 0x08, 0xA6, 0xF0, 0x01, 0x01, 8, 3, 0x00, 0x00])
        with patch.object(self.api, "get_status", return_value={"machine_state": "Ready", "alarms": []}):
            self.api._pre_brew_check("DSN", recipe, "espresso")  # Should not raise

    def test_status_fetch_failure_skips_check(self):
        """If status fetch fails, checks are skipped (lenient)."""
        recipe = bytes([0xD0, 0x08, 0xA6, 0xF0, 0x01, 0x01, 8, 3, 0x00, 0x00])
        with patch.object(self.api, "get_status", side_effect=DeLonghiApiError("network")):
            self.api._pre_brew_check("DSN", recipe, "espresso")  # Should not raise


class TestRecipeAccessory:
    """Test ACCESSORIO extraction from recipe."""

    def test_extracts_accessory(self):
        """ACCESSORIO(28) param extracted from recipe."""
        # Recipe with param 28 (ACCESSORIO) = 2
        recipe = bytes([0xD0, 0x0C, 0xA6, 0xF0, 0x01, 0x07, 28, 2, 8, 3, 0x00, 0x00])
        assert DeLonghiApi._get_recipe_accessory(recipe) == 2

    def test_no_accessory(self):
        """Recipe without ACCESSORIO returns None."""
        recipe = bytes([0xD0, 0x0A, 0xA6, 0xF0, 0x01, 0x01, 8, 3, 0x00, 0x00])
        assert DeLonghiApi._get_recipe_accessory(recipe) is None

    def test_big_params_skipped(self):
        """16-bit params (COFFEE=1) are 3 bytes, correctly skipped."""
        # COFFEE(1)=0x0100 (3 bytes) then ACCESSORIO(28)=2
        recipe = bytes([0xD0, 0x0E, 0xA6, 0xF0, 0x01, 0x01, 1, 0x01, 0x00, 28, 2, 0x00, 0x00])
        assert DeLonghiApi._get_recipe_accessory(recipe) == 2


class TestTokenRefresh:
    """Test token expiry and refresh logic."""

    def test_token_not_expired(self):
        """_ensure_token does nothing when token is valid."""
        api = DeLonghiApi.__new__(DeLonghiApi)
        api._token_expires = time.time() + 3600
        api._ayla_refresh = None
        api._ayla_token = "valid_token"
        api._ayla_user = "https://user.test.com"
        api._ayla_app_id = "test"
        api._ayla_app_secret = "test"
        api._gigya_url = "https://gigya.test.com"
        api._email = "test@example.com"
        api._password = "password"
        api._session = MagicMock()
        api._ayla_ads = "https://ads.test.com"
        api._oem_model = ""
        api._cmd_property = None
        api._ping_supported = None
        api._rate_tracker = MagicMock()
        api._devices = []
        # Should not call authenticate
        with patch.object(api, "authenticate") as mock_auth:
            api._ensure_token()
            mock_auth.assert_not_called()

    def test_token_expired_triggers_reauth(self):
        """Expired token triggers re-authentication."""
        api = DeLonghiApi.__new__(DeLonghiApi)
        api._token_expires = time.time() - 100  # expired
        api._ayla_refresh = None
        api._ayla_token = "expired_token"
        api._ayla_user = "https://user.test.com"
        api._ayla_app_id = "test"
        api._ayla_app_secret = "test"
        api._gigya_url = "https://gigya.test.com"
        api._email = "test@example.com"
        api._password = "password"
        api._session = MagicMock()
        api._ayla_ads = "https://ads.test.com"
        api._oem_model = ""
        api._cmd_property = None
        api._ping_supported = None
        api._rate_tracker = MagicMock()
        api._devices = []
        api._token_lock = threading.Lock()
        with patch.object(api, "authenticate") as mock_auth:
            api._ensure_token()
            mock_auth.assert_called_once()

    def test_concurrent_refresh_only_calls_authenticate_once(self):
        """Multiple threads racing on _ensure_token must call authenticate only once."""
        import time as time_mod
        from concurrent.futures import ThreadPoolExecutor

        api = DeLonghiApi.__new__(DeLonghiApi)
        api._token_expires = time_mod.time() - 100  # expired
        api._ayla_refresh = None
        api._ayla_token = "expired_token"
        api._ayla_user = "https://user.test.com"
        api._ayla_app_id = "test"
        api._ayla_app_secret = "test"
        api._gigya_url = "https://gigya.test.com"
        api._email = "test@example.com"
        api._password = "password"
        api._session = MagicMock()
        api._ayla_ads = "https://ads.test.com"
        api._oem_model = ""
        api._cmd_property = None
        api._ping_supported = None
        api._rate_tracker = MagicMock()
        api._devices = []
        api._token_lock = threading.Lock()

        call_count = [0]

        def fake_auth():
            call_count[0] += 1
            time_mod.sleep(0.05)
            api._token_expires = time_mod.time() + 3600

        with patch.object(api, "authenticate", side_effect=fake_auth), ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(api._ensure_token) for _ in range(8)]
            for f in futures:
                f.result()

        # Without the lock, multiple threads would each see the expired token
        # and each call authenticate(). The double-checked lock guarantees one.
        assert call_count[0] == 1, f"authenticate called {call_count[0]}x, expected 1"


class TestRateTrackerIntegration:
    """Test that rate tracker is wired into API calls."""

    def test_api_has_rate_tracker(self):
        """API instance has a rate tracker."""
        api = DeLonghiApi("test@example.com", "password", region="EU")
        assert api.rate_tracker is not None
        assert api.rate_tracker.current_rate == 0
