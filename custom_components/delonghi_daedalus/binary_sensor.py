"""Binary sensors: LAN connectivity + mapped cloud alarms/peripherals."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .cloud_keys import KIND_ALARM, CloudEntityDiscovery, CloudKey, coerce_bool
from .const import CONF_CLOUD_TELEMETRY, DOMAIN, WATER_FILTER_INSTALLED
from .coordinator import DaedalusCoordinator
from .entity import DaedalusCloudEntity, DaedalusEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: DaedalusCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([DaedalusConnectedSensor(coordinator)])
    if not entry.data.get(CONF_CLOUD_TELEMETRY):
        return
    discovery = CloudEntityDiscovery(want_binary=True)
    added_filter_installation = False

    def _add_cloud_binary_sensors() -> None:
        nonlocal added_filter_installation
        cloud = (coordinator.data or {}).get("cloud")
        new: list[BinarySensorEntity] = [
            DaedalusCloudBinarySensor(coordinator, source, key, meta)
            for source, key, meta in discovery.discover(cloud)
            if meta is not None
        ]
        if not added_filter_installation and WATER_FILTER_INSTALLED in (cloud or {}).get("machine_settings", {}):
            added_filter_installation = True
            new.append(DaedalusWaterFilterInstalledSensor(coordinator))
        if new:
            async_add_entities(new)

    _add_cloud_binary_sensors()
    entry.async_on_unload(coordinator.async_add_listener(_add_cloud_binary_sensors))


# These peripherals describe conditions a user can resolve at the machine.
_USER_FACING_PERIPHERALS = frozenset({"low_water", "drip_tray_presence", "spout_presence", "peripheral_tank_presence"})


class DaedalusConnectedSensor(DaedalusEntity, BinarySensorEntity):
    _attr_translation_key = "connected"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    # Wiring-state, not a brewing-state — keep off the main dashboard.
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: DaedalusCoordinator) -> None:
        super().__init__(coordinator, key="connected")

    @property
    def is_on(self) -> bool:
        data = self.coordinator.data or {}
        return bool(data.get("connected"))


class DaedalusWaterFilterInstalledSensor(DaedalusEntity, BinarySensorEntity):
    """The installation setting reported by the machine."""

    _attr_translation_key = WATER_FILTER_INSTALLED
    _attr_icon = "mdi:water-pump"

    def __init__(self, coordinator: DaedalusCoordinator) -> None:
        super().__init__(coordinator, key=WATER_FILTER_INSTALLED)

    @property
    def is_on(self) -> bool | None:
        cloud = (self.coordinator.data or {}).get("cloud") or {}
        return cloud.get("machine_settings", {}).get(WATER_FILTER_INSTALLED)

    @property
    def available(self) -> bool:
        return super().available and self.is_on is not None


class DaedalusCloudBinarySensor(DaedalusCloudEntity, BinarySensorEntity):
    """A mapped boolean shadow field (issue #18, mimasek71 snapshot).

    Alarms use ``device_class=problem`` (on = problem detected) and stay on
    the dashboard, as do user-facing peripherals. Internal switches and raw
    accessories are optional diagnostics. Values are coerced (bool / 0-1 /
    "true"/"false" / "Ano"/"Ne"); anything unparsable is unknown, never a
    silent False.
    """

    def __init__(
        self,
        coordinator: DaedalusCoordinator,
        source: str,
        key: str,
        meta: CloudKey,
    ) -> None:
        super().__init__(coordinator, source, key)
        self._attr_translation_key = f"cloud_{meta.key}"
        if meta.kind == KIND_ALARM:
            self._attr_device_class = BinarySensorDeviceClass.PROBLEM
        if source == "capabilities_flat" or (meta.kind != KIND_ALARM and meta.key not in _USER_FACING_PERIPHERALS):
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
            self._attr_entity_registry_enabled_default = False

    @property
    def is_on(self) -> bool | None:
        return coerce_bool(self._cloud_value)
