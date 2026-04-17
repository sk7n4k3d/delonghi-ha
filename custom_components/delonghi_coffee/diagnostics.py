"""Diagnostics support for De'Longhi Coffee."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN

REDACT_KEYS: set[str] = {
    "email",
    "password",
    "access_token",
    "refresh_token",
    "ayla_token",
    "ayla_refresh",
    "lanip_key",
    "session_token",
    "uid",
    "uidSignature",
    "signatureTimestamp",
    "lan_key",
    "device_serial",
}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    """Return diagnostics for a config entry.

    Includes redacted entry data + options, coordinator state, last raw
    properties, model identification, and rate-limit stats. Useful for
    triaging GitHub issues without asking users for separate exports.
    """
    data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    coordinator = data.get("coordinator")
    api = data.get("api")

    coord_state: dict[str, Any] = {}
    if coordinator is not None:
        coord_state = {
            "last_update_success": bool(getattr(coordinator, "last_update_success", False)),
            "last_exception": str(getattr(coordinator, "last_exception", "") or ""),
            "diagnostic_mode": bool(getattr(coordinator, "diagnostic_mode", False)),
            "selected_profile": getattr(coordinator, "selected_profile", None),
            "beverages_count": len(getattr(coordinator, "beverages", []) or []),
            "keepalive_failures": getattr(coordinator, "_keepalive_failures", 0),
            "lan_active": bool(getattr(coordinator, "_lan_active", False)),
            "custom_recipe_names": getattr(coordinator, "custom_recipe_names", {}),
            "raw_properties_keys": sorted(list((coordinator.data or {}).keys()))
            if getattr(coordinator, "data", None)
            else [],
        }

    api_state: dict[str, Any] = {}
    if api is not None:
        rate_tracker = getattr(api, "rate_tracker", None)
        api_state = {
            "oem_model": getattr(api, "_oem_model", None),
            "device_name": getattr(api, "device_name", None),
            "sw_version": getattr(api, "sw_version", None),
            "ping_supported": getattr(api, "_ping_supported", None),
            "rate_current": getattr(rate_tracker, "current_rate", None) if rate_tracker else None,
        }

    return {
        "entry": {
            "version": entry.version,
            "minor_version": entry.minor_version,
            "data": async_redact_data(dict(entry.data), REDACT_KEYS),
            "options": async_redact_data(dict(entry.options), REDACT_KEYS),
        },
        "device": {
            "model": data.get("model"),
            "device_name": data.get("device_name"),
            "sw_version": data.get("sw_version"),
        },
        "coordinator": coord_state,
        "api": api_state,
    }


async def async_get_device_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
    device,  # noqa: ANN001 - HA passes DeviceEntry, type hint avoided to skip import
) -> dict[str, Any]:
    """Return device-scoped diagnostics (same payload as entry-scoped for now)."""
    return await async_get_config_entry_diagnostics(hass, entry)
