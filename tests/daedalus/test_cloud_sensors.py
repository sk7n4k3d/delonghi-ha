"""Entity rendering for mapped vs unknown cloud shadow keys (issue #18).

Uses the stubbed homeassistant bases from the root conftest — no HA runtime.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import PERCENTAGE, UnitOfTime
from homeassistant.helpers.entity import EntityCategory

from custom_components.delonghi_daedalus import binary_sensor as bs
from custom_components.delonghi_daedalus import sensor as sp
from custom_components.delonghi_daedalus.cloud import _directory_machines
from custom_components.delonghi_daedalus.cloud_keys import lookup_cloud_key
from custom_components.delonghi_daedalus.const import CONF_CLOUD_TELEMETRY, CONF_DRINK_VISIBILITY, DOMAIN
from custom_components.delonghi_daedalus.device import DaedalusDevice


class _FakeCoordinator:
    serial_number = "SN-TEST"

    def __init__(self, data: dict, **entry_data: str) -> None:
        self.entry = SimpleNamespace(data=entry_data)
        self.data = data
        self.device = DaedalusDevice(self)
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
    assert entity.extra_state_attributes == {"shadow_key": "OperatingState"}


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


def test_oversized_languages_uses_count_and_list_attribute() -> None:
    languages = [f"LANGUAGE_{index:02}" for index in range(27)]
    value = json.dumps(languages)
    coord = _FakeCoordinator(_cloud(capabilities_flat={"Languages": value}))
    entity = sp.DaedalusCloudSensor(coord, "capabilities_flat", "Languages")
    assert entity.native_value == len(languages)
    assert entity.extra_state_attributes == {
        "shadow_key": "Languages",
        "languages": languages,
    }

    compact_value = json.dumps(languages[:2])
    coord.data = _cloud(capabilities_flat={"Languages": compact_value})
    assert entity.native_value == compact_value
    assert "languages" not in entity.extra_state_attributes


@pytest.mark.parametrize("key", ["opaque", "Languages"])
def test_state_length_boundary_preserves_short_string_and_omits_oversized_raw_value(key: str) -> None:
    value = "x" * 255
    coord = _FakeCoordinator(_cloud(capabilities_flat={key: value}))
    entity = sp.DaedalusCloudSensor(coord, "capabilities_flat", key)
    assert entity.native_value == value
    assert entity.extra_state_attributes == {"shadow_key": key}

    coord.data = _cloud(capabilities_flat={key: value + "x"})
    assert entity.native_value is None
    assert entity.extra_state_attributes == {"shadow_key": key, "value_length": 256}


@pytest.mark.parametrize("key, payload", [("opaque", ["x" * 256]), ("Languages", [{"token": "x" * 256}])])
def test_oversized_json_does_not_expose_unknown_or_structured_values(key: str, payload: list) -> None:
    value = json.dumps(payload)
    coord = _FakeCoordinator(_cloud(capabilities_flat={key: value}))
    entity = sp.DaedalusCloudSensor(coord, "capabilities_flat", key)
    assert entity.native_value is None
    assert entity.extra_state_attributes == {
        "shadow_key": key,
        "value_length": len(value),
    }


# ---------------------------------------------------------- binary_sensor --


def test_alarm_maps_to_problem_binary_sensor() -> None:
    coord = _FakeCoordinator(_cloud(status_flat={"Descale": True}))
    entity = bs.DaedalusCloudBinarySensor(coord, "status_flat", "Descale", lookup_cloud_key("Descale"))
    assert entity.is_on is True
    assert entity._attr_device_class == BinarySensorDeviceClass.PROBLEM
    assert entity._attr_translation_key == "cloud_descale"
    assert entity._attr_unique_id == "SN-TEST_cloud_Descale"
    assert entity.extra_state_attributes == {"shadow_key": "Descale"}
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


def test_setup_without_cloud_flag() -> None:
    coord = _FakeCoordinator(_CLOUD)
    sensors: list = []
    asyncio.run(sp.async_setup_entry(_hass(coord), _entry(cloud_telemetry=False), sensors.extend))
    assert all(not isinstance(e, sp.DaedalusCloudSensor) for e in sensors)
    assert not coord.listeners


def test_sensor_setup_registers_listener_when_shadows_absent() -> None:
    coord = _FakeCoordinator({"connected": True})  # no cloud key yet
    added: list = []
    asyncio.run(sp.async_setup_entry(_hass(coord), _entry(), added.extend))
    assert len(coord.listeners) == 1
    assert not any(isinstance(e, sp.DaedalusCloudSensor) for e in added)


def test_device_info_on_all_platforms_without_changing_existing_entity_registry_keys() -> None:
    coord = _FakeCoordinator(
        _cloud(
            status_flat={"OperatingState": "Standby", "Descale": True, "opaque": 1, "peripherals.DripPresence": False},
            capabilities_flat={"Languages": 27},
        ),
        model="ECAM63070",
        machine_name="PRIVATE-THING",
    )
    added: list = []
    for platform in (sp, bs):
        asyncio.run(platform.async_setup_entry(_hass(coord), _entry(), added.extend))

    # HA finds an existing registry entry by platform/domain/unique ID before
    # attaching the device or applying defaults for any new registry entries.
    entities = {entity._attr_unique_id.removeprefix("SN-TEST_"): entity for entity in added}
    assert set(entities) == {
        "connection_id",
        "serial_number",
        "lan_ip",
        "connected",
        "cloud_OperatingState",
        "cloud_Descale",
        "cloud_opaque",
        "cloudcap_Languages",
        "cloud_peripherals.DripPresence",
    }
    device = entities["connected"]._attr_device_info
    assert all(entity._attr_device_info == device for entity in added)
    assert device["model"] == "PrimaDonna Aromatic ECAM630.70"
    assert device["manufacturer"] == "De'Longhi"
    assert "PRIVATE-THING" not in str(device)
    assert "SN-TEST" not in str(device)
    assert "connections" not in device
    assert all(domain == DOMAIN and len(identifier) == 64 for domain, identifier in device["identifiers"])
    assert len(device["identifiers"]) == 1

    legacy_device = bs.DaedalusConnectedSensor(_FakeCoordinator({}))._attr_device_info
    assert device["identifiers"] == legacy_device["identifiers"]
    assert legacy_device["name"] == "De'Longhi My Coffee Lounge"
    another = _FakeCoordinator({})
    another.serial_number = "SN-DIFFERENT"
    another.device = DaedalusDevice(another)
    assert bs.DaedalusConnectedSensor(another)._attr_device_info["identifiers"] != device["identifiers"]

    assert {
        key for key, entity in entities.items() if not getattr(entity, "_attr_entity_registry_enabled_default", True)
    } == {
        "connection_id",
        "serial_number",
        "lan_ip",
        "cloud_opaque",
        "cloudcap_Languages",
    }
    assert getattr(entities["cloud_peripherals.DripPresence"], "_attr_entity_category", None) is None
    assert entities["cloud_opaque"]._attr_entity_category == EntityCategory.DIAGNOSTIC


def test_cloud_availability_is_independent_of_the_lan_and_each_shadow() -> None:
    coord = _FakeCoordinator(
        {"connected": False, **_cloud(status_flat={"Descale": False}, capabilities_flat={"Languages": 27})}
    )
    lan = bs.DaedalusConnectedSensor(coord)
    session_id = sp.DaedalusConnectionIdSensor(coord)
    status = bs.DaedalusCloudBinarySensor(coord, "status_flat", "Descale", lookup_cloud_key("Descale"))
    capabilities = sp.DaedalusCloudSensor(coord, "capabilities_flat", "Languages")
    assert lan.available and not lan.is_on
    assert not session_id.available
    assert status.available and status.is_on is False
    assert capabilities.available

    coord.data = _cloud(status_flat={"Descale": False})
    assert status.available
    assert not capabilities.available

    coord.data = {"connected": True, "connection_id": 0}
    assert lan.available and lan.is_on
    assert session_id.available and session_id.native_value == 0
    assert not status.available


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


def test_maintenance_counters_discovered_once_per_load_when_reported() -> None:
    coord = _FakeCoordinator(_CLOUD)
    added: list = []
    entry = _entry()
    asyncio.run(sp.async_setup_entry(_hass(coord), entry, added.extend))

    def counters(entities):
        return {e._attr_translation_key: e for e in entities if isinstance(e, sp.DaedalusMaintenanceCounterSensor)}

    assert not counters(added)
    coord.update(_cloud(maintenance_counters={"descale_count": 0}))
    descale = counters(added)["descale_count"]
    assert descale.native_value == 0
    assert descale.available

    totals = {"descale_count": 1, "water_filter_registration_count": 0}
    for _ in range(2):
        coord.update(_cloud(maintenance_counters=totals))
    assert sum(isinstance(e, sp.DaedalusMaintenanceCounterSensor) for e in added) == 2
    for key, entity in counters(added).items():
        assert entity._attr_unique_id == f"SN-TEST_{key}"
        assert entity._attr_state_class == SensorStateClass.TOTAL_INCREASING
        assert entity.native_value == totals[key]
        assert entity.available

    coord.update(_cloud(maintenance_counters={"descale_count": 1}))
    assert not counters(added)["water_filter_registration_count"].available
    coord.update({})
    assert all(not entity.available for entity in counters(added).values())
    entry.async_on_unload.call_args.args[0]()
    assert not coord.listeners

    coord.update(_cloud(maintenance_counters=totals))
    reloaded: list = []
    asyncio.run(sp.async_setup_entry(_hass(coord), _entry(), reloaded.extend))
    assert [entity._attr_unique_id for entity in counters(reloaded).values()] == [
        entity._attr_unique_id for entity in counters(added).values()
    ]


def test_machine_settings_discovery_availability_and_metadata() -> None:
    coord = _FakeCoordinator(_cloud(status_flat={"Alarms.Filter": True}))
    added: list = []
    hass = _hass(coord)
    asyncio.run(sp.async_setup_entry(hass, _entry(), added.extend))
    asyncio.run(bs.async_setup_entry(hass, _entry(), added.extend))
    assert {entity._attr_translation_key for entity in added}.isdisjoint(
        {"descale_progress", "water_filter_usage", "grounds_container_fill", "auto_off_delay", "water_filter_installed"}
    )

    settings = {
        "descale_progress": 72,
        "water_filter_usage": 83,
        "grounds_container_fill": 0,
        "auto_off_delay": 30,
        "water_filter_installed": True,
    }
    for _ in range(2):
        coord.update(_cloud(status_flat={"Alarms.Filter": True}, machine_settings=settings))
    assert sum(entity._attr_translation_key in settings for entity in added) == len(settings)
    measurements = {
        entity._attr_translation_key: entity
        for entity in added
        if isinstance(entity, sp.DaedalusMaintenanceProgressSensor)
    }
    assert set(measurements) == {"descale_progress", "water_filter_usage", "grounds_container_fill"}
    for key, entity in measurements.items():
        assert entity._attr_unique_id == f"SN-TEST_{key}"
        assert entity._attr_native_unit_of_measurement == PERCENTAGE
        assert entity._attr_state_class == SensorStateClass.MEASUREMENT
        assert entity.native_value == settings[key]
        assert entity.available

    delay = next(entity for entity in added if isinstance(entity, sp.DaedalusAutoOffDelaySensor))
    assert delay._attr_unique_id == "SN-TEST_auto_off_delay"
    assert delay._attr_device_class == SensorDeviceClass.DURATION
    assert delay._attr_native_unit_of_measurement == UnitOfTime.MINUTES
    assert delay.native_value == 30
    assert delay.available

    installed = next(entity for entity in added if isinstance(entity, bs.DaedalusWaterFilterInstalledSensor))
    replacement = next(entity for entity in added if entity._attr_translation_key == "cloud_water_filter")
    assert installed._attr_unique_id == "SN-TEST_water_filter_installed"
    assert installed._attr_unique_id != replacement._attr_unique_id
    assert replacement._attr_device_class == BinarySensorDeviceClass.PROBLEM
    assert installed.available and installed.is_on is True

    coord.update(_cloud(machine_settings={"descale_progress": 0, "water_filter_installed": False}))
    assert measurements["descale_progress"].native_value == 0
    assert measurements["descale_progress"].available
    assert not measurements["water_filter_usage"].available
    assert not delay.available
    assert installed.available and installed.is_on is False
    coord.update({})
    assert not installed.available
    assert all(not entity.available for entity in measurements.values())


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


def test_drink_counters_are_discovered_only_on_report_and_remain_registered() -> None:
    coord = _FakeCoordinator(_CLOUD, model="ECAM47075")
    added: list = []
    asyncio.run(sp.async_setup_entry(_hass(coord), _entry(), added.extend))

    def drink_counters():
        return {
            entity.suggested_object_id: entity
            for entity in added
            if isinstance(entity, (sp.DaedalusRecipeBrewCountSensor, sp.DaedalusClusterBrewCountSensor))
        }

    assert drink_counters() == {}
    coord.update(_cloud(recipe_brew_counts={42: 0}, cluster_brew_counts={"hot_brew_count": 0}))
    assert set(drink_counters()) == {"recipe_42_brew_count", "hot_brew_count"}
    for entity in drink_counters().values():
        assert entity.available
        assert entity.native_value == 0
        assert entity._attr_state_class == SensorStateClass.TOTAL_INCREASING
        assert entity._attr_unique_id == f"SN-TEST_{entity.suggested_object_id}"
    recipe = drink_counters()["recipe_42_brew_count"]
    assert recipe._attr_entity_registry_enabled_default is False
    assert recipe._attr_extra_state_attributes == {"recipe_id": 42}
    assert recipe._attr_icon == "mdi:coffee-outline"
    assert drink_counters()["hot_brew_count"]._attr_icon == "mdi:coffee"
    assert getattr(drink_counters()["hot_brew_count"], "_attr_entity_registry_enabled_default", True)

    report = _cloud(recipe_brew_counts={7: 1}, cluster_brew_counts={"cold_brew_count": 0})
    coord.update(report)
    coord.update(report)
    assert len([entity for entity in added if isinstance(entity, type(recipe))]) == 2
    assert len(drink_counters()) == 4
    assert recipe.native_value is None
    assert not recipe.available
    assert not drink_counters()["hot_brew_count"].available
    assert drink_counters()["cold_brew_count"].available
    assert drink_counters()["cold_brew_count"]._attr_icon == "mdi:snowflake"

    coord.update({})
    assert all(not entity.available for entity in drink_counters().values())
    coord.update(_cloud(recipe_brew_counts={42: 2}, cluster_brew_counts={"hot_brew_count": 1}))
    assert drink_counters()["recipe_42_brew_count"] is recipe
    assert recipe.available and recipe.native_value == 2
    assert not drink_counters()["cold_brew_count"].available


def test_drink_counters_depend_on_cloud_telemetry() -> None:
    report = _cloud(recipe_brew_counts={42: 1}, cluster_brew_counts={"hot_brew_count": 1})
    coord = _FakeCoordinator(report)
    added: list = []
    asyncio.run(sp.async_setup_entry(_hass(coord), _entry(cloud_telemetry=False), added.extend))
    coord.update(report)
    assert not any(isinstance(e, (sp.DaedalusRecipeBrewCountSensor, sp.DaedalusClusterBrewCountSensor)) for e in added)


@pytest.mark.parametrize(
    ("model", "capabilities", "named"),
    [
        ("ECAM47075", {}, True),
        (" ecam470.75.txb ", {}, True),
        (None, {"MachineModelUi": "ECAM470.75TXB"}, True),
        (None, {"MachineModelUI": "ECAM470.75TXB"}, True),
        (None, {"MachineModelUi": "ECAM470.75TXB", "MachineModelUI": "ECAM63070"}, False),
        (None, {"MachineModelUI": "206.3"}, False),
        ("ECAM63070", {"MachineModelUI": "ECAM470.75.TXB"}, False),
        ("ECAM470.75.B", {}, False),
        ("ECAM47076", {}, False),
        ("Eletta Ultra", {"MachineModelUI": "ECAM470.75.TXB"}, False),
        (None, {}, False),
    ],
)
def test_recipe_mapping_uses_verified_model_without_changing_entity_identity(model, capabilities, named):
    cloud = _cloud(recipe_brew_counts={7: 8}, capabilities_flat=capabilities)
    coord = _FakeCoordinator(cloud, model=model)
    recipe = sp.DaedalusRecipeBrewCountSensor(coord, recipe_id=7)
    assert recipe._attr_translation_key == ("recipe_cappuccino_brew_count" if named else "recipe_brew_count")
    assert recipe._attr_unique_id == "SN-TEST_recipe_7_brew_count"
    assert recipe.suggested_object_id == "recipe_7_brew_count"
    assert recipe.native_value == 8

    other = _FakeCoordinator(cloud, model=model)
    other.serial_number = "SN-SECOND"
    other_recipe = sp.DaedalusRecipeBrewCountSensor(other, recipe_id=7)
    assert other_recipe._attr_unique_id == "SN-SECOND_recipe_7_brew_count"


@pytest.mark.parametrize("directory_code", ["ECAM470.75.TXB", "ECAM47075", "ECAM47075TXB"])
def test_verified_directory_product_codes_reach_recipe_names(directory_code):
    (machine,) = _directory_machines([{"serialNumber": "SN-TEST", "machineModel": directory_code}])
    assert machine.model == directory_code
    coord = _FakeCoordinator(_cloud(recipe_brew_counts={7: 1}), model=machine.model)
    added: list = []
    asyncio.run(sp.async_setup_entry(_hass(coord), _entry(), added.extend))
    (recipe,) = [entity for entity in added if isinstance(entity, sp.DaedalusRecipeBrewCountSensor)]
    assert recipe._attr_translation_key == "recipe_cappuccino_brew_count"


def test_recipe_limit_keeps_existing_registry_entries_and_high_ids(monkeypatch):
    monkeypatch.setattr(sp, "_MAX_RECIPE_SENSORS", 2)
    records = [
        SimpleNamespace(
            unique_id="SN-TEST_recipe_123_brew_count",
            domain="sensor",
            platform=DOMAIN,
            entity_id="sensor.user_renamed_recipe",
            disabled_by="user",
        ),
        SimpleNamespace(unique_id="OTHER_recipe_0_brew_count", domain="sensor", platform=DOMAIN),
    ]
    monkeypatch.setattr(
        sp,
        "entity_registry",
        SimpleNamespace(async_get=lambda _hass: None, async_entries_for_config_entry=lambda *_: records),
    )
    high_id = 4294967295
    coord = _FakeCoordinator(_cloud(recipe_brew_counts={high_id: 2}))
    added: list = []
    asyncio.run(sp.async_setup_entry(_hass(coord), _entry(), added.extend))

    def recipe_ids(entities):
        return [
            entity._attr_extra_state_attributes["recipe_id"]
            for entity in entities
            if isinstance(entity, sp.DaedalusRecipeBrewCountSensor)
        ]

    assert recipe_ids(added) == [high_id]
    coord.update(_cloud(recipe_brew_counts={123: 1, 42: 1, 43: 1}))
    assert recipe_ids(added) == [high_id, 123]

    records.append(SimpleNamespace(unique_id=f"SN-TEST_recipe_{high_id}_brew_count", domain="sensor", platform=DOMAIN))
    reloaded = _FakeCoordinator(_cloud(recipe_brew_counts={high_id: 3, 123: 2, 14: 1}))
    reloaded_entities: list = []
    asyncio.run(sp.async_setup_entry(_hass(reloaded), _entry(), reloaded_entities.extend))
    assert recipe_ids(reloaded_entities) == [123, high_id]


@pytest.mark.parametrize(
    ("translation_file", "generic_name", "cold_name"),
    [
        ("strings.json", "Recipe 42 brew count", "Cold brew count"),
        ("translations/en.json", "Recipe 42 brew count", "Cold brew count"),
        ("translations/fr.json", "Nombre de préparations de la recette 42", "Nombre de préparations froides"),
        ("translations/ro.json", "Număr de preparări pentru rețeta 42", "Număr de preparări reci"),
    ],
)
def test_drink_counter_translations(translation_file, generic_name, cold_name):
    translations = json.loads((Path(sp.__file__).parent / translation_file).read_text(encoding="utf-8"))
    names = translations["entity"]["sensor"]
    coord = _FakeCoordinator(_CLOUD, model="ECAM47075")
    generic = sp.DaedalusRecipeBrewCountSensor(coord, recipe_id=42)
    assert names[generic._attr_translation_key]["name"].format(**generic._attr_translation_placeholders) == generic_name
    assert names["hot_brew_count"]["name"]
    assert names["cold_brew_count"]["name"] == cold_name
    for recipe_id, translation_key in {
        7: "recipe_cappuccino_brew_count",
        8: "recipe_latte_macchiato_brew_count",
        29: "recipe_cafe_con_leche_brew_count",
        33: "recipe_caffe_crema_brew_count",
        35: "recipe_black_eye_brew_count",
        52: "recipe_iced_latte_macchiato_brew_count",
    }.items():
        named = sp.DaedalusRecipeBrewCountSensor(coord, recipe_id=recipe_id)
        assert named._attr_translation_key == translation_key
        assert names[translation_key]["name"].format()


def test_visible_drinks_sensor_appears_on_first_valid_report() -> None:
    coord = _FakeCoordinator(_CLOUD)
    entry = _entry()
    entry.data[CONF_DRINK_VISIBILITY] = True
    added: list = []
    asyncio.run(sp.async_setup_entry(_hass(coord), entry, added.extend))
    assert not any(isinstance(entity, sp.DaedalusVisibleDrinksSensor) for entity in added)

    coord.update(_cloud(visible_drinks_count=0))
    coord.update(_cloud(visible_drinks_count=0))
    (sensor,) = [entity for entity in added if isinstance(entity, sp.DaedalusVisibleDrinksSensor)]
    assert sensor._attr_unique_id == "SN-TEST_visible_drinks_count"
    assert sensor.available
    assert sensor.native_value == 0
    coord.update(_cloud())
    assert not sensor.available


def test_time_buckets_are_an_opt_in_diagnostic_with_real_zero_attributes() -> None:
    coord = _FakeCoordinator(_CLOUD)
    added: list = []
    asyncio.run(sp.async_setup_entry(_hass(coord), _entry(), added.extend))

    coord.update(_cloud(brewing_statistics={"brew_count": 2}, brewing_hours={}))
    assert not any(isinstance(entity, sp.DaedalusBrewingHoursSensor) for entity in added)
    buckets = {"Time0_2": 0, "Time12_14": 2}
    coord.update(_cloud(brewing_hours=buckets))
    coord.update(_cloud(brewing_hours=buckets))
    (histogram,) = [entity for entity in added if isinstance(entity, sp.DaedalusBrewingHoursSensor)]
    assert histogram._attr_unique_id == "SN-TEST_brewing_hours"
    assert histogram._attr_entity_category == EntityCategory.DIAGNOSTIC
    assert histogram._attr_entity_registry_enabled_default is False
    assert histogram.native_value == 2
    assert histogram.available
    assert histogram.extra_state_attributes == buckets

    coord.update(_cloud(brewing_statistics={"brew_count": 3}, brewing_hours={}))
    assert histogram.native_value is None
    assert not histogram.available
    coord.update(_cloud(brewing_hours={"Time0_2": 0}))
    assert histogram.available
    assert histogram.extra_state_attributes == {"Time0_2": 0}


@pytest.mark.parametrize(
    ("model", "capabilities", "named"),
    [
        ("ECAM63070", {"MachineModelUI": "ECAM470.75.TXB"}, True),
        (" ecam630.70 ", {}, True),
        (None, {"MachineModelUi": "ECAM630.70"}, True),
        (None, {"MachineModelUI": "ECAM63070"}, True),
        (None, {"MachineModelUi": "ECAM630.70", "MachineModelUI": "ECAM63070"}, True),
        (None, {"MachineModelUi": "ECAM630.70", "MachineModelUI": "ECAM470.75.TXB"}, False),
        (None, {"MachineModelUI": "206.3"}, False),
        (None, {"OtherModelUI": "ECAM63070"}, False),
        ("206.3", {"MachineModelUI": "ECAM63070"}, False),
        ("PrimaDonna Aromatic ECAM630.70", {"MachineModelUI": "ECAM63070"}, False),
        ("ECAM470.75.TXB", {"MachineModelUI": "ECAM63070"}, False),
        ("ECAM630.70.TB", {}, False),
        ("ECAM630.75", {}, False),
    ],
)
def test_aromatic_recipe_labels_require_verified_product_code(model, capabilities, named):
    report = _cloud(recipe_brew_counts={1: 3, 15: 7, 42: 0}, capabilities_flat=capabilities)
    coord = _FakeCoordinator(report, model=model)
    added: list = []
    asyncio.run(sp.async_setup_entry(_hass(coord), _entry(), added.extend))
    recipes = {
        entity._attr_extra_state_attributes["recipe_id"]: entity
        for entity in added
        if isinstance(entity, sp.DaedalusRecipeBrewCountSensor)
    }

    assert recipes.keys() == {1, 15, 42}
    for recipe_id, name_key in {
        1: "recipe_espresso_brew_count",
        15: "recipe_cappuccino_mix_brew_count",
        42: None,
    }.items():
        recipe = recipes[recipe_id]
        assert recipe._attr_unique_id == f"SN-TEST_recipe_{recipe_id}_brew_count"
        assert recipe.suggested_object_id == f"recipe_{recipe_id}_brew_count"
        if named and name_key is not None:
            assert recipe._attr_translation_key == name_key
            assert not getattr(recipe, "_attr_translation_placeholders", None)
        else:
            assert recipe._attr_translation_key == "recipe_brew_count"
            assert recipe._attr_translation_placeholders == {"recipe_id": str(recipe_id)}


def test_aromatic_label_keeps_earlier_recipe_registry_identity(monkeypatch):
    generic = sp.DaedalusRecipeBrewCountSensor(_FakeCoordinator(_CLOUD), recipe_id=15)
    assert generic._attr_translation_key == "recipe_brew_count"
    record = SimpleNamespace(
        unique_id=generic._attr_unique_id,
        domain="sensor",
        platform=DOMAIN,
        entity_id="sensor.breakfast_preparations",
        name="Breakfast preparations",
        disabled_by="user",
    )
    registry = SimpleNamespace(async_get=lambda _hass: None, async_entries_for_config_entry=lambda *_: [record])
    monkeypatch.setattr(sp, "entity_registry", registry)
    monkeypatch.setattr(sp, "_MAX_RECIPE_SENSORS", 1)

    coord = _FakeCoordinator(_cloud(recipe_brew_counts={1: 8, 15: 13}), model="ECAM63070")
    added: list = []
    asyncio.run(sp.async_setup_entry(_hass(coord), _entry(), added.extend))
    (named,) = [entity for entity in added if isinstance(entity, sp.DaedalusRecipeBrewCountSensor)]

    assert named._attr_translation_key == "recipe_cappuccino_mix_brew_count"
    assert named._attr_unique_id == record.unique_id
    assert named.suggested_object_id == generic.suggested_object_id
    assert named._attr_state_class == generic._attr_state_class == SensorStateClass.TOTAL_INCREASING
    assert named.native_value == 13

    coord.update(_cloud(recipe_brew_counts={1: 8, 15: 14}))
    assert [entity for entity in added if isinstance(entity, sp.DaedalusRecipeBrewCountSensor)] == [named]
    assert named.native_value == 14


@pytest.mark.parametrize(
    ("translation_file", "espresso", "cappuccino_mix"),
    [
        ("strings.json", "Espresso brew count", "Cappuccino Mix brew count"),
        ("translations/en.json", "Espresso brew count", "Cappuccino Mix brew count"),
        ("translations/fr.json", "Nombre d’espressos", "Nombre de préparations de Cappuccino Mix"),
        ("translations/ro.json", "Număr de preparări de espresso", "Număr de preparări de Cappuccino Mix"),
    ],
)
def test_aromatic_recipe_count_translations(translation_file, espresso, cappuccino_mix):
    translations = json.loads((Path(sp.__file__).parent / translation_file).read_text(encoding="utf-8"))
    names = translations["entity"]["sensor"]
    coord = _FakeCoordinator(_CLOUD, model="ECAM63070")
    for recipe_id, expected in ((1, espresso), (15, cappuccino_mix)):
        recipe = sp.DaedalusRecipeBrewCountSensor(coord, recipe_id=recipe_id)
        assert names[recipe._attr_translation_key]["name"] == expected
