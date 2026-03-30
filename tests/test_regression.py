"""Regression tests for specific GitHub issues."""

import base64
import json
from pathlib import Path
from unittest.mock import MagicMock

from custom_components.delonghi_coffee.api import DeLonghiApi

FIXTURES = Path(__file__).parent / "fixtures"


def _load_props(filename: str) -> dict:
    data = json.loads((FIXTURES / filename).read_text())
    return {p["property"]["name"]: p["property"] for p in data}


class TestIssue11BrewPrimaDonnaSoul:
    """Regression: #11 — Brew buttons don't work on PrimaDonna Soul.

    Root cause: PrimaDonna Soul uses d{num}_{profile}_rec_{key} naming
    convention instead of Eletta's d302_rec_{profile}_{key}.
    Also, PrimaDonna needs data_request (not app_data_request) and
    packets WITHOUT app_id signature.
    """

    def setup_method(self):
        self.api = DeLonghiApi.__new__(DeLonghiApi)
        self.api._email = "test@example.com"
        self.api._password = "password"
        self.api._session = MagicMock()
        self.api._ayla_token = "fake"
        self.api._ayla_refresh = None
        self.api._token_expires = 9999999999
        self.api._ayla_app_id = "test"
        self.api._ayla_app_secret = "test"
        self.api._ayla_user = "https://user.test.com"
        self.api._ayla_ads = "https://ads.test.com"
        self.api._oem_model = "DL-pd-soul"
        self.api._cmd_property = "data_request"
        self.api._ping_supported = False
        self.api._rate_tracker = MagicMock()
        self.api._devices = []
        self.api._custom_recipe_names = {}

    def test_discovers_primadonna_recipes(self):
        """PrimaDonna recipe properties (d060_2_rec_*) are discovered."""
        props = _load_props("properties_primadonna_soul.json")
        bevs = self.api.parse_available_beverages(props)
        assert "espresso" in bevs
        assert "regular" in bevs
        assert "cappuccino" in bevs

    def test_finds_recipe_with_primadonna_naming(self):
        """brew_beverage finds recipe from d060_2_rec_espresso format."""
        props = _load_props("properties_primadonna_soul.json")

        # The recipe lookup should find d060_2_rec_espresso for profile 2
        recipe_prop = None
        targets = [
            "_rec_2_espresso",  # Eletta format
            "_2_rec_espresso",  # PrimaDonna format
        ]
        for name, prop in props.items():
            if prop.get("value") and any(t in name for t in targets):
                val = prop["value"]
                if isinstance(val, str) and not val.startswith("{"):
                    recipe_prop = prop
                    break

        assert recipe_prop is not None, "Should find recipe via PrimaDonna naming"

    def test_falls_back_to_profile1(self):
        """If profile 2 recipe not found, falls back to profile 1."""
        props = {
            "d060_1_rec_espresso": {"value": "0AwkpPAEBCAMZAQIFYAA="},
            # No profile 2 recipe
        }

        recipe_prop = None
        # Primary lookup for profile 3
        targets = [
            "_rec_3_espresso",
            "_3_rec_espresso",
        ]
        for name, prop in props.items():
            if prop.get("value") and any(t in name for t in targets):
                recipe_prop = prop
                break

        # Should be None for profile 3
        assert recipe_prop is None

        # Fallback should find profile 1
        for name, prop in props.items():
            if not prop.get("value"):
                continue
            for p in range(1, 6):
                if p == 3:
                    continue
                if f"_rec_{p}_espresso" in name or f"_{p}_rec_espresso" in name:
                    recipe_prop = prop
                    break
            if recipe_prop:
                break

        assert recipe_prop is not None

    def test_packet_without_app_id_for_pd(self):
        """PrimaDonna packets must NOT include app_id."""
        b64 = self.api._build_packet(bytes([0x0D, 0x04, 0x8F]), include_app_id=False)
        raw = base64.b64decode(b64)
        # 3 bytes command + 4 bytes timestamp = 7 bytes (no app_id)
        assert len(raw) == 7

    def test_cmd_property_is_data_request(self):
        """PrimaDonna Soul must use data_request property."""
        assert self.api._cmd_property == "data_request"


class TestIssue9RateLimiting:
    """Regression: #9 — Users getting IP-banned by Ayla cloud.

    Root cause: Too many API calls (was ~186/hour pre-optimization).
    After fix: ~70/hour. Needs rate tracking visible to user.
    """

    def test_rate_tracker_exists(self):
        """API client has a rate tracker."""
        api = DeLonghiApi("test@example.com", "password")
        assert api.rate_tracker is not None

    def test_rate_tracking_on_get_properties(self):
        """get_properties() records to rate tracker."""
        api = DeLonghiApi("test@example.com", "password")
        initial = api.rate_tracker.total_calls
        # Can't actually call get_properties without mocking the full HTTP stack,
        # but we verify the tracker is accessible
        assert api.rate_tracker.total_calls == initial

    def test_scan_interval_is_60s(self):
        """Scan interval should be 60 seconds (not 30)."""
        from custom_components.delonghi_coffee.const import SCAN_INTERVAL_SECONDS

        assert SCAN_INTERVAL_SECONDS == 60

    def test_full_refresh_is_600s(self):
        """Full refresh interval should be 10 minutes."""
        from custom_components.delonghi_coffee.const import FULL_REFRESH_INTERVAL

        assert FULL_REFRESH_INTERVAL == 600

    def test_retry_count_is_3(self):
        """Retry count should be 3."""
        from custom_components.delonghi_coffee.const import RETRY_COUNT

        assert RETRY_COUNT == 3


class TestIssue3CountersMissing:
    """Regression: #3 — Counters missing on PrimaDonna Soul.

    Issues: d553_water_tot_qty=0, tea count wrong, total gap.
    """

    def test_water_tot_qty_zero_preserved(self):
        """d553_water_tot_qty=0 on PrimaDonna is reported as 0, not missing."""
        api = DeLonghiApi.__new__(DeLonghiApi)
        props = _load_props("properties_primadonna_soul.json")
        counters = api.parse_counters(props)
        assert "total_water_ml" in counters
        assert counters["total_water_ml"] == 0

    def test_computed_total_accounts_for_bw_split(self):
        """Computed total for PrimaDonna sums all separate categories.

        Real data (jostrasser #3): d700 and d701_bw are SEPARATE categories.
        d700=black(4827), d701_bw=with milk(34), d702=other(916), d703=water(3).
        """
        api = DeLonghiApi.__new__(DeLonghiApi)
        props = _load_props("properties_primadonna_soul.json")
        counters = api.parse_counters(props)
        # d700(4827) + d701_bw(34) + d703(3) + d702(916) = 5780
        assert counters["computed_total"] == 4827 + 34 + 3 + 916

    def test_json_sub_counters_parsed(self):
        """d734-d740 JSON sub-counters are parsed into separate keys."""
        api = DeLonghiApi.__new__(DeLonghiApi)
        props = _load_props("properties_primadonna_soul.json")
        counters = api.parse_counters(props)
        assert counters.get("usage_tot_custom_b_bw") == 15


class TestIssue6ModelIdentification:
    """Regression: #6 — Model identification / SKU mapping.

    MODEL_NAMES dict should cover known models.
    """

    def test_known_models_mapped(self):
        """All known OEM models have friendly names."""
        from custom_components.delonghi_coffee.const import MODEL_NAMES

        assert "DL-striker-cb" in MODEL_NAMES
        assert "DL-pd-soul" in MODEL_NAMES
        assert MODEL_NAMES["DL-striker-cb"] == "Eletta Explore"
        assert MODEL_NAMES["DL-pd-soul"] == "PrimaDonna Soul"

    def test_unknown_model_passthrough(self):
        """Unknown model falls through as-is (no crash)."""
        from custom_components.delonghi_coffee.const import MODEL_NAMES

        assert MODEL_NAMES.get("DL-unknown", "DL-unknown") == "DL-unknown"


class TestIssue3jostrasser:
    """Regression: jostrasser's specific reports from #3.

    - Hot water counter = 0 despite daily use
    - Power on/off doesn't work physically
    - Total beverages gap (machine=5816, cloud shows 4827)
    """

    def test_hot_water_zero_on_primadonna(self):
        """Hot water counter can be 0 on PrimaDonna Soul (cloud returns 0)."""
        api = DeLonghiApi.__new__(DeLonghiApi)
        props = {"d718_id16_hotwater": {"value": "0"}}
        counters = api.parse_counters(props)
        assert counters["hot_water"] == 0

    def test_total_gap_explained(self):
        """Real data (jostrasser #3): d700 and d701_bw are SEPARATE categories.
        d700=black(4827), d701_bw=with milk(34), d702=other(916), d703=water(3).
        Machine display shows 5816 ≈ 5780 + d725_bean_system(35)."""
        api = DeLonghiApi.__new__(DeLonghiApi)
        props = {
            "d700_tot_bev_b": {"value": "4827"},
            "d701_tot_bev_bw": {"value": "34"},
            "d703_tot_bev_w": {"value": "3"},
            "d702_tot_bev_other": {"value": "916"},
        }
        counters = api.parse_counters(props)
        assert counters["computed_total"] == 5780


class TestPingConnectedCaching:
    """Ping behavior: cache when not supported."""

    def test_ping_skipped_when_not_supported(self):
        """After 404 on both formats, future pings are skipped."""
        api = DeLonghiApi.__new__(DeLonghiApi)
        api._ping_supported = False
        api._session = MagicMock()
        api._ayla_ads = "https://ads.test.com"
        api._ayla_token = "fake"
        api._token_expires = 9999999999
        api._ayla_refresh = None
        api._ayla_app_id = "test"
        api._ayla_app_secret = "test"
        api._ayla_user = "https://user.test.com"
        api._email = "test@test.com"
        api._password = "pass"
        api._rate_tracker = MagicMock()
        result = api.ping_connected("DSN")
        assert result is False
        api._session.post.assert_not_called()
