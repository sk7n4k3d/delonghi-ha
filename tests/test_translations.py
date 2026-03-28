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
