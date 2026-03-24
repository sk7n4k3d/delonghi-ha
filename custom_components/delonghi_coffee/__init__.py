"""De'Longhi Coffee integration for Home Assistant."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

from .api import DeLonghiApi, DeLonghiApiError, DeLonghiAuthError
from .const import DOMAIN
from .coordinator import DeLonghiCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.BUTTON]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up De'Longhi Coffee from a config entry."""
    api = DeLonghiApi(entry.data[CONF_EMAIL], entry.data[CONF_PASSWORD])

    try:
        await hass.async_add_executor_job(api.authenticate)
    except DeLonghiAuthError as err:
        raise ConfigEntryAuthFailed(f"Invalid credentials: {err}") from err
    except DeLonghiApiError as err:
        raise ConfigEntryNotReady(f"Cannot connect: {err}") from err

    dsn: str = entry.data["dsn"]

    # Fetch device info for device registry
    device_name: str | None = entry.data.get("device_name")
    sw_version: str | None = entry.data.get("sw_version")

    # If not stored in config entry, fetch from API
    if not device_name or not sw_version:
        try:
            devices = await hass.async_add_executor_job(api.get_devices)
            if devices:
                device_name = device_name or api.device_name
                sw_version = sw_version or api.sw_version
        except (DeLonghiApiError, DeLonghiAuthError):
            _LOGGER.debug("Could not fetch device info, using defaults")

    coordinator = DeLonghiCoordinator(hass, api, dsn)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "api": api,
        "coordinator": coordinator,
        "dsn": dsn,
        "model": entry.data.get("model", "unknown"),
        "device_name": device_name or "De'Longhi Coffee Machine",
        "sw_version": sw_version,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok
