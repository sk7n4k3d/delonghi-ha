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


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up binary sensor entities for alarms."""
    data: dict[str, Any] = hass.data[DOMAIN][entry.entry_id]
    coordinator: DeLonghiCoordinator = data["coordinator"]
    dsn: str = data["dsn"]
    model: str = data["model"]
    device_name: str = data["device_name"]
    sw_version: str | None = data.get("sw_version")

    entities: list[BinarySensorEntity] = []

    for bit, meta in ALARMS.items():
        entities.append(DeLonghiAlarmSensor(coordinator, dsn, model, device_name, sw_version, bit, meta))

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
    def is_on(self) -> bool | None:
        """Return True if alarm is active.

        Inverted bits (13=tank position, 18=grid present) indicate a
        positive state when SET. The alarm is active when the bit is NOT
        set (meaning the component is missing).

        To avoid false positives on machines that don't report these bits,
        inverted alarms only activate after the coordinator has confirmed
        the machine supports them (bit seen set at least once).
        """
        alarm_word: int | None = self.coordinator.data.get("alarm_word")
        if alarm_word is None:
            return None

        bit_set = bool(alarm_word & (1 << self._alarm_bit))

        if not self._inverted:
            return bit_set

        # Inverted alarm: only report problem if the machine actually
        # supports this bit (we've seen it set at least once)
        if self._alarm_bit not in self.coordinator.seen_alarm_bits:
            if bit_set:
                self.coordinator.seen_alarm_bits.add(self._alarm_bit)
            else:
                return False  # Never seen → assume unsupported, no problem
        return not bit_set
