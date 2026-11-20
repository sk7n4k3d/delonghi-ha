"""Diagnostic + cloud sensors for the Daedalus integration."""

from __future__ import annotations

import json

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import MAX_LENGTH_STATE_STATE, PERCENTAGE, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .cloud_keys import KIND_CAPABILITY, CloudEntityDiscovery, CloudKey, cloud_key_display, lookup_cloud_key
from .const import (
    AUTO_OFF_DELAY,
    BREWING_STATISTICS_KEYS,
    CONF_CLOUD_TELEMETRY,
    CONF_DRINK_VISIBILITY,
    DOMAIN,
    MAINTENANCE_COUNTER_KEYS,
    MAINTENANCE_PERCENTAGE_KEYS,
)
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
    added_settings_and_counters: set[str] = set()
    visibility_added = False

    def _add_cloud_sensors() -> None:
        nonlocal visibility_added
        cloud = (coordinator.data or {}).get("cloud")
        new: list[SensorEntity] = [
            DaedalusCloudSensor(coordinator, source, key, meta=meta) for source, key, meta in discovery.discover(cloud)
        ]
        for sensor_cls, keys in (
            (DaedalusBrewingStatisticSensor, BREWING_STATISTICS_KEYS.values()),
            (DaedalusMaintenanceCounterSensor, MAINTENANCE_COUNTER_KEYS.values()),
            (DaedalusMaintenanceProgressSensor, MAINTENANCE_PERCENTAGE_KEYS.values()),
            (DaedalusAutoOffDelaySensor, (AUTO_OFF_DELAY,)),
        ):
            values = (cloud or {}).get(sensor_cls.cloud_key, {})
            for key in keys:
                if key in values and key not in added_settings_and_counters:
                    added_settings_and_counters.add(key)
                    new.append(sensor_cls(coordinator, key=key))
        if (
            entry.data.get(CONF_DRINK_VISIBILITY)
            and not visibility_added
            and (cloud or {}).get("visible_drinks_count") is not None
        ):
            visibility_added = True
            new.append(DaedalusVisibleDrinksSensor(coordinator))
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


class _DaedalusCloudNumberSensor(_DaedalusSensorBase):
    cloud_key: str

    @property
    def available(self) -> bool:
        return super().available and self.native_value is not None

    @property
    def native_value(self) -> int | None:
        cloud = (self.coordinator.data or {}).get("cloud") or {}
        return cloud.get(self.cloud_key, {}).get(self._attr_translation_key)


class _DaedalusCloudCounterSensor(_DaedalusCloudNumberSensor):
    _attr_state_class = SensorStateClass.TOTAL_INCREASING


class DaedalusBrewingStatisticSensor(_DaedalusCloudCounterSensor):
    """One lifetime brewing counter reported by the machine."""

    cloud_key = "brewing_statistics"


class DaedalusMaintenanceCounterSensor(_DaedalusCloudCounterSensor):
    """One lifetime maintenance counter reported by the machine."""

    cloud_key = "maintenance_counters"


class DaedalusMaintenanceProgressSensor(_DaedalusCloudNumberSensor):
    """A maintenance percentage reported by the machine."""

    cloud_key = "machine_settings"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT


class DaedalusAutoOffDelaySensor(_DaedalusCloudNumberSensor):
    """The configured delay before the machine automatically turns off."""

    cloud_key = "machine_settings"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES


class DaedalusVisibleDrinksSensor(_DaedalusSensorBase):
    """The number of drink flags set to visible in the cloud shadow."""

    def __init__(self, coordinator: DaedalusCoordinator) -> None:
        super().__init__(coordinator, key="visible_drinks_count")

    @property
    def native_value(self) -> int | None:
        cloud = (self.coordinator.data or {}).get("cloud") or {}
        return cloud.get("visible_drinks_count")

    @property
    def available(self) -> bool:
        return super().available and self.native_value is not None


class DaedalusCloudSensor(DaedalusCloudEntity, SensorEntity):
    """One flattened AWS IoT shadow key (issue #18).

    Known fields get a translated name from ``cloud_keys``; unknown ones keep
    the v1.8.0 raw ``Cloud <key>`` fallback. Values are raw until each code
    is mapped for real — a bool becomes a "true"/"false" string because
    native_value only accepts str/num (mapped bools render as binary_sensors
    instead). Long language lists use a count as the state and keep the list
    as an attribute. The shadow key is exposed as an attribute so dashboards
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
        self._is_languages = meta is not None and meta.key == "languages"
        if meta is not None:
            self._attr_translation_key = f"cloud_{meta.key}"
        else:
            display = cloud_key_display(key)
            self._attr_name = f"Cloud {display.replace('.', ' ').replace('_', ' ').strip()}"
        if source == "capabilities_flat" or meta is None or meta.kind == KIND_CAPABILITY:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
            self._attr_entity_registry_enabled_default = False

    def _language_list(self, value: str) -> list[str] | None:
        """Decode the known Languages field that flatten_shadow stores as JSON."""
        if not self._is_languages:
            return None
        try:
            languages = json.loads(value)
        except (ValueError, RecursionError):
            return None
        if isinstance(languages, list) and all(isinstance(language, str) for language in languages):
            return languages
        return None

    @property
    def native_value(self) -> str | int | float | None:
        value = self._cloud_value
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, str) and len(value) > MAX_LENGTH_STATE_STATE:
            languages = self._language_list(value)
            return len(languages) if languages is not None else None
        if isinstance(value, (int, float, str)):
            return value
        return None

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        attrs = super().extra_state_attributes
        value = self._cloud_value
        if isinstance(value, str) and len(value) > MAX_LENGTH_STATE_STATE:
            languages = self._language_list(value)
            if languages is not None:
                attrs["languages"] = languages
            else:
                attrs["value_length"] = len(value)
        return attrs
