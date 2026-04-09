"""Button platform tests.

Focus on the pure beverage → button metadata resolver so we can verify the
full set of beverages advertised by PrimaDonna Soul end up with proper
names/icons, without having to spin up a Home Assistant instance.
"""

import json
from pathlib import Path

from custom_components.delonghi_coffee.const import BEVERAGES, resolve_beverage_meta

TRANSLATIONS_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "delonghi_coffee" / "translations"


# The actual list captured from jostrasser / lodzen's PrimaDonna Soul
# (see issue #11, latest-3.txt attachment). This is the real ECAM61075MB
# output of parse_available_beverages after the v1.4 naming-convention fix.
PRIMADONNA_SOUL_BEVERAGES = [
    "2x_espresso",
    "americano",
    "brew_over_ice",
    "caffelatte",
    "capp_doppio",
    "capp_reverse",
    "cappuccino",
    "coffee_pot",
    "cortado",
    "doppio",
    "espr_macchiato",
    "espresso",
    "flat_white",
    "hot_milk",
    "hot_water",
    "latte_macchiato",
    "long_black",
    "long_coffee",
    "mug_to_go",
    "regular",
    "tea",
]


class TestResolveBeverageMeta:
    """Unit tests for the resolve_beverage_meta helper."""

    def test_known_beverage_returns_const_entry(self):
        meta, is_known = resolve_beverage_meta("espresso", {})
        assert is_known is True
        assert meta["name"] == BEVERAGES["espresso"]["name"]
        assert meta["icon"] == BEVERAGES["espresso"]["icon"]

    def test_custom_recipe_name_wins(self):
        meta, is_known = resolve_beverage_meta("custom_1", {"custom_1": "Morning Booster"})
        assert is_known is True
        assert meta["name"] == "Morning Booster"
        assert meta["icon"] == "mdi:coffee-to-go"

    def test_custom_recipe_fallback_when_no_name(self):
        meta, is_known = resolve_beverage_meta("custom_2", {})
        assert is_known is False
        assert meta["name"] == "Custom 2"
        assert meta["icon"] == "mdi:coffee"

    def test_unknown_beverage_fallback(self):
        meta, is_known = resolve_beverage_meta("mystery_brew", {})
        assert is_known is False
        assert meta["name"] == "Mystery Brew"
        assert meta["icon"] == "mdi:coffee"

    def test_custom_recipe_copy_is_independent(self):
        """Mutating the returned dict must not affect BEVERAGES."""
        meta, _ = resolve_beverage_meta("espresso", {})
        meta["name"] = "Mutated"
        assert BEVERAGES["espresso"]["name"] != "Mutated"


class TestPrimaDonnaSoulButtonCoverage:
    """Regression #11: every PrimaDonna Soul beverage gets a proper button."""

    def test_21_beverages_count(self):
        """Jostrasser / lodzen report exactly 21 advertised beverages."""
        assert len(PRIMADONNA_SOUL_BEVERAGES) == 21

    def test_every_beverage_resolves_to_known_meta(self):
        """None of the PrimaDonna Soul beverages should hit the fallback."""
        unknown: list[str] = []
        for bev in PRIMADONNA_SOUL_BEVERAGES:
            _, is_known = resolve_beverage_meta(bev, {})
            if not is_known:
                unknown.append(bev)
        assert not unknown, f"Unknown PrimaDonna beverage keys: {unknown}"

    def test_every_beverage_has_english_translation(self):
        """Every PrimaDonna Soul beverage needs a brew_{key} entry in en.json."""
        en = json.loads((TRANSLATIONS_DIR / "en.json").read_text(encoding="utf-8"))
        button_keys = set(en["entity"]["button"].keys())
        missing = [bev for bev in PRIMADONNA_SOUL_BEVERAGES if f"brew_{bev}" not in button_keys]
        assert not missing, f"PrimaDonna Soul beverages without en translation: {missing}"

    def test_every_beverage_has_french_translation(self):
        fr = json.loads((TRANSLATIONS_DIR / "fr.json").read_text(encoding="utf-8"))
        button_keys = set(fr["entity"]["button"].keys())
        missing = [bev for bev in PRIMADONNA_SOUL_BEVERAGES if f"brew_{bev}" not in button_keys]
        assert not missing, f"PrimaDonna Soul beverages without fr translation: {missing}"
