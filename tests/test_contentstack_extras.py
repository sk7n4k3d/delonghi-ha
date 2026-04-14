"""Additional ContentStack coverage: error paths, skipping, pagination.

The base ``test_contentstack.py`` stubs ``_cs_get`` directly. This file
exercises ``_cs_get`` itself (line 78 onwards) plus the edge-case loops
inside ``fetch_drink_catalog`` / ``fetch_bean_adapt`` that the base
tests skip, and the full ``fetch_coffee_beans`` pagination flow.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import requests

from custom_components.delonghi_coffee import contentstack
from custom_components.delonghi_coffee.contentstack import (
    _cs_get,
    _int,
    fetch_bean_adapt,
    fetch_coffee_beans,
    fetch_drink_catalog,
)


def _mock_response(json_payload: dict[str, Any]) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = json_payload
    return resp


class TestCsGetDirect:
    """Exercise the low-level HTTP helper — covers line 78 + 88-90."""

    def test_with_query_serialises_to_json(self):
        captured: dict[str, Any] = {}

        def fake_get(url: str, headers: Any, params: Any, timeout: Any):
            captured["params"] = params
            return _mock_response({"entries": [{"drink_id": "1"}]})

        with patch.object(contentstack.requests, "get", side_effect=fake_get):
            entries = _cs_get("prod_drink", query={"title": {"$regex": "ECAM63050"}})

        assert entries == [{"drink_id": "1"}]
        # json.dumps happened — the query is a string, not the dict.
        assert isinstance(captured["params"]["query"], str)
        assert '"ECAM63050"' in captured["params"]["query"]

    def test_without_query_omits_query_param(self):
        captured: dict[str, Any] = {}

        def fake_get(url: str, headers: Any, params: Any, timeout: Any):
            captured["params"] = params
            return _mock_response({"entries": []})

        with patch.object(contentstack.requests, "get", side_effect=fake_get):
            result = _cs_get("coffee_bean", limit=50, skip=100)

        assert result == []
        assert "query" not in captured["params"]
        assert captured["params"]["limit"] == 50
        assert captured["params"]["skip"] == 100

    def test_request_exception_returns_empty(self):
        """Network failure is swallowed — covers 88-90."""
        with patch.object(
            contentstack.requests,
            "get",
            side_effect=requests.ConnectionError("boom"),
        ):
            assert _cs_get("prod_drink") == []

    def test_timeout_returns_empty(self):
        with patch.object(
            contentstack.requests,
            "get",
            side_effect=requests.Timeout("slow"),
        ):
            assert _cs_get("prod_drink") == []

    def test_http_error_returns_empty(self):
        """raise_for_status raising HTTPError goes through the except branch."""
        resp = MagicMock()
        resp.raise_for_status.side_effect = requests.HTTPError("500")
        with patch.object(contentstack.requests, "get", return_value=resp):
            assert _cs_get("prod_drink") == []

    def test_invalid_json_returns_empty(self):
        """ValueError from .json() is caught — empty list returned."""
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.side_effect = ValueError("not json")
        with patch.object(contentstack.requests, "get", return_value=resp):
            assert _cs_get("prod_drink") == []

    def test_empty_entries_key(self):
        """Response without 'entries' returns []."""
        with patch.object(
            contentstack.requests,
            "get",
            return_value=_mock_response({"something_else": []}),
        ):
            assert _cs_get("prod_drink") == []


class TestFetchDrinkCatalogEdgeCases:
    """Edge cases inside the drink parsing loop (146-166) + skipping (127)."""

    def test_invalid_drink_id_skipped(self):
        """Non-int drink_id lands in the except branch — covers 146-147."""
        entries = [
            {
                "drink_id": "not-a-number",
                "title": "Espresso ECAM63050",
                "ingredients": [],
            },
            {
                "drink_id": "5",
                "title": "Americano ECAM63050",
                "ingredients": [],
            },
        ]
        with patch.object(contentstack, "_cs_get", return_value=entries):
            catalog = fetch_drink_catalog("ECAM63050")

        assert 5 in catalog
        assert len(catalog) == 1

    def test_drink_id_zero_skipped(self):
        """drink_id == 0 is a sentinel — covers 149."""
        entries = [
            {"drink_id": "0", "title": "Placeholder ECAM63050", "ingredients": []},
            {"drink_id": "9", "title": "Real ECAM63050", "ingredients": []},
        ]
        with patch.object(contentstack, "_cs_get", return_value=entries):
            catalog = fetch_drink_catalog("ECAM63050")

        assert 0 not in catalog
        assert 9 in catalog

    def test_ingredient_without_name_skipped(self):
        """Nameless ingredient — covers 155."""
        entries = [
            {
                "drink_id": "3",
                "title": "Macchiato ECAM63050",
                "ingredients": [
                    {"name": "", "minval": "10", "maxval": "50", "defval": "20"},
                    {"name": "milk", "minval": "5", "maxval": "100", "defval": "30"},
                ],
            }
        ]
        with patch.object(contentstack, "_cs_get", return_value=entries):
            catalog = fetch_drink_catalog("ECAM63050")

        assert len(catalog[3]["ingredients"]) == 1
        assert catalog[3]["ingredients"][0]["name"] == "milk"

    def test_ingredient_invalid_int_skipped(self):
        """Non-numeric minval lands in the except branch — covers 165-166."""
        entries = [
            {
                "drink_id": "4",
                "title": "Latte ECAM63050",
                "ingredients": [
                    {"name": "coffee", "minval": "oops", "maxval": "80", "defval": "40"},
                    {"name": "milk", "minval": "5", "maxval": "90", "defval": "30"},
                ],
            }
        ]
        with patch.object(contentstack, "_cs_get", return_value=entries):
            catalog = fetch_drink_catalog("ECAM63050")

        # Only the valid ingredient survives.
        ingredients = catalog[4]["ingredients"]
        assert len(ingredients) == 1
        assert ingredients[0]["name"] == "milk"

    def test_model_name_extends_candidates(self):
        """Different model_name adds new candidates — covers 109-111."""
        calls: list[str] = []

        def fake_get(content_type: str, query: dict[str, Any] | None = None, **_: Any):
            calls.append(query["title"]["$regex"] if query else "")
            return []

        # sku ECAM63099 is supported; model_name ECAM47080 adds another family.
        with patch.object(contentstack, "_cs_get", side_effect=fake_get):
            result = fetch_drink_catalog("ECAM63099", model_name="ECAM47080")

        assert result == {}
        # The ECAM47 patterns are appended after the ECAM63 ones.
        assert "ECAM47080" in calls
        assert "ECAM63099" in calls
        # ECAM63 pattern comes first (sku is probed first).
        assert calls.index("ECAM63099") < calls.index("ECAM47080")

    def test_mixed_supported_unsupported_patterns_skip_unsupported(self):
        """Pattern not in supported families is skipped — covers 127."""
        # Inject an unsupported candidate into the list via model_name.
        calls: list[str] = []

        def fake_get(content_type: str, query: dict[str, Any] | None = None, **_: Any):
            pattern = query["title"]["$regex"] if query else ""
            calls.append(pattern)
            return []

        # sku unsupported, model_name supported — the sku candidates are
        # generated but filtered by is_family_supported inside the loop.
        # To hit line 127 we need at least one pattern pass the early
        # allowlist guard while another fails it. That happens when sku
        # is ECAM63099 (supported) and model_name yields ECAM37040
        # (unsupported). Both get into `patterns`, and line 127 triggers
        # on the ECAM37040 iteration.
        with patch.object(contentstack, "_cs_get", side_effect=fake_get):
            fetch_drink_catalog("ECAM63099", model_name="ECAM37040")

        # ECAM37* variants never hit the wire.
        for call in calls:
            assert not call.startswith("ECAM37")


class TestFetchBeanAdaptEdgeCases:
    """Mirror edge-case coverage for bean_adapt."""

    def test_model_name_extends_candidates(self):
        """Covers 187-189."""
        calls: list[str] = []

        def fake_get(content_type: str, query: dict[str, Any] | None = None, **_: Any):
            calls.append(query["title"]["$regex"] if query else "")
            return []

        with patch.object(contentstack, "_cs_get", side_effect=fake_get):
            fetch_bean_adapt("ECAM63099", model_name="ECAM47080")

        assert "ECAM63099" in calls
        assert "ECAM47080" in calls

    def test_mixed_supported_unsupported_skips_unsupported(self):
        """Covers 202 — unsupported patterns skipped inside bean_adapt loop."""
        calls: list[str] = []

        def fake_get(content_type: str, query: dict[str, Any] | None = None, **_: Any):
            calls.append(query["title"]["$regex"] if query else "")
            return []

        with patch.object(contentstack, "_cs_get", side_effect=fake_get):
            fetch_bean_adapt("ECAM63099", model_name="ECAM37040")

        for call in calls:
            assert not call.startswith("ECAM37")

    def test_no_entries_returns_none(self):
        """Empty results → debug log + None — covers 207-213."""
        with patch.object(contentstack, "_cs_get", return_value=[]):
            result = fetch_bean_adapt("ECAM63099", model_name="ECAM47099")

        assert result is None


class TestFetchCoffeeBeans:
    """Pagination + parsing — covers line 250 break and the loop body."""

    def test_empty_response_breaks_immediately(self):
        """First page empty — break on line 250."""
        with patch.object(contentstack, "_cs_get", return_value=[]) as mock_get:
            beans = fetch_coffee_beans()

        assert beans == []
        assert mock_get.call_count == 1

    def test_single_page_parses(self):
        """Page smaller than limit → single fetch, parsed entries."""
        entry = {
            "name": "House Blend",
            "roaster": "DeLabs",
            "roaster_id": "42",
            "roasting_level": "medium",
            "coffee_type": "arabica",
            "botany": "bourbon",
            "acidity": "3",
            "bitterness": "2",
            "body_level": "4",
            "taste_hint": "chocolatey",
            "description": "balanced",
            "image": "url",
            "buy_from": "store",
        }
        with patch.object(contentstack, "_cs_get", return_value=[entry]) as mock_get:
            beans = fetch_coffee_beans(limit=100)

        assert len(beans) == 1
        assert beans[0]["name"] == "House Blend"
        assert beans[0]["acidity"] == 3
        # len(entries) < limit → loop breaks after one call.
        assert mock_get.call_count == 1

    def test_multi_page_pagination(self):
        """Full page → second call → partial page → break."""
        # Generator returns a full page first, then a partial one.
        pages = [
            [{"name": f"bean{i}", "title": f"t{i}"} for i in range(3)],
            [{"name": "last"}],  # partial → break after processing.
        ]

        def fake_get(content_type: str, query: Any = None, limit: int = 100, skip: int = 0):
            assert content_type == "coffee_bean"
            # Use the skip offset to choose the page.
            page_idx = skip // limit
            return pages[page_idx] if page_idx < len(pages) else []

        with patch.object(contentstack, "_cs_get", side_effect=fake_get):
            beans = fetch_coffee_beans(limit=3)

        assert len(beans) == 4
        assert beans[-1]["name"] == "last"

    def test_name_falls_back_to_title(self):
        """Missing 'name' → uses 'title'."""
        entry = {"title": "Fallback"}
        with patch.object(contentstack, "_cs_get", return_value=[entry]):
            beans = fetch_coffee_beans(limit=100)

        assert beans[0]["name"] == "Fallback"
        # Defaults on every other field.
        assert beans[0]["roaster"] == ""
        assert beans[0]["acidity"] == 0


class TestSafeIntHelper:
    """Pin the ``_int`` helper used across parsing."""

    def test_none_returns_zero(self):
        assert _int(None) == 0

    def test_string_digit_converts(self):
        assert _int("42") == 42

    def test_invalid_string_returns_zero(self):
        assert _int("oops") == 0

    def test_unsupported_type_returns_zero(self):
        assert _int(object()) == 0

    def test_int_passthrough(self):
        assert _int(7) == 7
