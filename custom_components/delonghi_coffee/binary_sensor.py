"""Binary sensor platform for De'Longhi Coffee — machine alarms."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ALARMS, DOMAIN
from .coordinator import DeLonghiCoordinator
from .sensor import _device_info

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up binary sensor entities for alarms."""
    data: dict[str, Any] = hass.data[DOMAIN][entry.entry_id]
    coordinator: DeLonghiCoordinator = data["coordinator"]
    dsn: str = data["dsn"]
    model: str = data["model"]
    device_name: str = data["device_name"]
    sw_version: str | None = data.get("sw_version")

    entities: list[BinarySensorEntity] = []

    for bit, meta in ALARMS.items():
        entities.append(
            DeLonghiAlarmSensor(coordinator, dsn, model, device_name, sw_version, bit, meta)
        )

    async_add_entities(entities)


class DeLonghiAlarmSensor(CoordinatorEntity[DeLonghiCoordinator], BinarySensorEntity):
    """Binary sensor for a machine alarm."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(
        self,
        coordinator: DeLonghiCoordinator,
        dsn: str,
        model: str,
        device_name: str,
        sw_version: str | None,
        alarm_bit: int,
        meta: dict[str, str],
    ) -> None:
        super().__init__(coordinator)
        self._alarm_bit = alarm_bit
        self._inverted = meta.get("inverted", False)
        self._attr_unique_id = f"{dsn}_alarm_{alarm_bit}"
        self._attr_has_entity_name = True
        self._attr_translation_key = f"alarm_{alarm_bit}"
        self._attr_icon = meta["icon"]
        self._attr_device_info = _device_info(dsn, model, device_name, sw_version)

    @property
    def is_on(self) -> bool:
        """Return True if alarm is active.

        Some bits are inverted — they indicate a positive state (e.g. tank
        in position, grid present). For those, the alarm is active when
        the bit is NOT set (meaning the thing is missing).
        """
        alarms: list[dict[str, Any]] = self.coordinator.data.get("alarms", [])
        bit_set = any(a["bit"] == self._alarm_bit for a in alarms)
        return not bit_set if self._inverted else bit_set
