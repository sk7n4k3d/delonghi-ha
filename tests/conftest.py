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
    "homeassistant.components.diagnostics",
]


def _real_redact(data, keys_to_redact):
    """Stand-in for HA's async_redact_data — redacts top-level keys."""
    if not isinstance(data, dict):
        return data
    return {k: ("**REDACTED**" if k in keys_to_redact else v) for k, v in data.items()}

for mod_name in _HA_MODULES:
    if mod_name not in sys.modules:
        sys.modules[mod_name] = MagicMock()

# Diagnostics needs the real redacter, not a MagicMock that returns another MagicMock.
sys.modules["homeassistant.components.diagnostics"].async_redact_data = _real_redact


def _wire_submodule(parent: str, child: str) -> None:
    """Expose ``sys.modules['parent.child']`` as ``sys.modules['parent'].child``.

    Without this, ``from parent import child`` returns the parent MagicMock's
    auto-generated ``.child`` attribute instead of the submodule we carefully
    stubbed — which silently breaks subclassing of stub base classes.
    """
    parent_mod = sys.modules[parent]
    child_short = child.rsplit(".", 1)[-1]
    setattr(parent_mod, child_short, sys.modules[f"{parent}.{child_short}"])


for _mod in _HA_MODULES[1:]:
    if _mod.count(".") >= 1:
        _wire_submodule(_mod.rsplit(".", 1)[0], _mod)


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

    def async_write_ha_state(self) -> None:
        """No-op stub — real HA persists state to the bus here."""
        return None

    @property
    def name(self) -> str | None:
        """Default name stub."""
        return getattr(self, "_attr_name", None)


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

# HomeAssistantError needs to be a real exception class — entities raise it,
# and MagicMock subclasses of Exception don't satisfy `raise <cls>(...)`.
_exc_mod = sys.modules["homeassistant.exceptions"]
_exc_mod.HomeAssistantError = type("HomeAssistantError", (Exception,), {})
_exc_mod.ConfigEntryAuthFailed = type("ConfigEntryAuthFailed", (Exception,), {})
_exc_mod.ConfigEntryNotReady = type("ConfigEntryNotReady", (Exception,), {})

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


# config_flow.py subclasses ``config_entries.ConfigFlow`` with a
# ``domain=`` kwargs metaclass call — can't inherit from MagicMock. Provide
# a real stub metaclass-compatible base class, along with a stub for
# data_entry_flow.FlowResult.
class _ConfigFlowBase:
    def __init_subclass__(cls, **kwargs) -> None:  # absorb ``domain=``
        super().__init_subclass__()

    def __init__(self, *args, **kwargs) -> None:
        self.hass = kwargs.get("hass")
        self.context = {}

    async def async_set_unique_id(self, _uid: str) -> None:
        return None

    def _abort_if_unique_id_configured(self) -> None:
        return None

    def async_show_form(self, **kwargs) -> dict:
        return {"type": "form", **kwargs}

    def async_create_entry(self, **kwargs) -> dict:
        return {"type": "create_entry", **kwargs}

    def async_abort(self, **kwargs) -> dict:
        return {"type": "abort", **kwargs}


class _OptionsFlowBase:
    def __init__(self, *args, **kwargs) -> None:
        self.hass = None

    def async_create_entry(self, **kwargs) -> dict:
        return {"type": "create_entry", **kwargs}

    def async_show_form(self, **kwargs) -> dict:
        return {"type": "form", **kwargs}


_ce_mod = sys.modules["homeassistant.config_entries"]
_ce_mod.ConfigFlow = _ConfigFlowBase
_ce_mod.OptionsFlow = _OptionsFlowBase
_ce_mod.ConfigEntry = MagicMock  # referenced but only as a type hint

if "homeassistant.data_entry_flow" not in sys.modules:
    _def_mod = MagicMock()
    _def_mod.FlowResult = dict
    sys.modules["homeassistant.data_entry_flow"] = _def_mod
