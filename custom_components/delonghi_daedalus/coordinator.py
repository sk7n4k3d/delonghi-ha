"""Data coordinator for Daedalus.

Holds the LAN WebSocket connection, exposes a snapshot dict to entities,
and reconnects on failure. Optional read-only cloud telemetry (issue #18,
opt-in via CONF_CLOUD_TELEMETRY) adds the AWS IoT shadow documents on
top; cloud failures never poison the LAN snapshot.
"""

from __future__ import annotations

import asyncio
import time
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
from .cloud import CloudTelemetryError, fetch_cloud_state, flatten_shadow
from .const import (
    CLOUD_FETCH_TIMEOUT_SECONDS,
    CLOUD_POLL_SECONDS,
    CONF_CLOUD_TELEMETRY,
    CONF_HOST,
    CONF_JWT,
    CONF_LAN_AUTH_MESSAGE,
    CONF_MACHINE_NAME,
    CONF_POOL,
    CONF_SERIAL_NUMBER,
    CONF_SESSION_SECRET,
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
        # Cloud telemetry cache (opt-in): refreshed at most every
        # CLOUD_POLL_SECONDS, independent of the 30 s LAN cycle. The lock
        # serializes overlapping cycles (manual refresh + timer) so the
        # cache fields never interleave writes (review #18, F5).
        self._cloud_data: dict[str, Any] | None = None
        self._cloud_fetched_at: float = 0.0
        self._cloud_error: str | None = None
        self._cloud_lock = asyncio.Lock()

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
            data: dict[str, Any] = {
                "connected": not self._lan.closed,
                "connection_id": self._lan.connection_id,
                "serial_number": self.serial_number,
                "host": self.entry.data.get(CONF_HOST),
            }
        if self.entry.data.get(CONF_CLOUD_TELEMETRY):
            # Outside the LAN lock: the cloud hop is independent of the WS
            # and must never block or fail it.
            await self._async_cloud_update(data)
        return data

    async def _ensure_connected(self) -> None:
        if self._lan is not None and not self._lan.closed:
            return

        host = self.entry.data[CONF_HOST]
        jwt = self.entry.data[CONF_JWT]
        auth_message = self.entry.data.get(CONF_LAN_AUTH_MESSAGE, "AUTH")
        try:
            self._lan = await self._api.connect_lan(
                host=host, serial_number=self.serial_number, jwt=jwt, auth_message=auth_message
            )
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
                fresh_jwt = await self._api.refresh_jwt(
                    session_token=session_token,
                    api_key=api_key,
                    session_secret=self.entry.data.get(CONF_SESSION_SECRET),
                )
            except DaedalusAuthError as refresh_exc:
                raise ConfigEntryAuthFailed(
                    f"Daedalus session token rejected by Gigya: {sanitize_text(refresh_exc, host=host)}"
                ) from refresh_exc
            self.hass.config_entries.async_update_entry(self.entry, data={**self.entry.data, CONF_JWT: fresh_jwt})
            try:
                self._lan = await self._api.connect_lan(
                    host=host,
                    serial_number=self.serial_number,
                    jwt=fresh_jwt,
                    auth_message=auth_message,
                )
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
        # Remember which AUTH spelling this machine accepted (#18) so the
        # next reconnect does not start with the rejected one.
        if self._lan.auth_message != self.entry.data.get(CONF_LAN_AUTH_MESSAGE, "AUTH"):
            self.hass.config_entries.async_update_entry(
                self.entry, data={**self.entry.data, CONF_LAN_AUTH_MESSAGE: self._lan.auth_message}
            )

    async def _async_cloud_update(self, data: dict[str, Any]) -> None:
        """Refresh (or reuse) the shadow snapshot. Never raises.

        The whole body — fetch AND projection — sits under one lock +
        try/except: a pathological shadow document (deep nesting,
        unparsable shapes) and overlapping cycles must both degrade to
        cloud_error, never reach DataUpdateCoordinator (review #18, F3/F5).
        """
        async with self._cloud_lock:
            now = time.monotonic()
            if self._cloud_data is not None and now - self._cloud_fetched_at < CLOUD_POLL_SECONDS:
                data["cloud"] = self._cloud_data
                if self._cloud_error:
                    data["cloud_error"] = self._cloud_error
                return
            try:
                stored_name = self.entry.data.get(CONF_MACHINE_NAME)
                docs, thing = await asyncio.wait_for(
                    fetch_cloud_state(
                        self._api.http_session,
                        serial=self.serial_number,
                        jwt=self.entry.data[CONF_JWT],
                        # CONF_MACHINE_NAME defaults to the serial at setup time —
                        # only a different value means a real cloud thing name.
                        machine_name=stored_name if stored_name and stored_name != self.serial_number else None,
                    ),
                    # Global budget for the whole cloud hop (REST + 2 MQTT
                    # connects + shadow wait): without it a black-holed broker
                    # could hold _cloud_lock — and delay the coordinator
                    # refresh — on aiohttp's 300 s session default
                    # (audit #18, D1).
                    timeout=CLOUD_FETCH_TIMEOUT_SECONDS,
                )
                cloud_data = {
                    "machine_name": thing,
                    "status_flat": flatten_shadow(docs.get("MachineStatus", {})),
                    "capabilities_flat": flatten_shadow(docs.get("MachineCapabilities", {})),
                    "fetched_at": time.time(),
                }
            except CloudTelemetryError as exc:
                self._cloud_error = sanitize_text(exc, host=self.entry.data.get(CONF_HOST))
                _LOGGER.debug("Daedalus cloud telemetry failed (non-fatal): %s", self._cloud_error)
                data["cloud_error"] = self._cloud_error
                if self._cloud_data is not None:
                    data["cloud"] = self._cloud_data
                return
            except Exception as exc:  # noqa: BLE001 — cloud must never take the entry down
                self._cloud_error = sanitize_text(f"unexpected cloud error: {exc}", host=self.entry.data.get(CONF_HOST))
                _LOGGER.warning("Daedalus cloud telemetry unexpected failure (non-fatal): %s", self._cloud_error)
                data["cloud_error"] = self._cloud_error
                return
            self._cloud_error = None
            if thing != self.entry.data.get(CONF_MACHINE_NAME):
                self.hass.config_entries.async_update_entry(
                    self.entry, data={**self.entry.data, CONF_MACHINE_NAME: thing}
                )
            self._cloud_data = cloud_data
            self._cloud_fetched_at = now
            data["cloud"] = cloud_data

    async def async_shutdown(self) -> None:
        if self._lan is not None:
            await self._lan.close()
            self._lan = None
        await self._api.close()
        # Cancel the refresh timer + config-entry listener (audit #18, F3):
        # entry.py now also calls this on a failed platform setup, where
        # async_unload_entry never runs.
        await super().async_shutdown()
