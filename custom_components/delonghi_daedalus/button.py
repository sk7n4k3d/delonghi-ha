"""Device-page button for the opt-in Daedalus warm-up action."""

from __future__ import annotations

from hashlib import sha256

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_AROMATIC_WARMUP_PENDING, CONF_SERIAL_NUMBER, CONF_SESSION_TOKEN, DOMAIN, MANUFACTURER
from .warmup_action import ATTR_CONFIG_ENTRY_ID, SERVICE_WARM_UP, has_unique_warmup_serial, warmup_enabled


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Add a button only for an opted-in, uniquely configured machine."""
    serial = entry.data.get(CONF_SERIAL_NUMBER)
    if (
        entry.disabled_by is None
        and warmup_enabled(entry)
        and entry.data.get(CONF_SESSION_TOKEN)
        and isinstance(serial, str)
        and serial.strip()
        and has_unique_warmup_serial(hass, serial)
    ):
        async_add_entities([DaedalusWarmupButton(entry, serial)])


class DaedalusWarmupButton(ButtonEntity):
    """Route a physical machine's button through the existing admin service."""

    _attr_has_entity_name = True
    _attr_translation_key = SERVICE_WARM_UP

    def __init__(self, entry: ConfigEntry, serial: str) -> None:
        self._entry = entry
        self._attr_unique_id = f"{serial}_{SERVICE_WARM_UP}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, sha256(f"{DOMAIN}:{serial}".encode()).hexdigest())},
            manufacturer=MANUFACTURER,
            translation_key="coffee_machine",
        )

    @property
    def available(self) -> bool:
        """Keep a stale button unusable while its entry reloads."""
        return (
            self._entry.disabled_by is None
            and warmup_enabled(self._entry)
            and bool(self._entry.data.get(CONF_SESSION_TOKEN))
            and not self._entry.data.get(CONF_AROMATIC_WARMUP_PENDING)
        )

    async def async_press(self) -> None:
        """Request warm-up with the original entity service context."""
        context = self._context
        if context is None or not self.enabled or not self.available:
            raise HomeAssistantError("Warm-up is unavailable for this machine.")
        await self.hass.services.async_call(
            DOMAIN,
            SERVICE_WARM_UP,
            {ATTR_CONFIG_ENTRY_ID: self._entry.entry_id},
            blocking=True,
            context=context,
        )
