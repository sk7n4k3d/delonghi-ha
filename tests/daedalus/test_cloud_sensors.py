"""Entity rendering for mapped vs unknown cloud shadow keys (issue #18).

Uses the stubbed homeassistant bases from the root conftest — no HA runtime.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from unittest.mock import MagicMock

import pytest
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.sensor import SensorStateClass
from homeassistant.helpers.entity import EntityCategory

from custom_components.delonghi_daedalus import binary_sensor as bs
from custom_components.delonghi_daedalus import sensor as sp
from custom_components.delonghi_daedalus.cloud_keys import lookup_cloud_key
from custom_components.delonghi_daedalus.const import CONF_CLOUD_TELEMETRY, DOMAIN


class _FakeCoordinator:
    serial_number = "SN-TEST"

    def __init__(self, data: dict) -> None:
        self.data = data
        self.listeners: list[Callable[[], None]] = []

    def async_add_listener(self, callback: Callable[[], None]) -> Callable[[], None]:
        self.listeners.append(callback)
        return lambda: self.listeners.remove(callback)

    def update(self, data: dict) -> None:
        self.data = data
        for callback in list(self.listeners):
            callback()


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
    assert len(coord.listeners) == 1


def test_binary_setup_adds_only_mapped_bools() -> None:
    coord = _FakeCoordinator(_CLOUD)
    added: list = []
    asyncio.run(bs.async_setup_entry(_hass(coord), _entry(), added.extend))
    cloud_entities = [e for e in added if isinstance(e, bs.DaedalusCloudBinarySensor)]
    assert [e._key for e in cloud_entities] == ["Descale"]
    assert len(coord.listeners) == 1


@pytest.mark.parametrize("platform", [sp, bs])
def test_setup_without_cloud_flag(platform) -> None:
    coord = _FakeCoordinator(_CLOUD)
    sensors: list = []
    asyncio.run(platform.async_setup_entry(_hass(coord), _entry(cloud_telemetry=False), sensors.extend))
    assert all(not isinstance(e, (sp.DaedalusCloudSensor, bs.DaedalusCloudBinarySensor)) for e in sensors)
    assert not coord.listeners


@pytest.mark.parametrize("capabilities_at_setup", [False, True])
def test_platforms_discover_staggered_shadows_and_late_keys(capabilities_at_setup: bool) -> None:
    capabilities = {"Languages": 27, "Jug": True}
    status = {"OperatingState": "Standby", "Descale": True}
    first = _cloud(capabilities_flat=capabilities)
    coord = _FakeCoordinator(first if capabilities_at_setup else {"connected": True})
    entries = [_entry(), _entry()]
    added: list = []
    asyncio.run(sp.async_setup_entry(_hass(coord), entries[0], added.extend))
    asyncio.run(bs.async_setup_entry(_hass(coord), entries[1], added.extend))

    def cloud_ids(entities):
        return [
            e._attr_unique_id for e in entities if isinstance(e, (sp.DaedalusCloudSensor, bs.DaedalusCloudBinarySensor))
        ]

    capabilities_ids = ["SN-TEST_cloudcap_Languages", "SN-TEST_cloudcap_Jug"]
    assert cloud_ids(added) == (capabilities_ids if capabilities_at_setup else [])
    assert len(coord.listeners) == 2

    coord.update(first)
    assert cloud_ids(added) == capabilities_ids
    second = _cloud(status_flat=status)
    coord.update(second)
    coord.update(second)  # LAN refresh reusing the cloud snapshot
    coord.update(_cloud(status_flat=dict(status)))  # separate fetch with the same keys
    status_ids = ["SN-TEST_cloud_OperatingState", "SN-TEST_cloud_Descale"]
    assert cloud_ids(added) == capabilities_ids + status_ids

    # Both sources have supplied keys. A later snapshot can still introduce
    # new fields in them, and previously seen keys retain their IDs.
    latest_status = {**status, "CurrMode": 3, "alarms.Waste": True}
    latest = _cloud(status_flat=latest_status, capabilities_flat=capabilities)
    coord.update(latest)
    late_ids = ["SN-TEST_cloud_CurrMode", "SN-TEST_cloud_alarms.Waste"]
    assert cloud_ids(added) == capabilities_ids + status_ids + late_ids

    for entry in entries:
        entry.async_on_unload.call_args.args[0]()
    assert not coord.listeners
    coord.update(_cloud(status_flat={**latest_status, "opaque": 1}))
    assert cloud_ids(added) == capabilities_ids + status_ids + late_ids

    coord.update(latest)
    reloaded: list = []
    asyncio.run(sp.async_setup_entry(_hass(coord), _entry(), reloaded.extend))
    asyncio.run(bs.async_setup_entry(_hass(coord), _entry(), reloaded.extend))
    assert len(coord.listeners) == 2
    assert sorted(cloud_ids(reloaded)) == sorted(cloud_ids(added))


def test_brewing_counters_discovered_when_optional_statistics_arrive() -> None:
    coord = _FakeCoordinator(_CLOUD)
    added: list = []
    asyncio.run(sp.async_setup_entry(_hass(coord), _entry(), added.extend))

    def counters():
        return {e._attr_translation_key: e for e in added if isinstance(e, sp.DaedalusBrewingStatisticSensor)}

    assert counters() == {}
    coord.update(_cloud(brewing_statistics={"stopped_brew_count": 0}))
    assert counters()["stopped_brew_count"].native_value == 0
    totals = {"brew_count": 12, "stopped_brew_count": 1, "brewing_error_count": 0}
    coord.update(_cloud(brewing_statistics=totals))
    coord.update(_cloud(brewing_statistics=totals))
    assert sum(isinstance(e, sp.DaedalusBrewingStatisticSensor) for e in added) == 3
    for key, value in totals.items():
        entity = counters()[key]
        assert entity._attr_unique_id == f"SN-TEST_{key}"
        assert entity._attr_state_class == SensorStateClass.TOTAL_INCREASING
        assert entity.native_value == value
        assert entity.available

    coord.update(_cloud(brewing_statistics={}))
    assert all(not entity.available for entity in counters().values())
    coord.update(_cloud(brewing_statistics=totals))
    assert all(entity.available for entity in counters().values())
    assert sum(isinstance(e, sp.DaedalusBrewingStatisticSensor) for e in added) == 3
