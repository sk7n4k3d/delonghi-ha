"""Combine authenticated LAN updates and optional read-only cloud telemetry."""

from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .api import (
    DaedalusApi,
    DaedalusAuthError,
    DaedalusConnectionError,
    DaedalusLanConnection,
    DaedalusLanIdentityError,
)
from .cloud import CloudTelemetryError, fetch_cloud_state, flatten_shadow
from .const import (
    CLOUD_FETCH_TIMEOUT_SECONDS,
    CLOUD_POLL_SECONDS,
    CONF_CLOUD_TELEMETRY,
    CONF_HOST,
    CONF_JWT,
    CONF_LAN_AUTH_MESSAGE,
    CONF_LAN_TLS_FINGERPRINT,
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
from .device import DaedalusDevice
from .gigya_auth import jwt_expiry
from .lan_protocol import LanProtocolError
from .lan_repairs import async_clear_lan_issue, async_raise_lan_issue
from .redact import get_logger, mask_serial, sanitize_text

_LOGGER = get_logger(__name__)
_LAN_CONNECT_TIMEOUT = 35
_CLOUD_MAX_AGE = CLOUD_POLL_SECONDS + CLOUD_FETCH_TIMEOUT_SECONDS + DEFAULT_UPDATE_INTERVAL_SECONDS


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
            name=f"{DOMAIN}:{mask_serial(entry.data.get(CONF_SERIAL_NUMBER, '?'))}",
            update_interval=timedelta(seconds=DEFAULT_UPDATE_INTERVAL_SECONDS),
        )
        self.entry = entry
        self.device = DaedalusDevice(self)
        self._api = DaedalusApi()
        self._lan: DaedalusLanConnection | None = None
        self._lan_task: asyncio.Task | None = None
        self._lan_error: str | None = None
        self._lan_reauth_started = False
        self._reauth_requested = False
        self._lock = asyncio.Lock()
        self._jwt_lock = asyncio.Lock()
        self._refresh_auth_failed_for: tuple[str | None, ...] | None = None
        self._cloud_data: dict[str, Any] | None = None
        self._cloud_fetched_at: float = 0.0
        self._cloud_error: str | None = None
        self._cloud_error_at: float | None = None
        self._cloud_retry_at = 0.0
        self._cloud_auth_failed_for: tuple[str | None, ...] | None = None
        self._cloud_expiry: asyncio.TimerHandle | None = None
        self._cloud_task: asyncio.Task | None = None
        self._stopping = False

    @property
    def serial_number(self) -> str:
        return self.entry.data[CONF_SERIAL_NUMBER]

    @property
    def machine_name(self) -> str:
        return self.entry.data.get(CONF_MACHINE_NAME, self.serial_number)

    async def _async_update_data(self) -> dict[str, Any]:
        cloud_enabled = self.entry.data.get(CONF_CLOUD_TELEMETRY)
        if cloud_enabled:
            self._schedule_cloud_fetch()

        async with self._lock:
            host = self.entry.data.get(CONF_HOST)
            if self._lan is not None and self._lan.closed:
                await self._stop_lan_listener()
            if host and not self._stopping and not self._lan_reauth_started:
                previous_connection = self._lan
                try:
                    async with asyncio.timeout(_LAN_CONNECT_TIMEOUT):
                        await self._ensure_connected()
                except ConfigEntryAuthFailed as exc:
                    if not cloud_enabled:
                        raise
                    self._lan_error = sanitize_text(exc, host=host)
                    self._lan_reauth_started = True
                    self._request_reauth()
                except DaedalusLanIdentityError as exc:
                    self._lan_error = sanitize_text(exc, host=host)
                    async_raise_lan_issue(self.hass, self.entry.entry_id)
                except (DaedalusAuthError, DaedalusConnectionError) as exc:
                    self._lan_error = sanitize_text(exc, host=host)
                except TimeoutError:
                    self._lan_error = "LAN connection attempt timed out"
                else:
                    self._lan_error = None
                    if self._lan is not None and self._lan is not previous_connection:
                        async_clear_lan_issue(self.hass, self.entry.entry_id)
                        self._lan_task = self.hass.async_create_background_task(
                            self._async_listen_lan(self._lan), f"{DOMAIN} LAN listener"
                        )
        return self._snapshot()

    def _snapshot(self) -> dict[str, Any]:
        connected = self._lan is not None and not self._lan.closed
        data: dict[str, Any] = {
            "connected": connected,
            "serial_number": self.serial_number,
            "host": self.entry.data.get(CONF_HOST),
        }
        if connected:
            data["connection_id"] = self._lan.connection_id
        elif self._lan_error:
            data["lan_error"] = self._lan_error
        if self.entry.data.get(CONF_CLOUD_TELEMETRY):
            if self._cloud_data is not None and time.monotonic() - self._cloud_fetched_at < _CLOUD_MAX_AGE:
                data["cloud"] = self._cloud_data
            if self._cloud_error:
                data["cloud_error"] = self._cloud_error
        return data

    @callback
    def _publish_snapshot(self) -> None:
        if self._stopping or self.data is None:
            return
        snapshot = self._snapshot()
        if snapshot != self.data:
            # async_set_updated_data resets the polling deadline. Publishing
            # transport events here preserves the regular reconnect cadence.
            self.data = snapshot
            self.async_update_listeners()

    def _schedule_cloud_fetch(self) -> None:
        """Start a background shadow fetch when one is due and none is running."""
        if self._stopping or self._cloud_task is not None and not self._cloud_task.done():
            return
        if self._cloud_auth_failed_for is not None:
            if self._cloud_auth_failed_for == self._credentials():
                return
            self._cloud_auth_failed_for = None
            self._reauth_requested = False
            self._cloud_retry_at = 0.0
        now = time.monotonic()
        if now < self._cloud_retry_at:
            return
        if self._cloud_data is not None and now - self._cloud_fetched_at < CLOUD_POLL_SECONDS:
            return
        self._cloud_task = self.hass.async_create_background_task(self._async_cloud_fetch(), f"{DOMAIN} cloud fetch")

    async def _ensure_connected(self) -> None:
        if self._lan is not None and not self._lan.closed:
            return

        host = self.entry.data[CONF_HOST]
        jwt = self.entry.data[CONF_JWT]
        connect_options = {
            "host": host,
            "serial_number": self.serial_number,
            "auth_message": self.entry.data.get(CONF_LAN_AUTH_MESSAGE, "AUTH"),
            "fingerprint": self.entry.data.get(CONF_LAN_TLS_FINGERPRINT),
        }
        try:
            self._lan = await self._api.connect_lan(jwt=jwt, **connect_options)
        except DaedalusAuthError:
            fresh_jwt = await self._rotate_jwt(jwt)
            try:
                self._lan = await self._api.connect_lan(jwt=fresh_jwt, **connect_options)
            except DaedalusAuthError as second_exc:
                raise ConfigEntryAuthFailed(
                    f"Daedalus machine rejected freshly rotated JWT: {sanitize_text(second_exc, host=host)}"
                ) from second_exc
        # Remember which AUTH spelling this machine accepted (#18) so the
        # next reconnect does not start with the rejected one.
        if self._lan.auth_message != self.entry.data.get(CONF_LAN_AUTH_MESSAGE, "AUTH"):
            self.hass.config_entries.async_update_entry(
                self.entry, data={**self.entry.data, CONF_LAN_AUTH_MESSAGE: self._lan.auth_message}
            )

    def _request_reauth(self) -> None:
        if not self._reauth_requested:
            self._reauth_requested = True
            self.entry.async_start_reauth(self.hass)

    def _credentials(self) -> tuple[str | None, ...]:
        entry = self.entry.data
        return entry.get(CONF_JWT), entry.get(CONF_SESSION_TOKEN), entry.get(CONF_SESSION_SECRET)

    async def _rotate_jwt(self, stale_jwt: str) -> str:
        """Renew credentials once when LAN and cloud need them together."""
        async with self._jwt_lock:
            current, session_token, session_secret = credentials = self._credentials()
            if current is not None and current != stale_jwt:
                return current
            if self._refresh_auth_failed_for == credentials:
                raise ConfigEntryAuthFailed("Daedalus session credentials need reauthentication")
            if not session_token:
                self._refresh_auth_failed_for = credentials
                raise ConfigEntryAuthFailed("Daedalus session token missing, reauth required")
            pool = self.entry.data.get(CONF_POOL, GIGYA_POOL_EU)
            api_key = GIGYA_API_KEYS.get(pool, GIGYA_API_KEYS[GIGYA_POOL_EU])
            try:
                fresh_jwt = await self._api.refresh_jwt(
                    session_token=session_token,
                    api_key=api_key,
                    session_secret=session_secret,
                )
            except DaedalusAuthError as exc:
                self._refresh_auth_failed_for = credentials
                host = self.entry.data.get(CONF_HOST)
                raise ConfigEntryAuthFailed(
                    f"Daedalus session token rejected by Gigya: {sanitize_text(exc, host=host)}"
                ) from exc
            self.hass.config_entries.async_update_entry(self.entry, data={**self.entry.data, CONF_JWT: fresh_jwt})
            self._refresh_auth_failed_for = None
            return fresh_jwt

    async def _async_listen_lan(self, connection: DaedalusLanConnection) -> None:
        """Keep reading so aiohttp handles heartbeat and close frames."""
        try:
            while not connection.closed:
                try:
                    await connection.receive(timeout=None)
                except LanProtocolError:
                    continue
        except DaedalusConnectionError:
            pass  # The normal refresh reconnects without sending any commands.
        finally:
            if connection is self._lan:
                self._lan = None
                self._publish_snapshot()
            with contextlib.suppress(DaedalusConnectionError):
                await connection.close()

    async def _stop_lan_listener(self) -> None:
        connection, self._lan = self._lan, None
        task, self._lan_task = self._lan_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if connection is not None and not connection.closed:
            await connection.close()

    @callback
    def _cancel_cloud_expiry(self) -> None:
        if self._cloud_expiry is not None:
            self._cloud_expiry.cancel()
            self._cloud_expiry = None

    @callback
    def _expire_cloud(self) -> None:
        self._cloud_expiry = None
        self._clear_cloud()
        self._publish_snapshot()

    def _clear_cloud(self) -> None:
        self._cancel_cloud_expiry()
        self._cloud_data = None

    async def _async_cloud_fetch(self) -> None:
        """Fetch and publish the available shadows without reconnecting LAN."""
        credentials = self._credentials()
        try:
            stored_name = self.entry.data.get(CONF_MACHINE_NAME)
            async with asyncio.timeout(CLOUD_FETCH_TIMEOUT_SECONDS):
                jwt = self.entry.data[CONF_JWT]
                expires_at = jwt_expiry(jwt)
                if expires_at is not None and expires_at <= time.time():
                    jwt = await self._rotate_jwt(jwt)
                cloud_state = await fetch_cloud_state(
                    self._api.http_session,
                    serial=self.serial_number,
                    jwt=jwt,
                    machine_name=stored_name if stored_name and stored_name != self.serial_number else None,
                    check_device_status=True,
                )
            thing = cloud_state.machine_name
            cloud_data = {"machine_name": thing, "fetched_at": time.time()}
            for name, key in (("MachineStatus", "status_flat"), ("MachineCapabilities", "capabilities_flat")):
                document = cloud_state.shadows.get(name)
                state = document.get("state") if isinstance(document, dict) else None
                if isinstance(state, dict) and isinstance(state.get("reported"), dict):
                    try:
                        flat = flatten_shadow(document)
                    except (AttributeError, TypeError, ValueError, RecursionError):
                        _LOGGER.debug("Daedalus cloud: invalid %s shadow", name)
                    else:
                        if flat:
                            cloud_data[key] = flat
            if "status_flat" not in cloud_data:
                status_error = cloud_state.status_error or "MachineStatus shadow has no usable reported data"
                if "capabilities_flat" not in cloud_data:
                    raise CloudTelemetryError(status_error)
                cloud_data["status_error"] = status_error
                _LOGGER.debug("Daedalus cloud status unavailable: %s", status_error)
        except ConfigEntryAuthFailed as exc:
            self._cloud_auth_failed_for = credentials
            self._request_reauth()
            self._record_cloud_failure(sanitize_text(exc, host=self.entry.data.get(CONF_HOST)))
            return
        except (CloudTelemetryError, DaedalusConnectionError) as exc:
            self._record_cloud_failure(sanitize_text(exc, host=self.entry.data.get(CONF_HOST)))
            return
        except TimeoutError:
            self._record_cloud_failure(f"cloud hop timed out after {CLOUD_FETCH_TIMEOUT_SECONDS}s")
            return
        except Exception as exc:  # noqa: BLE001 — cloud must never take the entry down
            self._record_cloud_failure(
                sanitize_text(
                    f"unexpected cloud error: {exc or type(exc).__name__}", host=self.entry.data.get(CONF_HOST)
                )
            )
            return
        self._cloud_error = None
        self._cloud_error_at = None
        if thing != self.entry.data.get(CONF_MACHINE_NAME):
            self.hass.config_entries.async_update_entry(self.entry, data={**self.entry.data, CONF_MACHINE_NAME: thing})
        self._cloud_data = cloud_data
        self._cloud_fetched_at = time.monotonic()
        self._cancel_cloud_expiry()
        self._cloud_expiry = asyncio.get_running_loop().call_later(_CLOUD_MAX_AGE, self._expire_cloud)
        self._publish_snapshot()

    def _record_cloud_failure(self, error: str) -> None:
        self._cloud_error = error
        self._cloud_error_at = time.time()
        self._cloud_retry_at = time.monotonic() + DEFAULT_UPDATE_INTERVAL_SECONDS
        self._clear_cloud()
        _LOGGER.debug("Daedalus cloud telemetry failed: %s", error)
        self._publish_snapshot()

    async def async_shutdown(self) -> None:
        self._stopping = True
        await super().async_shutdown()
        if self._cloud_task is not None:
            self._cloud_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._cloud_task
        self._cloud_task = None
        self._cancel_cloud_expiry()
        async with self._lock:
            await self._stop_lan_listener()
        await self._api.close()
