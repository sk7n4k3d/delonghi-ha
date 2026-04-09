"""Test translation file integrity."""

import json
from pathlib import Path

import pytest

TRANSLATIONS_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "delonghi_coffee" / "translations"
TRANSLATION_FILES = sorted(TRANSLATIONS_DIR.glob("*.json"))


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
