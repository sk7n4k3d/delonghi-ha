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

# Real ConfigFlow / OptionsFlow stubs so config_flow.py can subclass them.
# class Foo(MagicMock_attr_value, domain=...): silently produces a MagicMock
# instead of a real class, which then has no real methods to test.
class _ConfigFlowBase:
    """Stand-in for HA's config_entries.ConfigFlow base."""

    def __init_subclass__(cls, **_kwargs) -> None:  # accept domain= kwarg
        super().__init_subclass__()

    async def async_set_unique_id(self, unique_id: str) -> None:
        return None

    def _abort_if_unique_id_configured(self) -> None:
        return None

    def async_create_entry(self, *, title: str, data: dict) -> dict:
        return {"type": "create_entry", "title": title, "data": data}

    def async_show_form(self, *, step_id: str, data_schema=None, errors=None, description_placeholders=None) -> dict:
        return {
            "type": "form",
            "step_id": step_id,
            "data_schema": data_schema,
            "errors": errors or {},
            "description_placeholders": description_placeholders,
        }

    def async_abort(self, *, reason: str) -> dict:
        return {"type": "abort", "reason": reason}


class _OptionsFlowBase:
    """Stand-in for HA's config_entries.OptionsFlow base."""

    def async_create_entry(self, *, title: str, data: dict) -> dict:
        return {"type": "create_entry", "title": title, "data": data}

    def async_show_form(self, *, step_id: str, data_schema=None) -> dict:
        return {"type": "form", "step_id": step_id, "data_schema": data_schema}


_ce_mod = sys.modules["homeassistant.config_entries"]
_ce_mod.ConfigFlow = _ConfigFlowBase
_ce_mod.OptionsFlow = _OptionsFlowBase

# data_entry_flow stub — FlowResult is a TypedDict at runtime, dict is fine.
sys.modules.setdefault("homeassistant.data_entry_flow", MagicMock())

# `from homeassistant import config_entries` reads the attribute on the
# parent MagicMock, not sys.modules — wire the submodules explicitly so
# config_flow.py picks up the real ConfigFlow/OptionsFlow stubs.
_ha_mod = sys.modules["homeassistant"]
_ha_mod.config_entries = _ce_mod
_ha_mod.const = sys.modules["homeassistant.const"]
_ha_mod.core = sys.modules["homeassistant.core"]
_ha_mod.exceptions = sys.modules["homeassistant.exceptions"]
_ha_mod.helpers = sys.modules["homeassistant.helpers"]
_ha_mod.components = sys.modules["homeassistant.components"]

# Fix specific constants that code references directly
sys.modules["homeassistant.const"].CONF_EMAIL = "email"
sys.modules["homeassistant.const"].CONF_PASSWORD = "password"
sys.modules["homeassistant.const"].Platform = MagicMock()
sys.modules["homeassistant.helpers.entity"] = MagicMock()
sys.modules["homeassistant.helpers.entity"].EntityCategory = MagicMock()

# Real exception class so production code can raise/catch it cleanly.
# Kept as a single shared class across all test modules to avoid identity
# drift when multiple test files import or reference HomeAssistantError.
sys.modules["homeassistant.exceptions"].HomeAssistantError = type(
    "HomeAssistantError", (Exception,), {}
)

# Same treatment for ConfigEntry* exceptions raised by the integration entry
# points (__init__.async_setup_entry). They must be real Exception subclasses
# so `raise ConfigEntryAuthFailed(...)` and `raise ConfigEntryNotReady(...)`
# don't blow up with "exceptions must derive from BaseException" during tests.
sys.modules["homeassistant.exceptions"].ConfigEntryAuthFailed = type(
    "ConfigEntryAuthFailed", (Exception,), {}
)
sys.modules["homeassistant.exceptions"].ConfigEntryNotReady = type(
    "ConfigEntryNotReady", (Exception,), {}
)
