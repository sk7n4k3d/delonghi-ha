"""Tests for the diagnostics export.

Critical behaviours:

1. Secrets MUST be redacted. If anyone ever adds a new credential / token
   field, it has to land in ``REDACT_KEYS`` — these tests fail loudly
   when the payload ever carries a raw secret.
2. Coordinator + API metrics must surface for triage.
3. Payload must not raise when coordinator / api are missing or partial.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from custom_components.delonghi_coffee.const import DOMAIN


# Upstream provides ``async_redact_data`` from the ``diagnostics`` component.
# Tests stub it with a minimal recursive implementation matching HA behaviour
# so the real diag function can run without the full framework.
def _fake_redact(payload, keys):
    if isinstance(payload, dict):
        return {
            k: ("**REDACTED**" if k in keys else _fake_redact(v, keys))
            for k, v in payload.items()
        }
    if isinstance(payload, list):
        return [_fake_redact(x, keys) for x in payload]
    return payload


@pytest.fixture(autouse=True)
def _patch_redact(monkeypatch):
    import sys

    diag_mod = sys.modules.get("homeassistant.components.diagnostics")
    if diag_mod is None:
        diag_mod = MagicMock()
        sys.modules["homeassistant.components.diagnostics"] = diag_mod
    monkeypatch.setattr(diag_mod, "async_redact_data", _fake_redact, raising=False)


def _import_diagnostics():
    import importlib
    import sys

    mod_name = "custom_components.delonghi_coffee.diagnostics"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    return importlib.import_module(mod_name)


def _make_entry(*, data: dict, options: dict, version=2, minor_version=0) -> MagicMock:
    entry = MagicMock()
    entry.version = version
    entry.minor_version = minor_version
    entry.data = data
    entry.options = options
    entry.entry_id = "test-entry"
    return entry


def _make_hass(coord=None, api=None, device_info=None) -> MagicMock:
    hass = MagicMock()
    hass.data = {
        DOMAIN: {
            "test-entry": {
                "coordinator": coord,
                "api": api,
                **(device_info or {}),
            }
        }
    }
    return hass


# ─────────────────────────────────────────────────────────────────────────
# Redaction invariants — each key listed in REDACT_KEYS must be scrubbed.
# ─────────────────────────────────────────────────────────────────────────


def test_every_redact_key_is_scrubbed_from_entry_data() -> None:
    """Populate every REDACT_KEYS entry in entry.data and confirm none leak."""
    diagnostics = _import_diagnostics()
    loud = {k: f"raw-{k}" for k in diagnostics.REDACT_KEYS}
    entry = _make_entry(data=loud, options={})
    hass = _make_hass()

    payload = asyncio.run(diagnostics.async_get_config_entry_diagnostics(hass, entry))

    for key in diagnostics.REDACT_KEYS:
        assert payload["entry"]["data"][key] == "**REDACTED**", (
            f"{key} must be redacted but is {payload['entry']['data'][key]!r}"
        )
        assert f"raw-{key}" not in str(payload), f"{key} leaked into payload"


def test_non_secret_fields_pass_through_unchanged() -> None:
    """Ensure redaction is surgical — innocent fields must not be touched."""
    diagnostics = _import_diagnostics()
    entry = _make_entry(
        data={
            "dsn": "AC000W038925641",
            "region": "EU",
            "model": "DL-striker-best",
        },
        options={"diagnostic_mode": True},
    )
    hass = _make_hass()

    payload = asyncio.run(diagnostics.async_get_config_entry_diagnostics(hass, entry))

    assert payload["entry"]["data"]["dsn"] == "AC000W038925641"
    assert payload["entry"]["data"]["region"] == "EU"
    assert payload["entry"]["data"]["model"] == "DL-striker-best"
    assert payload["entry"]["options"]["diagnostic_mode"] is True


def test_version_metadata_is_preserved() -> None:
    """Version + minor_version are needed to trace migration bugs."""
    diagnostics = _import_diagnostics()
    entry = _make_entry(data={}, options={}, version=2, minor_version=3)
    hass = _make_hass()

    payload = asyncio.run(diagnostics.async_get_config_entry_diagnostics(hass, entry))

    assert payload["entry"]["version"] == 2
    assert payload["entry"]["minor_version"] == 3


# ─────────────────────────────────────────────────────────────────────────
# Coordinator + API state must surface (or fail gracefully when absent).
# ─────────────────────────────────────────────────────────────────────────


def test_coordinator_state_surfaces_triage_fields() -> None:
    diagnostics = _import_diagnostics()

    coord = MagicMock()
    coord.last_update_success = True
    coord.last_exception = None
    coord.diagnostic_mode = True
    coord.selected_profile = 2
    coord.beverages = ["espresso", "cappuccino"]
    coord._keepalive_failures = 3
    coord._lan_active = True
    coord.custom_recipe_names = {1: "My Coffee"}
    coord.data = {"machine_state": "Ready", "connected": True}

    entry = _make_entry(data={"dsn": "X"}, options={})
    hass = _make_hass(coord=coord)

    payload = asyncio.run(diagnostics.async_get_config_entry_diagnostics(hass, entry))

    cstate = payload["coordinator"]
    assert cstate["last_update_success"] is True
    assert cstate["diagnostic_mode"] is True
    assert cstate["selected_profile"] == 2
    assert cstate["beverages_count"] == 2
    assert cstate["keepalive_failures"] == 3
    assert cstate["lan_active"] is True
    assert cstate["raw_properties_keys"] == ["connected", "machine_state"]


def test_api_state_surfaces_rate_limit_and_model() -> None:
    diagnostics = _import_diagnostics()

    api = MagicMock()
    api._oem_model = "DL-striker-best"
    api.device_name = "Eletta Explore"
    api.sw_version = "1.2.3"
    api._ping_supported = True
    api.rate_tracker = MagicMock()
    api.rate_tracker.current_rate = 42

    entry = _make_entry(data={}, options={})
    hass = _make_hass(api=api)

    payload = asyncio.run(diagnostics.async_get_config_entry_diagnostics(hass, entry))

    astate = payload["api"]
    assert astate["oem_model"] == "DL-striker-best"
    assert astate["device_name"] == "Eletta Explore"
    assert astate["sw_version"] == "1.2.3"
    assert astate["ping_supported"] is True
    assert astate["rate_current"] == 42


def test_diagnostics_tolerates_missing_entry() -> None:
    """Fresh entry with no bundle yet — payload must still render empty shells."""
    diagnostics = _import_diagnostics()
    entry = _make_entry(data={}, options={})
    hass = MagicMock()
    hass.data = {}  # no DOMAIN key at all

    payload = asyncio.run(diagnostics.async_get_config_entry_diagnostics(hass, entry))

    assert payload["coordinator"] == {}
    assert payload["api"] == {}


def test_device_diagnostics_mirrors_entry_diagnostics() -> None:
    diagnostics = _import_diagnostics()
    entry = _make_entry(data={"dsn": "X"}, options={})
    hass = _make_hass()

    entry_payload = asyncio.run(
        diagnostics.async_get_config_entry_diagnostics(hass, entry)
    )
    device_payload = asyncio.run(
        diagnostics.async_get_device_diagnostics(hass, entry, device=MagicMock())
    )

    assert entry_payload == device_payload
