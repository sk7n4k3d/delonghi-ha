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

from .api import DeLonghiApi, DeLonghiApiError
from .const import BEVERAGES, DOMAIN, POWER_ON_CMD
from .coordinator import DeLonghiCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up button entities."""
    data: dict[str, Any] = hass.data[DOMAIN][entry.entry_id]
    api: DeLonghiApi = data["api"]
    coordinator: DeLonghiCoordinator = data["coordinator"]
    dsn: str = data["dsn"]
    model: str = data["model"]
    device_name: str = data["device_name"]
    sw_version: str | None = data.get("sw_version")

    # Get available beverages from device
    if not coordinator.beverages:
        coordinator.beverages = await hass.async_add_executor_job(
            api.get_available_beverages, dsn
        )

    entities: list[ButtonEntity] = []

    # Power on button
    entities.append(
        DeLonghiPowerOnButton(api, coordinator, dsn, model, device_name, sw_version)
    )

    for bev_key in coordinator.beverages:
        meta = BEVERAGES.get(bev_key, {
            "name": bev_key.replace("_", " ").title(),
            "icon": "mdi:coffee",
        })
        entities.append(
            DeLonghiBrewButton(api, coordinator, dsn, model, device_name, sw_version, bev_key, meta)
        )

    async_add_entities(entities)


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
        self._attr_name = f"Brew {meta['name']}"
        self._attr_icon = meta["icon"]
        self._attr_device_info: dict[str, Any] = {
            "identifiers": {(DOMAIN, dsn)},
            "name": device_name,
            "manufacturer": "De'Longhi",
            "model": model,
        }
        if sw_version:
            self._attr_device_info["sw_version"] = sw_version

    async def async_press(self) -> None:
        """Brew the beverage."""
        _LOGGER.info("Brewing %s on %s", self._beverage_key, self._dsn)
        try:
            success = await self.hass.async_add_executor_job(
                self._api.brew_beverage, self._dsn, self._beverage_key
            )
            if not success:
                raise HomeAssistantError(
                    f"Failed to brew {self._beverage_key}: command was not accepted"
                )
        except DeLonghiApiError as err:
            raise HomeAssistantError(
                f"Failed to brew {self._beverage_key}: {err}"
            ) from err


class DeLonghiPowerOnButton(CoordinatorEntity[DeLonghiCoordinator], ButtonEntity):
    """Button to power on / wake up the coffee machine."""

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
        self._attr_unique_id = f"{dsn}_power_on"
        self._attr_name = "Power On"
        self._attr_icon = "mdi:power"
        self._attr_device_info: dict[str, Any] = {
            "identifiers": {(DOMAIN, dsn)},
            "name": device_name,
            "manufacturer": "De'Longhi",
            "model": model,
        }
        if sw_version:
            self._attr_device_info["sw_version"] = sw_version

    async def async_press(self) -> None:
        """Wake up the coffee machine."""
        _LOGGER.info("Sending power on to %s", self._dsn)
        try:
            success = await self.hass.async_add_executor_job(
                self._api.send_command, self._dsn, POWER_ON_CMD
            )
            if not success:
                raise HomeAssistantError("Failed to power on: command was not accepted")
        except DeLonghiApiError as err:
            raise HomeAssistantError(f"Failed to power on: {err}") from err
