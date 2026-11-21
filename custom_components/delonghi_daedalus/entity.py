"""Shared device information and cloud field handling for Daedalus entities."""

from __future__ import annotations

from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .cloud import machine_model_code
from .cloud_keys import cloud_key_display, lookup_cloud_key
from .const import CONF_MODEL
from .coordinator import DaedalusCoordinator


def telemetry_model_code(coordinator: DaedalusCoordinator) -> str | None:
    """Return the compact product code for model-scoped telemetry."""
    configured_model = coordinator.entry.data.get(CONF_MODEL)
    if configured_model is not None:
        model = machine_model_code(configured_model)
        return model.replace(".", "") if model is not None else None

    cloud = (coordinator.data or {}).get("cloud") or {}
    reported_models = {
        code.replace(".", "")
        for key, value in cloud.get("capabilities_flat", {}).items()
        if (meta := lookup_cloud_key(key)) is not None
        and meta.key == "machine_model"
        and (code := machine_model_code(value)) is not None
    }
    return reported_models.pop() if len(reported_models) == 1 else None


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
