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
    def available(self) -> bool:
        """Return True only when this alarm bit is known to be supported.

        The machine reports alarms as a single bitmask. There is no way
        to tell "bit 5 is off" from "bit 5 does not exist on this
        firmware", so the heuristic is:

        * if ``alarm_word`` is missing entirely, every alarm is
          unavailable (HA shows ``unavailable`` instead of ``off``),
        * inverted bits (tank/grid present) stay unavailable until we
          have seen them set at least once on this machine — that proves
          the firmware actually drives the bit.

        See issue #3 — jostrasser.
        """
        if not super().available:
            return False
        alarm_word: int | None = self.coordinator.data.get("alarm_word")
        if alarm_word is None:
            return False
        if self._inverted and self._alarm_bit not in self.coordinator.seen_alarm_bits:
            # Opportunistically learn support from the current word.
            if alarm_word & (1 << self._alarm_bit):
                self.coordinator.seen_alarm_bits.add(self._alarm_bit)
                return True
            return False
        return True

    @property
    def is_on(self) -> bool | None:
        """Return True if alarm is active.

        Inverted bits (13=tank position, 18=grid present) indicate a
        positive state when SET. The alarm is active when the bit is NOT
        set (meaning the component is missing).

        Availability is enforced by :meth:`available`, so this only runs
        when the bit is known to be supported.
        """
        alarm_word: int | None = self.coordinator.data.get("alarm_word")
        if alarm_word is None:
            return None

        bit_set = bool(alarm_word & (1 << self._alarm_bit))
        if not self._inverted:
            return bit_set
        return not bit_set
