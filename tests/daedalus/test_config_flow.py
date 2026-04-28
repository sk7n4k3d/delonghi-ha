"""Tests for the Daedalus config flow.

Validates happy path + auth/connection error branches. The flow intentionally
accepts the host/SN from the user (no BLE provisioning yet) and uses the LAN
WS to validate they're reachable before creating the entry.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.delonghi_daedalus.api import (
    DaedalusAuthError,
    DaedalusConnectionError,
)
from custom_components.delonghi_daedalus.config_flow import (
    DaedalusConfigFlow,
    _probe_pools,
)
from custom_components.delonghi_daedalus.const import (
    CONF_EMAIL,
    CONF_HOST,
    CONF_JWT,
    CONF_MACHINE_NAME,
    CONF_PASSWORD,
    CONF_POOL,
    CONF_SERIAL_NUMBER,
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


def test_create_entry_on_success() -> None:
    api = MagicMock()
    api.login_and_get_jwt = AsyncMock(return_value=("session-abc", "jwt-xyz"))
    lan = MagicMock()
    lan.connection_id = 7
    lan.close = AsyncMock()
    api.connect_lan = AsyncMock(return_value=lan)
    api.close = AsyncMock()

    flow = _make_flow(api)
    user_input = {
        CONF_EMAIL: "user@example.com",
        CONF_PASSWORD: "hunter2",
        CONF_HOST: "192.168.1.42",
        CONF_SERIAL_NUMBER: "SN1234",
    }

    result = asyncio.run(flow.async_step_user(user_input))

    assert result["type"] == "create_entry"
    assert result["title"].startswith("My Coffee Lounge")
    data = result["data"]
    assert data[CONF_EMAIL] == "user@example.com"
    # Password is intentionally NOT persisted — reauth flow is used to
    # ask the user for it again if the session token is ever revoked.
    assert CONF_PASSWORD not in data
    assert data[CONF_HOST] == "192.168.1.42"
    assert data[CONF_SERIAL_NUMBER] == "SN1234"
    assert data[CONF_JWT] == "jwt-xyz"
    assert data[CONF_SESSION_TOKEN] == "session-abc"
    # machine_name defaults to SN until we can pull it from the cloud /devices
    assert data[CONF_MACHINE_NAME] == "SN1234"
    # Probe connection must have been closed.
    lan.close.assert_awaited()


def test_invalid_auth_shows_form_error() -> None:
    api = MagicMock()
    api.login_and_get_jwt = AsyncMock(side_effect=DaedalusAuthError("bad creds"))
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
    assert result["errors"]["base"] == "invalid_auth"


def test_cannot_connect_shows_form_error() -> None:
    api = MagicMock()
    api.login_and_get_jwt = AsyncMock(return_value=("s", "jwt"))
    api.connect_lan = AsyncMock(side_effect=DaedalusConnectionError("no route"))
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
    assert result["errors"]["base"] == "cannot_connect"


def test_pool_selection_passes_matching_api_key_to_login() -> None:
    """When user picks Pool EU_US, the flow must forward the EU_US Gigya apiKey."""
    api = MagicMock()
    api.login_and_get_jwt = AsyncMock(return_value=("session-abc", "jwt-xyz"))
    lan = MagicMock()
    lan.connection_id = 7
    lan.close = AsyncMock()
    api.connect_lan = AsyncMock(return_value=lan)
    api.close = AsyncMock()

    flow = _make_flow(api)
    result = asyncio.run(
        flow.async_step_user(
            {
                CONF_EMAIL: "user@example.com",
                CONF_PASSWORD: "hunter2",
                CONF_HOST: "192.168.1.42",
                CONF_SERIAL_NUMBER: "SN1234",
                CONF_POOL: GIGYA_POOL_EU_US,
            }
        )
    )

    assert result["type"] == "create_entry"
    api.login_and_get_jwt.assert_awaited_once()
    call = api.login_and_get_jwt.await_args
    assert call.kwargs["api_key"] == GIGYA_API_KEYS[GIGYA_POOL_EU_US]
    assert result["data"][CONF_POOL] == GIGYA_POOL_EU_US


def test_pool_defaults_to_eu_when_not_provided() -> None:
    """Backwards-compat: existing flows without pool field keep using Pool EU."""
    api = MagicMock()
    api.login_and_get_jwt = AsyncMock(return_value=("s", "j"))
    lan = MagicMock()
    lan.connection_id = 1
    lan.close = AsyncMock()
    api.connect_lan = AsyncMock(return_value=lan)
    api.close = AsyncMock()

    flow = _make_flow(api)
    asyncio.run(
        flow.async_step_user(
            {
                CONF_EMAIL: "u",
                CONF_PASSWORD: "p",
                CONF_HOST: "192.168.1.42",
                CONF_SERIAL_NUMBER: "SN",
            }
        )
    )
    assert api.login_and_get_jwt.await_args.kwargs["api_key"] == GIGYA_API_KEYS[GIGYA_POOL_EU]


def test_invalid_auth_logs_reason(caplog: pytest.LogCaptureFixture) -> None:
    """Auth rejection must surface the upstream Gigya error in HA logs.

    Without this, users see only the translated 'Invalid credentials' form
    error and can't tell a real bad-password apart from a pool/apiKey
    mismatch — which is exactly what happened with Eletta Ultra users who
    sit on Gigya Pool EU_US while the integration defaulted to EU.
    """
    api = MagicMock()
    api.login_and_get_jwt = AsyncMock(side_effect=DaedalusAuthError("Gigya error 400093: invalid apiKey"))
    api.close = AsyncMock()

    flow = _make_flow(api)
    with caplog.at_level(logging.WARNING, logger="custom_components.delonghi_daedalus.config_flow"):
        asyncio.run(
            flow.async_step_user(
                {
                    CONF_EMAIL: "u",
                    CONF_PASSWORD: "p",
                    CONF_HOST: "192.168.1.42",
                    CONF_SERIAL_NUMBER: "SN",
                }
            )
        )
    assert any(
        "400093" in record.getMessage() or "invalid apiKey" in record.getMessage() for record in caplog.records
    ), f"expected Gigya error detail in logs, got: {[r.getMessage() for r in caplog.records]}"


def test_cannot_connect_logs_reason(caplog: pytest.LogCaptureFixture) -> None:
    api = MagicMock()
    api.login_and_get_jwt = AsyncMock(return_value=("s", "jwt"))
    api.connect_lan = AsyncMock(
        side_effect=DaedalusConnectionError(
            "LAN WS connect to wss://10.0.0.5/ws/lan2lan failed: [Errno 113] No route to host"
        )
    )
    api.close = AsyncMock()

    flow = _make_flow(api)
    with caplog.at_level(logging.WARNING, logger="custom_components.delonghi_daedalus.config_flow"):
        asyncio.run(
            flow.async_step_user(
                {
                    CONF_EMAIL: "u",
                    CONF_PASSWORD: "p",
                    CONF_HOST: "10.0.0.5",
                    CONF_SERIAL_NUMBER: "SN",
                }
            )
        )
    assert any(
        "No route to host" in record.getMessage() or "LAN WS connect" in record.getMessage()
        for record in caplog.records
    ), f"expected LAN connect detail in logs, got: {[r.getMessage() for r in caplog.records]}"


def test_unknown_error_shows_form_error() -> None:
    api = MagicMock()
    api.login_and_get_jwt = AsyncMock(side_effect=RuntimeError("boom"))
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
    assert result["errors"]["base"] == "unknown"


# ---------------------------------------------------------------------------
# _probe_pools — locks the contract of PR #22 (auto-probe on 403005).
# Originally merged without unit coverage; these tests make sure a future
# refactor can't quietly regress the fallback / short-circuit semantics.
# ---------------------------------------------------------------------------


class TestProbePools:
    def test_preferred_pool_succeeds_no_fallback(self) -> None:
        """Preferred pool succeeds → no other pool is tried."""
        api = MagicMock()
        api.login_and_get_jwt = AsyncMock(return_value=("session", "jwt"))

        pool, session_token, jwt = asyncio.run(
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
                DaedalusAuthError("Gigya error 403005: Unauthorized user"),
                ("session", "jwt"),
            ]
        )

        pool, session_token, jwt = asyncio.run(
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
            side_effect=DaedalusAuthError("Gigya error 403005: Unauthorized user")
        )

        with pytest.raises(DaedalusAuthError, match="^all_pools:"):
            asyncio.run(
                _probe_pools(api, email="u@example.com", password="p", preferred_pool=GIGYA_POOL_EU)
            )

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
        api.login_and_get_jwt = AsyncMock(return_value=("session", "jwt"))

        pool, _, _ = asyncio.run(
            _probe_pools(api, email="u", password="p", preferred_pool=GIGYA_POOL_EU_US)
        )

        assert pool == GIGYA_POOL_EU_US
        assert api.login_and_get_jwt.await_args.kwargs["api_key"] == GIGYA_API_KEYS[GIGYA_POOL_EU_US]

    def test_fallback_logs_info_with_resolved_pool(self, caplog: pytest.LogCaptureFixture) -> None:
        """Operators should see in logs which pool actually worked when fallback fires."""
        api = MagicMock()
        api.login_and_get_jwt = AsyncMock(
            side_effect=[
                DaedalusAuthError("Gigya error 403005: Unauthorized user"),
                ("session", "jwt"),
            ]
        )

        with caplog.at_level(logging.INFO, logger="custom_components.delonghi_daedalus.config_flow"):
            asyncio.run(_probe_pools(api, email="u", password="p", preferred_pool=GIGYA_POOL_EU))

        assert any(
            "preferred pool" in record.getMessage() and "succeeded" in record.getMessage()
            for record in caplog.records
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
        api.login_and_get_jwt = AsyncMock(return_value=("session-new", "jwt-new"))
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

    def test_reauth_confirm_invalid_password_keeps_form(self) -> None:
        api = MagicMock()
        api.login_and_get_jwt = AsyncMock(
            side_effect=DaedalusAuthError("Gigya error 403042: bad password")
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

        result = asyncio.run(flow.async_step_reauth_confirm({CONF_PASSWORD: "wrong"}))

        assert result["type"] == "form"
        assert result["errors"]["base"] == "invalid_auth"
        flow.hass.config_entries.async_update_entry.assert_not_called()

    def test_reauth_confirm_all_pools_failure_maps_to_dedicated_error(self) -> None:
        api = MagicMock()
        api.login_and_get_jwt = AsyncMock(
            side_effect=DaedalusAuthError("Gigya error 403005: Unauthorized user")
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
        side_effect=DaedalusAuthError("Gigya error 403005: Unauthorized user")
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
