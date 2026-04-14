"""Shared fixtures for De'Longhi tests.

Mock homeassistant at module level so imports work during collection.
Platform base classes are stubbed with real classes (not MagicMock) so
entity subclasses with metaclass-sensitive bases (SensorEntity,
BinarySensorEntity, …) can be instantiated in unit tests.
"""

import sys
from unittest.mock import MagicMock

# Must happen before any test module imports custom_components
_HA_MODULES = [
    "homeassistant",
    "homeassistant.core",
    "homeassistant.config_entries",
    "homeassistant.const",
    "homeassistant.exceptions",
    "homeassistant.helpers",
    "homeassistant.helpers.entity_platform",
    "homeassistant.helpers.update_coordinator",
    "homeassistant.components",
    "homeassistant.components.sensor",
    "homeassistant.components.binary_sensor",
    "homeassistant.components.button",
    "homeassistant.components.switch",
    "homeassistant.components.select",
]

for mod_name in _HA_MODULES:
    if mod_name not in sys.modules:
        sys.modules[mod_name] = MagicMock()


class _GenericBase:
    """Stand-in for HA's generic entity base classes.

    Supports ``Base[Something]`` subscription, swallows kwargs in
    ``__init__`` and records the coordinator (if any) so entities can
    interact with ``self.coordinator`` in tests.
    """

    _attr_available = True

    def __class_getitem__(cls, _item):
        return cls

    def __init__(self, coordinator=None, *args, **kwargs) -> None:
        if coordinator is not None:
            self.coordinator = coordinator

    @property
    def available(self) -> bool:
        return True

    async def async_will_remove_from_hass(self) -> None:
        """No-op stub — real HA provides cleanup hooks here."""
        return None


class _StubEntity:
    """Minimal stub for platform entity mixins (SensorEntity, …)."""

    _attr_available = True

    def __init__(self, *args, **kwargs) -> None:
        pass

    @property
    def available(self) -> bool:
        return True


# Replace the MagicMock bases with real stub classes where entity code
# inherits from them. Everything else on the module remains a MagicMock
# so unrelated attribute access still works.
_coord_mod = sys.modules["homeassistant.helpers.update_coordinator"]
_coord_mod.CoordinatorEntity = _GenericBase
_coord_mod.DataUpdateCoordinator = _GenericBase
_coord_mod.UpdateFailed = type("UpdateFailed", (Exception,), {})

_sensor_mod = sys.modules["homeassistant.components.sensor"]
_sensor_mod.SensorEntity = _StubEntity
_sensor_mod.SensorStateClass = MagicMock()

_bs_mod = sys.modules["homeassistant.components.binary_sensor"]
_bs_mod.BinarySensorEntity = _StubEntity
_bs_mod.BinarySensorDeviceClass = MagicMock()

_button_mod = sys.modules["homeassistant.components.button"]
_button_mod.ButtonEntity = _StubEntity

_switch_mod = sys.modules["homeassistant.components.switch"]
_switch_mod.SwitchEntity = _StubEntity

_select_mod = sys.modules["homeassistant.components.select"]
_select_mod.SelectEntity = _StubEntity

# Fix specific constants that code references directly
sys.modules["homeassistant.const"].CONF_EMAIL = "email"
sys.modules["homeassistant.const"].CONF_PASSWORD = "password"
sys.modules["homeassistant.const"].Platform = MagicMock()
sys.modules["homeassistant.helpers.entity"] = MagicMock()
sys.modules["homeassistant.helpers.entity"].EntityCategory = MagicMock()
