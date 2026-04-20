"""De'Longhi Daedalus — My Coffee Lounge cloud/LAN integration.

Separate from `delonghi_coffee` (Coffee Link / Ayla) because the Daedalus
stack (Gigya + AWS IoT Core + ESP-IDF BLE) shares nothing with Coffee Link
at the wire protocol level. See delonghi-ha issue #18 for context.

Setup entry points are lazy-imported so that pure-helper modules
(`gigya_auth`, `lan_protocol`) remain importable without pulling the full
Home Assistant runtime — useful for offline unit tests and for HACS install
paths where the HA core is present but not initialised.
"""

from __future__ import annotations

from .const import DOMAIN

__all__ = ["DOMAIN", "async_setup_entry", "async_unload_entry"]


async def async_setup_entry(hass, entry):  # pragma: no cover - thin wrapper
    from .entry import async_setup_entry as _impl

    return await _impl(hass, entry)


async def async_unload_entry(hass, entry):  # pragma: no cover - thin wrapper
    from .entry import async_unload_entry as _impl

    return await _impl(hass, entry)
