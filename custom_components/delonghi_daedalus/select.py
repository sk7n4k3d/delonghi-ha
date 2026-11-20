"""Per-device drink preferences stored in Home Assistant."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_DRINK_FAVORITES, CONF_FAVORITE_DRINK, DOMAIN
from .coordinator import DaedalusCoordinator
from .warmup_state import entry_lock


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Add the preference entity when the user has entered drink names."""
    unique_id = f"{entry.entry_id}_{CONF_FAVORITE_DRINK}"
    if entry.data.get(CONF_DRINK_FAVORITES):
        coordinator: DaedalusCoordinator = hass.data[DOMAIN][entry.entry_id]
        async_add_entities([DaedalusFavoriteDrink(coordinator, unique_id=unique_id)])
        return
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("select", DOMAIN, unique_id)
    registered = registry.async_get(entity_id) if entity_id else None
    if registered and registered.config_entry_id == entry.entry_id:
        registry.async_remove(registered.entity_id)


class DaedalusFavoriteDrink(SelectEntity):
    """Select one of the labels entered for this machine."""

    _attr_translation_key = "favorite_drink"
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: DaedalusCoordinator, *, unique_id: str) -> None:
        self.entry = coordinator.entry
        self._attr_unique_id = unique_id
        self._attr_device_info = coordinator.device.info()

    @property
    def options(self) -> list[str]:
        return self.entry.data.get(CONF_DRINK_FAVORITES, [])

    @property
    def current_option(self) -> str | None:
        selected = self.entry.data.get(CONF_FAVORITE_DRINK)
        return selected if selected in self.options else None

    async def async_select_option(self, option: str) -> None:
        async with entry_lock(self.hass, self.entry.entry_id):
            if option not in self.options:
                raise ServiceValidationError(translation_domain=DOMAIN, translation_key="unknown_favorite_drink")
            if option != self.current_option:
                self.hass.config_entries.async_update_entry(
                    self.entry, data={**self.entry.data, CONF_FAVORITE_DRINK: option}
                )
                self.async_write_ha_state()
