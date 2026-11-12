"""Audit 2026-11-05: JWT rotation rejected twice by the device must escalate
to a HA reauth flow, not loop in UpdateFailed.

`_ensure_connected` documents "if the session token is revoked we escalate to
reauth" — but only the *Gigya* refresh failure did. A freshly rotated JWT
that the machine still rejected fell through as DaedalusAuthError and
`_async_update_data` mapped it to UpdateFailed: endless retry, no user prompt.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed

from custom_components.delonghi_daedalus.api import (
    DaedalusAuthError,
    DaedalusConnectionError,
)
from custom_components.delonghi_daedalus.const import (
    CONF_HOST,
    CONF_JWT,
    CONF_POOL,
    CONF_SERIAL_NUMBER,
    CONF_SESSION_TOKEN,
)
from custom_components.delonghi_daedalus.coordinator import DaedalusCoordinator


def _make_coordinator() -> DaedalusCoordinator:
    entry = MagicMock()
    entry.data = {
        CONF_HOST: "192.168.1.50",
        CONF_JWT: "old-jwt",
        CONF_SERIAL_NUMBER: "SN1",
        CONF_SESSION_TOKEN: "session-token",
        CONF_POOL: "EU",
    }
    hass = MagicMock()
    coord = DaedalusCoordinator(hass, entry)
    # conftest's _GenericBase (the DataUpdateCoordinator stub) does not set
    # .hass — production base class does; wire it explicitly.
    coord.hass = hass
    coord._api = MagicMock()
    coord._api.connect_lan = AsyncMock()
    coord._api.refresh_jwt = AsyncMock(return_value="fresh-jwt")
    return coord


def test_second_rejection_escalates_to_reauth():
    """Device rejects BOTH the stored and the freshly rotated JWT."""
    coord = _make_coordinator()
    coord._api.connect_lan.side_effect = [
        DaedalusAuthError("expired"),
        DaedalusAuthError("rejected again"),
    ]

    with pytest.raises(ConfigEntryAuthFailed, match="freshly rotated JWT"):
        asyncio.run(coord._ensure_connected())

    coord._api.refresh_jwt.assert_awaited_once()
    assert coord._lan is None  # the rejected connection was never assigned


def test_rotation_success_assigns_connection():
    """Happy path unchanged: rotated JWT accepted → live connection."""
    coord = _make_coordinator()
    fake_conn = MagicMock()
    fake_conn.auth_message = "AUTH"  # matches entry default → no extra entry update
    coord._api.connect_lan.side_effect = [DaedalusAuthError("expired"), fake_conn]

    asyncio.run(coord._ensure_connected())

    assert coord._lan is fake_conn
    # The rotated JWT was persisted before the retry.
    coord.hass.config_entries.async_update_entry.assert_called_once()


def test_transport_error_on_retry_stays_transient():
    """A WiFi blip after rotation must NOT open a reauth flow — retry is right."""
    coord = _make_coordinator()
    coord._api.connect_lan.side_effect = [
        DaedalusAuthError("expired"),
        DaedalusConnectionError("wifi dropped"),
    ]

    with pytest.raises(DaedalusConnectionError):
        asyncio.run(coord._ensure_connected())


def test_first_attempt_transport_error_does_not_rotate_jwt():
    """Audit 2026-11-05 (M2 companion): a malformed/unreachable reply on the
    FIRST try (machine booting, wrong IP answering on 443) is transient — no
    JWT rotation, no reauth flow, auto-heal on the next 30 s cycle."""
    coord = _make_coordinator()
    coord._api.connect_lan = AsyncMock(side_effect=DaedalusConnectionError("LAN AUTH got a malformed reply: boom"))

    with pytest.raises(DaedalusConnectionError):
        asyncio.run(coord._ensure_connected())

    coord._api.refresh_jwt.assert_not_awaited()
    coord.hass.config_entries.async_update_entry.assert_not_called()


def test_coordinator_passes_config_entry_to_base():
    """Audit 2026-11-05 (L4): the reauth escalation depends on
    self.config_entry; without the explicit kwarg it rides on HA's
    `current_entry` ContextVar — undocumented-behavior territory the added
    test must pin."""
    entry = MagicMock()
    entry.data = {
        CONF_HOST: "192.168.1.50",
        CONF_JWT: "j",
        CONF_SERIAL_NUMBER: "SN1",
        CONF_SESSION_TOKEN: "s",
        CONF_POOL: "EU",
    }
    coord = DaedalusCoordinator(MagicMock(), entry)
    assert coord.init_kwargs.get("config_entry") is entry
