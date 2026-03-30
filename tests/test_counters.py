"""Test counter parsing for both Eletta and PrimaDonna Soul models."""

import json
from pathlib import Path

from custom_components.delonghi_coffee.api import DeLonghiApi

FIXTURES = Path(__file__).parent / "fixtures"


def _load_props(filename: str) -> dict:
    """Load properties fixture and convert to dict format."""
    data = json.loads((FIXTURES / filename).read_text())
    return {p["property"]["name"]: p["property"] for p in data}


class TestElettaCounters:
    """Counter parsing for Eletta Explore (d701_tot_bev_b = total)."""

    def setup_method(self):
        self.api = DeLonghiApi.__new__(DeLonghiApi)
        self.props = _load_props("properties_eletta.json")

    def test_total_beverages(self):
        """d701_tot_bev_b maps to total_beverages."""
        counters = self.api.parse_counters(self.props)
        assert counters["total_beverages"] == 1234

    def test_individual_counters(self):
        """Individual beverage counters are parsed correctly."""
        counters = self.api.parse_counters(self.props)
        assert counters["espresso"] == 300
        assert counters["coffee"] == 200
        assert counters["long_coffee"] == 50
        assert counters["doppio"] == 30
        assert counters["americano"] == 100
        assert counters["cappuccino"] == 80
        assert counters["latte_macchiato"] == 40
        assert counters["caffe_latte"] == 25
        assert counters["flat_white"] == 15
        assert counters["espresso_macchiato"] == 10
        assert counters["hot_milk"] == 5
        assert counters["cappuccino_doppio"] == 8
        assert counters["cappuccino_mix"] == 3
        assert counters["hot_water"] == 150
        assert counters["tea"] == 14

    def test_maintenance_counters(self):
        """Maintenance counters: grounds, descale, water, filter."""
        counters = self.api.parse_counters(self.props)
        assert counters["grounds_count"] == 1100
        assert counters["descale_count"] == 3
        assert counters["total_water_ml"] == 1742000
        assert counters["grounds_percentage"] == 45
        assert counters["filter_percentage"] == 78
        assert counters["filter_replacements"] == 2
        assert counters["water_through_filter_ml"] == 500000

    def test_descale_progress(self):
        """Descale progress calculated from d580_service_parameters."""
        counters = self.api.parse_counters(self.props)
        assert counters["descale_progress"] == 80  # 800/1000 * 100

    def test_computed_total_eletta(self):
        """Computed total for Eletta uses d701 (total_beverages) as base."""
        counters = self.api.parse_counters(self.props)
        assert "computed_total" in counters
        assert counters["computed_total"] >= counters["total_beverages"]


class TestPrimaDonnaSoulCounters:
    """Counter parsing for PrimaDonna Soul (d700/d701_bw/d703 split)."""

    def setup_method(self):
        self.api = DeLonghiApi.__new__(DeLonghiApi)
        self.props = _load_props("properties_primadonna_soul.json")

    def test_split_totals(self):
        """PrimaDonna has separate black/bw/water totals."""
        counters = self.api.parse_counters(self.props)
        assert counters["total_black_beverages"] == 3200  # d700
        assert counters["total_bw_beverages"] == 4827  # d701_bw
        assert counters["total_water_beverages"] == 500  # d703

    def test_no_total_beverages_key(self):
        """PrimaDonna doesn't have the Eletta-style total_beverages."""
        counters = self.api.parse_counters(self.props)
        assert "total_beverages" not in counters

    def test_json_counters(self):
        """JSON sub-counters from d734_tot_bev_usage."""
        counters = self.api.parse_counters(self.props)
        assert counters["usage_tot_custom_b_bw"] == 15
        assert counters["usage_tot_other"] == 3

    def test_iced_json_counters(self):
        """JSON iced beverage counters from d735."""
        counters = self.api.parse_counters(self.props)
        assert counters["iced_iced_americano"] == 10
        assert counters["iced_iced_latte"] == 5

    def test_cold_brew_json_counters(self):
        """JSON cold brew counters from d738."""
        counters = self.api.parse_counters(self.props)
        assert counters["cold_brew_cb_coffee"] == 8
        assert counters["cold_brew_cb_latte"] == 2

    def test_water_tot_qty_zero(self):
        """Issue #3: d553_water_tot_qty=0 on PrimaDonna Soul."""
        counters = self.api.parse_counters(self.props)
        assert counters["total_water_ml"] == 0

    def test_milk_clean_count(self):
        """PrimaDonna-specific milk clean counter."""
        counters = self.api.parse_counters(self.props)
        assert counters["milk_clean_count"] == 25

    def test_beverages_since_descale(self):
        """PrimaDonna-specific beverages since descale."""
        counters = self.api.parse_counters(self.props)
        assert counters["beverages_since_descale"] == 120

    def test_computed_total_primadonna(self):
        """Computed total for PrimaDonna uses d701_bw + d703 + d702."""
        counters = self.api.parse_counters(self.props)
        # d701_bw=4827 + d703=500 + d702.tot_bev_other=489 = 5816
        assert counters["computed_total"] == 5816

    def test_descale_progress_primadonna(self):
        """Descale progress from d580_service_parameters."""
        counters = self.api.parse_counters(self.props)
        assert counters["descale_progress"] == 50  # 500/1000 * 100


class TestCounterEdgeCases:
    """Edge cases in counter parsing."""

    def setup_method(self):
        self.api = DeLonghiApi.__new__(DeLonghiApi)

    def test_empty_properties(self):
        """No properties returns empty counters."""
        counters = self.api.parse_counters({})
        assert counters == {}

    def test_null_values(self):
        """Properties with null values are skipped."""
        props = {"d701_tot_bev_b": {"value": None}}
        counters = self.api.parse_counters(props)
        assert "total_beverages" not in counters

    def test_non_integer_values(self):
        """Non-integer values stored as-is."""
        props = {"d701_tot_bev_b": {"value": "not_a_number"}}
        counters = self.api.parse_counters(props)
        assert counters["total_beverages"] == "not_a_number"

    def test_json_invalid_json(self):
        """Invalid JSON in d733+ properties is silently ignored."""
        props = {"d733_tot_bev_counters": {"value": "{broken json"}}
        counters = self.api.parse_counters(props)
        # Should not crash, no sub-keys added
        assert not any(k.startswith("mug_") for k in counters)

    def test_json_not_string(self):
        """Non-string JSON properties are skipped."""
        props = {"d733_tot_bev_counters": {"value": 12345}}
        counters = self.api.parse_counters(props)
        assert not any(k.startswith("mug_") for k in counters)

    def test_descale_threshold_zero(self):
        """Zero threshold in descale calculation doesn't divide by zero."""
        props = {"d580_service_parameters": {"value": '{"last_4_water_calc_qty": 100, "last_4_calc_threshold": 0}'}}
        counters = self.api.parse_counters(props)
        # threshold=0 → the if guard prevents division
        assert "descale_progress" not in counters

    def test_descale_over_100(self):
        """Descale progress capped at 100%."""
        props = {"d580_service_parameters": {"value": '{"last_4_water_calc_qty": 2000, "last_4_calc_threshold": 1000}'}}
        counters = self.api.parse_counters(props)
        assert counters["descale_progress"] == 100

    def test_d702_as_integer(self):
        """d702 can be plain integer (not JSON) on some models."""
        props = {"d702_tot_bev_other": {"value": "42"}}
        counters = self.api.parse_counters(props)
        assert counters["computed_total"] == 42

    def test_d702_as_json_and_integer(self):
        """d702 as JSON takes priority over direct integer."""
        props = {
            "d702_tot_bev_other": {"value": '{"tot_bev_other": 100}'},
            "d701_tot_bev_bw": {"value": "500"},
        }
        counters = self.api.parse_counters(props)
        # other_tot_bev_other from JSON = 100
        assert counters["other_tot_bev_other"] == 100
