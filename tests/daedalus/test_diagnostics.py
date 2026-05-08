"""Tests for the Daedalus diagnostics endpoint.

Confirms every key in REDACT_KEYS is actually scrubbed before the payload
leaves the system, and that non-secret fields pass through. Without this,
a user uploading the diagnostics zip to a public GitHub issue would be
leaking JWTs / session tokens that grant 90-day account access.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from custom_components.delonghi_daedalus import diagnostics
from custom_components.delonghi_daedalus.const import DOMAIN


def _make_hass_with(entry_id: str, coordinator: Any) -> MagicMock:
    hass = MagicMock()
    hass.data = {DOMAIN: {entry_id: coordinator}}
    return hass


def _make_entry(*, data: dict[str, Any], options: dict[str, Any] | None = None) -> MagicMock:
    entry = MagicMock()
    entry.entry_id = "entry-test"
    entry.version = 1
    entry.minor_version = 0
    entry.data = data
    entry.options = options or {}
    return entry


def _full_data() -> dict[str, Any]:
    """Realistic entry.data shape."""
    return {
        "email": "user@example.com",
        "password": "should-not-be-here-but-test-defensively",
        "host": "192.168.1.42",
        "serial_number": "SN1234567890",
        "machine_name": "SN1234567890",
        "pool": "EU",
        "jwt": "eyJraWQiOiJYIn0.eyJzdWIiOiJ1In0.sig",
        "session_token": "st_abc123",
        "session_secret": "ss_xyz789",
    }


def test_redacts_every_listed_secret_key() -> None:
    entry = _make_entry(data=_full_data())
    coord = MagicMock()
    coord.data = {"connected": True, "connection_id": 7, "host": "192.168.1.42"}
    coord.last_update_success = True
    coord.last_exception = None
    coord.update_interval = MagicMock(total_seconds=lambda: 30)
    hass = _make_hass_with(entry.entry_id, coord)

    result = asyncio.run(diagnostics.async_get_config_entry_diagnostics(hass, entry))
    redacted_data = result["entry"]["data"]

    for key in diagnostics.REDACT_KEYS:
        if key in redacted_data:
            assert redacted_data[key] == "**REDACTED**", f"{key} not redacted: got {redacted_data[key]!r}"


def test_password_field_specifically_redacted() -> None:
    """Defensive: even though password should never be persisted, scrub it if it leaks back in."""
    entry = _make_entry(data=_full_data())
    hass = _make_hass_with(entry.entry_id, None)
    result = asyncio.run(diagnostics.async_get_config_entry_diagnostics(hass, entry))
    assert result["entry"]["data"]["password"] == "**REDACTED**"


def test_jwt_session_token_redacted() -> None:
    """Highest-impact secrets — JWT grants 90 days of account access if leaked."""
    entry = _make_entry(data=_full_data())
    hass = _make_hass_with(entry.entry_id, None)
    result = asyncio.run(diagnostics.async_get_config_entry_diagnostics(hass, entry))
    assert result["entry"]["data"]["jwt"] == "**REDACTED**"
    assert result["entry"]["data"]["session_token"] == "**REDACTED**"
    assert result["entry"]["data"]["session_secret"] == "**REDACTED**"


def test_non_secret_fields_pass_through() -> None:
    """Pool and version must remain visible for triage."""
    entry = _make_entry(data=_full_data())
    hass = _make_hass_with(entry.entry_id, None)
    result = asyncio.run(diagnostics.async_get_config_entry_diagnostics(hass, entry))
    assert result["entry"]["data"]["pool"] == "EU"
    assert result["entry"]["version"] == 1


def test_machine_name_is_redacted_because_it_holds_serial_number() -> None:
    """H-daedalus-3: ``machine_name`` is set to the serial number at
    config-entry creation (config_flow.py:179: ``CONF_MACHINE_NAME: serial``).
    The audit caught this — ``serial_number`` and ``SerialNo`` were
    redacted but the same value also lived under ``machine_name`` and
    leaked verbatim into uploaded diag dumps. Combined with a known
    cloud bug, the SN is what De'Longhi needs to issue an OTA / target
    a device on AWS IoT Core, so it qualifies as PII for support
    posts on a public tracker.
    """
    entry = _make_entry(data=_full_data())
    hass = _make_hass_with(entry.entry_id, None)
    result = asyncio.run(diagnostics.async_get_config_entry_diagnostics(hass, entry))
    assert result["entry"]["data"]["machine_name"] == "**REDACTED**"
    assert result["entry"]["data"]["serial_number"] == "**REDACTED**"


def test_coordinator_state_included_when_present() -> None:
    coord = MagicMock()
    coord.data = {"connected": True, "connection_id": 42}
    coord.last_update_success = True
    coord.last_exception = None
    coord.update_interval = MagicMock(total_seconds=lambda: 30)

    entry = _make_entry(data=_full_data())
    hass = _make_hass_with(entry.entry_id, coord)
    result = asyncio.run(diagnostics.async_get_config_entry_diagnostics(hass, entry))

    assert result["coordinator"]["last_update_success"] is True
    assert result["coordinator"]["lan_connected"] is True
    assert "connection_id" in result["coordinator"]["data_keys"]
    assert result["coordinator"]["update_interval_s"] == 30


def test_handles_missing_coordinator_gracefully() -> None:
    """If the entry hasn't been set up yet, diagnostics still works."""
    entry = _make_entry(data=_full_data())
    hass = MagicMock()
    hass.data = {DOMAIN: {}}  # entry_id not present
    result = asyncio.run(diagnostics.async_get_config_entry_diagnostics(hass, entry))
    assert result["coordinator"] == {}
    # Entry data still scrubbed.
    assert result["entry"]["data"]["jwt"] == "**REDACTED**"


def test_handles_no_domain_data_gracefully() -> None:
    entry = _make_entry(data=_full_data())
    hass = MagicMock()
    hass.data = {}
    result = asyncio.run(diagnostics.async_get_config_entry_diagnostics(hass, entry))
    assert result["coordinator"] == {}


def test_options_also_redacted() -> None:
    """If the user one day adds options containing tokens, they must be scrubbed too."""
    entry = _make_entry(
        data=_full_data(),
        options={"jwt": "should-be-redacted", "diagnostic_mode": True},
    )
    hass = _make_hass_with(entry.entry_id, None)
    result = asyncio.run(diagnostics.async_get_config_entry_diagnostics(hass, entry))
    assert result["entry"]["options"]["jwt"] == "**REDACTED**"
    assert result["entry"]["options"]["diagnostic_mode"] is True


def test_device_diagnostics_returns_same_payload() -> None:
    entry = _make_entry(data=_full_data())
    hass = _make_hass_with(entry.entry_id, None)
    payload_entry = asyncio.run(diagnostics.async_get_config_entry_diagnostics(hass, entry))
    payload_device = asyncio.run(diagnostics.async_get_device_diagnostics(hass, entry, MagicMock()))
    assert payload_entry == payload_device


def test_redact_keys_covers_high_value_secrets() -> None:
    """Sanity check on the REDACT_KEYS set — guard against accidental removals."""
    must_be_redacted = {
        "password",
        "jwt",
        "session_token",
        "session_secret",
        "AuthToken",
        "apiKey",
        "machine_name",  # H-daedalus-3: holds the SN by default
    }
    missing = must_be_redacted - diagnostics.REDACT_KEYS
    assert not missing, f"REDACT_KEYS missing high-value secrets: {missing}"


@pytest.mark.parametrize("entry_data_value", [None, {}, {"some": "thing"}])
def test_minimal_entry_data_does_not_crash(entry_data_value: Any) -> None:
    """Robustness: incomplete entry shapes should not break diagnostics."""
    entry = _make_entry(data=entry_data_value or {})
    hass = _make_hass_with(entry.entry_id, None)
    result = asyncio.run(diagnostics.async_get_config_entry_diagnostics(hass, entry))
    assert "entry" in result
