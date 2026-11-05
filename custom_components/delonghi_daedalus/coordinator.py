"""Data coordinator for Daedalus.

Currently minimal: holds the LAN WebSocket connection, exposes a snapshot
dict to entities, and reconnects on failure. Brewing commands / shadow
decoding come in a follow-up once we've captured the `Message` catalog.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .api import (
    DaedalusApi,
    DaedalusAuthError,
    DaedalusConnectionError,
    DaedalusLanConnection,
)
from .const import (
    CONF_HOST,
    CONF_JWT,
    CONF_MACHINE_NAME,
    CONF_POOL,
    CONF_SERIAL_NUMBER,
    CONF_SESSION_TOKEN,
    DEFAULT_UPDATE_INTERVAL_SECONDS,
    DOMAIN,
    GIGYA_API_KEYS,
    GIGYA_POOL_EU,
)
from .redact import get_logger, sanitize_text

_LOGGER = get_logger(__name__)


class DaedalusCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Maintain a live LAN connection to the Daedalus machine."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            # Explicit config_entry (audit 2026-11-05): without it the base
            # class relies on HA's `current_entry` ContextVar for the
            # reauth-escalation lookup — an implicit dependency HA warns
            # custom integrations off.
            config_entry=entry,
            name=f"{DOMAIN}:{entry.data.get(CONF_SERIAL_NUMBER, '?')}",
            update_interval=timedelta(seconds=DEFAULT_UPDATE_INTERVAL_SECONDS),
        )
        self.entry = entry
        self._api = DaedalusApi()
        self._lan: DaedalusLanConnection | None = None
        self._lock = asyncio.Lock()

    @property
    def serial_number(self) -> str:
        return self.entry.data[CONF_SERIAL_NUMBER]

    @property
    def machine_name(self) -> str:
        return self.entry.data.get(CONF_MACHINE_NAME, self.serial_number)

    async def _async_update_data(self) -> dict[str, Any]:
        async with self._lock:
            host = self.entry.data.get(CONF_HOST)
            try:
                await self._ensure_connected()
            except DaedalusAuthError as exc:
                raise UpdateFailed(f"auth failed: {sanitize_text(exc, host=host)}") from exc
            except DaedalusConnectionError as exc:
                # Sanitized (with the configured host so private IPv6 forms
                # are scrubbed too): the text embeds the LAN WS URL, and
                # UpdateFailed lands in HA's error log + last_exception →
                # diagnostics (audit 2026-11-05).
                raise UpdateFailed(f"connect failed: {sanitize_text(exc, host=host)}") from exc

            assert self._lan is not None  # ensured above
            return {
                "connected": not self._lan.closed,
                "connection_id": self._lan.connection_id,
                "serial_number": self.serial_number,
                "host": self.entry.data.get(CONF_HOST),
            }

    async def _ensure_connected(self) -> None:
        if self._lan is not None and not self._lan.closed:
            return

        host = self.entry.data[CONF_HOST]
        jwt = self.entry.data[CONF_JWT]
        try:
            self._lan = await self._api.connect_lan(host=host, serial_number=self.serial_number, jwt=jwt)
        except DaedalusAuthError as initial_exc:
            # Likely JWT expired — try to rotate via the stored session token.
            # If the session token itself is revoked, escalate to a HA reauth
            # flow so the user is prompted for the password again instead of
            # failing silently every 30 s.
            session_token = self.entry.data.get(CONF_SESSION_TOKEN)
            if not session_token:
                raise ConfigEntryAuthFailed("Daedalus session token missing, reauth required") from initial_exc
            pool = self.entry.data.get(CONF_POOL, GIGYA_POOL_EU)
            api_key = GIGYA_API_KEYS.get(pool, GIGYA_API_KEYS[GIGYA_POOL_EU])
            try:
                fresh_jwt = await self._api.refresh_jwt(session_token=session_token, api_key=api_key)
            except DaedalusAuthError as refresh_exc:
                raise ConfigEntryAuthFailed(
                    f"Daedalus session token rejected by Gigya: {sanitize_text(refresh_exc, host=host)}"
                ) from refresh_exc
            self.hass.config_entries.async_update_entry(self.entry, data={**self.entry.data, CONF_JWT: fresh_jwt})
            try:
                self._lan = await self._api.connect_lan(host=host, serial_number=self.serial_number, jwt=fresh_jwt)
            except DaedalusAuthError as second_exc:
                # A *fresh* JWT the device still rejects is not transient —
                # retrying the same rotation every cycle would loop in
                # UpdateFailed forever (the documented reauth escalation never
                # fires). Raise the flow the user can actually act on (audit
                # 2026-11-05). Only an explicit machine rejection reaches this
                # branch now: malformed frames map to DaedalusConnectionError
                # in api.connect_lan, so a booting/upgrade-ing machine keeps
                # the 30 s auto-healing retry instead of locking the entry
                # behind a password prompt. Transport errors keep propagating
                # as DaedalusConnectionError → UpdateFailed (retry is correct).
                raise ConfigEntryAuthFailed(
                    f"Daedalus machine rejected freshly rotated JWT: {sanitize_text(second_exc, host=host)}"
                ) from second_exc

    async def async_shutdown(self) -> None:
        if self._lan is not None:
            await self._lan.close()
            self._lan = None
        await self._api.close()
