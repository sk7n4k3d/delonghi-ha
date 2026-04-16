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
