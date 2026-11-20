"""Favorite drink selection and entity registry handling."""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from custom_components.delonghi_daedalus import select
from custom_components.delonghi_daedalus.const import CONF_DRINK_FAVORITES, CONF_FAVORITE_DRINK, DOMAIN


def test_selecting_a_removed_favorite_raises_validation_error(monkeypatch):
    class ServiceValidationError(Exception):
        def __init__(self, *, translation_domain, translation_key):
            super().__init__(translation_key)
            self.translation_domain = translation_domain

    monkeypatch.setattr(select, "ServiceValidationError", ServiceValidationError)
    entry = SimpleNamespace(
        entry_id="entry",
        data={CONF_DRINK_FAVORITES: ["Espresso", "Cappuccino"], CONF_FAVORITE_DRINK: "Espresso"},
    )
    favorite = select.DaedalusFavoriteDrink(
        SimpleNamespace(entry=entry, device=Mock()), unique_id="entry_favorite_drink"
    )
    favorite.hass = SimpleNamespace(data={}, config_entries=Mock())
    favorite.async_write_ha_state = Mock()
    assert "Cappuccino" in favorite.options
    entry.data = {**entry.data, CONF_DRINK_FAVORITES: ["Espresso"]}

    with pytest.raises(ServiceValidationError, match="^unknown_favorite_drink$") as error:
        asyncio.run(favorite.async_select_option("Cappuccino"))

    assert error.value.translation_domain == DOMAIN
    assert favorite.current_option == "Espresso"
    favorite.hass.config_entries.async_update_entry.assert_not_called()
    favorite.async_write_ha_state.assert_not_called()


@pytest.mark.parametrize("old_owner", ["entry", "another-entry"])
def test_cleared_favorites_remove_only_their_own_registry_entry(monkeypatch, old_owner):
    entry = SimpleNamespace(entry_id="entry", data={CONF_DRINK_FAVORITES: []})
    hass = SimpleNamespace()
    add = Mock()
    registry = Mock()
    registry.async_get_entity_id.return_value = "select.test_favorite"
    registered = {
        "select.test_favorite": SimpleNamespace(entity_id="select.test_favorite", config_entry_id=old_owner),
        "sensor.normal_entity": SimpleNamespace(entity_id="sensor.normal_entity", config_entry_id=entry.entry_id),
    }
    registry.async_get.side_effect = registered.get
    registry.async_remove.side_effect = registered.pop
    monkeypatch.setattr(select, "er", SimpleNamespace(async_get=lambda _hass: registry))

    asyncio.run(select.async_setup_entry(hass, entry, add))

    add.assert_not_called()
    registry.async_get_entity_id.assert_called_once_with("select", DOMAIN, "entry_favorite_drink")
    assert "sensor.normal_entity" in registered
    if old_owner == entry.entry_id:
        registry.async_remove.assert_called_once_with("select.test_favorite")
        assert "select.test_favorite" not in registered
    else:
        registry.async_remove.assert_not_called()
        assert "select.test_favorite" in registered
