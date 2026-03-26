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
        self._last_commanded_on: bool | None = None  # What we last commanded
        self._monitor_stale_count: int = 0  # How many polls returned same value after command
        self._last_monitor_state: str | None = None  # Previous poll's monitor value
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
        """Return True if state is assumed (no monitor available or stale)."""
        state = self.coordinator.data.get("machine_state", "Unknown")
        if state == "Unknown":
            return True
        # If we detected persistent staleness, we're in assumed mode
        if self._monitor_stale_count >= 2 and self._last_commanded_on is not None:
            return True
        return False

    @property
    def is_on(self) -> bool:
        """Return True if machine is on.

        Uses monitor state if available and responsive, falls back to local
        tracking for models with permanently stale cloud monitor data.

        Staleness detection: after a power command, if the monitor keeps
        returning the same value (contradicting what we commanded), we
        count consecutive stale polls. After 2+ stale polls, we trust
        the assumed state indefinitely until the monitor actually changes.
        """
        state = self.coordinator.data.get("machine_state", "Unknown")
        cloud_status = self.coordinator.data.get("status", "UNKNOWN")

        # No monitor data at all → use assumed state
        if state == "Unknown":
            _LOGGER.debug("Switch is_on: no monitor, assumed=%s", self._assumed_on)
            return self._assumed_on

        # Detect stale monitor: after a power command, check if monitor
        # reflects the change or stays frozen
        if self._last_commanded_on is not None:
            monitor_says_on = state not in ("Off", "Going to sleep")

            if monitor_says_on != self._last_commanded_on:
                # Monitor contradicts our command and hasn't changed
                if state == self._last_monitor_state:
                    self._monitor_stale_count += 1
                    _LOGGER.debug(
                        "Switch: monitor stuck on '%s' after %s command (%d polls)",
                        state,
                        "ON" if self._last_commanded_on else "OFF",
                        self._monitor_stale_count,
                    )
                else:
                    # Monitor changed to something else but still contradicts
                    self._monitor_stale_count = 1
            else:
                # Monitor agrees with our command → it's responsive
                self._monitor_stale_count = 0
                self._last_commanded_on = None
                _LOGGER.debug("Switch: monitor confirmed %s state", state)

        self._last_monitor_state = state

        # If monitor is persistently stale (2+ polls unchanged after command),
        # trust assumed state instead
        if self._monitor_stale_count >= 2 and self._last_commanded_on is not None:
            _LOGGER.debug(
                "Switch is_on: monitor stale ('%s' x%d), using assumed=%s (cloud=%s)",
                state,
                self._monitor_stale_count,
                self._assumed_on,
                cloud_status,
            )
            return self._assumed_on

        # Monitor is responsive → trust it
        result = state not in ("Off", "Going to sleep")
        self._assumed_on = result
        _LOGGER.debug("Switch is_on: monitor=%s → %s", state, result)
        return result

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
            self._last_commanded_on = True
            self._monitor_stale_count = 0
        except DeLonghiApiError as err:
            raise HomeAssistantError(f"Failed to power on: {err}") from err
        self.async_write_ha_state()
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
            self._last_commanded_on = False
            self._monitor_stale_count = 0
        except DeLonghiApiError as err:
            raise HomeAssistantError(f"Failed to power off: {err}") from err
        self.async_write_ha_state()
        await self.coordinator.async_request_refresh()
