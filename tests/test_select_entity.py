"""Test select.py — DeLonghiProfileSelect entity."""

import asyncio
from unittest.mock import MagicMock

import pytest

from custom_components.delonghi_coffee import select as select_mod  # noqa: E402
from custom_components.delonghi_coffee.select import DeLonghiProfileSelect  # noqa: E402


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_coordinator(profiles=None, selected_profile=None):
    coord = MagicMock()
    coord.selected_profile = selected_profile
    coord.data = {"profiles": profiles or {}}
    return coord


class TestProfileSelectInit:
    def test_attributes(self):
        sel = DeLonghiProfileSelect(_make_coordinator(), "DSN-1", "ECAM", "Soul", "1.0")
        assert sel._attr_unique_id == "DSN-1_profile_select"
        assert sel._attr_translation_key == "profile_select"
        assert sel._attr_icon == "mdi:account-circle"
        assert sel._attr_has_entity_name is True


class TestProfileSelectOptions:
    def test_default_when_no_profiles(self):
        sel = DeLonghiProfileSelect(_make_coordinator(), "DSN", "m", "n", None)
        assert sel.options == ["Profile 1", "Profile 2", "Profile 3", "Profile 4"]

    def test_named_profiles(self):
        profiles = {1: {"name": "Sebastien"}, 2: {"name": "Sasha"}, 3: {"name": "Anna"}}
        sel = DeLonghiProfileSelect(_make_coordinator(profiles=profiles), "DSN", "m", "n", None)
        assert sel.options == ["Sebastien", "Sasha", "Anna"]

    def test_unnamed_profile_uses_default_label(self):
        profiles = {1: {"name": "Sebastien"}, 2: {}, 3: {"name": "Anna"}}
        sel = DeLonghiProfileSelect(_make_coordinator(profiles=profiles), "DSN", "m", "n", None)
        assert sel.options == ["Sebastien", "Profile 2", "Anna"]

    def test_options_sorted_by_profile_id(self):
        profiles = {3: {"name": "C"}, 1: {"name": "A"}, 2: {"name": "B"}}
        sel = DeLonghiProfileSelect(_make_coordinator(profiles=profiles), "DSN", "m", "n", None)
        assert sel.options == ["A", "B", "C"]


class TestProfileSelectCurrentOption:
    def test_returns_none_when_unselected(self):
        sel = DeLonghiProfileSelect(_make_coordinator(selected_profile=None), "DSN", "m", "n", None)
        assert sel.current_option is None

    def test_returns_named_profile(self):
        profiles = {1: {"name": "Sebastien"}, 2: {"name": "Sasha"}}
        sel = DeLonghiProfileSelect(_make_coordinator(profiles=profiles, selected_profile=2), "DSN", "m", "n", None)
        assert sel.current_option == "Sasha"

    def test_falls_back_to_default_label_when_no_name(self):
        sel = DeLonghiProfileSelect(_make_coordinator(profiles={3: {}}, selected_profile=3), "DSN", "m", "n", None)
        assert sel.current_option == "Profile 3"

    def test_falls_back_to_default_label_when_profile_unknown(self):
        sel = DeLonghiProfileSelect(_make_coordinator(profiles={}, selected_profile=4), "DSN", "m", "n", None)
        assert sel.current_option == "Profile 4"


class TestProfileSelectAsyncSelectOption:
    def test_selects_by_named_profile(self):
        profiles = {1: {"name": "Sebastien"}, 2: {"name": "Sasha"}}
        coord = _make_coordinator(profiles=profiles, selected_profile=1)
        sel = DeLonghiProfileSelect(coord, "DSN", "m", "n", None)
        sel.async_write_ha_state = MagicMock()
        _run(sel.async_select_option("Sasha"))
        assert coord.selected_profile == 2
        sel.async_write_ha_state.assert_called_once()

    def test_selects_by_default_label_fallback(self):
        coord = _make_coordinator(profiles={}, selected_profile=1)
        sel = DeLonghiProfileSelect(coord, "DSN", "m", "n", None)
        sel.async_write_ha_state = MagicMock()
        _run(sel.async_select_option("Profile 3"))
        assert coord.selected_profile == 3
        sel.async_write_ha_state.assert_called_once()

    def test_unknown_option_logs_warning_no_change(self):
        coord = _make_coordinator(profiles={1: {"name": "X"}}, selected_profile=1)
        sel = DeLonghiProfileSelect(coord, "DSN", "m", "n", None)
        sel.async_write_ha_state = MagicMock()
        _run(sel.async_select_option("ghost_profile"))
        assert coord.selected_profile == 1  # unchanged
        sel.async_write_ha_state.assert_not_called()

    def test_named_match_takes_priority_over_fallback(self):
        # Profile 2 has named "Profile 3" — selecting "Profile 3" should pick id=2
        profiles = {2: {"name": "Profile 3"}}
        coord = _make_coordinator(profiles=profiles, selected_profile=1)
        sel = DeLonghiProfileSelect(coord, "DSN", "m", "n", None)
        sel.async_write_ha_state = MagicMock()
        _run(sel.async_select_option("Profile 3"))
        assert coord.selected_profile == 2

    def test_default_label_only_within_1_to_4(self):
        """Profile 5/6 default labels should NOT be matched by fallback."""
        coord = _make_coordinator(profiles={}, selected_profile=1)
        sel = DeLonghiProfileSelect(coord, "DSN", "m", "n", None)
        sel.async_write_ha_state = MagicMock()
        _run(sel.async_select_option("Profile 7"))
        assert coord.selected_profile == 1  # unchanged
        sel.async_write_ha_state.assert_not_called()


class TestAsyncSetupEntry:
    def test_adds_one_select_entity(self):
        hass = MagicMock()
        entry = MagicMock()
        entry.entry_id = "eid"
        coord = _make_coordinator()
        hass.data = {
            "delonghi_coffee": {
                entry.entry_id: {
                    "coordinator": coord,
                    "dsn": "DSN",
                    "model": "ECAM",
                    "device_name": "n",
                    "sw_version": None,
                }
            }
        }
        added: list = []
        async_add = MagicMock(side_effect=lambda ents: added.extend(ents))
        _run(select_mod.async_setup_entry(hass, entry, async_add))
        assert len(added) == 1
        assert isinstance(added[0], DeLonghiProfileSelect)


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
