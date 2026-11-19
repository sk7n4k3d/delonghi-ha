"""Audit 2026-11-05: JWT rotation rejected twice by the device must escalate
to a HA reauth flow, not loop in UpdateFailed.

`_ensure_connected` documents "if the session token is revoked we escalate to
reauth" — but only the *Gigya* refresh failure did. A freshly rotated JWT
that the machine still rejected fell through as DaedalusAuthError and
`_async_update_data` mapped it to UpdateFailed: endless retry, no user prompt.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed

from custom_components.delonghi_daedalus.api import (
    DaedalusAuthError,
    DaedalusConnectionError,
)
from custom_components.delonghi_daedalus.const import (
    CONF_CLOUD_TELEMETRY,
    CONF_HOST,
    CONF_JWT,
    CONF_POOL,
    CONF_SERIAL_NUMBER,
    CONF_SESSION_TOKEN,
    DOMAIN,
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


@pytest.mark.parametrize(("serial", "masked"), [("PrivateSerial1234", "*************1234"), ("1234", "****")])
def test_coordinator_log_name_masks_serial(serial, masked):
    entry = MagicMock(data={CONF_SERIAL_NUMBER: serial})
    coord = DaedalusCoordinator(MagicMock(), entry)

    assert coord.init_kwargs["name"] == f"{DOMAIN}:{masked}"


@pytest.mark.parametrize("cloud_enabled", [False, True])
def test_lan_recovers_after_transient_gigya_failure(cloud_enabled):
    async def _go():
        coord = _make_coordinator()
        coord.entry.data[CONF_CLOUD_TELEMETRY] = cloud_enabled
        coord._schedule_cloud_fetch = MagicMock()
        coord._async_listen_lan = AsyncMock()
        coord.hass.async_create_background_task.side_effect = lambda coro, _name: asyncio.create_task(coro)

        connection = MagicMock(auth_message="AUTH", closed=False, connection_id=7)
        coord._api.connect_lan.side_effect = [DaedalusAuthError("expired"), DaedalusAuthError("expired"), connection]
        coord._api.refresh_jwt.side_effect = [DaedalusConnectionError("Gigya error 6005: Rate limited"), "fresh-jwt"]
        with patch("custom_components.delonghi_daedalus.coordinator.time") as clock:
            clock.monotonic.return_value = 100.9
            data = await coord._async_update_data()
            assert not data["connected"]
            assert "6005" in data["lan_error"]
            await coord._async_update_data()
            coord._api.connect_lan.assert_awaited_once()
            coord._api.refresh_jwt.assert_awaited_once()

            clock.monotonic.return_value = 130.2
            data = await coord._async_update_data()
            assert data["connected"]
            assert "lan_error" not in data
            assert coord._api.connect_lan.await_args.kwargs["jwt"] == "fresh-jwt"
            assert coord._lan_retry_at == 0.0
            await coord._lan_task

        coord.entry.async_start_reauth.assert_not_called()
        assert coord._schedule_cloud_fetch.call_count == (3 if cloud_enabled else 0)

    asyncio.run(_go())


def test_lan_reauth_keeps_retrying_with_bounded_backoff_and_resets_for_changed_credentials():
    async def _go():
        coord = _make_coordinator()
        coord._api.connect_lan.side_effect = DaedalusAuthError("expired")
        coord._api.refresh_jwt.side_effect = DaedalusAuthError("session revoked")
        with patch("custom_components.delonghi_daedalus.coordinator.time") as clock:
            now = 100
            for attempt, delay in enumerate((30, 60, 120, 240, 300, 300), start=1):
                clock.monotonic.return_value = now
                data = await coord._async_update_data()
                assert not data["connected"]
                assert "reauthentication" in data["lan_error"] or "session token rejected" in data["lan_error"]
                assert coord._api.connect_lan.await_count == attempt
                clock.monotonic.return_value = now + delay - 1
                await coord._async_update_data()
                assert coord._api.connect_lan.await_count == attempt
                now += delay

            coord._api.refresh_jwt.assert_awaited_once()
            coord.entry.async_start_reauth.assert_called_once_with(coord.hass)

            # A new session skips the outstanding backoff and can renew its JWT.
            coord.entry.data[CONF_SESSION_TOKEN] = "new-session"
            coord._api.refresh_jwt.side_effect = None
            coord._api.connect_lan.side_effect = [DaedalusAuthError("expired"), DaedalusConnectionError("offline")]
            clock.monotonic.return_value = now - 250
            await coord._async_update_data()
            assert coord._api.refresh_jwt.await_count == 2
            assert coord._api.refresh_jwt.await_args.kwargs["session_token"] == "new-session"
            assert coord._lan_retry_at == now - 250 + 30
            coord.entry.async_start_reauth.assert_called_once_with(coord.hass)

    asyncio.run(_go())
