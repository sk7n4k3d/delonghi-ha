"""Test model identification via TranscodeTable and serial number parsing."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from custom_components.delonghi_coffee.api import DeLonghiApi

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def _load_props(filename: str) -> dict:
    data = json.loads((FIXTURES / filename).read_text())
    return {p["property"]["name"]: p["property"] for p in data}


def _load_transcode_table() -> list[dict]:
    data = json.loads((FIXTURES / "transcode_table.json").read_text())
    return data["machines"]


class TestSerialNumberParsing:
    """Parse d270_serialnumber to extract SKU prefix."""

    def test_ecam_serial_extracts_model(self):
        """ECAM61075MB12345 → model prefix ECAM610.75."""
        info = DeLonghiApi.parse_serial_number("ECAM61075MB12345")
        assert info is not None
        assert info["raw"] == "ECAM61075MB12345"

    def test_ecam_serial_extracts_digits(self):
        """Numeric portion is extracted for SKU matching."""
        info = DeLonghiApi.parse_serial_number("ECAM45065S12345")
        assert info is not None
        assert "digits" in info

    def test_none_value_returns_none(self):
        """None input returns None."""
        assert DeLonghiApi.parse_serial_number(None) is None

    def test_empty_value_returns_none(self):
        """Empty string returns None."""
        assert DeLonghiApi.parse_serial_number("") is None

    def test_short_value_returns_partial(self):
        """Short values still return what we can parse."""
        info = DeLonghiApi.parse_serial_number("EC")
        assert info is not None
        assert info["raw"] == "EC"


class TestTranscodeTableMatching:
    """Match serial/OEM model against TranscodeTable.

    Credit: TranscodeTable approach from FrozenGalaxy/PyDeLonghiAPI.
    """

    def setup_method(self):
        self.table = _load_transcode_table()

    def test_fixture_has_machines(self):
        """TranscodeTable fixture has 180+ machines."""
        assert len(self.table) > 150

    def test_match_eletta_by_sku(self):
        """SKU 0132217129 matches Eletta Explore STRIKER_COLD-BREW."""
        match = DeLonghiApi.match_transcode_table(self.table, sku_digits="217129")
        assert match is not None
        assert "Eletta Explore" in match["name"]
        assert match["appModelId"] == "STRIKER_COLD-BREW"

    def test_match_pd_soul_by_sku(self):
        """SKU digits matching PrimaDonna Soul."""
        match = DeLonghiApi.match_transcode_table(self.table, sku_digits="217055")
        assert match is not None
        assert "SOUL" in match["name"].upper()
        assert match["appModelId"] == "PD_SOUL"

    def test_match_by_oem_model(self):
        """OEM model DL-striker-cb matches via appModelId mapping."""
        match = DeLonghiApi.match_transcode_table(self.table, oem_model="DL-striker-cb")
        assert match is not None
        assert "Eletta" in match["name"] or "Striker" in match["name"]

    def test_match_pd_soul_by_oem(self):
        """OEM model DL-pd-soul matches PrimaDonna Soul."""
        match = DeLonghiApi.match_transcode_table(self.table, oem_model="DL-pd-soul")
        assert match is not None
        assert "PD_SOUL" in match["appModelId"]

    def test_no_match_returns_none(self):
        """Unknown SKU/model returns None."""
        match = DeLonghiApi.match_transcode_table(self.table, sku_digits="999999")
        assert match is None

    def test_oem_fallback_when_sku_fails(self):
        """If SKU doesn't match, falls back to oem_model."""
        match = DeLonghiApi.match_transcode_table(
            self.table, sku_digits="999999", oem_model="DL-pd-soul"
        )
        assert match is not None
        assert "PD_SOUL" in match["appModelId"]

    def test_match_returns_capabilities(self):
        """Matched entry includes capability fields."""
        match = DeLonghiApi.match_transcode_table(self.table, sku_digits="217129")
        assert match is not None
        assert "nProfiles" in match
        assert "nStandardRecipes" in match
        assert "connectionType" in match
        assert "protocolVersion" in match

    def test_match_dinamica_plus(self):
        """Dinamica Plus matched by OEM model."""
        match = DeLonghiApi.match_transcode_table(self.table, oem_model="DL-dinamica-plus")
        assert match is not None
        assert "DINAMICA_PLUS" in match["appModelId"]


class TestIdentifyModel:
    """End-to-end model identification from properties."""

    def setup_method(self):
        self.api = DeLonghiApi.__new__(DeLonghiApi)
        self.api._oem_model = "DL-striker-cb"
        self.api._transcode_table = _load_transcode_table()
        self.api._model_info = None

    def test_identify_from_eletta_props(self):
        """Eletta Explore identified from serial + TranscodeTable."""
        props = _load_props("properties_eletta.json")
        info = self.api.identify_model(props)
        assert info is not None
        assert "Eletta" in info["name"]

    def test_identify_from_primadonna_props(self):
        """PrimaDonna Soul identified from serial + TranscodeTable."""
        self.api._oem_model = "DL-pd-soul"
        props = _load_props("properties_primadonna_soul.json")
        info = self.api.identify_model(props)
        assert info is not None
        assert "SOUL" in info["name"].upper() or "PD_SOUL" in info["appModelId"]

    def test_identify_caches_result(self):
        """Model info is cached after first identification."""
        props = _load_props("properties_eletta.json")
        info1 = self.api.identify_model(props)
        info2 = self.api.identify_model(props)
        assert info1 is info2  # Same object = cached

    def test_identify_without_serial_uses_oem(self):
        """If no serial property, falls back to OEM model."""
        props = {"d302_monitor_machine": {"value": "some_data"}}
        info = self.api.identify_model(props)
        assert info is not None

    def test_identify_without_table_uses_model_names(self):
        """If TranscodeTable not loaded, falls back to MODEL_NAMES."""
        self.api._transcode_table = None
        props = _load_props("properties_eletta.json")
        info = self.api.identify_model(props)
        assert info is not None
        assert info["name"] == "Eletta Explore"

    def test_model_info_property(self):
        """model_info property returns cached info."""
        props = _load_props("properties_eletta.json")
        self.api.identify_model(props)
        assert self.api.model_info is not None


class TestFetchTranscodeTable:
    """Test TranscodeTable HTTP fetch with caching."""

    def setup_method(self):
        self.api = DeLonghiApi.__new__(DeLonghiApi)
        self.api._session = MagicMock()
        self.api._transcode_table = None

    def test_fetch_success(self):
        """Successful fetch stores table."""
        table_data = {"result": {"code": 0}, "machines": [{"appModelId": "TEST", "product_code": "123"}]}
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = table_data
        self.api._session.post.return_value = mock_resp

        self.api.fetch_transcode_table()
        assert self.api._transcode_table is not None
        assert len(self.api._transcode_table) == 1

    def test_fetch_failure_leaves_none(self):
        """Failed fetch leaves table as None (graceful degradation)."""
        self.api._session.post.side_effect = Exception("Network error")
        self.api.fetch_transcode_table()
        assert self.api._transcode_table is None

    def test_fetch_cached(self):
        """Second fetch doesn't hit network if already cached."""
        self.api._transcode_table = [{"appModelId": "CACHED"}]
        self.api.fetch_transcode_table()
        self.api._session.post.assert_not_called()
