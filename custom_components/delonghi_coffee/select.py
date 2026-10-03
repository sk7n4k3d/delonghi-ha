"""Select platform for De'Longhi Coffee — profile selection."""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import DeLonghiCoordinator
from .sensor import _device_info

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up select entities."""
    data: dict[str, Any] = hass.data[DOMAIN][entry.entry_id]
    coordinator: DeLonghiCoordinator = data["coordinator"]
    dsn: str = data["dsn"]
    model: str = data["model"]
    device_name: str = data["device_name"]
    sw_version: str | None = data.get("sw_version")

    async_add_entities([DeLonghiProfileSelect(coordinator, dsn, model, device_name, sw_version)])


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
        self._attr_device_info = _device_info(dsn, model, device_name, sw_version)

    @property
    def options(self) -> list[str]:
        """Return available profile options."""
        profiles = self.coordinator.data.get("profiles", {})
        if not profiles:
            return ["Profile 1", "Profile 2", "Profile 3", "Profile 4"]
        return [profiles.get(i, {}).get("name", f"Profile {i}") for i in sorted(profiles.keys())]

    @property
    def current_option(self) -> str | None:
        """Return the currently selected profile.

        Authoritative ordering:
          1. ``coordinator.selected_profile`` — explicit user click on this
             dropdown overrides everything until next refresh.
          2. ``data["active_profile"]`` — cloud property (10min refresh),
             reflects what the *machine UI* currently shows as active.
          3. ``data["profile"]`` — monitor byte (60s polling), can lag and
             report the *last brewed* profile. Last-resort fallback only.
        """
        profiles = self.coordinator.data.get("profiles", {})

        active = self.coordinator.selected_profile
        if active is None:
            cloud = self.coordinator.data.get("active_profile")
            if isinstance(cloud, int) and cloud > 0:
                active = cloud
            else:
                monitor = self.coordinator.data.get("profile")
                if isinstance(monitor, int) and monitor > 0:
                    active = monitor

        if active is None:
            return None
        return profiles.get(active, {}).get("name", f"Profile {active}")

    async def async_select_option(self, option: str) -> None:
        """Handle profile selection.

        Sends the ECAM 0xA9 ProfileSelection command to the machine (issue
        #36: previously this only updated local state, so the machine never
        changed its active profile). On success, we schedule a fast poll so
        the monitor byte confirms (or reverts) the new profile quickly.
        """
        profiles = self.coordinator.data.get("profiles", {})
        pid: int | None = None
        for p, pdata in profiles.items():
            if pdata.get("name") == option:
                pid = p
                break
        if pid is None:
            # Fallback: parse "Profile N"
            for i in range(1, 5):
                if option == f"Profile {i}":
                    pid = i
                    break
        if pid is None:
            _LOGGER.warning("Profile selection failed: '%s' not found in profiles", option)
            return

        ok = await self.hass.async_add_executor_job(self.coordinator.api.set_active_profile, self.coordinator.dsn, pid)
        if not ok:
            _LOGGER.warning("Profile selection failed: machine did not accept profile %d", pid)
            return
        self.coordinator.selected_profile = pid
        _LOGGER.info("Profile switched to %d (%s)", pid, option)
        # Reactive refresh: pick up the machine's new active profile
        # immediately instead of waiting for the next 60s poll.
        with contextlib.suppress(AttributeError):
            await self.coordinator.async_refresh_after_command(duration_s=90.0, interval_s=5.0)
        self.async_write_ha_state()
