"""Button platform for De'Longhi Coffee — one button per beverage."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import DeLonghiApi, DeLonghiApiError, DeLonghiAuthError
from .const import BEVERAGES, DOMAIN
from .coordinator import DeLonghiCoordinator
from .sensor import _device_info

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up button entities."""
    data: dict[str, Any] = hass.data[DOMAIN][entry.entry_id]
    api: DeLonghiApi = data["api"]
    coordinator: DeLonghiCoordinator = data["coordinator"]
    dsn: str = data["dsn"]
    model: str = data["model"]
    device_name: str = data["device_name"]
    sw_version: str | None = data.get("sw_version")

    known_keys: set[str] = set()

    def _add_buttons(beverage_keys: list[str]) -> None:
        """Create button entities for newly discovered beverages."""
        new_entities: list[ButtonEntity] = []
        for bev_key in beverage_keys:
            if bev_key in known_keys:
                continue
            known_keys.add(bev_key)
            # Check for custom recipe name first
            custom_name = coordinator.custom_recipe_names.get(bev_key)
            if custom_name:
                meta = {"name": custom_name, "icon": "mdi:coffee-to-go"}
            else:
                meta = BEVERAGES.get(
                    bev_key,
                    {
                        "name": bev_key.replace("_", " ").title(),
                        "icon": "mdi:coffee",
                    },
                )
            new_entities.append(
                DeLonghiBrewButton(api, coordinator, dsn, model, device_name, sw_version, bev_key, meta)
            )
        if new_entities:
            _LOGGER.info("Adding %d brew buttons", len(new_entities))
            async_add_entities(new_entities)

    # Add static control buttons
    async_add_entities(
        [
            DeLonghiCancelButton(api, coordinator, dsn, model, device_name, sw_version),
            DeLonghiSyncButton(api, coordinator, dsn, model, device_name, sw_version),
        ]
    )

    # Add buttons for currently known beverages
    _add_buttons(coordinator.beverages)

    # If no beverages found yet, listen for coordinator updates
    # and add buttons when they're discovered on the next full refresh
    if not coordinator.beverages:
        _LOGGER.warning("No beverages discovered yet — will retry on next refresh")

        def _on_update() -> None:
            if coordinator.beverages:
                _add_buttons(coordinator.beverages)

        coordinator.async_add_listener(_on_update)


class DeLonghiBrewButton(CoordinatorEntity[DeLonghiCoordinator], ButtonEntity):
    """Button to brew a specific beverage."""

    def __init__(
        self,
        api: DeLonghiApi,
        coordinator: DeLonghiCoordinator,
        dsn: str,
        model: str,
        device_name: str,
        sw_version: str | None,
        beverage_key: str,
        meta: dict[str, str],
    ) -> None:
        super().__init__(coordinator)
        self._api = api
        self._dsn = dsn
        self._beverage_key = beverage_key
        self._attr_unique_id = f"{dsn}_brew_{beverage_key}"
        self._attr_has_entity_name = True
        # Use translation_key for beverages with known translations,
        # otherwise use the name directly (custom recipes, unknown beverages)
        if beverage_key in BEVERAGES:
            self._attr_translation_key = f"brew_{beverage_key}"
        else:
            self._attr_name = meta["name"]
        self._attr_icon = meta["icon"]
        self._attr_device_info = _device_info(dsn, model, device_name, sw_version)

    async def async_press(self) -> None:
        """Brew the beverage using the selected profile's recipe."""
        profile = self.coordinator.selected_profile or 1
        _LOGGER.info("Brewing %s on %s (profile %d)", self._beverage_key, self._dsn, profile)
        try:
            await self.hass.async_add_executor_job(self._api.brew_beverage, self._dsn, self._beverage_key, profile)
        except (DeLonghiApiError, DeLonghiAuthError) as err:
            raise HomeAssistantError(f"Failed to brew {self._beverage_key}: {err}") from err


class DeLonghiCancelButton(CoordinatorEntity[DeLonghiCoordinator], ButtonEntity):
    """Button to cancel the current brew operation."""

    def __init__(
        self,
        api: DeLonghiApi,
        coordinator: DeLonghiCoordinator,
        dsn: str,
        model: str,
        device_name: str,
        sw_version: str | None,
    ) -> None:
        super().__init__(coordinator)
        self._api = api
        self._dsn = dsn
        self._attr_unique_id = f"{dsn}_cancel_brew"
        self._attr_has_entity_name = True
        self._attr_translation_key = "cancel_brew"
        self._attr_icon = "mdi:stop-circle-outline"
        self._attr_device_info = _device_info(dsn, model, device_name, sw_version)

    @property
    def available(self) -> bool:
        """Only available when machine is actively brewing."""
        state = self.coordinator.data.get("machine_state", "Unknown")
        return state in ("Brewing", "Grinding", "Milk Frothing", "Dispensing")

    async def async_press(self) -> None:
        """Cancel current operation."""
        try:
            await self.hass.async_add_executor_job(self._api.cancel_brew, self._dsn)
        except (DeLonghiApiError, DeLonghiAuthError) as err:
            raise HomeAssistantError(f"Failed to cancel: {err}") from err


class DeLonghiSyncButton(CoordinatorEntity[DeLonghiCoordinator], ButtonEntity):
    """Button to force machine to sync its recipes to the cloud."""

    def __init__(
        self,
        api: DeLonghiApi,
        coordinator: DeLonghiCoordinator,
        dsn: str,
        model: str,
        device_name: str,
        sw_version: str | None,
    ) -> None:
        super().__init__(coordinator)
        self._api = api
        self._dsn = dsn
        self._attr_unique_id = f"{dsn}_sync_recipes"
        self._attr_has_entity_name = True
        self._attr_translation_key = "sync_recipes"
        self._attr_icon = "mdi:cloud-sync-outline"
        self._attr_device_info = _device_info(dsn, model, device_name, sw_version)

    async def async_press(self) -> None:
        """Force sync recipes for the selected profile."""
        profile = self.coordinator.selected_profile or 1
        try:
            await self.hass.async_add_executor_job(self._api.sync_recipes, self._dsn, profile)
        except (DeLonghiApiError, DeLonghiAuthError) as err:
            raise HomeAssistantError(f"Failed to sync recipes: {err}") from err
