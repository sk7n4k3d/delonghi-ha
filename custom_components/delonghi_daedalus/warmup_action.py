"""Home Assistant action for one explicitly selected, opted-in device."""

from __future__ import annotations

import time

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.service import async_register_admin_service

from .api import DaedalusApi, DaedalusError
from .const import (
    CONF_AROMATIC_WARMUP,
    CONF_AROMATIC_WARMUP_PENDING,
    CONF_POOL,
    CONF_SERIAL_NUMBER,
    CONF_SESSION_SECRET,
    CONF_SESSION_TOKEN,
    DOMAIN,
    GIGYA_POOL_EU,
)
from .warmup import COMMAND_EXPIRY_SECONDS, WarmupClient, WarmupError, resolve_owned_aromatic
from .warmup_state import entry_lock, is_pending, save_pending

ATTR_CONFIG_ENTRY_ID = "config_entry_id"
SERVICE_WARM_UP = "warm_up"


def warmup_enabled(entry: ConfigEntry) -> bool:
    """Check whether this entry has opted into warm-up."""
    return entry.data.get(CONF_POOL) == GIGYA_POOL_EU and entry.data.get(CONF_AROMATIC_WARMUP) is True


def has_unique_warmup_serial(hass: HomeAssistant, serial: str) -> bool:
    """Require one configured warm-up target for this physical machine."""
    serial_key = serial.strip().casefold()
    return (
        sum(
            isinstance(other_serial := other.data.get(CONF_SERIAL_NUMBER), str)
            and other_serial.strip().casefold() == serial_key
            for other in hass.config_entries.async_entries(DOMAIN)
        )
        == 1
    )


def register_warmup_action(hass: HomeAssistant) -> None:
    """Register one action with no implicit machine or group target."""
    running: set[str] = set()
    cooldown: dict[str, float] = {}

    async def warm_up(call: ServiceCall) -> None:
        entry_id = call.data[ATTR_CONFIG_ENTRY_ID]
        entry = hass.config_entries.async_get_entry(entry_id)
        if (
            entry is None
            or entry.domain != DOMAIN
            or entry.disabled_by is not None
            or entry.state is not ConfigEntryState.LOADED
        ):
            raise HomeAssistantError("Select a loaded De'Longhi My Coffee Lounge configuration entry.")
        if not warmup_enabled(entry):
            raise HomeAssistantError("Enable ECAM630.70 warm-up on this EU configuration entry first.")
        if entry.data.get(CONF_AROMATIC_WARMUP_PENDING):
            raise HomeAssistantError(
                "Check the previous warm-up on the machine, then turn the option off and on again."
            )

        serial = entry.data.get(CONF_SERIAL_NUMBER)
        token = entry.data.get(CONF_SESSION_TOKEN)
        if not isinstance(serial, str) or not serial.strip() or not token:
            raise HomeAssistantError(
                "Warm-up requires a configured serial number and an authenticated EU owner session."
            )
        serial_key = serial.strip().casefold()
        if not has_unique_warmup_serial(hass, serial):
            raise HomeAssistantError("Warm-up requires a unique configuration entry for this serial number.")
        lock = entry_lock(hass, entry_id)
        if lock.locked() or serial_key in running:
            raise HomeAssistantError("A warm-up request for this machine is already in progress or was just sent.")

        def require_loaded_target() -> None:
            if (
                hass.config_entries.async_get_entry(entry_id) is not entry
                or entry.disabled_by is not None
                or entry.state is not ConfigEntryState.LOADED
                or not warmup_enabled(entry)
                or entry.data.get(CONF_AROMATIC_WARMUP_PENDING)
                or entry.data.get(CONF_SESSION_TOKEN) != token
                or entry.data.get(CONF_SERIAL_NUMBER) != serial
            ):
                raise WarmupError("The target entry changed while checking readiness. No warm-up command was sent.")

        async def mark_pending() -> None:
            require_loaded_target()
            await save_pending(hass, entry_id, True)
            require_loaded_target()
            cooldown[serial_key] = time.monotonic() + COMMAND_EXPIRY_SECONDS

        running.add(serial_key)
        try:
            async with lock:
                if await is_pending(hass, entry_id):
                    raise WarmupError(
                        "Check the previous warm-up on the machine, then turn the option off and on again."
                    )
                if time.monotonic() < cooldown.get(serial_key, 0.0):
                    raise WarmupError("A warm-up request for this machine was just sent.")
                require_loaded_target()
                api = DaedalusApi()
                try:
                    jwt = await api.refresh_jwt(session_token=token, session_secret=entry.data.get(CONF_SESSION_SECRET))
                    thing = await resolve_owned_aromatic(api.http_session, serial=serial, jwt=jwt)
                    client = WarmupClient(api.http_session, thing_name=thing, jwt=jwt)
                    await client.verify_firmware()
                    await client.warm_up(before_send=mark_pending)
                    await save_pending(hass, entry_id, False)
                finally:
                    await api.close()
        except WarmupError as exc:
            raise HomeAssistantError(str(exc)) from None
        except (DaedalusError, TimeoutError, OSError):
            raise HomeAssistantError("The EU owner session could not be refreshed.") from None
        finally:
            running.remove(serial_key)

    async_register_admin_service(
        hass,
        DOMAIN,
        SERVICE_WARM_UP,
        warm_up,
        schema=vol.Schema({vol.Required(ATTR_CONFIG_ENTRY_ID): vol.All(str, vol.Length(min=1))}),
    )
