"""cloud_keys table: alias integrity, lookup, bool coercion, translations.

The table content is inferred from mimasek71's Eletta Ultra snapshot
(issue #18) — these tests guard the invariants (no alias collisions, every
known key translated) rather than the guessed raw names themselves.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from custom_components.delonghi_daedalus.cloud_keys import (
    CLOUD_KEYS,
    coerce_bool,
    lookup_cloud_key,
    normalize,
    plan_cloud_entities,
)

TRANSLATIONS_DIR = Path(__file__).resolve().parents[2] / "custom_components" / "delonghi_daedalus" / "translations"


def test_ids_unique() -> None:
    ids = [meta.key for meta in CLOUD_KEYS]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("meta", CLOUD_KEYS, ids=lambda m: m.key)
def test_alias_roundtrip(meta) -> None:
    """Every alias must resolve back to its own entry (catches collisions)."""
    for alias in (meta.key, *meta.aliases):
        found = lookup_cloud_key(alias)
        assert found is not None and found.key == meta.key, f"alias {alias!r} resolved to {found}"


def test_lookup_uses_last_dot_segment() -> None:
    assert lookup_cloud_key("alarms.Descale").key == "descale"
    assert lookup_cloud_key("MachineStatus.OperatingState").key == "operating_state"
    assert lookup_cloud_key("a.b.tank_presence").key == "tank_presence"


def test_lookup_unknown_returns_none() -> None:
    assert lookup_cloud_key("totally_unknown_field") is None
    assert lookup_cloud_key("") is None
    assert lookup_cloud_key("...") is None


def test_normalize() -> None:
    assert normalize("alarms.Descale") == "descale"
    assert normalize("  MixedCASE  ") == "mixedcase"
    assert normalize("with_underscore") == "withunderscore"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (True, True),
        (False, False),
        (1, True),
        (0, False),
        ("true", True),
        ("FALSE", False),
        ("Ano", True),
        ("Ne", False),
        (" 1 ", True),
        ("garbage", None),
        (None, None),
        ([], None),
    ],
)
def test_coerce_bool(raw, expected) -> None:
    assert coerce_bool(raw) is expected


def test_plan_partitions_platforms() -> None:
    cloud = {
        "status_flat": {"Descale": True, "OperatingState": "Standby", "opaque.field": 3},
        "capabilities_flat": {"Languages": 27},
    }
    binary = {(s, k) for s, k, _ in plan_cloud_entities(cloud, want_binary=True)}
    sensors = {(s, k, m is None) for s, k, m in plan_cloud_entities(cloud, want_binary=False)}
    assert binary == {("status_flat", "Descale")}
    assert sensors == {
        ("status_flat", "OperatingState", False),
        ("status_flat", "opaque.field", True),  # unknown → raw fallback sensor
        ("capabilities_flat", "Languages", False),
    }


def test_plan_drops_blank_keys() -> None:
    cloud = {"status_flat": {"   ": 1}}
    assert plan_cloud_entities(cloud, want_binary=True) == []
    assert plan_cloud_entities(cloud, want_binary=False) == []


def test_plan_caps_per_source() -> None:
    cloud = {"status_flat": {f"opaque{i}": i for i in range(200)}}
    assert len(plan_cloud_entities(cloud, want_binary=False)) <= 80


@pytest.mark.parametrize("lang", ["en", "fr"])
def test_translations_cover_every_known_key(lang: str) -> None:
    data = json.loads((TRANSLATIONS_DIR / f"{lang}.json").read_text(encoding="utf-8"))
    entity = data["entity"]
    for meta in CLOUD_KEYS:
        section = "binary_sensor" if meta.binary else "sensor"
        entry = entity.get(section, {}).get(f"cloud_{meta.key}")
        assert entry is not None, f"{lang}.json missing {section}.cloud_{meta.key}"
        assert entry["name"].strip(), f"{lang}.json empty name for cloud_{meta.key}"


# Real key names from mimasek71's anonymised raw shadow dump (issue #18,
# 2026-11-14) — the regression guard for the guessed-alias replacement.
_REAL_DUMP_KEYS = {
    "Status": "operating_state",
    "CurrMode": "current_mode",
    "CurrFuncProgress": "function_progress",
    "ExitBeverageWithError": "dispensing_error",
    "BeverageDispensing": "dispensing_value",
    "StepDescr": "step_description",
    "alarms.TankLevel": "tank_level",
    "alarms.Waste": "grounds_container",
    "alarms.Descale": "descale",
    "alarms.Filter": "water_filter",
    "alarms.GroundFine": "ground_fine",
    "alarms.General": "general_fault",
    "alarms.HeaterNTC": "heater_ntc",
    "alarms.Imbocco": "imbocco",
    "alarms.MotorPos": "motor_pos",
    "alarms.Drain": "drain",
    "alarms.Bubble": "bubble",
    "alarms.TankPresence": "tank_presence",
    "alarms.Clean": "cleaning",
    "alarms.BrokenGrinder": "broken_grinder",
    "alarms.NoCoffee": "no_coffee_beans",
    "alarms.Infuser": "infuser",
    "alarms.UIMemory": "ui_memory",
    "alarms.WifiDown": "wifi_down",
    "alarms.Fan": "fan",
    "alarms.BrewUnitClean": "brew_unit_cleaning",
    "alarms.Fridge": "fridge",
    "peripherals.Switch": "switch",
    "peripherals.WaterSpout": "spout_presence",
    "peripherals.Jug": "jug",
    "peripherals.TankPresence": "peripheral_tank_presence",
    "peripherals.WaterTankLow": "low_water",
    "peripherals.DripPresence": "drip_tray_presence",
    "peripherals.HotJug": "hot_jug",
    "peripherals.Grinder1ZSW": "grinder1_zsw",
    "peripherals.MotDwSw": "motor_down_switch",
    "peripherals.MotUpSw": "motor_up_switch",
    "accessories.None": "accessory_none",
    "accessories.Water": "accessory_water",
    "accessories.IDFHot": "idf_hot",
    "accessories.CleanHot": "clean_hot",
    "accessories.IDFCold": "idf_cold",
    "accessories.CleanCold": "clean_cold",
    "accessories.SteamWand": "steam_wand",
    "MachineModelUI": "machine_model",
    "MachineModelMain": "machine_model_main",
    "FWUIVersion": "display_firmware",
    "FWMainboardVersion": "mainboard_firmware",
    "FWWiFiVersion": "wifi_firmware",
    "DLAWSAgentVersion": "cloud_agent_version",
    "GFXVersion": "graphics_version",
    "ExtraShot": "extra_shot",
    "MugToGo": "mug_to_go",
    "DrinkClusters": "drink_groups",
    "CustomizedDrinks": "custom_drinks",
    "CoffeeMoments": "coffee_moments",
    "BeanAdaptType": "bean_adapt_type",
    "Grinder": "grinder_type",
    "MachineUserProfiles": "machine_user_profiles",
    "MachineUserProfileIcons": "user_profile_icons",
    "MachineUserProfileColours": "profile_colors",
    "MachineStatisticTypes": "statistics_types",
    "Hopper": "bean_hopper_type",
    "Languages": "languages",
    "Dictionaries": "dictionaries",
    "SupportedAccessories": "supported_accessories",
    "DisplayType": "display_type",
    "CoffeeTemperatures": "coffee_temperatures",
    "AutoTurnOffTimeInMinutes": "auto_turn_off_options",
    "WaterHardness": "water_hardness_options",
    "SoundEnable": "sound_options",
    "EnergySavingMode": "eco_mode_options",
    "Granulometries": "grind_levels",
    "DrainWater": "water_drain_support",
    "CustomRecipeNameLen": "recipe_name_length",
    "BeanSystemNameLen": "bean_profile_name_length",
    "UserProfileNameLen": "user_profile_name_length",
}


@pytest.mark.parametrize(("raw_key", "expected"), sorted(_REAL_DUMP_KEYS.items()))
def test_real_dump_keys_resolve(raw_key: str, expected: str) -> None:
    found = lookup_cloud_key(raw_key)
    assert found is not None and found.key == expected, f"{raw_key} -> {found}"


def test_path_alias_beats_segment_alias() -> None:
    """alarms vs peripherals TankPresence must map to distinct entries."""
    assert lookup_cloud_key("alarms.TankPresence").key == "tank_presence"
    assert lookup_cloud_key("peripherals.TankPresence").key == "peripheral_tank_presence"


def test_sensitive_keys_never_planned() -> None:
    """BLE passcode / MAC / SN must not become entities in either platform."""
    cloud = {
        "status_flat": {"BlePassCode": "123456", "MAC": "08:aa", "SN": "AC000", "Status": "Standby"},
        "capabilities_flat": {"BlePassCode": "x", "Languages": 27},
    }
    planned = {k for s, k, _ in plan_cloud_entities(cloud, want_binary=False)}
    planned |= {k for s, k, _ in plan_cloud_entities(cloud, want_binary=True)}
    assert planned == {"Status", "Languages"}
