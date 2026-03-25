"""Switch platform for De'Longhi Coffee — power on/off toggle."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import DeLonghiApi, DeLonghiApiError
from .const import DOMAIN, POWER_OFF_CMD, POWER_ON_CMD
from .coordinator import DeLonghiCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up switch entity."""
    data: dict[str, Any] = hass.data[DOMAIN][entry.entry_id]
    api: DeLonghiApi = data["api"]
    coordinator: DeLonghiCoordinator = data["coordinator"]
    dsn: str = data["dsn"]
    model: str = data["model"]
    device_name: str = data["device_name"]
    sw_version: str | None = data.get("sw_version")

    async_add_entities([
        DeLonghiPowerSwitch(api, coordinator, dsn, model, device_name, sw_version)
    ])


class DeLonghiPowerSwitch(CoordinatorEntity[DeLonghiCoordinator], SwitchEntity):
    """Switch to power on/off the coffee machine."""

    _attr_device_class = SwitchDeviceClass.SWITCH

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
        self._assumed_on = True  # Assume on at startup (safe default)
        self._attr_unique_id = f"{dsn}_power"
        self._attr_has_entity_name = True
        self._attr_translation_key = "power"
        self._attr_icon = "mdi:coffee-maker"
        self._attr_device_info: dict[str, Any] = {
            "identifiers": {(DOMAIN, dsn)},
            "name": device_name,
            "manufacturer": "De'Longhi",
            "model": model,
        }
        if sw_version:
            self._attr_device_info["sw_version"] = sw_version

    @property
    def assumed_state(self) -> bool:
        """Return True if state is assumed (no monitor available)."""
        return self.coordinator.data.get("machine_state", "Unknown") == "Unknown"

    @property
    def is_on(self) -> bool:
        """Return True if machine is on.

        Uses monitor state if available, falls back to local tracking
        for models without monitor properties (PrimaDonna Soul).
        """
        state = self.coordinator.data.get("machine_state", "Unknown")
        if state != "Unknown":
            result = state not in ("Off", "Going to sleep")
            _LOGGER.debug("Switch is_on: state=%s → %s", state, result)
            return result
        _LOGGER.debug("Switch is_on: no monitor, assumed=%s", self._assumed_on)
        return self._assumed_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Power on the machine."""
        _LOGGER.info("Powering on %s", self._dsn)
        try:
            success = await self.hass.async_add_executor_job(
                self._api.send_command, self._dsn, POWER_ON_CMD
            )
            if not success:
                raise HomeAssistantError("Failed to power on")
            self._assumed_on = True
        except DeLonghiApiError as err:
            raise HomeAssistantError(f"Failed to power on: {err}") from err
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Power off the machine (standby)."""
        _LOGGER.info("Powering off %s", self._dsn)
        try:
            success = await self.hass.async_add_executor_job(
                self._api.send_command, self._dsn, POWER_OFF_CMD
            )
            if not success:
                raise HomeAssistantError("Failed to power off")
            self._assumed_on = False
        except DeLonghiApiError as err:
            raise HomeAssistantError(f"Failed to power off: {err}") from err
        await self.coordinator.async_request_refresh()
