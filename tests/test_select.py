"""Profile select entity tests.

Covers option enumeration, current_option resolution, and async_select_option
dispatch — including the silent-failure branch where an option string does
not correspond to any known profile.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock


def _make_coord(profiles: dict | None = None, selected: int | None = None) -> MagicMock:
    coord = MagicMock()
    coord.data = {"profiles": profiles} if profiles is not None else {}
    coord.selected_profile = selected
    return coord


def _entity(coord: MagicMock):
    from custom_components.delonghi_coffee.select import DeLonghiProfileSelect

    entity = DeLonghiProfileSelect(
        coordinator=coord,
        dsn="DSN",
        model="t",
        device_name="T",
        sw_version="1",
    )
    entity.coordinator = coord
    return entity


class TestProfileSelectOptions:
    def test_options_fallback_when_no_profiles(self):
        entity = _entity(_make_coord())
        assert entity.options == ["Profile 1", "Profile 2", "Profile 3", "Profile 4"]

    def test_options_use_profile_names_when_available(self):
        profiles = {
            1: {"name": "Work"},
            2: {"name": "Weekend"},
            3: {"name": "Guest"},
        }
        entity = _entity(_make_coord(profiles=profiles))
        assert entity.options == ["Work", "Weekend", "Guest"]

    def test_options_fall_back_per_missing_name(self):
        profiles = {1: {"name": "Work"}, 2: {}, 3: {"name": "Guest"}}
        entity = _entity(_make_coord(profiles=profiles))
        assert entity.options == ["Work", "Profile 2", "Guest"]

    def test_options_sorted_by_profile_id(self):
        """Profiles dict order is not stable — entity must sort by pid."""
        profiles = {3: {"name": "C"}, 1: {"name": "A"}, 2: {"name": "B"}}
        entity = _entity(_make_coord(profiles=profiles))
        assert entity.options == ["A", "B", "C"]


class TestProfileSelectCurrentOption:
    def test_current_option_none_when_nothing_selected(self):
        entity = _entity(_make_coord(selected=None))
        assert entity.current_option is None

    def test_current_option_reads_profile_name(self):
        profiles = {2: {"name": "Weekend"}}
        entity = _entity(_make_coord(profiles=profiles, selected=2))
        assert entity.current_option == "Weekend"

    def test_current_option_fallback_when_name_missing(self):
        entity = _entity(_make_coord(profiles={2: {}}, selected=2))
        assert entity.current_option == "Profile 2"


class TestProfileSelectSelectOption:
    def test_select_option_by_profile_name_updates_coordinator(self):
        profiles = {
            1: {"name": "Work"},
            2: {"name": "Weekend"},
        }
        coord = _make_coord(profiles=profiles)
        entity = _entity(coord)
        asyncio.run(entity.async_select_option("Weekend"))
        assert coord.selected_profile == 2

    def test_select_option_falls_back_to_profile_number_format(self):
        """No matching profile, but 'Profile N' parsed directly."""
        coord = _make_coord()
        entity = _entity(coord)
        asyncio.run(entity.async_select_option("Profile 3"))
        assert coord.selected_profile == 3

    def test_select_option_unknown_does_not_change_profile(self):
        coord = _make_coord(selected=1)
        entity = _entity(coord)
        asyncio.run(entity.async_select_option("Mystery"))
        assert coord.selected_profile == 1

    def test_select_option_profile_name_wins_over_numeric_parse(self):
        """A profile named 'Profile 2' should still use the profile's real id."""
        profiles = {
            1: {"name": "Profile 2"},  # troll naming
            2: {"name": "Real Two"},
        }
        coord = _make_coord(profiles=profiles)
        entity = _entity(coord)
        asyncio.run(entity.async_select_option("Profile 2"))
        # Must match on profile name first — id 1
        assert coord.selected_profile == 1
