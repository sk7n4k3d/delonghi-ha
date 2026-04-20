"""Tests for the Daedalus config flow.

Validates happy path + auth/connection error branches. The flow intentionally
accepts the host/SN from the user (no BLE provisioning yet) and uses the LAN
WS to validate they're reachable before creating the entry.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from custom_components.delonghi_daedalus.api import (
    DaedalusAuthError,
    DaedalusConnectionError,
)
from custom_components.delonghi_daedalus.config_flow import DaedalusConfigFlow
from custom_components.delonghi_daedalus.const import (
    CONF_EMAIL,
    CONF_HOST,
    CONF_JWT,
    CONF_MACHINE_NAME,
    CONF_PASSWORD,
    CONF_SERIAL_NUMBER,
    CONF_SESSION_TOKEN,
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
    assert data[CONF_PASSWORD] == "hunter2"
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
