"""Home Assistant action for one explicitly selected, opted-in device."""

from __future__ import annotations

import asyncio
import time

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import ATTR_DEVICE_ID
from homeassistant.core import Context, HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry
from homeassistant.helpers.service import async_register_admin_service

from .api import DaedalusApi, DaedalusError
from .cloud import _directory_presence
from .const import (
    CONF_AROMATIC_WARMUP,
    CONF_AROMATIC_WARMUP_PENDING,
    CONF_CLOUD_TELEMETRY,
    CONF_POOL,
    CONF_SERIAL_NUMBER,
    CONF_SESSION_SECRET,
    CONF_SESSION_TOKEN,
    DOMAIN,
    GIGYA_POOL_EU,
)
from .warmup import (
    COMMAND_EXPIRY_SECONDS,
    WarmupClient,
    WarmupError,
    fetch_owned_device,
    resolve_owned_aromatic,
    status_is_recent,
)
from .warmup_state import entry_lock, is_pending, save_pending

ATTR_CONFIG_ENTRY_ID = "config_entry_id"
SERVICE_WARM_UP = "warm_up"
_OBSERVATION_WINDOW_SECONDS = 240


def _idle_event(device_id: str) -> str:
    return f"{DOMAIN}_warmup_idle_{device_id}"


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


def _notification_device(hass: HomeAssistant, entry: ConfigEntry) -> str | None:
    if not (
        entry.data.get(CONF_CLOUD_TELEMETRY)
        and entry.state is ConfigEntryState.LOADED
        and entry.disabled_by is None
        and warmup_enabled(entry)
        and hass.is_running
    ):
        return None
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coordinator is None:
        return None
    registry = device_registry.async_get(hass)
    device = registry.async_get_device(identifiers=coordinator.device.info()["identifiers"])
    if (
        device is not None
        and device.disabled_by is None
        and entry.entry_id in device.config_entries
        and hass.bus.async_listeners().get(_idle_event(device.id))
    ):
        return device.id
    return None


async def _observe_idle(
    hass: HomeAssistant,
    entry: ConfigEntry,
    *,
    serial: str,
    jwt: str,
    thing: str,
    device_id: str,
    context: Context,
    sent_at: int,
    standby_version: int,
    deadline: float,
) -> None:
    api = DaedalusApi()
    try:
        async with asyncio.timeout_at(deadline):
            client = WarmupClient(api.http_session, thing_name=thing, jwt=jwt)
            idle_at = await client.observe_idle(sent_at=sent_at, standby_version=standby_version)
            if idle_at is None or _notification_device(hass, entry) != device_id:
                return
            record = await fetch_owned_device(api.http_session, serial=serial, jwt=jwt)
            if (
                record["machineName"] == thing
                and _directory_presence(record) in ("ONLINE", None)
                and status_is_recent(idle_at)
                and _notification_device(hass, entry) == device_id
            ):
                hass.bus.async_fire(_idle_event(device_id), {ATTR_DEVICE_ID: device_id}, context=context)
    except (DaedalusError, WarmupError, OSError, TimeoutError):
        pass  # Lost or unavailable telemetry leaves the attempt unannounced.
    finally:
        await api.close()


def register_warmup_action(hass: HomeAssistant) -> None:
    """Register one action with no implicit machine or group target."""
    running: set[str] = set()
    # The pending marker survives restart while delivery is uncertain. After a
    # successful reply and marker clear, restarting drops any remaining cooldown.
    cooldown: dict[str, float] = {}
    observations: dict[str, asyncio.Task[None]] = {}

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

        sent_at = None
        observation_deadline = 0.0
        device_id = None

        async def mark_pending() -> None:
            nonlocal sent_at, observation_deadline, device_id
            require_loaded_target()
            await save_pending(hass, entry_id, True)
            require_loaded_target()
            previous = observations.pop(serial_key, None)
            if previous is not None:
                previous.cancel()
            device_id = _notification_device(hass, entry)
            sent_at = int(time.time())
            observation_deadline = asyncio.get_running_loop().time() + _OBSERVATION_WINDOW_SECONDS
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
                    standby_version = await client.warm_up(before_send=mark_pending)
                    await save_pending(hass, entry_id, False)
                finally:
                    await api.close()

                if (
                    sent_at is not None
                    and standby_version is not None
                    and device_id is not None
                    and _notification_device(hass, entry) == device_id
                ):
                    observation = entry.async_create_background_task(
                        hass,
                        _observe_idle(
                            hass,
                            entry,
                            serial=serial,
                            jwt=jwt,
                            thing=thing,
                            device_id=device_id,
                            context=call.context,
                            sent_at=sent_at,
                            standby_version=standby_version,
                            deadline=observation_deadline,
                        ),
                        f"{DOMAIN} observe warm-up status",
                    )
                    observations[serial_key] = observation

                    def clear_observation(task: asyncio.Task[None]) -> None:
                        if observations.get(serial_key) is task:
                            del observations[serial_key]

                    observation.add_done_callback(clear_observation)
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
