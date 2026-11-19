"""Tests for the Daedalus diagnostics endpoint.

Confirms every key in REDACT_KEYS is actually scrubbed before the payload
leaves the system, and that non-secret fields pass through. Without this,
a user uploading the diagnostics zip to a public GitHub issue would be
leaking JWTs / session tokens that grant 90-day account access.

The ``cloud_*`` tests cover issue #18: the dump must show *why* the cloud
telemetry failed (sanitized text + category + codes) and whether the
cloud session is up at all — without ever leaking the JWT, the broker
credentials or the full serial number.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
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


def test_last_exception_free_text_is_scrubbed() -> None:
    """Audit 2026-11-05: async_redact_data is key-indexed — it cannot scrub
    the exception TEXT, which carries the LAN WS URL (host), JWT fragments
    and possibly Gigya messages echoing the account email. The dump is made
    for public issue trackers."""
    coord = MagicMock()
    coord.data = {"connected": False}
    coord.last_update_success = False
    coord.last_exception = ConnectionError(
        "LAN WS connect to wss://192.168.1.42/ws/lan2lan failed for "
        "sebastien@corp.example.net with token eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.sig123"
    )
    coord.update_interval = MagicMock(total_seconds=lambda: 30)

    entry = _make_entry(data=_full_data())
    hass = _make_hass_with(entry.entry_id, coord)
    result = asyncio.run(diagnostics.async_get_config_entry_diagnostics(hass, entry))
    le = result["coordinator"]["last_exception"]

    assert "192.168.1.42" not in le
    assert "sebastien@corp.example.net" not in le
    assert "eyJ" not in le
    assert "sig123" not in le
    assert le.startswith("ConnectionError:")  # type kept for triage


def test_last_exception_none_stays_empty_string() -> None:
    coord = MagicMock()
    coord.data = {}
    coord.last_update_success = True
    coord.last_exception = None
    coord.update_interval = MagicMock(total_seconds=lambda: 30)
    entry = _make_entry(data=_full_data())
    hass = _make_hass_with(entry.entry_id, coord)
    result = asyncio.run(diagnostics.async_get_config_entry_diagnostics(hass, entry))
    assert result["coordinator"]["last_exception"] == ""


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


# --------------------------------------------------------------- cloud block --

# A cloud_error string as the coordinator stores it: already sanitized once,
# but the dump re-scrubs it (defense in depth) because it is written for a
# public issue tracker.
_RAW_CLOUD_ERROR = (
    "MQTT WS connect failed: password Sup3rS3cret! for mimasek71@corp.example.net "
    "at wss://192.168.1.42:443/mqtt with token "
    "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.realSigFragment for SN1234567890"
)
_SECRETS = (
    "Sup3rS3cret!",
    "mimasek71@corp.example.net",
    "192.168.1.42",
    "SN1234567890",
    "realSigFragment",
    "eyJ",
)


def _cloud_coordinator(**attrs: Any) -> MagicMock:
    coord = MagicMock()
    coord.data = {"connected": True}
    coord.last_update_success = True
    coord.last_exception = None
    coord.update_interval = MagicMock(total_seconds=lambda: 30)
    coord.last_checked_time = None
    for key, value in attrs.items():
        setattr(coord, key, value)
    return coord


def _cloud_payload(coord: Any, *, options: dict[str, Any] | None = None) -> dict[str, Any]:
    entry = _make_entry(data=_full_data(), options=options)
    hass = _make_hass_with(entry.entry_id, coord)
    return asyncio.run(diagnostics.async_get_config_entry_diagnostics(hass, entry))["cloud"]


def test_cloud_error_value_is_exposed_and_scrubbed() -> None:
    """issue #18: the key existed but its value never reached the dump.

    The tester's report could not be triaged because auth / MQTT /
    subscription / parsing failures were all indistinguishable. The value
    must now be there — with every secret class still scrubbed.
    """
    cloud = _cloud_payload(_cloud_coordinator(data={"connected": True, "cloud_error": _RAW_CLOUD_ERROR}))
    error = cloud["cloud_error"]

    assert error is not None, "cloud_error value must be exposed"
    assert error["category"] == "mqtt_connect"
    assert "MQTT WS connect failed" in error["message"]

    blob = json.dumps(cloud)
    for secret in _SECRETS:
        assert secret not in blob, f"leaked {secret!r} in {blob}"


def test_cloud_error_from_coordinator_cache_when_refresh_has_not_run() -> None:
    """The background fetch records the error before the next refresh projects it."""
    cloud = _cloud_payload(
        _cloud_coordinator(_cloud_error="MQTT broker refused the JWT (CONNACK rc=5)"),
    )
    assert cloud["cloud_error"]["category"] == "auth"
    assert cloud["cloud_error"]["mqtt_connack_rc"] == 5


def test_mock_coordinator_never_fabricates_a_cloud_error() -> None:
    """A coordinator without the attribute must not invent an error (MagicMock)."""
    cloud = _cloud_payload(_cloud_coordinator())
    assert cloud["cloud_error"] is None
    assert cloud["cloud_connected"] is False
    assert cloud["last_cloud_message"] is None


@pytest.mark.parametrize(
    ("raw", "category", "codes"),
    [
        ("MQTT broker refused the JWT (CONNACK rc=5)", "auth", {"mqtt_connack_rc": 5}),
        ("/devices returned HTTP 403", "auth", {"http_status": 403}),
        ("/devices returned HTTP 500", "device_lookup", {"http_status": 500}),
        ("serial **** not found in /devices response", "device_lookup", {}),
        ("no CONNACK from broker", "mqtt_connect", {}),
        ("MQTT WS closed by broker", "mqtt_connect", {}),
        ("no shadow document received before timeout", "no_shadow", {}),
        (
            'shadow get rejected by broker: {"code":404,"message":"No method defined"}',
            "shadow_rejected",
            {"shadow_code": 404},
        ),
        ("unparsable shadow: Expecting value", "parse", {}),
        ("unexpected cloud error: something odd", "unexpected", {}),
        ("a failure nobody has seen yet", "unknown", {}),
    ],
)
def test_cloud_error_is_classified_for_triage(raw: str, category: str, codes: dict[str, int]) -> None:
    """The category + codes are what replaces a log scrape (issue #18)."""
    error = _cloud_payload(_cloud_coordinator(data={"connected": True, "cloud_error": raw}))["cloud_error"]
    assert error is not None
    assert error["category"] == category
    assert {k: v for k, v in error.items() if k in codes} == codes


def test_cloud_error_message_is_truncated_without_losing_its_category() -> None:
    """The truncation marker must not reclassify a long message as a parse error."""
    error = _cloud_payload(
        _cloud_coordinator(data={"connected": True, "cloud_error": "MQTT session failed: " + "x" * 600})
    )["cloud_error"]
    assert error["truncated"] is True
    assert len(error["message"]) <= 240
    assert error["category"] == "mqtt_connect"


def test_cloud_error_codes_survive_truncation() -> None:
    """A code past the display ceiling is still parsed out of the full text."""
    error = _cloud_payload(
        _cloud_coordinator(data={"connected": True, "cloud_error": "MQTT session failed: " + "x" * 600 + " HTTP 500"})
    )["cloud_error"]
    assert error["truncated"] is True
    assert error["http_status"] == 500


def test_cloud_error_json_secrets_are_scrubbed() -> None:
    """A broker-controlled /get/rejected payload can echo token-shaped fields."""
    raw = 'shadow get rejected by broker: {"code":404,"clientToken":"aGVsbG8gd29ybGQ","password":"hunter2secret"}'
    cloud = _cloud_payload(_cloud_coordinator(data={"connected": True, "cloud_error": raw}))
    blob = json.dumps(cloud)
    assert "aGVsbG8gd29ybGQ" not in blob
    assert "hunter2secret" not in blob
    assert cloud["cloud_error"]["shadow_code"] == 404


def test_cloud_error_carries_the_attempt_timestamp() -> None:
    coord = _cloud_coordinator(
        data={"connected": True, "cloud_error": "MQTT broker refused the JWT (CONNACK rc=5)"},
    )
    coord.last_checked_time = datetime(2026, 11, 18, 3, 4, 5, tzinfo=UTC)
    error = _cloud_payload(coord)["cloud_error"]
    assert error["last_attempt"] == "2026-11-18T03:04:05+00:00"


def test_cloud_connected_and_last_message_when_shadows_arrive() -> None:
    stamp = datetime(2026, 11, 18, 2, 15, 33, tzinfo=UTC).timestamp()
    cloud = _cloud_payload(
        _cloud_coordinator(
            data={
                "connected": True,
                "cloud": {
                    "machine_name": "SN1234567890",
                    "status_flat": {"operatingState": "STANDBY"},
                    "capabilities_flat": {},
                    "fetched_at": stamp,
                },
            }
        )
    )
    assert cloud["cloud_connected"] is True
    assert cloud["last_cloud_message"] == "2026-11-18T02:15:33+00:00"
    assert cloud["cloud_error"] is None


def test_cloud_connected_false_when_an_error_is_pending() -> None:
    """A stale cached shadow must not be reported as a live cloud session."""
    cloud = _cloud_payload(
        _cloud_coordinator(
            data={
                "connected": True,
                "cloud": {"status_flat": {"operatingState": "STANDBY"}, "fetched_at": 1763443000.0},
                "cloud_error": "MQTT WS closed by broker",
            }
        )
    )
    assert cloud["cloud_connected"] is False
    assert cloud["last_cloud_message"] is not None


def test_cloud_topics_subscribed_have_the_serial_masked() -> None:
    """Topics embed the thing name (= the serial by default) — masked, last 4 only."""
    cloud = _cloud_payload(_cloud_coordinator())
    assert cloud["cloud_topics_subscribed"] == [
        "$aws/things/********7890/shadow/name/MachineStatus/get/accepted",
        "$aws/things/********7890/shadow/name/MachineStatus/get/rejected",
        "$aws/things/********7890/shadow/name/MachineCapabilities/get/accepted",
        "$aws/things/********7890/shadow/name/MachineCapabilities/get/rejected",
    ]
    assert "SN1234567890" not in json.dumps(cloud)


def test_cloud_topics_empty_without_a_thing_name() -> None:
    entry = _make_entry(data={"pool": "EU"})
    hass = _make_hass_with(entry.entry_id, None)
    result = asyncio.run(diagnostics.async_get_config_entry_diagnostics(hass, entry))
    assert result["cloud"]["cloud_topics_subscribed"] == []


def test_cloud_telemetry_flag_read_from_options_then_data() -> None:
    """The reconfigure flow writes the switch into ``entry.data``; options wins if set."""
    coord = _cloud_coordinator(data={"connected": True, "cloud": {"status_flat": {"a": 1}}})
    assert _cloud_payload(coord)["cloud_telemetry_enabled"] is False
    assert _cloud_payload(coord, options={"cloud_telemetry": True})["cloud_telemetry_enabled"] is True

    entry = _make_entry(data={**_full_data(), "cloud_telemetry": True})
    hass = _make_hass_with(entry.entry_id, coord)
    result = asyncio.run(diagnostics.async_get_config_entry_diagnostics(hass, entry))
    assert result["cloud"]["cloud_telemetry_enabled"] is True


def test_cloud_block_survives_a_missing_coordinator() -> None:
    """Telemetry can be enabled before the first refresh — no KeyError, no crash."""
    entry = _make_entry(data={**_full_data(), "cloud_telemetry": True})
    hass = MagicMock()
    hass.data = {}
    result = asyncio.run(diagnostics.async_get_config_entry_diagnostics(hass, entry))
    assert result["cloud"] == {
        "cloud_telemetry_enabled": True,
        "cloud_connected": False,
        "last_cloud_message": None,
        "cloud_topics_subscribed": [
            "$aws/things/********7890/shadow/name/MachineStatus/get/accepted",
            "$aws/things/********7890/shadow/name/MachineStatus/get/rejected",
            "$aws/things/********7890/shadow/name/MachineCapabilities/get/accepted",
            "$aws/things/********7890/shadow/name/MachineCapabilities/get/rejected",
        ],
        "cloud_error": None,
    }


def test_diagnostics_scrub_distinct_identifiers_and_secrets_in_free_text() -> None:
    identifiers = {
        "serial_number": "DEVICE12345",
        "machine_name": "configured-thing-1234",
        "host": "fe80::cafe",
        "session_token": "stored-session-token-1234",
        "lan_tls_fingerprint": "12" * 32,
    }
    resolved_thing = "account-directory-thing-5678"
    private_in_error = "private identifier with spaces"
    context = f"{' '.join(identifiers.values())} {resolved_thing}"
    coord = _cloud_coordinator(
        last_exception=ConnectionError(f"LAN WS closed for {context}"),
        data={
            "connected": True,
            "cloud": {"machine_name": resolved_thing},
            "cloud_error": f'MQTT session failed for {context}, "machineName":"{private_in_error}" HTTP 500',
        },
    )
    entry = _make_entry(data={**_full_data(), **identifiers})
    payload = asyncio.run(
        diagnostics.async_get_device_diagnostics(_make_hass_with(entry.entry_id, coord), entry, MagicMock())
    )
    assert payload["cloud"]["cloud_error"]["category"] == "mqtt_connect"
    assert payload["cloud"]["cloud_error"]["http_status"] == 500
    assert payload["coordinator"]["last_exception"].startswith("ConnectionError: LAN WS closed")
    for private in (*identifiers.values(), resolved_thing, private_in_error):
        assert private not in json.dumps(payload)


def test_lan_error_uses_free_text_redaction() -> None:
    data = {
        **_full_data(),
        "machine_name": "private-thing-id",
        "lan_tls_fingerprint": "a0" * 32,
    }
    context = " ".join(data[key] for key in ("host", "serial_number", "machine_name", "lan_tls_fingerprint"))
    coord = _cloud_coordinator(data={"connected": False, "lan_error": f"LAN certificate changed for {context}"})
    entry = _make_entry(data=data)
    payload = asyncio.run(diagnostics.async_get_config_entry_diagnostics(_make_hass_with(entry.entry_id, coord), entry))
    error = payload["coordinator"]["lan_error"]

    assert error.startswith("LAN certificate changed")
    for private in context.split():
        assert private not in error
