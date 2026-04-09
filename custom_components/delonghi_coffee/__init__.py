"""De'Longhi Coffee integration for Home Assistant."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

from .api import DeLonghiApi, DeLonghiApiError, DeLonghiAuthError
from .const import DOMAIN, MODEL_NAMES
from .coordinator import DeLonghiCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.BUTTON, Platform.SWITCH, Platform.SELECT]


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate config entry to a new version."""
    _LOGGER.debug("Migrating from version %s.%s", entry.version, entry.minor_version)

    if entry.version == 1:
        # v1 → v2: add region field (default EU for existing installs)
        new_data = {**entry.data, "region": "EU"}
        hass.config_entries.async_update_entry(entry, data=new_data, version=2)
        _LOGGER.info("Migrated config entry to version 2 (added region=EU)")

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up De'Longhi Coffee from a config entry."""
    api = DeLonghiApi(
        entry.data[CONF_EMAIL],
        entry.data[CONF_PASSWORD],
        region=entry.data.get("region", "EU"),
        oem_model=entry.data.get("model", ""),
    )

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
    coordinator.diagnostic_mode = entry.options.get("diagnostic_mode", False)
    await coordinator.async_config_entry_first_refresh()

    # Resolve friendly model name from OEM model
    oem_model = entry.data.get("model", "unknown")
    friendly_model = MODEL_NAMES.get(oem_model, oem_model)
    if not device_name or device_name == dsn:
        device_name = f"De'Longhi {friendly_model}"

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "api": api,
        "coordinator": coordinator,
        "dsn": dsn,
        "model": friendly_model,
        "device_name": device_name,
        "sw_version": sw_version,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register custom brew service
    async def handle_brew_custom(call) -> None:  # noqa: ANN001
        """Handle the brew_custom service call."""
        from homeassistant.exceptions import HomeAssistantError as HAError

        beverage = call.data["beverage"]
        coffee_qty = call.data.get("coffee_qty")
        milk_qty = call.data.get("milk_qty")
        water_qty = call.data.get("water_qty")
        taste = call.data.get("taste", 3)
        milk_froth = call.data.get("milk_froth", 2)
        temperature = call.data.get("temperature", 1)
        profile = call.data.get("profile", coordinator.selected_profile or 1)

        try:
            await hass.async_add_executor_job(
                api.brew_custom,
                dsn,
                beverage,
                coffee_qty,
                milk_qty,
                water_qty,
                taste,
                milk_froth,
                temperature,
                profile,
            )
        except (DeLonghiApiError, DeLonghiAuthError) as err:
            raise HAError(str(err)) from err

    hass.services.async_register(DOMAIN, "brew_custom", handle_brew_custom)

    async def handle_cancel_brew(call) -> None:  # noqa: ANN001
        """Handle the cancel_brew service call."""
        from homeassistant.exceptions import HomeAssistantError as HAError

        try:
            await hass.async_add_executor_job(api.cancel_brew, dsn)
        except (DeLonghiApiError, DeLonghiAuthError) as err:
            raise HAError(str(err)) from err

    hass.services.async_register(DOMAIN, "cancel_brew", handle_cancel_brew)

    async def handle_sync_recipes(call) -> None:  # noqa: ANN001
        """Handle the sync_recipes service call."""
        from homeassistant.exceptions import HomeAssistantError as HAError

        profile = call.data.get("profile", coordinator.selected_profile or 1)
        try:
            await hass.async_add_executor_job(api.sync_recipes, dsn, profile)
        except (DeLonghiApiError, DeLonghiAuthError) as err:
            raise HAError(str(err)) from err

    hass.services.async_register(DOMAIN, "sync_recipes", handle_sync_recipes)

    async def handle_select_bean_profile(call) -> None:  # noqa: ANN001
        """Handle the select_bean_profile service call (ECAM 0xB9)."""
        from homeassistant.exceptions import HomeAssistantError as HAError

        slot = int(call.data["slot"])
        try:
            await hass.async_add_executor_job(api.select_bean_system, dsn, slot)
        except (DeLonghiApiError, DeLonghiAuthError) as err:
            raise HAError(str(err)) from err

    hass.services.async_register(DOMAIN, "select_bean_profile", handle_select_bean_profile)

    async def handle_write_bean_profile(call) -> None:  # noqa: ANN001
        """Handle the write_bean_profile service call (ECAM 0xBB)."""
        from homeassistant.exceptions import HomeAssistantError as HAError

        slot = int(call.data["slot"])
        name = str(call.data["name"])
        temperature = int(call.data.get("temperature", 0))
        intensity = int(call.data.get("intensity", 0))
        grinder = int(call.data.get("grinder", 0))
        flag1 = int(call.data.get("flag1", 0))
        flag2 = int(call.data.get("flag2", 1))
        try:
            await hass.async_add_executor_job(
                api.write_bean_system,
                dsn,
                slot,
                name,
                temperature,
                intensity,
                grinder,
                flag1,
                flag2,
            )
        except (DeLonghiApiError, DeLonghiAuthError) as err:
            raise HAError(str(err)) from err

    hass.services.async_register(DOMAIN, "write_bean_profile", handle_write_bean_profile)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)
        # Remove service if no more entries
        if not hass.data.get(DOMAIN):
            hass.services.async_remove(DOMAIN, "brew_custom")
            hass.services.async_remove(DOMAIN, "cancel_brew")
            hass.services.async_remove(DOMAIN, "sync_recipes")
            hass.services.async_remove(DOMAIN, "select_bean_profile")
            hass.services.async_remove(DOMAIN, "write_bean_profile")
    return unload_ok
