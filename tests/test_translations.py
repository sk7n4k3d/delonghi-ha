"""Test translation file integrity."""

import json
import re
from pathlib import Path

import pytest

COMPONENT_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "delonghi_coffee"
TRANSLATIONS_DIR = COMPONENT_DIR / "translations"
TRANSLATION_FILES = sorted(TRANSLATIONS_DIR.glob("*.json"))
STRINGS_JSON = COMPONENT_DIR / "strings.json"


def _flat_keys(data, prefix=""):
    """Flatten a nested dict into a set of dotted leaf keys."""
    keys = set()
    for k, v in data.items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            keys |= _flat_keys(v, key)
        else:
            keys.add(key)
    return keys


@pytest.mark.parametrize("path", TRANSLATION_FILES, ids=lambda p: p.name)
def test_valid_json(path):
    """Every translation file must be valid JSON."""
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)


@pytest.mark.parametrize("path", TRANSLATION_FILES, ids=lambda p: p.name)
def test_has_required_sections(path):
    """Every translation must have config + entity sections."""
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "config" in data, f"{path.name} missing 'config'"
    assert "entity" in data, f"{path.name} missing 'entity'"


@pytest.mark.parametrize("path", TRANSLATION_FILES, ids=lambda p: p.name)
def test_has_cancel_and_sync_buttons(path):
    """Every translation must have cancel_brew and sync_recipes buttons."""
    data = json.loads(path.read_text(encoding="utf-8"))
    buttons = data.get("entity", {}).get("button", {})
    assert "cancel_brew" in buttons, f"{path.name} missing cancel_brew button"
    assert "sync_recipes" in buttons, f"{path.name} missing sync_recipes button"


def test_en_is_reference():
    """English translation must have all expected entity types."""
    en = json.loads((TRANSLATIONS_DIR / "en.json").read_text(encoding="utf-8"))
    entity = en["entity"]
    assert "sensor" in entity
    assert "binary_sensor" in entity
    assert "button" in entity
    assert "switch" in entity
    assert "select" in entity


def test_all_languages_have_same_button_keys():
    """All translations must have the same set of button keys."""
    en = json.loads((TRANSLATIONS_DIR / "en.json").read_text(encoding="utf-8"))
    en_keys = set(en["entity"]["button"].keys())

    for path in TRANSLATION_FILES:
        if path.name == "en.json":
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        keys = set(data.get("entity", {}).get("button", {}).keys())
        assert keys == en_keys, f"{path.name} button keys differ from en.json: {en_keys - keys | keys - en_keys}"


def test_every_beverage_has_english_translation():
    """Every key in BEVERAGES must have a matching `brew_{key}` entry in en.json.

    Regression guard for #11: when a beverage is in BEVERAGES but has no
    translation, the button entity silently shows the raw translation key
    ("Brew Espresso Lungo") instead of the localised name. This test fails
    fast when someone adds a beverage to const.py without updating en.json.
    """
    from custom_components.delonghi_coffee.const import BEVERAGES

    en = json.loads((TRANSLATIONS_DIR / "en.json").read_text(encoding="utf-8"))
    button_keys = set(en["entity"]["button"].keys())

    missing = sorted(bev for bev in BEVERAGES if f"brew_{bev}" not in button_keys)
    assert not missing, (
        f"BEVERAGES keys without matching en.json translation: {missing}. "
        "Add `brew_<key>` entries to every translations/*.json file."
    )


def test_strings_json_matches_en_json():
    """strings.json is the source of truth for HA sync — it must hold exactly
    the same keys as translations/en.json (config + options + entity)."""
    strings = json.loads(STRINGS_JSON.read_text(encoding="utf-8"))
    en = json.loads((TRANSLATIONS_DIR / "en.json").read_text(encoding="utf-8"))
    s_keys = _flat_keys(strings)
    e_keys = _flat_keys(en)
    missing = sorted(e_keys - s_keys)
    extra = sorted(s_keys - e_keys)
    assert not missing and not extra, (
        f"strings.json out of sync with en.json — missing from strings.json: {missing}; extra in strings.json: {extra}"
    )


@pytest.mark.parametrize("path", TRANSLATION_FILES, ids=lambda p: p.name)
def test_full_key_parity_with_en(path):
    """Every language must carry the exact same key set as en.json — config,
    options and entity alike, not just buttons."""
    en = json.loads((TRANSLATIONS_DIR / "en.json").read_text(encoding="utf-8"))
    data = json.loads(path.read_text(encoding="utf-8"))
    en_keys = _flat_keys(en)
    lang_keys = _flat_keys(data)
    missing = sorted(en_keys - lang_keys)
    extra = sorted(lang_keys - en_keys)
    assert not missing and not extra, f"{path.name} key set differs from en.json — missing: {missing}; extra: {extra}"


# Platform modules that assign _attr_translation_key, mapped to their domain.
_PLATFORM_DOMAINS = {
    "sensor.py": "sensor",
    "binary_sensor.py": "binary_sensor",
    "button.py": "button",
    "switch.py": "switch",
    "select.py": "select",
}


def _translation_keys_used_in_code():
    """Collect every translation_key the platform code can assign, per domain.

    Covers static literals (``= "power"``), f-string patterns expanded through
    their source enumerations (``f"alarm_{bit}"`` → ALARMS, ``f"brew_{key}"`` →
    BEVERAGES) and the ``counter_key`` variable (→ COUNTER_SENSORS). Unknown
    patterns fail the test loudly instead of being silently skipped.
    """
    from custom_components.delonghi_coffee.const import ALARMS, BEVERAGES
    from custom_components.delonghi_coffee.sensor import COUNTER_SENSORS

    static_re = re.compile(r'_attr_translation_key\s*=\s*"([a-z0-9_]+)"')
    fstring_re = re.compile(r'_attr_translation_key\s*=\s*f"([a-z0-9_]+)\{')
    var_re = re.compile(r"_attr_translation_key\s*=\s*([a-z_][a-z0-9_]*)\s*$", re.MULTILINE)

    used: dict[str, set[str]] = {domain: set() for domain in _PLATFORM_DOMAINS.values()}
    for fname, domain in _PLATFORM_DOMAINS.items():
        src = (COMPONENT_DIR / fname).read_text(encoding="utf-8")
        used[domain].update(static_re.findall(src))
        for prefix in fstring_re.findall(src):
            if prefix == "alarm_":
                used[domain].update(f"alarm_{bit}" for bit in ALARMS)
            elif prefix == "brew_":
                used[domain].update(f"brew_{bev}" for bev in BEVERAGES)
            else:
                pytest.fail(f"{fname}: unhandled f-string translation_key prefix {prefix!r}")
        for var in var_re.findall(src):
            if var == "counter_key":
                used[domain].update(COUNTER_SENSORS)
            else:
                pytest.fail(f"{fname}: unhandled translation_key variable {var!r}")
    return used


def test_every_translation_key_in_code_exists_in_en():
    """Regression guard: a translation_key assigned in code but absent from
    en.json makes the entity silently display its raw key in the UI."""
    en = json.loads((TRANSLATIONS_DIR / "en.json").read_text(encoding="utf-8"))
    entity = en["entity"]
    for domain, keys in _translation_keys_used_in_code().items():
        available = set(entity.get(domain, {}))
        missing = sorted(keys - available)
        assert not missing, f"{domain} translation_keys used in code but missing from en.json: {missing}"
