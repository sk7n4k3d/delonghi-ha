"""Shared device information and cloud field handling for Daedalus entities."""

from __future__ import annotations

from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .cloud_keys import cloud_key_display
from .coordinator import DaedalusCoordinator


class DaedalusEntity(CoordinatorEntity[DaedalusCoordinator]):
    """An entity belonging to one coffee machine."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: DaedalusCoordinator, *, key: str) -> None:
        super().__init__(coordinator)
        serial = coordinator.serial_number
        # Preserve the existing entity registry lookup, including custom IDs
        # and enabled/disabled settings, when attaching entities to the device.
        self._attr_unique_id = f"{serial}_{key}"
        self._attr_device_info = coordinator.device.info()


class DaedalusCloudEntity(DaedalusEntity):
    """An entity backed by one field of a cloud shadow."""

    def __init__(self, coordinator: DaedalusCoordinator, source: str, key: str) -> None:
        prefix = "cloud" if source == "status_flat" else "cloudcap"
        super().__init__(coordinator, key=f"{prefix}_{cloud_key_display(key)}")
        self._source = source
        self._key = key

    @property
    def _cloud_value(self) -> str | bool | int | float | None:
        cloud = (self.coordinator.data or {}).get("cloud") or {}
        return cloud.get(self._source, {}).get(self._key)

    @property
    def available(self) -> bool:
        return super().available and self._cloud_value is not None

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {"shadow_key": self._key}
