"""Favorites registry handling for empty lists and duplicate device entries."""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from custom_components.delonghi_daedalus import select
from custom_components.delonghi_daedalus.const import CONF_DRINK_FAVORITES, DOMAIN


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
    monkeypatch.setattr(select.er, "async_get", Mock(return_value=registry))

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
