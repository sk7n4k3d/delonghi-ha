"""Diagnostics support for the De'Longhi Daedalus integration.

Provides a HA-native "Download diagnostics" payload that triages auth /
LAN-connectivity bugs without asking users to scrape logs by hand. All
fields that could leak the user's account or session material are
redacted via `homeassistant.components.diagnostics.async_redact_data`.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_HOST, DOMAIN
from .redact import sanitize_text

# Anything in this set is replaced with "**REDACTED**" in the payload.
# Conservative on purpose: even the email is redacted because the support
# zip can be uploaded to a public issue tracker.
REDACT_KEYS: set[str] = {
    "email",
    "password",
    "jwt",
    "session_token",
    "session_secret",
    "id_token",
    "oauth_token",
    "AuthToken",
    "apiKey",
    "uid",
    "uidSignature",
    "signatureTimestamp",
    "serial_number",
    "SerialNo",
    # H-daedalus-3 (audit 2026-05-08): config_flow stores the serial number
    # under CONF_MACHINE_NAME too (see config_flow.py:179). Without this
    # entry the SN leaked verbatim into uploaded diag dumps via the
    # ``machine_name`` field even though ``serial_number`` was scrubbed.
    "machine_name",
    "host",
    "lan_ip",
}


def _redact_last_exception(exc: object | None, *, host: object = None) -> str:
    """Free-text render of ``coordinator.last_exception``, safe to share.

    ``async_redact_data`` is key-indexed and cannot scrub a free-text value,
    yet exception texts carry the LAN WS URL (host), Gigya error strings that
    may echo the account email, and JWT fragments (audit 2026-11-05). The
    dump is designed to be pasted into a public issue — so the value is
    sanitized (with the entry's configured host: private IPv6 forms are only
    scrubbed literally) and prefixed with the exception type for triage.
    """
    if exc is None:
        return ""
    return f"{type(exc).__name__}: {sanitize_text(str(exc), host=host)}"


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    """Return a redacted diagnostics dump for a Daedalus config entry."""
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)

    coord_state: dict[str, Any] = {}
    if coordinator is not None:
        last_data = getattr(coordinator, "data", None) or {}
        coord_state = {
            "last_update_success": bool(getattr(coordinator, "last_update_success", False)),
            "last_exception": _redact_last_exception(
                getattr(coordinator, "last_exception", None),
                host=entry.data.get(CONF_HOST),
            ),
            "update_interval_s": getattr(
                getattr(coordinator, "update_interval", None), "total_seconds", lambda: None
            )(),
            "data_keys": sorted(last_data.keys()),
            "lan_connected": bool(last_data.get("connected")),
        }

    return {
        "entry": {
            "version": entry.version,
            "minor_version": entry.minor_version,
            "data": async_redact_data(dict(entry.data), REDACT_KEYS),
            "options": async_redact_data(dict(entry.options), REDACT_KEYS),
        },
        "coordinator": coord_state,
    }


async def async_get_device_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
    device,  # noqa: ANN001 — DeviceEntry, type avoided to skip import cycle
) -> dict[str, Any]:
    """Device-scoped diagnostics — same payload as entry-scoped for now."""
    return await async_get_config_entry_diagnostics(hass, entry)
