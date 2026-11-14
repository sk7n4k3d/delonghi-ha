"""Diagnostic + cloud sensors for the Daedalus integration."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .cloud_keys import KIND_CAPABILITY, CloudKey, lookup_cloud_key, plan_cloud_entities
from .const import CONF_CLOUD_TELEMETRY, DOMAIN
from .coordinator import DaedalusCoordinator


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
    # Cloud shadows arrive late (first successful fetch) and their key set is
    # only known at runtime: add each planned shadow key exactly once, on the
    # first coordinator update that carries it. The listener self-unregisters
    # once the data has arrived (same pattern as the delonghi_coffee button
    # platform) — it must not fire on every 30 s update for the lifetime of
    # the entry (review #18, F6).
    added: set[tuple[str, str]] = set()
    unsub: Callable[[], None] | None = None

    def _add_cloud_sensors() -> None:
        nonlocal unsub
        cloud = (coordinator.data or {}).get("cloud") or {}
        new: list[DaedalusCloudSensor] = []
        # Mapped alarm/bool keys belong to the binary_sensor platform —
        # plan_cloud_entities partitions the two platforms exactly.
        for source, key, meta in plan_cloud_entities(cloud, want_binary=False):
            if (source, key) in added:
                continue
            added.add((source, key))
            new.append(DaedalusCloudSensor(coordinator, source, key, meta=meta))
        if new:
            async_add_entities(new)
        if any(cloud.get(s) for s in ("status_flat", "capabilities_flat")) and unsub is not None:
            unsub()
            unsub = None

    if any((coordinator.data or {}).get("cloud", {}).get(s) for s in ("status_flat", "capabilities_flat")):
        # First refresh already carried the shadows — add directly, no listener.
        _add_cloud_sensors()
    else:
        unsub = coordinator.async_add_listener(_add_cloud_sensors)
    entry.async_on_unload(lambda: unsub() if unsub is not None else None)


class _DaedalusSensorBase(CoordinatorEntity[DaedalusCoordinator], SensorEntity):
    _attr_has_entity_name = True
    # These are wiring-state sensors (LAN ID / SN / LAN IP), not user-facing
    # measurements — keep them out of the main dashboard.
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: DaedalusCoordinator, *, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.serial_number}_{key}"
        self._attr_translation_key = key


class DaedalusConnectionIdSensor(_DaedalusSensorBase):
    def __init__(self, coordinator: DaedalusCoordinator) -> None:
        super().__init__(coordinator, key="connection_id")

    @property
    def native_value(self) -> int | None:
        data = self.coordinator.data or {}
        return data.get("connection_id")


class DaedalusSerialNumberSensor(_DaedalusSensorBase):
    def __init__(self, coordinator: DaedalusCoordinator) -> None:
        super().__init__(coordinator, key="serial_number")

    @property
    def native_value(self) -> str:
        return self.coordinator.serial_number


class DaedalusLanIpSensor(_DaedalusSensorBase):
    def __init__(self, coordinator: DaedalusCoordinator) -> None:
        super().__init__(coordinator, key="lan_ip")

    @property
    def native_value(self) -> str | None:
        data = self.coordinator.data or {}
        return data.get("host")


class DaedalusCloudSensor(CoordinatorEntity[DaedalusCoordinator], SensorEntity):
    """One flattened AWS IoT shadow key (issue #18).

    Known fields get a translated name from ``cloud_keys``; unknown ones keep
    the v1.8.0 raw ``Cloud <key>`` fallback. Values are raw until each code
    is mapped for real — a bool becomes a "true"/"false" string because
    native_value only accepts str/num (mapped bools render as binary_sensors
    instead). The shadow key is exposed as an attribute so dashboards stay
    stable even if the raw value shape changes later.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DaedalusCoordinator,
        source: str,
        key: str,
        meta: CloudKey | None = None,
    ) -> None:
        super().__init__(coordinator)
        self._source = source
        self._key = key
        display = str(key).strip()[:64]
        if meta is None:
            meta = lookup_cloud_key(key)
        # capabilities_flat keys keep a distinct unique_id namespace so a same
        # named status/capability key can never collide.
        prefix = "cloud" if source == "status_flat" else "cloudcap"
        self._attr_unique_id = f"{coordinator.serial_number}_{prefix}_{display}"
        if meta is not None:
            self._attr_translation_key = f"cloud_{meta.key}"
        else:
            self._attr_name = f"Cloud {display.replace('.', ' ').replace('_', ' ').strip()}"
        if source == "capabilities_flat" or (meta is not None and meta.kind == KIND_CAPABILITY):
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self) -> str | int | float | None:
        cloud = (self.coordinator.data or {}).get("cloud") or {}
        value: Any = cloud.get(self._source, {}).get(self._key)
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float, str)):
            return value
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        cloud = (self.coordinator.data or {}).get("cloud") or {}
        return {"shadow_key": self._key, "machine_name": cloud.get("machine_name")}
