"""HA entry setup for the Daedalus integration (imported lazily)."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN, GIGYA_API_KEYS
from .coordinator import DaedalusCoordinator
from .gigya_auth import apikey_fingerprint

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR]

# Canonical Gigya apiKey lengths extracted from the My Coffee Lounge APK
# manifest (v1.2.2). A truncated value reproduces Gigya errorCode 400093 —
# H-daedalus-4 surfaced this as the most likely cause of Issue #18's "Invalid
# ApiKey" reports (HACS mirror sync corruption suspected). Logging at startup
# turns a silent corruption into a visible warning the user can act on.
_EXPECTED_APIKEY_LENGTHS: dict[str, int] = {"EU": 24, "EU_US": 66, "CH": 66}


def _validate_apikey_lengths() -> None:
    """Warn loudly if any GIGYA_API_KEYS value has the wrong length.

    Truncation is the live root-cause hypothesis for stivxgamer's 400093
    on Issue #18. We do NOT raise — a wrong length is fixable by reinstall
    and crashing the integration would just hide the diagnostic from the
    user. We log a fingerprint of every key (non-secret, see
    `gigya_auth.apikey_fingerprint`) so a remote bug report shows the
    real install state at a glance.
    """
    for pool, expected_len in _EXPECTED_APIKEY_LENGTHS.items():
        key = GIGYA_API_KEYS.get(pool, "")
        actual_len = len(key)
        fingerprint = apikey_fingerprint(key) if key else "len:0 sha1[:8]=<empty>"
        if actual_len != expected_len:
            _LOGGER.error(
                "Daedalus apiKey for pool %s has unexpected length %d (expected %d) — install likely corrupted; %s",
                pool,
                actual_len,
                expected_len,
                fingerprint,
            )
        else:
            _LOGGER.debug("Daedalus apiKey %s OK: %s", pool, fingerprint)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    _validate_apikey_lengths()
    coordinator = DaedalusCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator: DaedalusCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_shutdown()
    return unload_ok
