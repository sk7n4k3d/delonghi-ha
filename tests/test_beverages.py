"""Test beverage discovery for both property naming conventions."""

import json
from pathlib import Path

from custom_components.delonghi_coffee.api import DeLonghiApi

FIXTURES = Path(__file__).parent / "fixtures"


def _load_props(filename: str) -> dict:
    data = json.loads((FIXTURES / filename).read_text())
    return {p["property"]["name"]: p["property"] for p in data}


class TestElettaBeverageDiscovery:
    """Eletta Explore: d302_rec_{profile}_{key} format."""

    def setup_method(self):
        self.api = DeLonghiApi.__new__(DeLonghiApi)
        self.api._custom_recipe_names = {}

    def test_discovers_beverages(self):
        """Should find espresso, regular, cappuccino from Eletta props."""
        props = _load_props("properties_eletta.json")
        bevs = self.api.parse_available_beverages(props)
        assert "espresso" in bevs
        assert "regular" in bevs
        assert "cappuccino" in bevs

    def test_excludes_numeric_keys(self):
        """Purely numeric keys (e.g. '1') should be filtered."""
        props = {"d302_rec_2_1": {"value": "base64data"}}
        bevs = self.api.parse_available_beverages(props)
        assert "1" not in bevs

    def test_excludes_priority_props(self):
        """Properties with 'priority' in name should be excluded."""
        props = {"d302_rec_priority_list": {"value": "some_data"}}
        bevs = self.api.parse_available_beverages(props)
        assert len(bevs) == 0

    def test_excludes_custom_from_rec_parsing(self):
        """Custom recipes parsed separately, not from _rec_ pattern."""
        props = {
            "d240_rec_custom_1": {"value": "base64data"},
            "d302_rec_2_espresso": {"value": "base64data"},
        }
        bevs = self.api.parse_available_beverages(props)
        assert "espresso" in bevs
        assert "custom_1" in bevs


class TestPrimaDonnaSoulBeverageDiscovery:
    """PrimaDonna Soul: d{num}_{profile}_rec_{key} format."""

    def setup_method(self):
        self.api = DeLonghiApi.__new__(DeLonghiApi)
        self.api._custom_recipe_names = {}

    def test_discovers_beverages(self):
        """Should find espresso, regular, cappuccino from PrimaDonna props."""
        props = _load_props("properties_primadonna_soul.json")
        bevs = self.api.parse_available_beverages(props)
        assert "espresso" in bevs
        assert "regular" in bevs
        assert "cappuccino" in bevs

    def test_primadonna_naming_convention(self):
        """d{num}_{profile}_rec_{key} is parsed correctly."""
        props = {
            "d060_2_rec_espresso": {"value": "base64data"},
            "d070_1_rec_cappuccino": {"value": "base64data"},
        }
        bevs = self.api.parse_available_beverages(props)
        assert "espresso" in bevs
        assert "cappuccino" in bevs

    def test_default_profile_format(self):
        """d{num}_rec_{key} (no profile number) is also supported."""
        props = {"d060_rec_espresso": {"value": "base64data"}}
        bevs = self.api.parse_available_beverages(props)
        assert "espresso" in bevs


class TestBeverageEdgeCases:
    """Edge cases in beverage discovery."""

    def setup_method(self):
        self.api = DeLonghiApi.__new__(DeLonghiApi)
        self.api._custom_recipe_names = {}

    def test_empty_properties(self):
        """No properties returns empty list."""
        bevs = self.api.parse_available_beverages({})
        assert bevs == []

    def test_null_values_still_discovered(self):
        """Properties with null values are still discovered (machine supports it,
        recipe just hasn't been synced yet). brew_beverage handles missing recipes."""
        props = {"d302_rec_2_espresso": {"value": None}}
        bevs = self.api.parse_available_beverages(props)
        assert "espresso" in bevs

    def test_json_values_still_discovered(self):
        """Properties with JSON values are still discovered by name
        (value check happens at brew time, not discovery time)."""
        props = {"d302_rec_2_espresso": {"value": '{"some": "json"}'}}
        bevs = self.api.parse_available_beverages(props)
        assert "espresso" in bevs

    def test_deduplication(self):
        """Same beverage from multiple profiles counted once."""
        props = {
            "d060_1_rec_espresso": {"value": "base64data"},
            "d060_2_rec_espresso": {"value": "base64data"},
            "d060_3_rec_espresso": {"value": "base64data"},
        }
        bevs = self.api.parse_available_beverages(props)
        assert bevs.count("espresso") == 1

    def test_sorted_output(self):
        """Output is sorted alphabetically."""
        props = {
            "d302_rec_2_tea": {"value": "base64data"},
            "d302_rec_2_espresso": {"value": "base64data"},
            "d302_rec_2_americano": {"value": "base64data"},
        }
        bevs = self.api.parse_available_beverages(props)
        assert bevs == sorted(bevs)

    def test_iced_beverages(self):
        """Iced beverage keys are preserved."""
        props = {
            "d302_rec_2_i_americano": {"value": "base64data"},
            "d302_rec_2_i_cappuccino": {"value": "base64data"},
        }
        bevs = self.api.parse_available_beverages(props)
        assert "i_americano" in bevs
        assert "i_cappuccino" in bevs

    def test_cold_brew_beverages(self):
        """Cold brew keys (a_cb_, b_cb_, etc.) are preserved."""
        props = {
            "d302_rec_2_a_cb_coffee": {"value": "base64data"},
            "d302_rec_2_d_cb_latte": {"value": "base64data"},
        }
        bevs = self.api.parse_available_beverages(props)
        assert "a_cb_coffee" in bevs
        assert "d_cb_latte" in bevs
