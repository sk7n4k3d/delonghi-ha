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

    _register_services(hass)

    return True


def _resolve_target(hass: HomeAssistant, call) -> tuple[DeLonghiApi, DeLonghiCoordinator, str]:  # noqa: ANN001
    """Pick the (api, coordinator, dsn) tuple for a service call.

    Honors ``config_entry_id`` in call data when present, otherwise falls back
    to the first registered entry. Multi-entry users without the field get a
    warning so the ambiguity doesn't go silent.
    """
    from homeassistant.exceptions import HomeAssistantError

    entries: dict = hass.data.get(DOMAIN, {})
    if not entries:
        raise HomeAssistantError("No De'Longhi machine configured")

    entry_id = None
    if hasattr(call, "data") and isinstance(call.data, dict):
        entry_id = call.data.get("config_entry_id")

    if entry_id and entry_id in entries:
        bundle = entries[entry_id]
    else:
        if len(entries) > 1 and not entry_id:
            _LOGGER.warning(
                "Multiple De'Longhi entries configured; service call defaulted to the first one. "
                "Pass config_entry_id in service data to target a specific machine."
            )
        bundle = next(iter(entries.values()))

    return bundle["api"], bundle["coordinator"], bundle["dsn"]


def _register_services(hass: HomeAssistant) -> None:
    """Register domain-wide services exactly once across all entries.

    Handlers look up the target entry at call time via ``_resolve_target``,
    so reload / multi-entry setups behave correctly without re-registering
    (which would keep stale closures from a previous ``async_setup_entry``).
    """
    if hass.services.has_service(DOMAIN, "brew_custom"):
        return

    from homeassistant.exceptions import HomeAssistantError as HAError

    async def handle_brew_custom(call) -> None:  # noqa: ANN001
        api, coord, dsn = _resolve_target(hass, call)
        beverage = call.data["beverage"]
        coffee_qty = call.data.get("coffee_qty")
        milk_qty = call.data.get("milk_qty")
        water_qty = call.data.get("water_qty")
        taste = call.data.get("taste", 3)
        milk_froth = call.data.get("milk_froth", 2)
        temperature = call.data.get("temperature", 1)
        profile = call.data.get("profile", coord.selected_profile or 1)

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

    async def handle_cancel_brew(call) -> None:  # noqa: ANN001
        api, _coord, dsn = _resolve_target(hass, call)
        try:
            await hass.async_add_executor_job(api.cancel_brew, dsn)
        except (DeLonghiApiError, DeLonghiAuthError) as err:
            raise HAError(str(err)) from err

    async def handle_sync_recipes(call) -> None:  # noqa: ANN001
        api, coord, dsn = _resolve_target(hass, call)
        profile = call.data.get("profile", coord.selected_profile or 1)
        try:
            await hass.async_add_executor_job(api.sync_recipes, dsn, profile)
        except (DeLonghiApiError, DeLonghiAuthError) as err:
            raise HAError(str(err)) from err

    async def handle_select_bean_profile(call) -> None:  # noqa: ANN001
        api, _coord, dsn = _resolve_target(hass, call)
        slot = int(call.data["slot"])
        try:
            await hass.async_add_executor_job(api.select_bean_system, dsn, slot)
        except (DeLonghiApiError, DeLonghiAuthError) as err:
            raise HAError(str(err)) from err

    async def handle_write_bean_profile(call) -> None:  # noqa: ANN001
        api, _coord, dsn = _resolve_target(hass, call)
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

    # Counter baselines (firmware-freeze workaround — see local_baseline.py).
    # Keys accepted on the screen → counter mapping. Values are entered in
    # the same unit the counter is exposed in HA (cups for cup counters,
    # litres for total_water_ml, raw ml for water_through_filter_ml).
    _SCREEN_FIELD_TO_COUNTER: dict[str, tuple[str, float]] = {
        # field_name → (counter_key, multiplier_to_storage_unit)
        # total_beverages and total_espressos are the two big "Tot. seulement café"
        # values shown on the touchscreen — Eletta exposes both, set whichever
        # matches your firmware's naming on the cloud side.
        "total_beverages": ("total_beverages", 1.0),
        "total_espressos": ("total_espressos", 1.0),
        "espresso": ("espresso", 1.0),
        "coffee": ("coffee", 1.0),
        "long_coffee": ("long_coffee", 1.0),
        "doppio": ("doppio", 1.0),
        "americano": ("americano", 1.0),
        "cappuccino": ("cappuccino", 1.0),
        "tea": ("tea", 1.0),
        "hot_water": ("hot_water", 1.0),
        "descale_count": ("descale_count", 1.0),
        "filter_replacements": ("filter_replacements", 1.0),
        "beverages_since_descale": ("beverages_since_descale", 1.0),
        "grounds_count": ("grounds_count", 1.0),
        # Litres on the UI → ml internally (the sensor applies scale=0.001 back).
        "total_water_liters": ("total_water_ml", 1000.0),
    }

    async def handle_set_baseline_from_screen(call) -> None:  # noqa: ANN001
        _api, coord, _dsn = _resolve_target(hass, call)
        await coord.local_baseline.async_load()
        updates: dict[str, int] = {}
        unknown: list[str] = []
        for field, raw in call.data.items():
            mapping = _SCREEN_FIELD_TO_COUNTER.get(field)
            if mapping is None:
                unknown.append(field)
                continue
            counter_key, mult = mapping
            try:
                updates[counter_key] = int(float(raw) * mult)
            except (TypeError, ValueError) as err:
                raise HAError(f"Invalid value for {field}: {raw!r}") from err
        if unknown:
            _LOGGER.warning("set_baseline_from_screen ignored unknown fields: %s", unknown)
        if not updates:
            raise HAError("No valid baseline fields provided")
        await coord.local_baseline.async_set_many(updates)
        # Trigger a coordinator refresh so sensors pick up the new values
        # immediately instead of waiting for the next 60s poll.
        await coord.async_request_refresh()

    async def handle_reset_local_baseline(call) -> None:  # noqa: ANN001
        _api, coord, _dsn = _resolve_target(hass, call)
        await coord.local_baseline.async_clear()
        await coord.async_request_refresh()

    hass.services.async_register(DOMAIN, "brew_custom", handle_brew_custom)
    hass.services.async_register(DOMAIN, "cancel_brew", handle_cancel_brew)
    hass.services.async_register(DOMAIN, "sync_recipes", handle_sync_recipes)
    hass.services.async_register(DOMAIN, "select_bean_profile", handle_select_bean_profile)
    hass.services.async_register(DOMAIN, "write_bean_profile", handle_write_bean_profile)
    hass.services.async_register(DOMAIN, "set_baseline_from_screen", handle_set_baseline_from_screen)
    hass.services.async_register(DOMAIN, "reset_local_baseline", handle_reset_local_baseline)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    # Stop LAN server before cleaning up
    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    coordinator = entry_data.get("coordinator")
    if coordinator is not None:
        await coordinator.async_stop_lan()

    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)
        # Remove service if no more entries
        if not hass.data.get(DOMAIN):
            hass.services.async_remove(DOMAIN, "brew_custom")
            hass.services.async_remove(DOMAIN, "cancel_brew")
            hass.services.async_remove(DOMAIN, "sync_recipes")
            hass.services.async_remove(DOMAIN, "select_bean_profile")
            hass.services.async_remove(DOMAIN, "write_bean_profile")
            hass.services.async_remove(DOMAIN, "set_baseline_from_screen")
            hass.services.async_remove(DOMAIN, "reset_local_baseline")
    return unload_ok
