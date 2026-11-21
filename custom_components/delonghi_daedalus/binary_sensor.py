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

from .cloud import reported_timestamp_is_recent
from .cloud_keys import KIND_ALARM, CloudEntityDiscovery, CloudKey, coerce_bool
from .const import CONF_CLOUD_TELEMETRY, DOMAIN, HOT_CARAFE_RINSE_EVIDENCE_FIELDS, WATER_FILTER_INSTALLED
from .coordinator import DaedalusCoordinator
from .entity import DaedalusCloudEntity, DaedalusEntity, telemetry_model_code

_AROMATIC_MODEL_CODE = "ECAM63070"
_HOT_CARAFE_RINSE_KEY = "hot_milk_carafe_rinse_in_progress"
_HOT_CARAFE_STATUS_FIELDS = {*HOT_CARAFE_RINSE_EVIDENCE_FIELDS, "peripherals.HotJug"}


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
    added_hot_carafe_rinse = False

    def _add_cloud_binary_sensors() -> None:
        nonlocal added_filter_installation, added_hot_carafe_rinse
        cloud = (coordinator.data or {}).get("cloud")
        new: list[BinarySensorEntity] = [
            DaedalusCloudBinarySensor(coordinator, source, key, meta)
            for source, key, meta in discovery.discover(cloud)
            if meta is not None
        ]
        if not added_filter_installation and WATER_FILTER_INSTALLED in (cloud or {}).get("machine_settings", {}):
            added_filter_installation = True
            new.append(DaedalusWaterFilterInstalledSensor(coordinator))
        if (
            not added_hot_carafe_rinse
            and _HOT_CARAFE_STATUS_FIELDS.issubset((cloud or {}).get("status_flat", {}))
            and telemetry_model_code(coordinator) == _AROMATIC_MODEL_CODE
        ):
            added_hot_carafe_rinse = True
            new.append(DaedalusHotCarafeRinseSensor(coordinator))
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


class DaedalusHotCarafeRinseSensor(DaedalusEntity, BinarySensorEntity):
    """The observed hot milk carafe rinse activity on the ECAM630.70."""

    _attr_translation_key = _HOT_CARAFE_RINSE_KEY
    _attr_icon = "mdi:water-sync"

    def __init__(self, coordinator: DaedalusCoordinator) -> None:
        super().__init__(coordinator, key=_HOT_CARAFE_RINSE_KEY)

    @property
    def suggested_object_id(self) -> str:
        return _HOT_CARAFE_RINSE_KEY

    @property
    def is_on(self) -> bool | None:
        data = self.coordinator.data or {}
        cloud = data.get("cloud") or {}
        if data.get("cloud_error") or cloud.get("status_error"):
            return None
        status = cloud.get("status_flat") or {}
        state = status.get("Status")
        mode = status.get("CurrMode")
        hot_carafe = status.get("peripherals.HotJug")
        clean_selector = status.get("accessories.CleanHot")
        if (
            state not in ("Busy", "Idle", "Standby", "Initialization")
            or type(mode) is not int
            or mode < 0
            or type(hot_carafe) is not bool
            or type(clean_selector) is not bool
        ):
            return None
        if state != "Busy" or mode != 12 or not hot_carafe:
            return False
        # Busy mode 12 does not identify the activity by itself. The selector
        # can also turn off near the end of an observed rinse.
        timestamps = cloud.get("status_reported_at", {})
        if clean_selector and all(
            reported_timestamp_is_recent(timestamps.get(field)) for field in HOT_CARAFE_RINSE_EVIDENCE_FIELDS
        ):
            return True
        return None

    @property
    def available(self) -> bool:
        return (
            super().available
            and telemetry_model_code(self.coordinator) == _AROMATIC_MODEL_CODE
            and self.is_on is not None
        )


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
        if (
            source == "status_flat"
            and key == "accessories.CleanHot"
            and telemetry_model_code(coordinator) == _AROMATIC_MODEL_CODE
        ):
            self._attr_translation_key = "cloud_hot_carafe_clean_selector"
        if meta.kind == KIND_ALARM:
            self._attr_device_class = BinarySensorDeviceClass.PROBLEM
        if source == "capabilities_flat" or (meta.kind != KIND_ALARM and meta.key not in _USER_FACING_PERIPHERALS):
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
            self._attr_entity_registry_enabled_default = False

    @property
    def suggested_object_id(self) -> str | None:
        if self._attr_translation_key == "cloud_hot_carafe_clean_selector":
            return "hot_cleaning_cycle"
        return super().suggested_object_id

    @property
    def is_on(self) -> bool | None:
        return coerce_bool(self._cloud_value)
