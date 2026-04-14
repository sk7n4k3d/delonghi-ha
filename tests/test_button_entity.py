"""Test button.py — entity classes + async_setup_entry flow."""

import asyncio
from unittest.mock import MagicMock

import pytest

from custom_components.delonghi_coffee import button as button_mod  # noqa: E402
from custom_components.delonghi_coffee.api import DeLonghiApiError  # noqa: E402
from custom_components.delonghi_coffee.button import (  # noqa: E402
    DeLonghiBrewButton,
    DeLonghiCancelButton,
    DeLonghiSyncButton,
)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_coordinator(selected_profile=2, custom_recipe_names=None, beverages=None, machine_state="Ready"):
    coord = MagicMock()
    coord.selected_profile = selected_profile
    coord.custom_recipe_names = custom_recipe_names or {}
    coord.beverages = beverages or []
    coord.data = {"machine_state": machine_state}
    coord.async_add_listener = MagicMock(return_value=lambda: None)
    return coord


def _make_api():
    api = MagicMock()
    api.brew_beverage = MagicMock()
    api.cancel_brew = MagicMock()
    api.sync_recipes = MagicMock()
    return api


def _make_hass_with_executor():
    hass = MagicMock()

    async def _run_executor(func, *args, **kwargs):
        return func(*args, **kwargs)

    hass.async_add_executor_job = _run_executor
    return hass


class TestBrewButtonInit:
    def test_known_beverage_uses_translation_key(self):
        coord = _make_coordinator()
        btn = DeLonghiBrewButton(
            _make_api(), coord, "DSN-1", "ECAM61075MB", "Soul", "1.0", "espresso",
            {"name": "Espresso", "icon": "mdi:coffee"},
        )
        assert btn._attr_unique_id == "DSN-1_brew_espresso"
        assert btn._attr_translation_key == "brew_espresso"
        assert not hasattr(btn, "_attr_name") or btn._attr_name is None or btn._attr_name == "Espresso"
        assert btn._attr_icon == "mdi:coffee"
        assert btn._attr_has_entity_name is True

    def test_unknown_beverage_uses_attr_name(self):
        coord = _make_coordinator()
        btn = DeLonghiBrewButton(
            _make_api(), coord, "DSN-1", "model", "name", None, "mystery_brew",
            {"name": "Mystery Brew", "icon": "mdi:coffee"},
        )
        assert btn._attr_name == "Mystery Brew"
        assert not hasattr(btn, "_attr_translation_key") or btn._attr_translation_key is None

    def test_custom_recipe_uses_attr_name(self):
        coord = _make_coordinator(custom_recipe_names={"custom_1": "Booster"})
        btn = DeLonghiBrewButton(
            _make_api(), coord, "DSN-1", "model", "name", None, "custom_1",
            {"name": "Booster", "icon": "mdi:coffee-to-go"},
        )
        # custom_1 is not in BEVERAGES so should use _attr_name
        assert btn._attr_name == "Booster"


class TestBrewButtonPress:
    def test_uses_selected_profile(self):
        coord = _make_coordinator(selected_profile=3)
        api = _make_api()
        btn = DeLonghiBrewButton(api, coord, "DSN", "m", "n", None, "espresso", {"name": "X", "icon": "y"})
        btn.hass = _make_hass_with_executor()
        _run(btn.async_press())
        api.brew_beverage.assert_called_once_with("DSN", "espresso", 3)

    def test_falls_back_to_profile_1_when_none(self):
        coord = _make_coordinator(selected_profile=None)
        api = _make_api()
        btn = DeLonghiBrewButton(api, coord, "DSN", "m", "n", None, "espresso", {"name": "X", "icon": "y"})
        btn.hass = _make_hass_with_executor()
        _run(btn.async_press())
        api.brew_beverage.assert_called_once_with("DSN", "espresso", 1)

    def test_api_error_raises_home_assistant_error(self):
        from homeassistant.exceptions import HomeAssistantError

        coord = _make_coordinator()
        api = _make_api()
        api.brew_beverage.side_effect = DeLonghiApiError("water tank empty")
        btn = DeLonghiBrewButton(api, coord, "DSN", "m", "n", None, "espresso", {"name": "X", "icon": "y"})
        btn.hass = _make_hass_with_executor()
        with pytest.raises(HomeAssistantError, match="Failed to brew espresso"):
            _run(btn.async_press())


class TestCancelButton:
    def test_init_attributes(self):
        coord = _make_coordinator()
        btn = DeLonghiCancelButton(_make_api(), coord, "DSN-2", "model", "name", "1.0")
        assert btn._attr_unique_id == "DSN-2_cancel_brew"
        assert btn._attr_translation_key == "cancel_brew"
        assert btn._attr_icon == "mdi:stop-circle-outline"

    def test_available_when_brewing(self):
        coord = _make_coordinator(machine_state="Brewing")
        btn = DeLonghiCancelButton(_make_api(), coord, "DSN", "m", "n", None)
        assert btn.available is True

    def test_unavailable_when_idle(self):
        coord = _make_coordinator(machine_state="Ready")
        btn = DeLonghiCancelButton(_make_api(), coord, "DSN", "m", "n", None)
        assert btn.available is False

    def test_unavailable_when_unknown(self):
        coord = MagicMock()
        coord.data = {}  # no machine_state key
        btn = DeLonghiCancelButton(_make_api(), coord, "DSN", "m", "n", None)
        assert btn.available is False

    def test_press_calls_cancel_brew(self):
        coord = _make_coordinator(machine_state="Brewing")
        api = _make_api()
        btn = DeLonghiCancelButton(api, coord, "DSN", "m", "n", None)
        btn.hass = _make_hass_with_executor()
        _run(btn.async_press())
        api.cancel_brew.assert_called_once_with("DSN")

    def test_press_error_raises_ha_error(self):
        from homeassistant.exceptions import HomeAssistantError

        coord = _make_coordinator(machine_state="Brewing")
        api = _make_api()
        api.cancel_brew.side_effect = DeLonghiApiError("nope")
        btn = DeLonghiCancelButton(api, coord, "DSN", "m", "n", None)
        btn.hass = _make_hass_with_executor()
        with pytest.raises(HomeAssistantError, match="Failed to cancel"):
            _run(btn.async_press())


class TestSyncButton:
    def test_init_attributes(self):
        btn = DeLonghiSyncButton(_make_api(), _make_coordinator(), "DSN-3", "m", "n", None)
        assert btn._attr_unique_id == "DSN-3_sync_recipes"
        assert btn._attr_translation_key == "sync_recipes"
        assert btn._attr_icon == "mdi:cloud-sync-outline"

    def test_press_uses_selected_profile(self):
        coord = _make_coordinator(selected_profile=4)
        api = _make_api()
        btn = DeLonghiSyncButton(api, coord, "DSN", "m", "n", None)
        btn.hass = _make_hass_with_executor()
        _run(btn.async_press())
        api.sync_recipes.assert_called_once_with("DSN", 4)

    def test_press_defaults_profile_to_1(self):
        coord = _make_coordinator(selected_profile=None)
        api = _make_api()
        btn = DeLonghiSyncButton(api, coord, "DSN", "m", "n", None)
        btn.hass = _make_hass_with_executor()
        _run(btn.async_press())
        api.sync_recipes.assert_called_once_with("DSN", 1)


class TestAsyncSetupEntry:
    """async_setup_entry wires static buttons + dynamic listener for late beverages."""

    def _setup(self, beverages):
        hass = MagicMock()
        entry = MagicMock()
        entry.entry_id = "eid"
        entry.async_on_unload = MagicMock()
        coord = _make_coordinator(beverages=beverages, custom_recipe_names={})
        hass.data = {
            "delonghi_coffee": {
                entry.entry_id: {
                    "api": _make_api(),
                    "coordinator": coord,
                    "dsn": "DSN",
                    "model": "ECAM",
                    "device_name": "Test",
                    "sw_version": "1.0",
                }
            }
        }
        added: list = []
        return hass, entry, coord, added

    def test_static_buttons_added_when_no_beverages(self):
        hass, entry, coord, added = self._setup(beverages=[])
        async_add = MagicMock(side_effect=lambda ents: added.extend(ents))
        _run(button_mod.async_setup_entry(hass, entry, async_add))
        # Static cancel + sync are added always
        assert any(isinstance(e, DeLonghiCancelButton) for e in added)
        assert any(isinstance(e, DeLonghiSyncButton) for e in added)
        # Listener registered + cleanup wired
        coord.async_add_listener.assert_called_once()
        entry.async_on_unload.assert_called_once()

    def test_brew_buttons_added_when_beverages_known(self):
        hass, entry, coord, added = self._setup(beverages=["espresso", "americano"])
        async_add = MagicMock(side_effect=lambda ents: added.extend(ents))
        _run(button_mod.async_setup_entry(hass, entry, async_add))
        brew_buttons = [e for e in added if isinstance(e, DeLonghiBrewButton)]
        assert len(brew_buttons) == 2
        keys = {b._beverage_key for b in brew_buttons}
        assert keys == {"espresso", "americano"}
        # No listener needed when we already have beverages
        coord.async_add_listener.assert_not_called()

    def test_dedup_known_keys_on_second_invocation(self):
        """Listener callback re-invoked must not duplicate buttons."""
        hass, entry, coord, added = self._setup(beverages=[])
        async_add = MagicMock(side_effect=lambda ents: added.extend(ents))
        _run(button_mod.async_setup_entry(hass, entry, async_add))
        # Capture the listener that was registered
        listener_callback = coord.async_add_listener.call_args[0][0]
        # Simulate beverages arriving on next refresh
        coord.beverages = ["espresso", "americano"]
        listener_callback()
        # Should now have 2 brew buttons
        brew_buttons = [e for e in added if isinstance(e, DeLonghiBrewButton)]
        assert len(brew_buttons) == 2
        # Trigger again — must NOT add duplicates
        listener_callback()
        brew_buttons = [e for e in added if isinstance(e, DeLonghiBrewButton)]
        assert len(brew_buttons) == 2

    def test_unknown_beverage_still_added_with_warning(self):
        hass, entry, coord, added = self._setup(beverages=["mystery_brew", "espresso"])
        async_add = MagicMock(side_effect=lambda ents: added.extend(ents))
        _run(button_mod.async_setup_entry(hass, entry, async_add))
        brew_buttons = [e for e in added if isinstance(e, DeLonghiBrewButton)]
        assert len(brew_buttons) == 2  # both added, even unknown ones


@pytest.fixture(autouse=True)
def _ensure_event_loop():
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError("closed")
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    yield
