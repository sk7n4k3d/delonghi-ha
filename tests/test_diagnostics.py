"""Test diagnostics module — entry-scoped + device-scoped redaction.

The `homeassistant.components.diagnostics` module is stubbed in
`tests/conftest.py` with a real `async_redact_data` implementation, so
top-level keys in REDACT_KEYS are actually scrubbed in the test payload.
"""

import asyncio
from unittest.mock import MagicMock

import pytest

from custom_components.delonghi_coffee import diagnostics
from custom_components.delonghi_coffee.const import DOMAIN


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_hass(entry_id: str, coordinator=None, api=None, model_data=None):
    hass = MagicMock()
    payload = {"coordinator": coordinator, "api": api}
    if model_data:
        payload.update(model_data)
    hass.data = {DOMAIN: {entry_id: payload}}
    return hass


def _make_entry(entry_id="abc123", data=None, options=None):
    entry = MagicMock()
    entry.entry_id = entry_id
    entry.version = 1
    entry.minor_version = 0
    entry.data = data or {}
    entry.options = options or {}
    return entry


class TestRedactKeys:
    """REDACT_KEYS covers all known sensitive fields."""

    def test_includes_credentials(self):
        for key in ("email", "password"):
            assert key in diagnostics.REDACT_KEYS

    def test_includes_tokens(self):
        for key in ("access_token", "refresh_token", "ayla_token", "ayla_refresh", "session_token"):
            assert key in diagnostics.REDACT_KEYS

    def test_includes_lan_secrets(self):
        for key in ("lan_key", "lanip_key"):
            assert key in diagnostics.REDACT_KEYS

    def test_includes_gigya_signatures(self):
        for key in ("uid", "uidSignature", "signatureTimestamp"):
            assert key in diagnostics.REDACT_KEYS


class TestEntryDiagnostics:
    """async_get_config_entry_diagnostics shape + redaction."""

    def test_redacts_email_and_password_in_entry_data(self):
        entry = _make_entry(data={"email": "user@x.com", "password": "secret", "region": "EU"})
        hass = _make_hass(entry.entry_id)
        result = _run(diagnostics.async_get_config_entry_diagnostics(hass, entry))
        assert result["entry"]["data"]["email"] == "**REDACTED**"
        assert result["entry"]["data"]["password"] == "**REDACTED**"
        assert result["entry"]["data"]["region"] == "EU"

    def test_redacts_options(self):
        entry = _make_entry(options={"diagnostic_mode": True, "lan_key": "abc"})
        hass = _make_hass(entry.entry_id)
        result = _run(diagnostics.async_get_config_entry_diagnostics(hass, entry))
        assert result["entry"]["options"]["diagnostic_mode"] is True
        assert result["entry"]["options"]["lan_key"] == "**REDACTED**"

    def test_handles_missing_coordinator_and_api(self):
        """Gracefully degrades when entry hasn't fully loaded."""
        entry = _make_entry()
        hass = _make_hass(entry.entry_id)  # no coordinator, no api
        result = _run(diagnostics.async_get_config_entry_diagnostics(hass, entry))
        assert result["coordinator"] == {}
        assert result["api"] == {}

    def test_coordinator_state_extracted(self):
        coord = MagicMock()
        coord.last_update_success = True
        coord.last_exception = None
        coord.diagnostic_mode = False
        coord.selected_profile = 2
        coord.beverages = [{}, {}, {}]
        coord.custom_recipe_names = {1: "mocha"}
        coord.data = {"foo": 1, "bar": 2}
        entry = _make_entry()
        hass = _make_hass(entry.entry_id, coordinator=coord)
        result = _run(diagnostics.async_get_config_entry_diagnostics(hass, entry))
        cs = result["coordinator"]
        assert cs["last_update_success"] is True
        assert cs["selected_profile"] == 2
        assert cs["beverages_count"] == 3
        assert cs["custom_recipe_names"] == {1: "mocha"}
        assert cs["raw_properties_keys"] == ["bar", "foo"]

    def test_coordinator_handles_none_data(self):
        coord = MagicMock()
        coord.data = None
        entry = _make_entry()
        hass = _make_hass(entry.entry_id, coordinator=coord)
        result = _run(diagnostics.async_get_config_entry_diagnostics(hass, entry))
        assert result["coordinator"]["raw_properties_keys"] == []

    def test_api_state_extracted(self):
        api = MagicMock()
        api._oem_model = "DL-pd-soul"
        api.device_name = "Kitchen Coffee"
        api.sw_version = "v1.2.3"
        api._ping_supported = True
        api.rate_tracker = MagicMock(current_rate=42)
        entry = _make_entry()
        hass = _make_hass(entry.entry_id, api=api)
        result = _run(diagnostics.async_get_config_entry_diagnostics(hass, entry))
        api_state = result["api"]
        assert api_state["oem_model"] == "DL-pd-soul"
        assert api_state["device_name"] == "Kitchen Coffee"
        assert api_state["sw_version"] == "v1.2.3"
        assert api_state["ping_supported"] is True
        assert api_state["rate_current"] == 42

    def test_api_state_handles_no_rate_tracker(self):
        api = MagicMock()
        api.rate_tracker = None
        entry = _make_entry()
        hass = _make_hass(entry.entry_id, api=api)
        result = _run(diagnostics.async_get_config_entry_diagnostics(hass, entry))
        assert result["api"]["rate_current"] is None

    def test_device_block_from_data(self):
        entry = _make_entry()
        hass = _make_hass(
            entry.entry_id,
            model_data={"model": "ECAM61075MB", "device_name": "Soul", "sw_version": "1.0"},
        )
        result = _run(diagnostics.async_get_config_entry_diagnostics(hass, entry))
        assert result["device"] == {"model": "ECAM61075MB", "device_name": "Soul", "sw_version": "1.0"}

    def test_entry_version_included(self):
        entry = _make_entry()
        entry.version = 2
        entry.minor_version = 1
        hass = _make_hass(entry.entry_id)
        result = _run(diagnostics.async_get_config_entry_diagnostics(hass, entry))
        assert result["entry"]["version"] == 2
        assert result["entry"]["minor_version"] == 1

    def test_handles_missing_domain_data(self):
        """If integration entry not present in hass.data, returns empty subblocks."""
        entry = _make_entry()
        hass = MagicMock()
        hass.data = {}  # DOMAIN not present
        result = _run(diagnostics.async_get_config_entry_diagnostics(hass, entry))
        assert result["coordinator"] == {}
        assert result["api"] == {}


class TestDeviceDiagnostics:
    """async_get_device_diagnostics returns same payload as entry-scoped."""

    def test_delegates_to_entry_diagnostics(self):
        entry = _make_entry(data={"email": "a@b.com"})
        hass = _make_hass(entry.entry_id)
        device = MagicMock()
        result = _run(diagnostics.async_get_device_diagnostics(hass, entry, device))
        assert "entry" in result and "coordinator" in result
        assert result["entry"]["data"]["email"] == "**REDACTED**"


@pytest.fixture(autouse=True)
def _ensure_event_loop():
    """Provide an event loop for asyncio.run_until_complete on Python 3.14+."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError("closed")
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    yield
