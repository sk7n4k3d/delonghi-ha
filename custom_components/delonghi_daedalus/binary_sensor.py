"""Binary sensors: LAN connectivity + mapped cloud alarms/peripherals."""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .cloud_keys import KIND_ALARM, CloudKey, coerce_bool, plan_cloud_entities
from .const import CONF_CLOUD_TELEMETRY, DOMAIN
from .coordinator import DaedalusCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: DaedalusCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([DaedalusConnectedSensor(coordinator)])
    if not entry.data.get(CONF_CLOUD_TELEMETRY):
        return
    # Same lazy pattern as sensor.py: the shadow key set is only known once
    # the first cloud fetch lands, and the listener self-unregisters after
    # that (review #18, F6). plan_cloud_entities(want_binary=True) hands this
    # platform exactly the mapped alarm/bool keys — sensor.py keeps the rest.
    added: set[tuple[str, str]] = set()
    unsub: Callable[[], None] | None = None

    def _add_cloud_binary_sensors() -> None:
        nonlocal unsub
        cloud = (coordinator.data or {}).get("cloud") or {}
        new: list[DaedalusCloudBinarySensor] = []
        for source, key, meta in plan_cloud_entities(cloud, want_binary=True):
            if (source, key) in added:
                continue
            added.add((source, key))
            new.append(DaedalusCloudBinarySensor(coordinator, source, key, meta))
        if new:
            async_add_entities(new)
        if any(cloud.get(s) for s in ("status_flat", "capabilities_flat")) and unsub is not None:
            unsub()
            unsub = None

    if any((coordinator.data or {}).get("cloud", {}).get(s) for s in ("status_flat", "capabilities_flat")):
        _add_cloud_binary_sensors()
    else:
        unsub = coordinator.async_add_listener(_add_cloud_binary_sensors)
    entry.async_on_unload(lambda: unsub() if unsub is not None else None)


class DaedalusConnectedSensor(CoordinatorEntity[DaedalusCoordinator], BinarySensorEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "connected"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    # Wiring-state, not a brewing-state — keep off the main dashboard.
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: DaedalusCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.serial_number}_connected"

    @property
    def is_on(self) -> bool:
        data = self.coordinator.data or {}
        return bool(data.get("connected"))


class DaedalusCloudBinarySensor(CoordinatorEntity[DaedalusCoordinator], BinarySensorEntity):
    """A mapped boolean shadow field (issue #18, mimasek71 snapshot).

    Alarms use ``device_class=problem`` (on = problem detected) and stay on
    the dashboard; peripherals/accessories are wiring states and land in the
    DIAGNOSTIC category. Raw values are coerced defensively (bool / 0-1 /
    "true"/"false" / "Ano"/"Ne"); anything unparsable is unknown, never a
    silent False.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DaedalusCoordinator,
        source: str,
        key: str,
        meta: CloudKey,
    ) -> None:
        super().__init__(coordinator)
        self._source = source
        self._key = key
        display = str(key).strip()[:64]
        prefix = "cloud" if source == "status_flat" else "cloudcap"
        self._attr_unique_id = f"{coordinator.serial_number}_{prefix}_{display}"
        self._attr_translation_key = f"cloud_{meta.key}"
        if meta.kind == KIND_ALARM:
            self._attr_device_class = BinarySensorDeviceClass.PROBLEM
        if source == "capabilities_flat" or meta.kind != KIND_ALARM:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def is_on(self) -> bool | None:
        cloud = (self.coordinator.data or {}).get("cloud") or {}
        return coerce_bool(cloud.get(self._source, {}).get(self._key))

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        cloud = (self.coordinator.data or {}).get("cloud") or {}
        return {"shadow_key": self._key, "machine_name": cloud.get("machine_name")}
