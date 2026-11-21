"""Issue #90 — say out loud when a counter is being masked by its baseline.

A baseline sitting above the cloud is normal at first (it is a manual entry, so
it starts ahead by design). What is not normal is a cloud that never catches up:
the sensor is then publishing a number nobody is measuring. These tests pin the
streak the coordinator keeps and the attributes the sensor exposes.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from custom_components.delonghi_coffee.const import BASELINE_MASK_ALERT_REFRESHES
from custom_components.delonghi_coffee.coordinator import DeLonghiCoordinator
from custom_components.delonghi_coffee.local_baseline import (
    UNKNOWN_DATE,
    LocalBaselineStore,
)
from custom_components.delonghi_coffee.sensor import COUNTER_SENSORS, DeLonghiCounterSensor


def _make_coord(baselines: dict[str, int]):
    coord = DeLonghiCoordinator(MagicMock(), MagicMock(), "DSN-1")
    store = coord.local_baseline
    asyncio.run(store.async_set_many(baselines))
    coord._baseline_loaded = True
    return coord, store


def _observe(coord, counters):
    coord._track_baseline_masking(counters, coord._baseline_revisions.copy())


def _make_sensor(coord, key: str):
    return DeLonghiCounterSensor(coord, "DSN-1", "Eletta", "Machine", None, key, COUNTER_SENSORS[key])


class TestStreak:
    def test_counts_one_per_full_refresh_while_masked(self):
        coord, _ = _make_coord({"espresso": 30})
        for expected in range(1, 5):
            _observe(coord, {"espresso": 20})
            assert coord._baseline_mask_streak == {"espresso": expected}

    def test_catch_up_resets_the_streak(self):
        coord, _ = _make_coord({"espresso": 30})
        _observe(coord, {"espresso": 20})
        _observe(coord, {"espresso": 20})
        _observe(coord, {"espresso": 31})
        assert coord._baseline_mask_streak == {}

    def test_missing_cloud_value_counts_as_masked(self):
        """parse_counters can leave a key out or keep a raw string; neither is
        a catch-up, and merge() would keep showing the baseline."""
        coord, _ = _make_coord({"espresso": 30})
        _observe(coord, {})
        assert coord._baseline_mask_streak == {"espresso": 1}

        _observe(coord, {"espresso": "20"})
        assert coord._baseline_mask_streak == {"espresso": 2}

    def test_baseline_removed_drops_the_key(self):
        coord, store = _make_coord({"espresso": 30, "tea": 15})
        cloud = {"espresso": 20, "tea": 1}
        _observe(coord, cloud)
        coord.data = {"counters": cloud, "baseline_masking": coord._baseline_mask_streak.copy()}
        asyncio.run(store.async_clear(["tea"]))
        assert coord.data["baseline_masking"] == coord._baseline_mask_streak == {"espresso": 1}
        asyncio.run(store.async_set_many({"tea": 15}))
        assert _make_sensor(coord, "tea").extra_state_attributes["cloud_below_baseline_refreshes"] == 0
        _observe(coord, cloud)
        assert coord._baseline_mask_streak == {"espresso": 2, "tea": 1}

    def test_threshold_is_three_refreshes(self):
        """3 × FULL_REFRESH_INTERVAL ≈ 30 min: past the transient window, and
        the freeze being masked is permanent, so the latency is free."""
        assert BASELINE_MASK_ALERT_REFRESHES == 3


class TestSensorAttributes:
    def _coord_with_data(self, coord, counters, streak):
        coord.data = {"counters": counters, "baseline_masking": streak}
        return coord

    def test_undated_baseline_is_unknown_text_but_a_real_none_age(self):
        """Two shapes on purpose: the date field is prose ("unknown" reads fine
        on the card), the age field is numeric, so it must be a real None for
        templates to stay `is none` rather than compare a magic string."""
        coord, store = _make_coord({"espresso": 1476})
        # Simulate a pre-dating entry: value present, no capture date.
        store._set_at = {}
        self._coord_with_data(coord, {"espresso": 1406}, {"espresso": 5})
        attrs = _make_sensor(coord, "espresso").extra_state_attributes
        assert attrs["baseline_set_at"] == UNKNOWN_DATE
        assert attrs["baseline_age_days"] is None
        assert attrs["source"] == "baseline"

    def test_stale_flag_flips_at_the_threshold(self):
        coord, _ = _make_coord({"espresso": 1476})
        sensor = _make_sensor(coord, "espresso")
        self._coord_with_data(coord, {"espresso": 1406}, {"espresso": 2})
        assert sensor.extra_state_attributes["baseline_stale"] is False
        self._coord_with_data(coord, {"espresso": 1406}, {"espresso": 3})
        assert sensor.extra_state_attributes["baseline_stale"] is True

    def test_age_and_streak_are_exposed_together(self):
        coord, store = _make_coord({"espresso": 1476})
        store._set_at = {"espresso": (datetime.now(UTC) - timedelta(days=191)).isoformat(timespec="seconds")}
        self._coord_with_data(coord, {"espresso": 1406}, {"espresso": 7})
        attrs = _make_sensor(coord, "espresso").extra_state_attributes
        assert attrs["baseline_age_days"] == 191
        assert attrs["cloud_below_baseline_refreshes"] == 7
        assert attrs["cloud_value"] == 1406
        assert attrs["baseline_value"] == 1476

    def test_no_baseline_means_no_attributes(self):
        """Users who never touched the feature keep a clean entity card."""
        coord = DeLonghiCoordinator(MagicMock(), MagicMock(), "DSN-1")
        coord.local_baseline = LocalBaselineStore(MagicMock(), "DSN-1")
        coord.data = {"counters": {"espresso": 1406}, "baseline_masking": {}}
        assert _make_sensor(coord, "espresso").extra_state_attributes == {}

    def test_missing_masking_key_is_tolerated(self):
        """An older coordinator payload must not break the attribute write."""
        coord, _ = _make_coord({"espresso": 1476})
        coord.data = {"counters": {"espresso": 1406}}
        attrs = _make_sensor(coord, "espresso").extra_state_attributes
        assert attrs["cloud_below_baseline_refreshes"] == 0
        assert attrs["baseline_stale"] is False
