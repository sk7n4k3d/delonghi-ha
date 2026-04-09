"""ContentStack drink/bean-adapt fetch tests.

Exercises the supported-family allowlist and progressive prefix fallback
introduced to fix issue #6 (PrimaDonna Soul gets zero drink metadata and
Eletta Explore variants with odd SKUs are missed by the exact-match
lookup).

All HTTP traffic is mocked — ContentStack is read-only and not stubbed
elsewhere in CI, so we patch the low-level ``_cs_get`` helper directly.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from custom_components.delonghi_coffee import contentstack
from custom_components.delonghi_coffee.contentstack import (
    _SUPPORTED_FAMILIES,
    _iter_pattern_candidates,
    fetch_bean_adapt,
    fetch_drink_catalog,
    is_family_supported,
)


def _drink_entry(drink_id: int, title: str) -> dict[str, Any]:
    return {
        "drink_id": str(drink_id),
        "title": title,
        "original_title": f"Drink {drink_id}",
        "cluster": [],
        "ingredients": [
            {"name": "coffee", "minval": "20", "maxval": "80", "defval": "40"},
        ],
    }


def _bean_adapt_entry(title: str) -> dict[str, Any]:
    return {
        "title": title,
        "technical_parameters": {},
        "contents": {},
    }


class TestPatternCandidates:
    """Cover the prefix-shortening helper used by both fetchers."""

    def test_ecam61075_shortens_down_to_ecam61(self):
        assert _iter_pattern_candidates("ECAM61075") == [
            "ECAM61075",
            "ECAM6107",
            "ECAM610",
            "ECAM61",
        ]

    def test_lower_case_input_is_normalised(self):
        assert _iter_pattern_candidates("ecam63050")[0] == "ECAM63050"

    def test_non_ecam_pattern_returned_verbatim(self):
        assert _iter_pattern_candidates("0132217129") == ["0132217129"]

    def test_empty_suffix_is_not_generated(self):
        # We stop at length 2 so "ECAM" alone never hits the network —
        # it would match every machine in the CMS.
        candidates = _iter_pattern_candidates("ECAM6")
        assert "ECAM" not in candidates
        assert candidates[-1] == "ECAM6"


class TestFamilySupport:
    """Sanity-check the allowlist that gates CMS traffic."""

    def test_ecam47_and_ecam63_are_supported(self):
        for prefix in _SUPPORTED_FAMILIES:
            assert is_family_supported(prefix)
            assert is_family_supported(f"{prefix}050")

    def test_ecam61_is_not_supported(self):
        assert not is_family_supported("ECAM61075")
        assert not is_family_supported("ECAM610")

    def test_ecam37_dinamica_is_not_supported(self):
        assert not is_family_supported("ECAM37040")

    def test_empty_pattern_is_not_supported(self):
        assert not is_family_supported("")


class TestFetchDrinkCatalogUnsupportedFamily:
    """Issue #6: PrimaDonna Soul (ECAM61075) must not hit the CMS."""

    def test_primadonna_soul_serial_skips_fetch(self):
        # jostrasser's ECAM serial from the latest-3.txt debug log.
        with patch.object(contentstack, "_cs_get") as mock_get:
            result = fetch_drink_catalog("ECAM61075")
            assert result == {}
            mock_get.assert_not_called()

    def test_primadonna_soul_model_name_skips_fetch(self):
        # TranscodeTable name "Prima Donna SOUL ECAM610.75" collapses to
        # ECAM61075 once the coordinator strips punctuation.
        with patch.object(contentstack, "_cs_get") as mock_get:
            result = fetch_drink_catalog("ECAM61075", model_name="ECAM61075")
            assert result == {}
            mock_get.assert_not_called()

    def test_dinamica_plus_skips_fetch(self):
        with patch.object(contentstack, "_cs_get") as mock_get:
            assert fetch_drink_catalog("ECAM37040") == {}
            mock_get.assert_not_called()

    def test_unsupported_family_bean_adapt_skips_fetch(self):
        with patch.object(contentstack, "_cs_get") as mock_get:
            assert fetch_bean_adapt("ECAM61075") is None
            mock_get.assert_not_called()


class TestFetchDrinkCatalogSupportedFamily:
    """Exact matches and progressive fallback for ECAM63 / ECAM47 hits."""

    def test_exact_match_returns_parsed_catalog(self):
        entries = [_drink_entry(1, "Espresso ECAM63050 0132250181 1.1-1.0-2.5")]
        with patch.object(contentstack, "_cs_get", return_value=entries) as mock_get:
            catalog = fetch_drink_catalog("ECAM63050")
        assert 1 in catalog
        assert catalog[1]["ingredients"][0] == {
            "name": "coffee",
            "min": 20,
            "max": 80,
            "default": 40,
        }
        # Exact match hits on the first call.
        assert mock_get.call_count == 1
        call_kwargs = mock_get.call_args.kwargs
        assert call_kwargs["query"] == {"title": {"$regex": "ECAM63050"}}

    def test_progressive_fallback_when_specific_sku_missing(self):
        """ECAM63099 doesn't exist → shorten to ECAM6309 → ECAM630 → hit."""
        calls: list[str] = []

        def fake_get(content_type: str, query: dict[str, Any] | None = None, **_: Any):
            pattern = query["title"]["$regex"] if query else ""
            calls.append(pattern)
            if pattern == "ECAM630":
                return [_drink_entry(42, "Espresso ECAM63050 0132250064 1.1-1.2-2.5")]
            return []

        with patch.object(contentstack, "_cs_get", side_effect=fake_get):
            catalog = fetch_drink_catalog("ECAM63099")

        assert 42 in catalog
        assert calls == ["ECAM63099", "ECAM6309", "ECAM630"]

    def test_progressive_fallback_bottoms_out_on_family(self):
        """If nothing shorter works we still bottom out at the 2-char family."""
        calls: list[str] = []

        def fake_get(content_type: str, query: dict[str, Any] | None = None, **_: Any):
            calls.append(query["title"]["$regex"] if query else "")
            return []

        with patch.object(contentstack, "_cs_get", side_effect=fake_get):
            catalog = fetch_drink_catalog("ECAM63099")

        assert catalog == {}
        # Every candidate is tried in order, bottoming out at "ECAM63".
        assert calls == ["ECAM63099", "ECAM6309", "ECAM630", "ECAM63"]

    def test_drink_catalog_skips_model_name_already_in_sku_candidates(self):
        """A redundant model_name doesn't duplicate calls."""
        entries = [_drink_entry(7, "Americano ECAM63050 0132250064 1.1-1.2-2.5")]
        with patch.object(contentstack, "_cs_get", return_value=entries) as mock_get:
            fetch_drink_catalog("ECAM63050", model_name="ECAM63050")
        # Single call — no duplicate probes for the same pattern.
        assert mock_get.call_count == 1


class TestFetchBeanAdaptSupportedFamily:
    """Mirror the drink catalog coverage for bean_adapt lookups."""

    def test_exact_match(self):
        with patch.object(
            contentstack,
            "_cs_get",
            return_value=[_bean_adapt_entry("Bean Adapt ECAM63050")],
        ) as mock_get:
            result = fetch_bean_adapt("ECAM63050")
        assert result is not None
        assert result["title"] == "Bean Adapt ECAM63050"
        assert mock_get.call_count == 1

    def test_progressive_fallback(self):
        calls: list[str] = []

        def fake_get(content_type: str, query: dict[str, Any] | None = None, **_: Any):
            pattern = query["title"]["$regex"] if query else ""
            calls.append(pattern)
            if pattern == "ECAM470":
                return [_bean_adapt_entry("Bean Adapt ECAM47080")]
            return []

        with patch.object(contentstack, "_cs_get", side_effect=fake_get):
            result = fetch_bean_adapt("ECAM47099")

        assert result is not None
        assert calls == ["ECAM47099", "ECAM4709", "ECAM470"]
