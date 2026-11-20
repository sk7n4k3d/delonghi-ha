"""Tests for My Coffee Lounge discovery, LAN verification and account reauth."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.delonghi_daedalus import config_flow
from custom_components.delonghi_daedalus.api import (
    DaedalusAuthError,
    DaedalusConnectionError,
    DaedalusLanIdentityError,
    DaedalusLanSerialMismatchError,
)
from custom_components.delonghi_daedalus.cloud import CloudTelemetryError, DirectoryMachine
from custom_components.delonghi_daedalus.config_flow import (
    DaedalusConfigFlow,
    _probe_pools,
)
from custom_components.delonghi_daedalus.const import (
    CONF_AROMATIC_WARMUP,
    CONF_CLOUD_TELEMETRY,
    CONF_DRINK_FAVORITES,
    CONF_DRINK_VISIBILITY,
    CONF_EMAIL,
    CONF_FAVORITE_DRINK,
    CONF_HOST,
    CONF_JWT,
    CONF_LAN_AUTH_MESSAGE,
    CONF_LAN_TLS_FINGERPRINT,
    CONF_MACHINE_NAME,
    CONF_MODEL,
    CONF_PASSWORD,
    CONF_POOL,
    CONF_SERIAL_NUMBER,
    CONF_SESSION_SECRET,
    CONF_SESSION_TOKEN,
    GIGYA_API_KEYS,
    GIGYA_POOL_EU,
    GIGYA_POOL_EU_US,
)


def _make_flow(api: MagicMock) -> DaedalusConfigFlow:
    flow = DaedalusConfigFlow()
    flow.hass = MagicMock()
    flow._api_factory = lambda: api  # type: ignore[attr-defined]
    return flow


def test_show_form_when_no_input() -> None:
    flow = _make_flow(MagicMock())
    result = asyncio.run(flow.async_step_user(None))
    assert result["type"] == "form"
    assert result["step_id"] == "user"


_CREDENTIALS = {CONF_EMAIL: "user@example.com", CONF_PASSWORD: "test-password"}
_PIN = "ab" * 32
_NEW_PIN = "ba" * 32
_OWNER = DirectoryMachine("SN1234", "owner-thing", "ECAM63070")
_SHARED = DirectoryMachine("SN4321", "shared-thing", "ECAM65075")
_HOST = "192.168.1.42"


@pytest.fixture
def setup_flow(monkeypatch):
    api = MagicMock()
    api.login_and_get_jwt = AsyncMock(return_value=("session", "secret", "jwt"))
    api.probe_lan_fingerprint = AsyncMock(return_value=_PIN)
    lan = MagicMock(auth_message="auth", close=AsyncMock())
    api.connect_lan = AsyncMock(return_value=lan)
    api.close = AsyncMock()
    directory = AsyncMock(return_value=[_OWNER, _SHARED])
    shadow = AsyncMock(return_value=_HOST)
    monkeypatch.setattr(config_flow, "list_machines", directory)
    monkeypatch.setattr(config_flow, "fetch_reported_lan_ip", shadow)
    return _make_flow(api), api, directory, shadow


def _option_key(flow, serial):
    return next(key for key, machine in flow._machines.items() if machine.serial == serial)


def test_device_choices_use_opaque_keys_and_mask_duplicate_labels(setup_flow):
    flow, _, directory, _ = setup_flow
    directory.return_value = [_OWNER, DirectoryMachine("SN4321", "shared", "ECAM63070")]
    result = asyncio.run(flow.async_step_user(_CREDENTIALS))
    choices = next(iter(result["data_schema"].schema.values())).container
    assert set(choices.values()) == {"PrimaDonna Aromatic (…1234)", "PrimaDonna Aromatic (…4321)"}
    assert not set(choices) & {"SN1234", "SN4321"}


def test_explicit_shared_machine_selection_and_pin_confirmation(setup_flow):
    flow, api, _, shadow = setup_flow

    async def run():
        result = await flow.async_step_user(_CREDENTIALS)
        assert result["step_id"] == "device"
        assert result["data_schema"]({"device": _option_key(flow, "SN1234")})
        assert result["data_schema"]({"device": _option_key(flow, "SN4321")})
        assert (await flow.async_step_device({"device": "other"}))["step_id"] == "device"
        shadow.assert_not_awaited()
        result = await flow.async_step_device({"device": _option_key(flow, "SN4321")})
        shadow.assert_awaited_once_with(api.http_session, thing_name="shared-thing", jwt="jwt")
        assert result["data_schema"]({})[CONF_HOST] == _HOST
        result = await flow.async_step_lan({CONF_HOST: _HOST})
        assert result["step_id"] == "verify_lan"
        assert result["description_placeholders"]["serial"] == "…4321"
        assert result["description_placeholders"]["fingerprint"] == ":".join(["AB"] * 32)
        api.probe_lan_fingerprint.assert_awaited_once_with(host=_HOST)
        api.connect_lan.assert_not_awaited()
        assert (await flow.async_step_verify_lan({"verified_fingerprint": False}))["errors"]["base"] == (
            "fingerprint_not_verified"
        )
        api.connect_lan.assert_not_awaited()

        result = await flow.async_step_verify_lan({"verified_fingerprint": True})
        assert result["type"] == "create_entry"
        assert result["title"] == "ECAM65075"
        data = result["data"]
        assert data[CONF_SERIAL_NUMBER] == "SN4321"
        assert data[CONF_MODEL] == "ECAM65075"
        assert data[CONF_MACHINE_NAME] == "shared-thing"
        assert data[CONF_EMAIL] == _CREDENTIALS[CONF_EMAIL]
        assert CONF_PASSWORD not in data
        assert data[CONF_HOST] == _HOST
        assert data[CONF_LAN_TLS_FINGERPRINT] == _PIN
        assert data[CONF_JWT] == "jwt"
        assert data[CONF_SESSION_TOKEN] == "session"
        assert data[CONF_SESSION_SECRET] == "secret"
        assert data[CONF_LAN_AUTH_MESSAGE] == "auth"
        api.connect_lan.assert_awaited_once_with(
            host=_HOST, serial_number="SN4321", jwt="jwt", auth_message="AUTH", fingerprint=_PIN
        )
        api.connect_lan.return_value.close.assert_awaited_once()

    asyncio.run(run())


def test_zero_machines_aborts_and_one_machine_still_requires_selection(setup_flow):
    flow, api, directory, shadow = setup_flow
    directory.return_value = []
    assert asyncio.run(flow.async_step_user(_CREDENTIALS))["reason"] == "no_devices"
    directory.return_value = [_OWNER]
    assert asyncio.run(flow.async_step_user(_CREDENTIALS))["step_id"] == "device"
    shadow.assert_not_awaited()
    api.connect_lan.assert_not_awaited()


def test_directory_outage_supports_manual_serial_and_address(setup_flow):
    flow, api, directory, shadow = setup_flow
    directory.side_effect = CloudTelemetryError("offline")

    async def run():
        result = await flow.async_step_user(_CREDENTIALS)
        assert result["step_id"] == "manual"
        assert result["errors"]["base"] == "discovery_unavailable"
        result = await flow.async_step_manual({CONF_SERIAL_NUMBER: "SN1234"})
        assert result["step_id"] == "lan"
        assert result["data_schema"]({})[CONF_HOST] == ""
        shadow.assert_not_awaited()
        result = await flow.async_step_lan({CONF_HOST: _HOST})
        assert result["step_id"] == "verify_lan"
        result = await flow.async_step_verify_lan({"verified_fingerprint": True})
        assert result["data"][CONF_MACHINE_NAME] == "SN1234"

    asyncio.run(run())


def test_shadow_outage_allows_manual_host_and_cloud_only(setup_flow):
    flow, api, _, shadow = setup_flow
    shadow.side_effect = TimeoutError

    async def run():
        await flow.async_step_user({**_CREDENTIALS, CONF_CLOUD_TELEMETRY: True})
        result = await flow.async_step_device({"device": _option_key(flow, "SN1234")})
        assert result["data_schema"]({})[CONF_HOST] == ""
        result = await flow.async_step_lan({"cloud_only": True})
        assert result["type"] == "create_entry"
        assert result["data"][CONF_CLOUD_TELEMETRY] is True
        assert result["data"][CONF_LAN_TLS_FINGERPRINT] is None
        api.probe_lan_fingerprint.assert_not_awaited()
        api.connect_lan.assert_not_awaited()

    asyncio.run(run())


@pytest.mark.parametrize("host", ["172.32.0.1", "169.254.169.254", "127.0.0.1", "192.168.1.10/other"])
def test_invalid_hosts_never_probe_or_authenticate(setup_flow, host):
    flow, api, _, _ = setup_flow

    async def run():
        await flow.async_step_user(_CREDENTIALS)
        await flow.async_step_device({"device": _option_key(flow, "SN1234")})
        result = await flow.async_step_lan({CONF_HOST: host})
        assert result["errors"][CONF_HOST] == "invalid_host"
        api.probe_lan_fingerprint.assert_not_awaited()
        api.connect_lan.assert_not_awaited()

    asyncio.run(run())


def test_reconfigure_moves_address_with_known_certificate(setup_flow):
    flow, api, _, _ = setup_flow
    entry = _reconfigure_entry(flow, pin=_PIN)
    new_host = "192.168.1.81"

    async def run():
        assert (await flow.async_step_reconfigure())["step_id"] == "lan"
        result = await flow.async_step_lan({CONF_HOST: new_host})
        assert result["reason"] == "reconfigure_successful"
        assert flow.hass.config_entries.async_update_entry.call_args.kwargs["data"] == {
            **entry.data,
            CONF_HOST: new_host,
            CONF_LAN_AUTH_MESSAGE: "auth",
        }
        api.connect_lan.assert_awaited_once_with(
            host=new_host, serial_number="OLD-SN", jwt="existing-jwt", auth_message="AUTH", fingerprint=_PIN
        )

    asyncio.run(run())


@pytest.mark.parametrize("pin", [None, _PIN])
def test_reconfigure_requires_confirmation_for_missing_or_replaced_pin(setup_flow, pin):
    flow, api, _, _ = setup_flow
    entry = _reconfigure_entry(flow, pin)
    api.probe_lan_fingerprint.return_value = _NEW_PIN

    async def run():
        await flow.async_step_reconfigure()
        result = await flow.async_step_lan({CONF_HOST: _HOST})
        assert result["step_id"] == "verify_lan"
        api.connect_lan.assert_not_awaited()
        flow.hass.config_entries.async_update_entry.assert_not_called()

        api.connect_lan.side_effect = DaedalusLanIdentityError("test")
        result = await flow.async_step_verify_lan({"verified_fingerprint": True})
        assert result["step_id"] == "lan"
        assert result["errors"]["base"] == "certificate_changed"
        flow.hass.config_entries.async_update_entry.assert_not_called()

        api.connect_lan.side_effect = None
        await flow.async_step_lan({CONF_HOST: _HOST})
        result = await flow.async_step_verify_lan({"verified_fingerprint": True})
        assert result["reason"] == "reconfigure_successful"
        data = flow.hass.config_entries.async_update_entry.call_args.kwargs["data"]
        assert data[CONF_SERIAL_NUMBER] == entry.data[CONF_SERIAL_NUMBER]
        assert data[CONF_LAN_TLS_FINGERPRINT] == _NEW_PIN
        flow.hass.config_entries.async_reload.assert_awaited_once_with(entry.entry_id)

    asyncio.run(run())


def test_reconfigure_serial_mismatch_rejects_candidate_without_writing(setup_flow):
    flow, api, directory, _ = setup_flow
    entry = _reconfigure_entry(flow, pin=_PIN)
    original_data = entry.data.copy()
    api.probe_lan_fingerprint.return_value = _NEW_PIN
    api.connect_lan.side_effect = DaedalusLanSerialMismatchError("different machine")
    api.refresh_jwt = AsyncMock()

    async def run():
        await flow.async_step_reconfigure()
        result = await flow.async_step_lan({CONF_HOST: "192.168.1.81"})
        assert result["step_id"] == "verify_lan"
        api.connect_lan.assert_not_awaited()
        result = await flow.async_step_verify_lan({"verified_fingerprint": True})
        assert result["step_id"] == "lan"
        assert result["errors"]["base"] == "serial_mismatch"

    asyncio.run(run())
    api.connect_lan.assert_awaited_once_with(
        host="192.168.1.81",
        serial_number=original_data[CONF_SERIAL_NUMBER],
        jwt=original_data[CONF_JWT],
        auth_message="AUTH",
        fingerprint=_NEW_PIN,
    )
    api.refresh_jwt.assert_not_awaited()
    directory.assert_not_awaited()
    flow.hass.config_entries.async_update_entry.assert_not_called()
    flow.hass.config_entries.async_reload.assert_not_awaited()
    assert entry.data == original_data


def test_warmup_option_and_cloud_only_reconfigure_preserve_a_transport(setup_flow, monkeypatch):
    flow, _, _, _ = setup_flow
    entry = _reconfigure_entry(flow, pin=_PIN)
    entry.data.update({CONF_CLOUD_TELEMETRY: True, CONF_POOL: GIGYA_POOL_EU})
    flow.hass.config_entries.async_update_entry.side_effect = lambda entry, *, data, **_: setattr(entry, "data", data)
    options = config_flow.DaedalusOptionsFlow(entry)
    options.hass = flow.hass

    async def run():
        saving = asyncio.Event()
        finish = asyncio.Event()

        async def save_pending(*_args):
            saving.set()
            await finish.wait()

        monkeypatch.setattr(config_flow, "save_pending", save_pending)
        await flow.async_step_reconfigure()
        disable_cloud = asyncio.create_task(
            options.async_step_init({CONF_CLOUD_TELEMETRY: False, CONF_AROMATIC_WARMUP: True})
        )
        await saving.wait()
        cloud_only = asyncio.create_task(flow.async_step_lan({"cloud_only": True}))
        await asyncio.sleep(0)
        assert not cloud_only.done()
        finish.set()
        assert (await disable_cloud)["type"] == "create_entry"
        assert (await cloud_only)["errors"]["base"] == "cloud_only_unavailable"

    asyncio.run(run())
    assert entry.data[CONF_CLOUD_TELEMETRY] is False
    assert entry.data[CONF_AROMATIC_WARMUP] is True
    assert entry.data[CONF_LAN_TLS_FINGERPRINT] == _PIN


@pytest.mark.parametrize(
    ("current_title", "expected_title"),
    [
        ("My Coffee Lounge (OLD-SN)", "PrimaDonna Aromatic"),
        ("Kitchen Coffee", "Kitchen Coffee"),
    ],
)
def test_reconfigure_enriches_legacy_metadata_without_changing_serial_or_custom_title(
    setup_flow, current_title, expected_title
):
    flow, api, directory, _ = setup_flow
    entry = _reconfigure_entry(flow, pin=_PIN)
    entry.title = current_title
    directory.return_value = [DirectoryMachine("old-sn", "existing-thing", "ECAM63070")]

    async def run():
        await flow.async_step_reconfigure()
        result = await flow.async_step_lan({CONF_HOST: _HOST})
        assert result["reason"] == "reconfigure_successful"
        update = flow.hass.config_entries.async_update_entry.call_args.kwargs
        assert update["title"] == expected_title
        assert update["data"][CONF_MODEL] == "ECAM63070"
        assert update["data"][CONF_MACHINE_NAME] == "existing-thing"
        assert update["data"][CONF_SERIAL_NUMBER] == "OLD-SN"
        directory.assert_awaited_once_with(api.http_session, jwt="existing-jwt")

    asyncio.run(run())


def test_reconfigure_merges_concurrent_credentials_and_settings(setup_flow):
    flow, api, _, _ = setup_flow
    entry = _reconfigure_entry(flow, pin=_PIN)
    validate = api.connect_lan.return_value

    async def change_entry(**kwargs):
        entry.data = {**entry.data, CONF_JWT: "rotated-jwt", CONF_CLOUD_TELEMETRY: True}
        return validate

    api.connect_lan.side_effect = change_entry

    async def run():
        await flow.async_step_reconfigure()
        result = await flow.async_step_lan({CONF_HOST: "192.168.1.81"})
        assert result["reason"] == "reconfigure_successful"
        data = flow.hass.config_entries.async_update_entry.call_args.kwargs["data"]
        assert data[CONF_JWT] == "rotated-jwt"
        assert data[CONF_CLOUD_TELEMETRY] is True
        assert data[CONF_HOST] == "192.168.1.81"
        assert data[CONF_LAN_TLS_FINGERPRINT] == _PIN

    asyncio.run(run())


def _reconfigure_entry(flow, pin):
    data = {CONF_HOST: _HOST, CONF_JWT: "existing-jwt", CONF_SERIAL_NUMBER: "OLD-SN", CONF_EMAIL: "user@example.com"}
    if pin is not None:
        data[CONF_LAN_TLS_FINGERPRINT] = pin
    entry = MagicMock(entry_id="entry-1", data=data)
    flow.hass.data = {}
    flow.hass.config_entries.async_get_entry.return_value = entry
    flow.hass.config_entries.async_reload = AsyncMock()
    flow.context = {"entry_id": entry.entry_id}
    return entry


@pytest.mark.parametrize(
    ("error", "message"),
    [(DaedalusAuthError("bad creds"), "invalid_auth"), (DaedalusConnectionError("unavailable"), "cannot_login")],
)
def test_login_errors_leave_form_and_hide_credentials(setup_flow, caplog, error, message):
    flow, api, _, _ = setup_flow
    api.login_and_get_jwt.side_effect = error
    result = asyncio.run(flow.async_step_user(_CREDENTIALS))
    assert result["step_id"] == "user"
    assert result["errors"]["base"] == message
    assert _CREDENTIALS[CONF_EMAIL] not in caplog.text
    assert _CREDENTIALS[CONF_PASSWORD] not in caplog.text


# ---------------------------------------------------------------------------
# _probe_pools — locks the contract of PR #22 (auto-probe on 403005).
# Originally merged without unit coverage; these tests make sure a future
# refactor can't quietly regress the fallback / short-circuit semantics.
# ---------------------------------------------------------------------------


class TestProbePools:
    def test_preferred_pool_succeeds_no_fallback(self) -> None:
        """Preferred pool succeeds → no other pool is tried."""
        api = MagicMock()
        api.login_and_get_jwt = AsyncMock(return_value=("session", "ss", "jwt"))

        pool, session_token, session_secret, jwt = asyncio.run(
            _probe_pools(api, email="u", password="p", preferred_pool=GIGYA_POOL_EU)
        )

        assert pool == GIGYA_POOL_EU
        assert session_token == "session"
        assert jwt == "jwt"
        assert api.login_and_get_jwt.await_count == 1
        # Preferred pool's apiKey was used.
        assert api.login_and_get_jwt.await_args.kwargs["api_key"] == GIGYA_API_KEYS[GIGYA_POOL_EU]

    def test_falls_back_when_preferred_returns_403005(self) -> None:
        """Preferred returns 403005 → next pool tried, returned on success."""
        api = MagicMock()
        api.login_and_get_jwt = AsyncMock(
            side_effect=[
                DaedalusAuthError("Gigya error 403005: Unauthorized user", login_error_code=403005),
                ("session", "ss", "jwt"),
            ]
        )

        pool, session_token, session_secret, jwt = asyncio.run(
            _probe_pools(api, email="u", password="p", preferred_pool=GIGYA_POOL_EU)
        )

        # Resolved pool must be one of the non-preferred pools.
        assert pool != GIGYA_POOL_EU
        assert pool in GIGYA_API_KEYS
        assert session_token == "session"
        assert jwt == "jwt"
        assert api.login_and_get_jwt.await_count == 2

    def test_all_pools_403005_raises_all_pools_marker(self) -> None:
        """Every pool returns 403005 → raise with `all_pools:` prefix for caller mapping."""
        api = MagicMock()
        api.login_and_get_jwt = AsyncMock(
            side_effect=DaedalusAuthError("Gigya error 403005: Unauthorized user", login_error_code=403005)
        )

        with pytest.raises(DaedalusAuthError, match="^all_pools:"):
            asyncio.run(_probe_pools(api, email="u@example.com", password="p", preferred_pool=GIGYA_POOL_EU))

        # Every pool must have been tried exactly once.
        assert api.login_and_get_jwt.await_count == len(GIGYA_API_KEYS)

    def test_non_403005_short_circuits(self) -> None:
        """Wrong password (403042) must NOT cause us to burn the other pools."""
        api = MagicMock()
        api.login_and_get_jwt = AsyncMock(
            side_effect=DaedalusAuthError("Gigya error 403042: Login was unsuccessful — bad password")
        )

        with pytest.raises(DaedalusAuthError, match="403042"):
            asyncio.run(_probe_pools(api, email="u", password="p", preferred_pool=GIGYA_POOL_EU))

        assert api.login_and_get_jwt.await_count == 1

    def test_preferred_pool_first_in_order(self) -> None:
        """When preferred is EU_US, the first call uses EU_US apiKey, not EU."""
        api = MagicMock()
        api.login_and_get_jwt = AsyncMock(return_value=("session", "ss", "jwt"))

        pool, _, _, _ = asyncio.run(_probe_pools(api, email="u", password="p", preferred_pool=GIGYA_POOL_EU_US))

        assert pool == GIGYA_POOL_EU_US
        assert api.login_and_get_jwt.await_args.kwargs["api_key"] == GIGYA_API_KEYS[GIGYA_POOL_EU_US]

    def test_fallback_logs_info_with_resolved_pool(self, caplog: pytest.LogCaptureFixture) -> None:
        """Operators should see in logs which pool actually worked when fallback fires."""
        api = MagicMock()
        api.login_and_get_jwt = AsyncMock(
            side_effect=[
                DaedalusAuthError("Gigya error 403005: Unauthorized user", login_error_code=403005),
                ("session", "ss", "jwt"),
            ]
        )

        with caplog.at_level(logging.INFO, logger="custom_components.delonghi_daedalus.config_flow"):
            asyncio.run(_probe_pools(api, email="u", password="p", preferred_pool=GIGYA_POOL_EU))

        assert any(
            "preferred pool" in record.getMessage() and "succeeded" in record.getMessage() for record in caplog.records
        ), f"expected fallback log, got: {[r.getMessage() for r in caplog.records]}"


class TestReauthFlow:
    """Reauth path replaces a revoked session_token without losing host/SN/region."""

    def _make_entry(self, *, data: dict[str, Any]) -> MagicMock:
        entry = MagicMock()
        entry.entry_id = "entry-1"
        entry.data = data
        return entry

    def _make_reauth_flow(self, api: MagicMock, entry: MagicMock) -> DaedalusConfigFlow:
        flow = DaedalusConfigFlow()
        flow.hass = MagicMock()
        flow.hass.config_entries.async_get_entry = MagicMock(return_value=entry)
        flow.hass.config_entries.async_update_entry = MagicMock()
        flow.hass.config_entries.async_reload = AsyncMock()
        flow.context = {"entry_id": entry.entry_id}
        flow._api_factory = lambda: api  # type: ignore[attr-defined]
        return flow

    def test_reauth_confirm_renews_session_and_aborts_successful(self) -> None:
        api = MagicMock()
        api.login_and_get_jwt = AsyncMock(return_value=("session-new", "secret-new", "jwt-new"))
        api.close = AsyncMock()
        entry = self._make_entry(
            data={
                CONF_EMAIL: "user@example.com",
                CONF_HOST: "192.168.1.42",
                CONF_SERIAL_NUMBER: "SN1234",
                CONF_MACHINE_NAME: "SN1234",
                CONF_POOL: GIGYA_POOL_EU,
                CONF_JWT: "jwt-old",
                CONF_SESSION_TOKEN: "session-old",
            }
        )
        flow = self._make_reauth_flow(api, entry)

        # Step 1 — HA triggers reauth, we render the password form.
        first = asyncio.run(flow.async_step_reauth({}))
        assert first["type"] == "form"
        assert first["step_id"] == "reauth_confirm"

        # Step 2 — user submits new password.
        result = asyncio.run(flow.async_step_reauth_confirm({CONF_PASSWORD: "new-pw"}))

        assert result["type"] == "abort"
        assert result["reason"] == "reauth_successful"
        # Entry was updated with renewed tokens; non-secret fields preserved.
        flow.hass.config_entries.async_update_entry.assert_called_once()
        kwargs = flow.hass.config_entries.async_update_entry.call_args.kwargs
        new_data = kwargs["data"]
        assert new_data[CONF_JWT] == "jwt-new"
        assert new_data[CONF_SESSION_TOKEN] == "session-new"
        assert new_data[CONF_HOST] == "192.168.1.42"
        assert new_data[CONF_SERIAL_NUMBER] == "SN1234"
        assert CONF_PASSWORD not in new_data  # still not persisted
        flow.hass.config_entries.async_reload.assert_awaited_once_with("entry-1")

    @pytest.mark.parametrize(
        ("failure", "error"),
        [
            (DaedalusAuthError("Gigya error 403042: bad password"), "invalid_auth"),
            (DaedalusConnectionError("Gigya error 6005: Rate limited"), "cannot_login"),
            (TimeoutError(), "cannot_login"),
        ],
    )
    def test_reauth_confirm_failure_keeps_form(self, failure, error) -> None:
        api = MagicMock()
        api.login_and_get_jwt = AsyncMock(side_effect=failure)
        api.close = AsyncMock()
        entry = self._make_entry(
            data={
                CONF_EMAIL: "user@example.com",
                CONF_HOST: "192.168.1.42",
                CONF_SERIAL_NUMBER: "SN1234",
                CONF_POOL: GIGYA_POOL_EU,
                CONF_JWT: "jwt-old",
                CONF_SESSION_TOKEN: "session-old",
            }
        )
        flow = self._make_reauth_flow(api, entry)
        asyncio.run(flow.async_step_reauth({}))

        result = asyncio.run(flow.async_step_reauth_confirm({CONF_PASSWORD: "user-input"}))

        assert result["type"] == "form"
        assert result["errors"]["base"] == error
        api.login_and_get_jwt.assert_awaited_once()
        flow.hass.config_entries.async_update_entry.assert_not_called()

    def test_reauth_confirm_all_pools_failure_maps_to_dedicated_error(self) -> None:
        api = MagicMock()
        api.login_and_get_jwt = AsyncMock(
            side_effect=DaedalusAuthError("Gigya error 403005: Unauthorized user", login_error_code=403005)
        )
        api.close = AsyncMock()
        entry = self._make_entry(
            data={
                CONF_EMAIL: "user@example.com",
                CONF_HOST: "192.168.1.42",
                CONF_SERIAL_NUMBER: "SN1234",
                CONF_POOL: GIGYA_POOL_EU,
                CONF_JWT: "jwt-old",
                CONF_SESSION_TOKEN: "session-old",
            }
        )
        flow = self._make_reauth_flow(api, entry)
        asyncio.run(flow.async_step_reauth({}))

        result = asyncio.run(flow.async_step_reauth_confirm({CONF_PASSWORD: "p"}))

        assert result["type"] == "form"
        assert result["errors"]["base"] == "all_pools_unauthorized"


def test_step_user_maps_all_pools_failure_to_dedicated_error_key() -> None:
    """End-to-end: when probe burns all pools, surface the all_pools_unauthorized error key."""
    api = MagicMock()
    api.login_and_get_jwt = AsyncMock(
        side_effect=DaedalusAuthError("Gigya error 403005: Unauthorized user", login_error_code=403005)
    )
    api.close = AsyncMock()

    flow = _make_flow(api)
    result = asyncio.run(
        flow.async_step_user(
            {
                CONF_EMAIL: "u",
                CONF_PASSWORD: "p",
                CONF_HOST: "192.168.1.42",
                CONF_SERIAL_NUMBER: "SN",
            }
        )
    )
    assert result["type"] == "form"
    assert result["errors"]["base"] == "all_pools_unauthorized"


class TestProbePoolsEmailPrivacy:
    """Audit 2026-11-05: the all_pools failure message is logged verbatim by
    the flow — the email inside it must be masked, not raw.
    """

    def test_all_pools_message_masks_email(self):
        async def _run_probe():
            api = MagicMock()
            api.login_and_get_jwt = AsyncMock(
                side_effect=DaedalusAuthError("Gigya error 403005: Unauthorized user", login_error_code=403005)
            )
            return await _probe_pools(
                api,
                email="sebastien.martin@example.com",
                password="x",
                preferred_pool=GIGYA_POOL_EU,
            )

        with pytest.raises(DaedalusAuthError) as excinfo:
            asyncio.run(_run_probe())

        msg = str(excinfo.value)
        assert msg.startswith("all_pools:")
        assert "sebastien.martin@example.com" not in msg
        assert "s***n@***.com" in msg


# --------------------------------------------------------------------------- #
# Options flow — cloud telemetry toggle (issue #18, stivxgamer)               #
# --------------------------------------------------------------------------- #

from custom_components.delonghi_daedalus.config_flow import DaedalusOptionsFlow  # noqa: E402


def _make_entry(telemetry: bool) -> MagicMock:
    entry = MagicMock()
    entry.entry_id = "entry-1"
    entry.data = {CONF_EMAIL: "a@b.c", CONF_CLOUD_TELEMETRY: telemetry}
    return entry


def _make_options_flow(entry: MagicMock) -> DaedalusOptionsFlow:
    flow = DaedalusOptionsFlow(entry)
    flow.hass = MagicMock()
    flow.hass.data = {}
    flow.hass.config_entries.async_reload = AsyncMock()
    return flow


def test_options_flow_class_wires_to_entry() -> None:
    # NOTE: async_get_options_flow itself is @callback-decorated, which the
    # mocked homeassistant turns into a MagicMock — construct directly.
    entry = _make_entry(False)
    flow = DaedalusOptionsFlow(entry)
    assert flow._entry is entry


def test_options_shows_form_with_current_value() -> None:
    flow = _make_options_flow(_make_entry(True))
    result = asyncio.run(flow.async_step_init(None))
    assert result["type"] == "form"
    assert result["step_id"] == "init"
    # the checkbox defaults to the stored value
    schema_keys = list(result["data_schema"].schema)
    assert any(str(k) == CONF_CLOUD_TELEMETRY for k in schema_keys)
    assert result["data_schema"]({})[CONF_DRINK_VISIBILITY] is False


def test_options_can_toggle_drink_visibility() -> None:
    entry = _make_entry(True)
    flow = _make_options_flow(entry)
    result = asyncio.run(flow.async_step_init({CONF_DRINK_VISIBILITY: True}))
    assert result["type"] == "create_entry"
    assert flow.hass.config_entries.async_update_entry.call_args.kwargs["data"][CONF_DRINK_VISIBILITY] is True
    flow.hass.config_entries.async_reload.assert_awaited_once_with(entry.entry_id)


def test_favorite_options_normalize_labels_and_clear_removed_selection() -> None:
    entry = _make_entry(True)
    entry.data.update({CONF_DRINK_FAVORITES: ["Lungo"], CONF_FAVORITE_DRINK: "Lungo", CONF_JWT: "test-token"})
    flow = _make_options_flow(entry)
    result = asyncio.run(flow.async_step_init({CONF_DRINK_FAVORITES: ["", " Espresso ", "ESPRESSO", "Café"]}))
    assert result["type"] == "create_entry"
    assert flow.hass.config_entries.async_update_entry.call_args.kwargs["data"] == {
        CONF_EMAIL: "a@b.c",
        CONF_CLOUD_TELEMETRY: True,
        CONF_DRINK_FAVORITES: ["Espresso", "Café"],
        CONF_JWT: "test-token",
    }
    flow.hass.config_entries.async_reload.assert_awaited_once_with(entry.entry_id)


@pytest.mark.parametrize(
    "labels", [["x" * 256], ["Too\nLong"], ["unavailable"], ["unknown"], [f"Label {i}" for i in range(51)]]
)
def test_favorite_options_reject_unusable_labels(labels: list[str]) -> None:
    flow = _make_options_flow(_make_entry(True))
    result = asyncio.run(flow.async_step_init({CONF_DRINK_FAVORITES: labels}))
    assert result["type"] == "form"
    assert result["errors"] == {CONF_DRINK_FAVORITES: "invalid_drink_favorites"}
    flow.hass.config_entries.async_update_entry.assert_not_called()
    flow.hass.config_entries.async_reload.assert_not_awaited()


def test_options_toggle_updates_data_and_reloads() -> None:
    entry = _make_entry(False)
    flow = _make_options_flow(entry)
    result = asyncio.run(flow.async_step_init({CONF_CLOUD_TELEMETRY: True}))
    assert result["type"] == "create_entry"
    flow.hass.config_entries.async_update_entry.assert_called_once()
    args, kwargs = flow.hass.config_entries.async_update_entry.call_args
    assert args[0] is entry
    new_data = kwargs["data"]
    assert new_data[CONF_CLOUD_TELEMETRY] is True
    assert new_data[CONF_EMAIL] == "a@b.c"  # other keys preserved
    flow.hass.config_entries.async_reload.assert_awaited_once_with("entry-1")


def test_options_no_change_does_not_reload() -> None:
    entry = _make_entry(True)
    flow = _make_options_flow(entry)
    result = asyncio.run(flow.async_step_init({CONF_CLOUD_TELEMETRY: True}))
    assert result["type"] == "create_entry"
    flow.hass.config_entries.async_update_entry.assert_not_called()
    flow.hass.config_entries.async_reload.assert_not_called()


def test_cloud_only_options_cannot_disable_last_transport() -> None:
    entry = _make_entry(True)
    flow = _make_options_flow(entry)
    result = asyncio.run(flow.async_step_init({CONF_CLOUD_TELEMETRY: False}))
    assert result["type"] == "form"
    flow.hass.config_entries.async_update_entry.assert_not_called()
    flow.hass.config_entries.async_reload.assert_not_awaited()


def test_options_can_disable_cloud_when_lan_is_verified() -> None:
    entry = _make_entry(True)
    entry.data[CONF_LAN_TLS_FINGERPRINT] = _PIN
    flow = _make_options_flow(entry)
    result = asyncio.run(flow.async_step_init({CONF_CLOUD_TELEMETRY: False}))
    assert result["type"] == "create_entry"
    assert flow.hass.config_entries.async_update_entry.call_args.kwargs["data"][CONF_CLOUD_TELEMETRY] is False
    flow.hass.config_entries.async_reload.assert_awaited_once_with(entry.entry_id)
