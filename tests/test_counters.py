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
        assert counters["total_black_beverages"] == 4827  # d700 (real: jostrasser)
        assert counters["total_bw_beverages"] == 34  # d701_bw (with milk)
        assert counters["total_water_beverages"] == 3  # d703

    def test_total_beverages_aliases_computed(self):
        """PrimaDonna has no d701_tot_bev_b, so total_beverages is aliased
        from computed_total — otherwise the Total Beverages sensor would be
        permanently unknown (reported in issue #3 by @jostrasser)."""
        counters = self.api.parse_counters(self.props)
        assert counters["total_beverages"] == counters["computed_total"]

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
        """Computed total for PrimaDonna sums all separate categories.

        Real data (jostrasser issue #3): d700=4827, d701_bw=34, d702=916, d703=3.
        d700 and d701_bw are SEPARATE (not superset).
        """
        counters = self.api.parse_counters(self.props)
        # d700=4827 + d701_bw=34 + d703=3 + d702=916 = 5780
        assert counters["computed_total"] == 4827 + 34 + 3 + 916

    def test_descale_progress_primadonna(self):
        """Descale progress from d580_service_parameters."""
        counters = self.api.parse_counters(self.props)
        assert counters["descale_progress"] == 50  # 500/1000 * 100


class TestPrimaDonnaSoulReal:
    """Real PrimaDonna Soul layout (from issue #3 @jostrasser's full debug log).

    On the real firmware, d733-d748 are **individual integer counters** —
    not JSON aggregates like the Eletta. The custom-beverage total lives in
    its own `d741_tot_custom_b_bw` property and there is no
    `d580_service_parameters`.
    """

    def setup_method(self):
        self.api = DeLonghiApi.__new__(DeLonghiApi)
        self.props = _load_props("properties_primadonna_soul_real.json")

    def test_core_totals_present(self):
        counters = self.api.parse_counters(self.props)
        assert counters["total_black_beverages"] == 52
        assert counters["total_bw_beverages"] == 15
        assert counters["total_water_beverages"] == 8
        # d702 is a plain integer 0 on PrimaDonna — should map to other_tot_bev_other
        assert counters["other_tot_bev_other"] == 0

    def test_custom_beverages_from_d741(self):
        """Resolves @jostrasser's 'Coffee Custom Beverages = unknown'."""
        counters = self.api.parse_counters(self.props)
        assert counters["usage_tot_custom_b_bw"] == 4

    def test_new_primadonna_drinks(self):
        """Cortado, Long Black, Travel Mug are PrimaDonna-only counters."""
        counters = self.api.parse_counters(self.props)
        assert counters["cortado"] == 0
        assert counters["long_black"] == 0
        assert counters["travel_mug"] == 0

    def test_modified_and_aborted_counters(self):
        """d742 / d747 / d748 are actionable usage stats."""
        counters = self.api.parse_counters(self.props)
        assert counters["beverages_modified"] == 12
        assert counters["beverages_aborted"] == 5
        assert counters["beverages_doubled"] == 3

    def test_bean_system_usage(self):
        """d721-d726 bean profile usage counters."""
        counters = self.api.parse_counters(self.props)
        assert counters["bean_system_5_uses"] == 35
        assert counters["bean_system_1_uses"] == 0

    def test_total_beverages_aliased(self):
        """With no d701_tot_bev_b the 'Total Beverages' sensor still works."""
        counters = self.api.parse_counters(self.props)
        assert counters["total_beverages"] == counters["computed_total"]
        assert counters["computed_total"] == 52 + 15 + 8 + 0  # d700+d701_bw+d703+d702

    def test_descale_progress_absent(self):
        """No d580 on PrimaDonna — descale_progress is intentionally missing."""
        counters = self.api.parse_counters(self.props)
        assert "descale_progress" not in counters

    def test_d733_d740_not_parsed_as_json(self):
        """The PrimaDonna d733+ integer values must not poison the counters dict
        with stray mug_/taste_/water_qty_ keys."""
        counters = self.api.parse_counters(self.props)
        bad_prefixes = ("mug_", "taste_", "iced_", "mug_bev_", "mug_iced_", "cold_brew_", "water_qty_")
        for key in counters:
            for p in bad_prefixes:
                assert not key.startswith(p), f"leaked JSON prefix: {key}"


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
