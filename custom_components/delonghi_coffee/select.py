"""Select platform for De'Longhi Coffee — profile selection."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import DeLonghiCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up select entities."""
    data: dict[str, Any] = hass.data[DOMAIN][entry.entry_id]
    coordinator: DeLonghiCoordinator = data["coordinator"]
    dsn: str = data["dsn"]
    model: str = data["model"]
    device_name: str = data["device_name"]
    sw_version: str | None = data.get("sw_version")

    async_add_entities([
        DeLonghiProfileSelect(coordinator, dsn, model, device_name, sw_version)
    ])


class DeLonghiProfileSelect(CoordinatorEntity[DeLonghiCoordinator], SelectEntity):
    """Select entity for choosing the active user profile."""

    def __init__(
        self,
        coordinator: DeLonghiCoordinator,
        dsn: str,
        model: str,
        device_name: str,
        sw_version: str | None,
    ) -> None:
        super().__init__(coordinator)
        self._dsn = dsn
        self._attr_unique_id = f"{dsn}_profile_select"
        self._attr_has_entity_name = True
        self._attr_translation_key = "profile_select"
        self._attr_icon = "mdi:account-circle"
        self._attr_device_info: dict[str, Any] = {
            "identifiers": {(DOMAIN, dsn)},
            "name": device_name,
            "manufacturer": "De'Longhi",
            "model": model,
        }
        if sw_version:
            self._attr_device_info["sw_version"] = sw_version

    @property
    def options(self) -> list[str]:
        """Return available profile options."""
        profiles = self.coordinator.data.get("profiles", {})
        if not profiles:
            return ["Profile 1", "Profile 2", "Profile 3", "Profile 4"]
        return [
            profiles.get(i, {}).get("name", f"Profile {i}")
            for i in sorted(profiles.keys())
        ]

    @property
    def current_option(self) -> str | None:
        """Return the currently selected profile."""
        active = self.coordinator.selected_profile
        profiles = self.coordinator.data.get("profiles", {})
        return profiles.get(active, {}).get("name", f"Profile {active}")

    async def async_select_option(self, option: str) -> None:
        """Handle profile selection."""
        profiles = self.coordinator.data.get("profiles", {})
        for pid, pdata in profiles.items():
            if pdata.get("name") == option:
                self.coordinator.selected_profile = pid
                _LOGGER.info("Profile switched to %d (%s)", pid, option)
                self.async_write_ha_state()
                return

        # Fallback: try to parse "Profile N"
        for i in range(1, 5):
            if option == f"Profile {i}":
                self.coordinator.selected_profile = i
                _LOGGER.info("Profile switched to %d", i)
                self.async_write_ha_state()
                return
