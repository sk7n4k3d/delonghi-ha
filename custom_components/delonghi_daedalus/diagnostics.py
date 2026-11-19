"""Diagnostics support for the De'Longhi Daedalus integration.

Provides a HA-native "Download diagnostics" payload that triages auth /
LAN-connectivity bugs without asking users to scrape logs by hand. All
fields that could leak the user's account or session material are
redacted via `homeassistant.components.diagnostics.async_redact_data`.

Issue #18: with ``cloud_telemetry`` on but every cloud sensor
``unavailable``, the dump only proved *that* the cloud hop had failed
(``cloud_error`` appeared in ``coordinator.data_keys``) — never why. The
``cloud`` block below therefore exposes the sanitized error text, a
category, any MQTT CONNACK / HTTP / shadow code parsed out of it, and
the connection state (last successful shadow, subscribed topics) so a
report can be triaged as auth vs. MQTT vs. subscription vs. parsing
without asking the user for a log scrape.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .cloud import SHADOWS, shadow_accepted_topic, shadow_rejected_topic
from .cloud_keys import SENSITIVE_KEYS, normalize
from .const import (
    CONF_CLOUD_TELEMETRY,
    CONF_HOST,
    CONF_MACHINE_NAME,
    CONF_SERIAL_NUMBER,
    DOMAIN,
)
from .redact import mask_serial, sanitize_text

# Anything in this set is replaced with "**REDACTED**" in the payload.
# Conservative on purpose: even the email is redacted because the support
# zip can be uploaded to a public issue tracker.
REDACT_KEYS: set[str] = {
    "email",
    "password",
    "jwt",
    "session_token",
    "sessionToken",
    "session_secret",
    "sessionSecret",
    "id_token",
    "oauth_token",
    "AuthToken",
    "apiKey",
    "uid",
    "uidSignature",
    "signatureTimestamp",
    "serial_number",
    "serialNumber",
    "SerialNo",
    # H-daedalus-3 (audit 2026-05-08): config_flow stores the serial number
    # under CONF_MACHINE_NAME too (see config_flow.py:179). Without this
    # entry the SN leaked verbatim into uploaded diag dumps via the
    # ``machine_name`` field even though ``serial_number`` was scrubbed.
    "machine_name",
    "machineName",
    "thing_name",
    "thingName",
    "mac",
    "mac_address",
    "macAddress",
    "connection_id",
    "model",
    "host",
    "lan_ip",
    "lan_tls_fingerprint",
}


# --- cloud telemetry block (issue #18) --------------------------------------

# Ceiling for the free-text error shown in the dump. The coordinator already
# ran ``sanitize_text`` over it, but the dump is written for a *public* issue
# tracker, so it is re-sanitized here (defense in depth) and kept short.
_CLOUD_ERROR_MAX_CHARS = 240
_TRUNCATED_MARK = "...[TRUNCATED]"

# Broker errors may include key/value fields we cannot look up in the entry.
_JSON_FIELD_RE = re.compile(r"""(?i)(?<!\w)"?([a-z][\w]*)"?\s*[:=]\s*("(?:[^"\\]|\\.)*"|[^"\s,}]+)""")
_PRIVATE_ERROR_FIELDS = SENSITIVE_KEYS | {"token", "accesstoken", "refreshtoken", "passwd"}


def _sanitize_diagnostic_text(text: str, *, host: object, private_values: tuple[str, ...]) -> str:
    for value in private_values:
        text = text.replace(value, "**REDACTED**")
    text = _JSON_FIELD_RE.sub(
        lambda match: f'"{match[1]}":"***"' if normalize(match[1]) in _PRIVATE_ERROR_FIELDS else match[0],
        text,
    )
    return sanitize_text(text, host=host)


# Triage categories, first match wins — ordered most-specific first so, e.g.,
# "no shadow document received before timeout" is reported as a missing
# shadow rather than a generic timeout.
_CLOUD_ERROR_CATEGORIES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "auth",
        re.compile(r"(?i)refused the jwt|not authorized|unauthorized|forbidden|http 40[13]\b|connack rc=[45]\b"),
    ),
    # "shadow get rejected by broker" would be swallowed by the loose
    # ``mqtt_connect`` patterns below, so it is matched first.
    ("shadow_rejected", re.compile(r"(?i)shadow get rejected")),
    ("no_shadow", re.compile(r"(?i)no shadow document received")),
    (
        "mqtt_connect",
        re.compile(
            r"(?i)mqtt ws connect failed|no connack|expected binary connack|mqtt ws closed by broker"
            r"|mqtt session failed|connect budget exhausted|connack"
        ),
    ),
    ("parse", re.compile(r"(?i)unparsable|not json|neither a list|malformed|truncated|buffer overflow")),
    ("device_lookup", re.compile(r"(?i)/devices|no rest device directory|thing name rejected")),
    ("timeout", re.compile(r"(?i)timed out|timeout")),
    ("unexpected", re.compile(r"(?i)^unexpected cloud error")),
)

_MQTT_RC_RE = re.compile(r"(?i)\bconnack rc\s*[:=]\s*(\d{1,3})\b|\brc\s*[:=]\s*(\d{1,3})\b")
_HTTP_STATUS_RE = re.compile(r"(?i)\bhttp\s+(\d{3})\b")
_SHADOW_CODE_RE = re.compile(r'(?i)"code"\s*:\s*(\d{3})')


def _entry_mapping(candidate: object) -> Mapping[str, Any]:
    """Return ``entry.data``/``entry.options`` as a real mapping.

    A config entry always carries a dict; anything else (a bare mock in a
    unit test, ``None`` on a half-migrated entry) degrades to an empty
    mapping instead of fabricating values.
    """
    return candidate if isinstance(candidate, Mapping) else {}


def _to_iso(value: object) -> str | None:
    """Render a wall-clock timestamp (epoch float or ``datetime``) as ISO-8601.

    Type-checked on purpose: the values come off a coordinator that unit
    tests hand around as mocks, and a mock repr must never reach a dump.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        stamp = value
    elif isinstance(value, (int, float)) and value > 0:
        stamp = datetime.fromtimestamp(value, tz=UTC)
    else:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp.isoformat()


def _sanitize_cloud_error(raw: object, *, host: object, private_values: tuple[str, ...]) -> dict[str, Any] | None:
    """Structured, shareable view of ``coordinator``'s ``cloud_error`` string.

    Returns ``None`` when there is no error. Non-strings are ignored: the
    coordinator stores ``sanitize_text(...)`` (always a ``str``), so
    anything else means "no error recorded", never a value to render.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = _sanitize_diagnostic_text(raw, host=host, private_values=private_values)

    # Category and codes are read off the *full* text: a code sitting past the
    # display ceiling must not be lost, and the truncation marker itself
    # ("...[TRUNCATED]") would otherwise classify every long message as a
    # parse failure.
    category = next((name for name, rx in _CLOUD_ERROR_CATEGORIES if rx.search(text)), "unknown")
    truncated = len(text) > _CLOUD_ERROR_MAX_CHARS
    message = text[: _CLOUD_ERROR_MAX_CHARS - len(_TRUNCATED_MARK)] + _TRUNCATED_MARK if truncated else text

    payload: dict[str, Any] = {"category": category, "message": message, "truncated": truncated}
    rc = _MQTT_RC_RE.search(text)
    if rc:
        payload["mqtt_connack_rc"] = int(rc.group(1) or rc.group(2))
    http = _HTTP_STATUS_RE.search(text)
    if http:
        payload["http_status"] = int(http.group(1))
    code = _SHADOW_CODE_RE.search(text)
    if code:
        payload["shadow_code"] = int(code.group(1))
    return payload


def _cloud_topics(entry_data: Mapping[str, Any], cloud: Mapping[str, Any]) -> list[str]:
    """The $aws shadow topics the MQTT session subscribes to, SN masked.

    The thing name defaults to the serial number, which is exactly what the
    topics would leak (same reasoning as ``machine_name`` in REDACT_KEYS),
    so it is masked with the shared serial mask: only the last 4 characters
    stay visible — enough to match a topic against a broker log.
    """
    thing = cloud.get("machine_name") or entry_data.get(CONF_MACHINE_NAME) or entry_data.get(CONF_SERIAL_NUMBER)
    if not isinstance(thing, str) or not thing.strip():
        return []
    masked = mask_serial(thing.strip())
    topics: list[str] = []
    for shadow in SHADOWS:
        topics.append(shadow_accepted_topic(masked, shadow))
        topics.append(shadow_rejected_topic(masked, shadow))
    return topics


def _cloud_diagnostics(
    entry_data: Mapping[str, Any],
    entry_options: Mapping[str, Any],
    coordinator: Any,
    last_data: Mapping[str, Any],
    private_values: tuple[str, ...],
) -> dict[str, Any]:
    """Cloud connection state for the dump (issue #18)."""
    # The reconfigure flow stores the switch in ``entry.data``; ``options`` is
    # read first so a future move to options needs no change here.
    enabled = bool(entry_options.get(CONF_CLOUD_TELEMETRY, entry_data.get(CONF_CLOUD_TELEMETRY, False)))
    cloud = last_data.get("cloud")
    cloud = cloud if isinstance(cloud, Mapping) else {}
    raw_error = last_data.get("cloud_error")
    if not isinstance(raw_error, str):
        # ``data["cloud_error"]`` only exists once a refresh has run; the
        # coordinator's own cache is the earlier of the two.
        raw_error = getattr(coordinator, "_cloud_error", None) if coordinator is not None else None
    error = _sanitize_cloud_error(
        raw_error,
        host=entry_data.get(CONF_HOST),
        private_values=private_values,
    )
    if error is not None:
        # Exact wall clock of the failed cloud hop when the coordinator
        # recorded it; the refresh time is the fallback for older states.
        error["last_attempt"] = _to_iso(getattr(coordinator, "_cloud_error_at", None)) or (
            _to_iso(getattr(coordinator, "last_checked_time", None)) if coordinator is not None else None
        )
    return {
        "cloud_telemetry_enabled": enabled,
        "cloud_connected": bool(cloud.get("status_flat") or cloud.get("capabilities_flat")) and error is None,
        "last_cloud_message": _to_iso(cloud.get("fetched_at")),
        "cloud_topics_subscribed": _cloud_topics(entry_data, cloud),
        "cloud_error": error,
    }


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    """Return a redacted diagnostics dump for a Daedalus config entry."""
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    entry_data = _entry_mapping(getattr(entry, "data", None))
    entry_options = _entry_mapping(getattr(entry, "options", None))

    coord_state: dict[str, Any] = {}
    last_data = _entry_mapping(getattr(coordinator, "data", None))
    last_cloud = _entry_mapping(last_data.get("cloud"))
    private_values = {
        value
        for data in (entry_data, entry_options, last_cloud)
        for key in REDACT_KEYS
        if isinstance(value := data.get(key), str) and value
    }
    # Longest first handles values that contain another private value.
    private_values_by_length = tuple(sorted(private_values, key=len, reverse=True))
    if coordinator is not None:
        exception = getattr(coordinator, "last_exception", None)
        exception_message = ""
        if exception is not None:
            sanitized = _sanitize_diagnostic_text(
                str(exception), host=entry_data.get(CONF_HOST), private_values=private_values_by_length
            )
            exception_message = f"{type(exception).__name__}: {sanitized}"
        coord_state = {
            "last_update_success": bool(getattr(coordinator, "last_update_success", False)),
            "last_exception": exception_message,
            "update_interval_s": getattr(
                getattr(coordinator, "update_interval", None), "total_seconds", lambda: None
            )(),
            "data_keys": sorted(last_data.keys()),
            "lan_connected": bool(last_data.get("connected")),
        }
        if isinstance(lan_error := last_data.get("lan_error"), str):
            coord_state["lan_error"] = _sanitize_diagnostic_text(
                lan_error, host=entry_data.get(CONF_HOST), private_values=private_values_by_length
            )

    return {
        "entry": {
            "version": entry.version,
            "minor_version": entry.minor_version,
            "data": async_redact_data(dict(entry_data), REDACT_KEYS),
            "options": async_redact_data(dict(entry_options), REDACT_KEYS),
        },
        "coordinator": coord_state,
        "cloud": _cloud_diagnostics(entry_data, entry_options, coordinator, last_data, private_values_by_length),
    }


async def async_get_device_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
    device,  # noqa: ANN001 — DeviceEntry, type avoided to skip import cycle
) -> dict[str, Any]:
    """Device-scoped diagnostics — same payload as entry-scoped for now."""
    return await async_get_config_entry_diagnostics(hass, entry)
