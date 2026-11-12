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

from .const import CONF_CLOUD_TELEMETRY, DOMAIN
from .coordinator import DaedalusCoordinator

# Cap mirrors cloud.flatten_shadow(max_entries): one sensor per flattened
# MachineStatus key, never more.
_MAX_CLOUD_SENSORS = 80


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
    # only known at runtime: add each flattened MachineStatus key exactly
    # once, on the first coordinator update that carries it. The listener
    # self-unregisters once the data has arrived (same pattern as the
    # delonghi_coffee button platform) — it must not fire on every 30 s
    # update for the lifetime of the entry (review #18, F6).
    added: set[str] = set()
    unsub: Callable[[], None] | None = None

    def _add_cloud_sensors() -> None:
        nonlocal unsub
        cloud = (coordinator.data or {}).get("cloud") or {}
        flat: dict[str, Any] = cloud.get("status_flat") or {}
        new: list[DaedalusCloudSensor] = []
        for key in flat:
            # Audit #18 (E1): shadow keys are cloud-controlled — drop blank
            # ones and cap the identity/display length so a hostile key
            # cannot build a colliding or oversized entity id. The raw key
            # stays the lookup key in status_flat.
            display = str(key).strip()[:64]
            if not display or key in added or len(added) >= _MAX_CLOUD_SENSORS:
                continue
            added.add(key)
            new.append(DaedalusCloudSensor(coordinator, key, display))
        if new:
            async_add_entities(new)
        if flat and unsub is not None:
            unsub()
            unsub = None

    if (coordinator.data or {}).get("cloud", {}).get("status_flat"):
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
    """One flattened AWS IoT MachineStatus shadow key (issue #18).

    Values are raw until each code is mapped for real — a bool becomes a
    "true"/"false" string because native_value only accepts str/num. The
    shadow key is exposed as an attribute so dashboards stay stable even
    if the raw value shape changes later.
    """

    _attr_has_entity_name = True

    def __init__(self, coordinator: DaedalusCoordinator, key: str, display: str | None = None) -> None:
        super().__init__(coordinator)
        self._key = key
        display = display or str(key).strip()[:64]
        self._attr_unique_id = f"{coordinator.serial_number}_cloud_{display}"
        self._attr_name = f"Cloud {display.replace('.', ' ').replace('_', ' ').strip()}"

    @property
    def native_value(self) -> str | int | float | None:
        cloud = (self.coordinator.data or {}).get("cloud") or {}
        value: Any = cloud.get("status_flat", {}).get(self._key)
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float, str)):
            return value
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        cloud = (self.coordinator.data or {}).get("cloud") or {}
        return {"shadow_key": self._key, "machine_name": cloud.get("machine_name")}
