"""Entity rendering for mapped vs unknown cloud shadow keys (issue #18).

Uses the stubbed homeassistant bases from the root conftest — no HA runtime.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.helpers.entity import EntityCategory

from custom_components.delonghi_daedalus import binary_sensor as bs
from custom_components.delonghi_daedalus import sensor as sp
from custom_components.delonghi_daedalus.cloud_keys import lookup_cloud_key
from custom_components.delonghi_daedalus.const import CONF_CLOUD_TELEMETRY, DOMAIN


class _FakeCoordinator:
    serial_number = "SN-TEST"

    def __init__(self, data: dict) -> None:
        self.data = data
        self.listeners = 0

    def async_add_listener(self, _cb):
        self.listeners += 1
        return lambda: None


def _cloud(**flats) -> dict:
    return {"cloud": {"machine_name": "thing-1", **flats}}


# ------------------------------------------------------------------ sensor --


def test_known_status_key_uses_translation() -> None:
    coord = _FakeCoordinator(_cloud(status_flat={"OperatingState": "Standby"}))
    entity = sp.DaedalusCloudSensor(coord, "status_flat", "OperatingState", meta=lookup_cloud_key("OperatingState"))
    assert entity._attr_translation_key == "cloud_operating_state"
    assert getattr(entity, "_attr_name", None) is None
    assert entity._attr_unique_id == "SN-TEST_cloud_OperatingState"
    assert entity.native_value == "Standby"
    assert getattr(entity, "_attr_entity_category", None) is None
    assert entity.extra_state_attributes == {"shadow_key": "OperatingState", "machine_name": "thing-1"}


def test_unknown_key_keeps_raw_fallback() -> None:
    coord = _FakeCoordinator(_cloud(status_flat={"opaque.field": 7}))
    entity = sp.DaedalusCloudSensor(coord, "status_flat", "opaque.field", meta=None)
    assert entity._attr_name == "Cloud opaque field"
    assert getattr(entity, "_attr_translation_key", None) is None
    assert entity.native_value == 7


def test_unmapped_bool_stringified() -> None:
    coord = _FakeCoordinator(_cloud(status_flat={"OpaqueFlag": True}))
    entity = sp.DaedalusCloudSensor(coord, "status_flat", "OpaqueFlag", meta=None)
    assert entity.native_value == "true"


def test_capability_diagnostic_own_namespace() -> None:
    coord = _FakeCoordinator(_cloud(capabilities_flat={"Languages": 27}))
    entity = sp.DaedalusCloudSensor(coord, "capabilities_flat", "Languages", meta=lookup_cloud_key("Languages"))
    assert entity._attr_unique_id == "SN-TEST_cloudcap_Languages"
    assert entity._attr_entity_category == EntityCategory.DIAGNOSTIC
    assert entity.native_value == 27


# ---------------------------------------------------------- binary_sensor --


def test_alarm_maps_to_problem_binary_sensor() -> None:
    coord = _FakeCoordinator(_cloud(status_flat={"Descale": True}))
    entity = bs.DaedalusCloudBinarySensor(coord, "status_flat", "Descale", lookup_cloud_key("Descale"))
    assert entity.is_on is True
    assert entity._attr_device_class == BinarySensorDeviceClass.PROBLEM
    assert entity._attr_translation_key == "cloud_descale"
    assert entity._attr_unique_id == "SN-TEST_cloud_Descale"
    assert getattr(entity, "_attr_entity_category", None) is None


def test_peripheral_bool_is_diagnostic() -> None:
    coord = _FakeCoordinator(_cloud(status_flat={"Jug": "Ano"}))
    entity = bs.DaedalusCloudBinarySensor(coord, "status_flat", "Jug", lookup_cloud_key("Jug"))
    assert entity.is_on is True
    assert getattr(entity, "_attr_device_class", None) is None
    assert entity._attr_entity_category == EntityCategory.DIAGNOSTIC


def test_unparsable_bool_is_unknown() -> None:
    coord = _FakeCoordinator(_cloud(status_flat={"Descale": "garbage"}))
    entity = bs.DaedalusCloudBinarySensor(coord, "status_flat", "Descale", lookup_cloud_key("Descale"))
    assert entity.is_on is None


# ------------------------------------------------------------- entry setup --


def _entry(cloud_telemetry: bool = True) -> MagicMock:
    entry = MagicMock()
    entry.entry_id = "ENTRY"
    entry.data = {CONF_CLOUD_TELEMETRY: cloud_telemetry}
    return entry


def _hass(coord: _FakeCoordinator) -> MagicMock:
    hass = MagicMock()
    hass.data = {DOMAIN: {"ENTRY": coord}}
    return hass


_CLOUD = _cloud(status_flat={"OperatingState": "Standby", "Descale": True, "opaque": 1})


def test_sensor_setup_skips_binary_keys() -> None:
    coord = _FakeCoordinator(_CLOUD)
    added: list = []
    asyncio.run(sp.async_setup_entry(_hass(coord), _entry(), added.extend))
    cloud_entities = [e for e in added if isinstance(e, sp.DaedalusCloudSensor)]
    assert {e._key for e in cloud_entities} == {"OperatingState", "opaque"}
    assert coord.listeners == 0  # data already present → no listener


def test_binary_setup_adds_only_mapped_bools() -> None:
    coord = _FakeCoordinator(_CLOUD)
    added: list = []
    asyncio.run(bs.async_setup_entry(_hass(coord), _entry(), added.extend))
    cloud_entities = [e for e in added if isinstance(e, bs.DaedalusCloudBinarySensor)]
    assert [e._key for e in cloud_entities] == ["Descale"]
    assert coord.listeners == 0


def test_setup_without_cloud_flag() -> None:
    coord = _FakeCoordinator(_CLOUD)
    sensors: list = []
    asyncio.run(sp.async_setup_entry(_hass(coord), _entry(cloud_telemetry=False), sensors.extend))
    assert all(not isinstance(e, sp.DaedalusCloudSensor) for e in sensors)
    assert coord.listeners == 0


def test_sensor_setup_registers_listener_when_shadows_absent() -> None:
    coord = _FakeCoordinator({"connected": True})  # no cloud key yet
    added: list = []
    asyncio.run(sp.async_setup_entry(_hass(coord), _entry(), added.extend))
    assert coord.listeners == 1
    assert not any(isinstance(e, sp.DaedalusCloudSensor) for e in added)
