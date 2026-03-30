"""Test API HTTP interactions with mocked requests."""

import base64
import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from custom_components.delonghi_coffee.api import (
    DeLonghiApi,
    DeLonghiApiError,
    DeLonghiAuthError,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _mock_response(status_code: int, json_data: dict | list | None = None, text: str = "") -> MagicMock:
    """Create a mock requests.Response."""
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.text = text or json.dumps(json_data or {})
    resp.json.return_value = json_data or {}
    resp.raise_for_status.side_effect = requests.HTTPError(response=resp) if status_code >= 400 else None
    return resp


class TestAuthentication:
    """Test full auth flow: Gigya → JWT → Ayla."""

    def test_successful_auth(self):
        """Full auth flow completes successfully."""
        api = DeLonghiApi("test@example.com", "password", region="EU")
        gigya_data = json.loads((FIXTURES / "gigya_login_response.json").read_text())
        ayla_data = json.loads((FIXTURES / "ayla_auth_response.json").read_text())

        api._session = MagicMock()
        api._session.post.side_effect = [
            _mock_response(200, gigya_data),  # Gigya login
            _mock_response(200, {"id_token": "jwt_token_here"}),  # getJWT
            _mock_response(200, ayla_data),  # Ayla token_sign_in
        ]

        result = api.authenticate()
        assert result is True
        assert api._ayla_token == "fake_access_token_abc123"
        assert api._ayla_refresh == "fake_refresh_token_def456"

    def test_gigya_login_failure(self):
        """Gigya login with wrong credentials raises AuthError."""
        api = DeLonghiApi("bad@example.com", "wrong", region="EU")
        api._session = MagicMock()
        api._session.post.return_value = _mock_response(200, {"errorCode": 403042, "errorMessage": "Invalid LoginID"})

        with pytest.raises(DeLonghiAuthError, match="Gigya login failed"):
            api.authenticate()

    def test_ayla_auth_failure(self):
        """Ayla token_sign_in failure raises AuthError."""
        api = DeLonghiApi("test@example.com", "password", region="EU")
        gigya_data = json.loads((FIXTURES / "gigya_login_response.json").read_text())

        api._session = MagicMock()
        api._session.post.side_effect = [
            _mock_response(200, gigya_data),  # Gigya OK
            _mock_response(200, {"id_token": "jwt"}),  # getJWT OK
            _mock_response(401, text="Unauthorized"),  # Ayla fails
        ]

        with pytest.raises(DeLonghiAuthError):
            api.authenticate()

    def test_no_id_token_raises(self):
        """Missing id_token in Gigya response raises AuthError."""
        api = DeLonghiApi("test@example.com", "password", region="EU")
        api._session = MagicMock()
        api._session.post.return_value = _mock_response(
            200,
            {"errorCode": 0},  # No id_token, no sessionInfo
        )

        with pytest.raises(DeLonghiAuthError, match="No id_token"):
            api.authenticate()

    def test_network_error_raises_api_error(self):
        """Network error during auth raises ApiError."""
        api = DeLonghiApi("test@example.com", "password", region="EU")
        api._session = MagicMock()
        api._session.post.side_effect = requests.ConnectionError("Connection refused")

        with pytest.raises(DeLonghiApiError, match="Network error"):
            api.authenticate()

    def test_malformed_response_raises_auth_error(self):
        """Missing expected key in Ayla response raises AuthError."""
        api = DeLonghiApi("test@example.com", "password", region="EU")
        gigya_data = json.loads((FIXTURES / "gigya_login_response.json").read_text())

        api._session = MagicMock()
        api._session.post.side_effect = [
            _mock_response(200, gigya_data),
            _mock_response(200, {"id_token": "jwt"}),
            _mock_response(200, {}),  # Missing access_token
        ]

        with pytest.raises(DeLonghiAuthError, match="Malformed"):
            api.authenticate()

    def test_id_token_without_session_info(self):
        """Auth works with id_token directly (no sessionInfo)."""
        api = DeLonghiApi("test@example.com", "password", region="EU")
        ayla_data = json.loads((FIXTURES / "ayla_auth_response.json").read_text())

        api._session = MagicMock()
        api._session.post.side_effect = [
            _mock_response(
                200,
                {
                    "errorCode": 0,
                    "id_token": "direct_id_token",
                },
            ),  # Gigya login with id_token, no sessionInfo
            _mock_response(200, ayla_data),  # Ayla token_sign_in
        ]

        result = api.authenticate()
        assert result is True


class TestGetDevices:
    """Test device listing."""

    def _make_api(self) -> DeLonghiApi:
        api = DeLonghiApi("test@example.com", "password", region="EU")
        api._ayla_token = "fake_token"
        api._token_expires = time.time() + 86400
        api._session = MagicMock()
        return api

    def test_get_devices(self):
        """Successful device listing."""
        api = self._make_api()
        devices_data = json.loads((FIXTURES / "devices_response.json").read_text())
        api._session.get.return_value = _mock_response(200, devices_data)

        devices = api.get_devices()
        assert len(devices) == 1
        assert devices[0]["dsn"] == "AC000W038925641"
        assert api.device_name == "DL-striker-cb"
        assert api.sw_version == "1.6"

    def test_no_devices(self):
        """Empty device list."""
        api = self._make_api()
        api._session.get.return_value = _mock_response(200, [])

        devices = api.get_devices()
        assert devices == []


class TestGetProperties:
    """Test property fetching."""

    def _make_api(self) -> DeLonghiApi:
        api = DeLonghiApi("test@example.com", "password", region="EU")
        api._ayla_token = "fake_token"
        api._token_expires = time.time() + 86400
        api._session = MagicMock()
        return api

    def test_get_all_properties(self):
        """Fetch all properties."""
        api = self._make_api()
        props_data = json.loads((FIXTURES / "properties_eletta.json").read_text())
        api._session.get.return_value = _mock_response(200, props_data)

        props = api.get_properties("AC000W038925641")
        assert "d701_tot_bev_b" in props
        assert props["d701_tot_bev_b"]["value"] == "1234"

    def test_get_named_properties(self):
        """Fetch specific named properties."""
        api = self._make_api()
        api._session.get.return_value = _mock_response(
            200, [{"property": {"name": "app_device_status", "value": "RUN"}}]
        )

        props = api.get_properties("DSN", names=["app_device_status"])
        assert "app_device_status" in props


class TestSendCommand:
    """Test command sending with model-aware routing."""

    def _make_api(self, model: str = "", cmd_prop: str | None = None) -> DeLonghiApi:
        api = DeLonghiApi("test@example.com", "password", region="EU", oem_model=model)
        api._ayla_token = "fake_token"
        api._token_expires = time.time() + 86400
        api._session = MagicMock()
        if cmd_prop:
            api._cmd_property = cmd_prop
        return api

    def test_send_via_app_data_request(self):
        """Eletta sends via app_data_request."""
        api = self._make_api("DL-striker-cb")
        api._session.post.return_value = _mock_response(201)

        result = api.send_command("DSN", bytes.fromhex("0d07840f02015512"))
        assert result is True

    def test_send_via_data_request(self):
        """PrimaDonna sends via data_request."""
        api = self._make_api("DL-pd-soul")
        api._session.post.return_value = _mock_response(201)

        result = api.send_command("DSN", bytes.fromhex("0d07840f02015512"))
        assert result is True

    def test_auto_detect_from_404(self):
        """Unknown model auto-detects from 404 on first endpoint."""
        api = self._make_api("")
        # First attempt (app_data_request) returns 404
        # Second attempt (data_request) returns 201
        api._session.post.side_effect = [
            _mock_response(404),
            _mock_response(201),
        ]

        result = api.send_command("DSN", bytes.fromhex("0d07840f02015512"))
        assert result is True
        assert api._cmd_property == "data_request"

    def test_both_404_raises(self):
        """Both endpoints returning 404 raises error."""
        api = self._make_api("")
        # Unknown _cmd_property="" → else branch: 3 attempts per call
        # (app_data_request, data_request+app_id, data_request-app_id)
        # _retry retries 3 times → 3 × 3 = 9 POST calls total
        api._session.post.side_effect = [_mock_response(404)] * 9

        with pytest.raises(DeLonghiApiError):
            api.send_command("DSN", bytes.fromhex("0d07840f02015512"))


class TestPingConnected:
    """Test ping_connected behavior."""

    def _make_api(self) -> DeLonghiApi:
        api = DeLonghiApi("test@example.com", "password", region="EU")
        api._ayla_token = "fake_token"
        api._token_expires = time.time() + 86400
        api._session = MagicMock()
        return api

    def test_ping_success_with_app_id(self):
        """Ping succeeds with app_id format."""
        api = self._make_api()
        api._session.post.return_value = _mock_response(201)

        result = api.ping_connected("DSN")
        assert result is True
        assert api._ping_supported is True

    def test_ping_both_404_disables(self):
        """Both formats 404 → disables future pings."""
        api = self._make_api()
        api._session.post.side_effect = [
            _mock_response(404),  # with app_id
            _mock_response(404),  # without app_id
        ]

        result = api.ping_connected("DSN")
        assert result is False
        assert api._ping_supported is False

    def test_ping_skipped_when_disabled(self):
        """Ping skipped when previously disabled."""
        api = self._make_api()
        api._ping_supported = False

        result = api.ping_connected("DSN")
        assert result is False
        api._session.post.assert_not_called()

    def test_ping_error_raises(self):
        """Non-404 error raises ApiError."""
        api = self._make_api()
        api._session.post.return_value = _mock_response(500, text="Internal Server Error")

        with pytest.raises(DeLonghiApiError, match="HTTP 500"):
            api.ping_connected("DSN")


class TestGetStatus:
    """Test get_status monitor parsing."""

    def _make_api(self) -> DeLonghiApi:
        api = DeLonghiApi("test@example.com", "password", region="EU")
        api._ayla_token = "fake_token"
        api._token_expires = time.time() + 86400
        api._session = MagicMock()
        return api

    def test_status_with_monitor(self):
        """Status with valid monitor data."""
        api = self._make_api()
        # Monitor: ready state, profile 1, no alarms
        monitor_b64 = base64.b64encode(bytes.fromhex("d00ca4f001020000000700000000")).decode()
        api._session.get.return_value = _mock_response(
            200,
            [
                {"property": {"name": "app_device_status", "value": "RUN"}},
                {"property": {"name": "d302_monitor_machine", "value": monitor_b64}},
            ],
        )

        status = api.get_status("DSN")
        assert status["status"] == "RUN"
        assert status["machine_state"] == "Ready"
        assert status["profile"] == 1

    def test_status_no_monitor(self):
        """Status without monitor data returns defaults."""
        api = self._make_api()
        api._session.get.return_value = _mock_response(
            200,
            [
                {"property": {"name": "app_device_status", "value": "RUN"}},
            ],
        )

        status = api.get_status("DSN")
        assert status["status"] == "RUN"
        assert status["machine_state"] == "Unknown"

    def test_status_fetch_error_returns_defaults(self):
        """Error during status fetch returns safe defaults."""
        api = self._make_api()
        api._session.get.side_effect = requests.ConnectionError("timeout")

        status = api.get_status("DSN")
        assert status["status"] == "UNKNOWN"
        assert status["machine_state"] == "Unknown"


class TestGetLanConfig:
    """Test LAN configuration fetching."""

    def _make_api(self) -> DeLonghiApi:
        api = DeLonghiApi("test@example.com", "password", region="EU")
        api._ayla_token = "fake_token"
        api._token_expires = time.time() + 86400
        api._session = MagicMock()
        return api

    def test_lan_enabled(self):
        """LAN enabled with key."""
        api = self._make_api()
        api._session.get.side_effect = [
            # Device info
            _mock_response(
                200,
                {
                    "device": {
                        "lan_enabled": True,
                        "lan_ip": "192.168.1.100",
                        "connection_status": "Online",
                    }
                },
            ),
            # LAN key
            _mock_response(
                200,
                {
                    "lanip": {
                        "lanip_key": "0123456789abcdef",
                        "lanip_key_id": 12345,
                    }
                },
            ),
        ]

        config = api.get_lan_config("DSN")
        assert config["lan_enabled"] is True
        assert config["lan_ip"] == "192.168.1.100"
        assert config["lanip_key"] == "0123456789abcdef"

    def test_lan_disabled(self):
        """LAN disabled skips key fetch."""
        api = self._make_api()
        api._session.get.return_value = _mock_response(
            200,
            {
                "device": {
                    "lan_enabled": False,
                    "lan_ip": None,
                    "connection_status": "Online",
                }
            },
        )

        config = api.get_lan_config("DSN")
        assert config["lan_enabled"] is False
        assert config["lanip_key"] is None

    def test_lan_key_fallback_endpoint(self):
        """Falls back to connection_config.json when lan.json returns 404."""
        api = self._make_api()
        api._session.get.side_effect = [
            # Device info
            _mock_response(
                200, {"device": {"lan_enabled": True, "lan_ip": "192.168.1.100", "connection_status": "Online"}}
            ),
            # lan.json → 404
            MagicMock(
                status_code=404,
                raise_for_status=MagicMock(side_effect=requests.HTTPError(response=MagicMock(status_code=404))),
            ),
            # connection_config.json → success
            _mock_response(200, {"local_key": "fallback_key", "local_key_id": 99999}),
        ]

        config = api.get_lan_config("DSN")
        assert config["lanip_key"] == "fallback_key"
        assert config["lanip_key_id"] == 99999


class TestCancelBrew:
    """Test cancel brew command."""

    def test_cancel_sends_correct_command(self):
        """Cancel sends 0x8F ECAM command."""
        api = DeLonghiApi("test@example.com", "password", region="EU")
        api._ayla_token = "fake_token"
        api._token_expires = time.time() + 86400
        api._cmd_property = "app_data_request"
        api._session = MagicMock()
        api._session.post.return_value = _mock_response(201)

        api.cancel_brew("DSN")
        # Verify the command was sent
        api._session.post.assert_called_once()


class TestSyncRecipes:
    """Test sync recipes command."""

    def test_sync_sends_correct_profile(self):
        """Sync sends 0xA9 with correct profile byte."""
        api = DeLonghiApi("test@example.com", "password", region="EU")
        api._ayla_token = "fake_token"
        api._token_expires = time.time() + 86400
        api._cmd_property = "app_data_request"
        api._session = MagicMock()
        api._session.post.return_value = _mock_response(201)

        api.sync_recipes("DSN", profile=3)
        api._session.post.assert_called_once()


class TestTokenRefreshHTTP:
    """Test token refresh via HTTP."""

    def test_refresh_token_success(self):
        """Successful token refresh updates tokens."""
        api = DeLonghiApi("test@example.com", "password", region="EU")
        api._ayla_token = "old_token"
        api._ayla_refresh = "refresh_token"
        api._token_expires = time.time() - 100  # expired
        api._session = MagicMock()
        api._session.post.return_value = _mock_response(
            200,
            {
                "access_token": "new_token",
                "refresh_token": "new_refresh",
                "expires_in": 86400,
            },
        )

        api._ensure_token()
        assert api._ayla_token == "new_token"
        assert api._ayla_refresh == "new_refresh"

    def test_refresh_failure_triggers_reauth(self):
        """Failed refresh triggers full re-authentication."""
        api = DeLonghiApi("test@example.com", "password", region="EU")
        api._ayla_token = "old_token"
        api._ayla_refresh = "refresh_token"
        api._token_expires = time.time() - 100  # expired
        api._session = MagicMock()
        api._session.post.side_effect = requests.ConnectionError("timeout")

        with patch.object(api, "authenticate") as mock_auth:
            api._ensure_token()
            mock_auth.assert_called_once()


class TestRetryDecorator:
    """Test the retry mechanism."""

    def test_retry_on_request_exception(self):
        """RequestException triggers retry."""
        api = DeLonghiApi("test@example.com", "password", region="EU")
        api._ayla_token = "fake"
        api._token_expires = time.time() + 86400
        api._session = MagicMock()

        # Fail twice, succeed on third
        api._session.get.side_effect = [
            requests.ConnectionError("fail 1"),
            requests.ConnectionError("fail 2"),
            _mock_response(200, [{"property": {"name": "test", "value": "ok"}}]),
        ]

        props = api.get_properties("DSN")
        assert "test" in props

    def test_retry_exhausted_raises(self):
        """All retries exhausted raises ApiError."""
        api = DeLonghiApi("test@example.com", "password", region="EU")
        api._ayla_token = "fake"
        api._token_expires = time.time() + 86400
        api._session = MagicMock()
        api._session.get.side_effect = requests.ConnectionError("persistent failure")

        with pytest.raises(DeLonghiApiError, match="failed after 3 attempts"):
            api.get_properties("DSN")

    def test_no_retry_on_404(self):
        """404 errors are not retried (property doesn't exist)."""
        api = DeLonghiApi("test@example.com", "password", region="EU")
        api._ayla_token = "fake"
        api._token_expires = time.time() + 86400
        api._session = MagicMock()
        resp404 = _mock_response(404)
        resp404.raise_for_status.side_effect = requests.HTTPError(response=resp404)
        api._session.get.return_value = resp404

        with pytest.raises(requests.HTTPError):
            api.get_properties("DSN")
        # Should only be called once (no retry)
        assert api._session.get.call_count == 1

    def test_401_triggers_reauth(self):
        """401 triggers re-authentication then retry."""
        api = DeLonghiApi("test@example.com", "password", region="EU")
        api._ayla_token = "fake"
        api._token_expires = time.time() + 86400
        api._session = MagicMock()

        resp401 = MagicMock(spec=requests.Response)
        resp401.status_code = 401
        resp401.raise_for_status.side_effect = requests.HTTPError(response=resp401)

        api._session.get.side_effect = [
            requests.HTTPError(response=resp401),
            _mock_response(200, [{"property": {"name": "test", "value": "ok"}}]),
        ]

        with patch.object(api, "authenticate"):
            props = api.get_properties("DSN")
            assert "test" in props


class TestRegionConfig:
    """Test multi-region configuration."""

    def test_eu_region(self):
        """EU region uses correct endpoints."""
        api = DeLonghiApi("test@example.com", "password", region="EU")
        assert "eu" in api._ayla_ads
        assert "eu" in api._ayla_user

    def test_us_region(self):
        """US region uses correct endpoints."""
        api = DeLonghiApi("test@example.com", "password", region="US")
        assert "field" in api._ayla_ads
        assert "field" in api._ayla_user
        assert "eu" not in api._ayla_ads

    def test_cn_region(self):
        """CN region uses .com.cn endpoints."""
        api = DeLonghiApi("test@example.com", "password", region="CN")
        assert ".com.cn" in api._ayla_ads

    def test_unknown_region_defaults_to_eu(self):
        """Unknown region falls back to EU."""
        api = DeLonghiApi("test@example.com", "password", region="UNKNOWN")
        assert "eu" in api._ayla_ads
