"""Device-page button for the opt-in Daedalus warm-up action."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_AROMATIC_WARMUP_PENDING, CONF_SERIAL_NUMBER, CONF_SESSION_TOKEN, DOMAIN
from .coordinator import DaedalusCoordinator
from .entity import DaedalusEntity
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
        coordinator: DaedalusCoordinator = hass.data[DOMAIN][entry.entry_id]
        async_add_entities([DaedalusWarmupButton(coordinator, entry)])


class DaedalusWarmupButton(DaedalusEntity, ButtonEntity):
    """Route a physical machine's button through the existing admin service."""

    _attr_has_entity_name = True
    _attr_translation_key = SERVICE_WARM_UP

    def __init__(self, coordinator: DaedalusCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, key=SERVICE_WARM_UP)
        self._entry = entry

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
