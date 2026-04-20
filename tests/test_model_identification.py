"""Test model identification via TranscodeTable and serial number parsing."""

import json
from pathlib import Path
from unittest.mock import MagicMock

from custom_components.delonghi_coffee.api import DeLonghiApi

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


class TestBinarySerialDecoder:
    """Modern ECAM firmware (PrimaDonna Soul et al.) exposes d270_serialnumber
    as base64-encoded binary: 6-byte header + 19 ASCII chars + 3-byte trailer.

    The 19-char payload structure (per De'Longhi service documentation):
        CCCCCC EE AA MM GG L PPPP
        where C=codice, E=esecuzione, A=anno, M=mese, G=giorno,
              L=lettera, P=produzione
    """

    # Real-world sample from @lodzen (PrimaDonna Soul, DSN AC000W040821014,
    # production 2025-07-01). Structure: 217055 ZZ 25 07 01 3 0134.
    PRIMADONNA_SOUL_B64 = "0BuhDwDNMjE3MDU1WloyNTA3MDEzMDEzNAD9pg=="

    def test_binary_serial_extracts_sku_digits(self):
        """Base64 binary serial exposes the first 6 SKU digits."""
        info = DeLonghiApi.parse_serial_number(self.PRIMADONNA_SOUL_B64)
        assert info is not None
        assert info["digits"].startswith("217055")

    def test_binary_serial_surfaces_production_date(self):
        """Production date fields are decoded when binary envelope detected."""
        info = DeLonghiApi.parse_serial_number(self.PRIMADONNA_SOUL_B64)
        assert info is not None
        assert info.get("year") == 2025
        assert info.get("month") == 7
        assert info.get("day") == 1

    def test_binary_serial_surfaces_execution_code(self):
        """Execution code (2 alphanumeric chars after SKU) is surfaced."""
        info = DeLonghiApi.parse_serial_number(self.PRIMADONNA_SOUL_B64)
        assert info is not None
        assert info.get("execution") == "ZZ"

    def test_binary_serial_surfaces_production_sequence(self):
        """Production sequence (last 4 digits) is surfaced for uniqueness."""
        info = DeLonghiApi.parse_serial_number(self.PRIMADONNA_SOUL_B64)
        assert info is not None
        assert info.get("production") == "0134"

    def test_binary_serial_preserves_raw(self):
        """Raw base64 is preserved alongside decoded fields."""
        info = DeLonghiApi.parse_serial_number(self.PRIMADONNA_SOUL_B64)
        assert info is not None
        assert info["raw"] == self.PRIMADONNA_SOUL_B64

    def test_binary_serial_marks_format(self):
        """Binary-decoded samples expose format='binary' for downstream logic."""
        info = DeLonghiApi.parse_serial_number(self.PRIMADONNA_SOUL_B64)
        assert info is not None
        assert info.get("format") == "binary"

    def test_plaintext_fallback_marks_format(self):
        """Legacy plaintext serials expose format='plaintext'."""
        info = DeLonghiApi.parse_serial_number("ECAM61075MB12345")
        assert info is not None
        assert info.get("format") == "plaintext"

    def test_invalid_base64_falls_back_to_regex(self):
        """Non-b64 garbage still yields best-effort digit extraction."""
        info = DeLonghiApi.parse_serial_number("!!!garbage!!!XY42Z")
        assert info is not None
        assert info.get("format") == "plaintext"
        assert info["digits"] == "42"

    def test_binary_serial_matches_transcode_table(self):
        """Decoded SKU digits feed directly into match_transcode_table."""
        info = DeLonghiApi.parse_serial_number(self.PRIMADONNA_SOUL_B64)
        assert info is not None
        # The match_transcode_table call uses the first 6 digits of `digits`,
        # which the decoder guarantees to be the codice.
        assert info["digits"][:6] == "217055"

    def test_binary_serial_roundtrips_into_transcode_table(self):
        """End-to-end: decoded SKU resolves to the PrimaDonna Soul entry."""
        table = _load_transcode_table()
        info = DeLonghiApi.parse_serial_number(self.PRIMADONNA_SOUL_B64)
        assert info is not None
        match = DeLonghiApi.match_transcode_table(table, sku_digits=info["digits"][:6])
        assert match is not None
        assert match["appModelId"] == "PD_SOUL"

    def test_invalid_calendar_date_falls_back_to_plaintext(self):
        """Structurally valid but calendrically impossible date (Feb 31) is rejected.

        Without datetime.date validation, 217055ZZ250231 1 0700 passes the
        month<=12 / day<=31 guard and yields a bogus binary record. It must
        instead fall back to the plaintext regex path.
        """
        import base64

        payload = b"header\x00217055ZZ2502311" + b"0700" + b"\x00\x00\x00"
        encoded = base64.b64encode(payload).decode("ascii")
        info = DeLonghiApi.parse_serial_number(encoded)
        assert info is not None
        assert info.get("format") == "plaintext", (
            "Feb 31 must be rejected by real-date validation, forcing plaintext fallback"
        )

    def test_binary_regex_does_not_match_outside_payload_slice(self):
        """Payload shaped like a serial but placed outside the documented frame
        slice (6-byte header + payload + 3-byte trailer) must not be accepted.

        This guards against false positives where random bytes in a larger
        envelope happen to match the SKU pattern.
        """
        import base64

        # 6-byte header, NO real payload, trailer, then a decoy shaped like
        # a serial (but the frame length is inconsistent — payload would extend
        # past the declared trailer).
        header = b"hdr\x01\x02\x03"
        trailer = b"\xaa\xbb\xcc"
        decoy = b"999999ZZ250815X1234"
        garbage = header + trailer + decoy
        encoded = base64.b64encode(garbage).decode("ascii")
        info = DeLonghiApi.parse_serial_number(encoded)
        assert info is not None
        assert info.get("format") == "plaintext", (
            "Decoy at wrong frame offset must not be treated as a valid binary serial"
        )


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
        match = DeLonghiApi.match_transcode_table(self.table, sku_digits="999999", oem_model="DL-pd-soul")
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
        import requests

        self.api._session.post.side_effect = requests.ConnectionError("Network error")
        self.api.fetch_transcode_table()
        assert self.api._transcode_table is None

    def test_fetch_invalid_json_leaves_none(self):
        """Non-JSON payload still degrades gracefully."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.side_effect = ValueError("not json")
        self.api._session.post.return_value = mock_resp
        self.api.fetch_transcode_table()
        assert self.api._transcode_table is None

    def test_fetch_cached(self):
        """Second fetch doesn't hit network if already cached."""
        self.api._transcode_table = [{"appModelId": "CACHED"}]
        self.api.fetch_transcode_table()
        self.api._session.post.assert_not_called()
