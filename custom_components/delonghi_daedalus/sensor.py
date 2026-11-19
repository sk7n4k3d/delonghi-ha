"""Diagnostic + cloud sensors for the Daedalus integration."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .cloud_keys import KIND_CAPABILITY, CloudEntityDiscovery, CloudKey, cloud_key_display, lookup_cloud_key
from .const import CONF_CLOUD_TELEMETRY, DOMAIN
from .coordinator import DaedalusCoordinator
from .entity import DaedalusCloudEntity, DaedalusEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: DaedalusCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            DaedalusConnectionIdSensor(coordinator),
            DaedalusSerialNumberSensor(coordinator),
            DaedalusLanIpSensor(coordinator),
        ]
    )
    if not entry.data.get(CONF_CLOUD_TELEMETRY):
        return
    discovery = CloudEntityDiscovery(want_binary=False)

    def _add_cloud_sensors() -> None:
        cloud = (coordinator.data or {}).get("cloud")
        new = [
            DaedalusCloudSensor(coordinator, source, key, meta=meta) for source, key, meta in discovery.discover(cloud)
        ]
        if new:
            async_add_entities(new)

    _add_cloud_sensors()
    entry.async_on_unload(coordinator.async_add_listener(_add_cloud_sensors))


class _DaedalusSensorBase(DaedalusEntity, SensorEntity):
    def __init__(self, coordinator: DaedalusCoordinator, *, key: str) -> None:
        super().__init__(coordinator, key=key)
        self._attr_translation_key = key


class _DaedalusLanDiagnosticSensor(_DaedalusSensorBase):
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    # Home Assistant applies this when creating a registry entry, so existing
    # entities keep their previous enabled/disabled settings.
    _attr_entity_registry_enabled_default = False


class DaedalusConnectionIdSensor(_DaedalusLanDiagnosticSensor):
    def __init__(self, coordinator: DaedalusCoordinator) -> None:
        super().__init__(coordinator, key="connection_id")

    @property
    def native_value(self) -> int | None:
        data = self.coordinator.data or {}
        return data.get("connection_id") if data.get("connected") else None

    @property
    def available(self) -> bool:
        return super().available and self.native_value is not None


class DaedalusSerialNumberSensor(_DaedalusLanDiagnosticSensor):
    def __init__(self, coordinator: DaedalusCoordinator) -> None:
        super().__init__(coordinator, key="serial_number")

    @property
    def native_value(self) -> str:
        return self.coordinator.serial_number


class DaedalusLanIpSensor(_DaedalusLanDiagnosticSensor):
    def __init__(self, coordinator: DaedalusCoordinator) -> None:
        super().__init__(coordinator, key="lan_ip")

    @property
    def native_value(self) -> str | None:
        data = self.coordinator.data or {}
        return data.get("host")


class DaedalusCloudSensor(DaedalusCloudEntity, SensorEntity):
    """One flattened AWS IoT shadow key (issue #18).

    Known fields get a translated name from ``cloud_keys``; unknown ones keep
    the v1.8.0 raw ``Cloud <key>`` fallback. Values are raw until each code
    is mapped for real — a bool becomes a "true"/"false" string because
    native_value only accepts str/num (mapped bools render as binary_sensors
    instead). The shadow key is exposed as an attribute so dashboards
    stay stable even if the raw value shape changes later.
    """

    def __init__(
        self,
        coordinator: DaedalusCoordinator,
        source: str,
        key: str,
        meta: CloudKey | None = None,
    ) -> None:
        super().__init__(coordinator, source, key)
        if meta is None:
            meta = lookup_cloud_key(key)
        if meta is not None:
            self._attr_translation_key = f"cloud_{meta.key}"
        else:
            display = cloud_key_display(key)
            self._attr_name = f"Cloud {display.replace('.', ' ').replace('_', ' ').strip()}"
        if source == "capabilities_flat" or meta is None or meta.kind == KIND_CAPABILITY:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
            self._attr_entity_registry_enabled_default = False

    @property
    def native_value(self) -> str | int | float | None:
        value = self._cloud_value
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float, str)):
            return value
        return None
