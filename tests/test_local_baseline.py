"""Issue #90 — per-key capture dates and selective clearing.

A baseline is a number somebody read off the machine's screen on some date.
Before #90 the date was not kept at all, so a two-year-old entry looked exactly
like one entered this morning — which is how a frozen 1476 passed for a live
count. These tests pin the dating, the "unknown is not fresh" rule, and the
backwards compatibility that keeps existing users' baselines on disk.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from custom_components.delonghi_coffee.local_baseline import (
    _SET_AT_KEY,
    UNKNOWN_DATE,
    LocalBaselineStore,
)


def _store() -> LocalBaselineStore:
    return LocalBaselineStore(MagicMock(), "DSN123")


def _seed(store: LocalBaselineStore, payload: dict) -> None:
    """Put a raw payload straight into the (mocked) store before loading."""
    store._store._data = payload


class TestLegacyPayload:
    """A file written before dating existed must load untouched."""

    def test_flat_int_file_loads(self):
        store = _store()
        _seed(store, {"total_espressos": 2472, "espresso": 1476})
        asyncio.run(store.async_load())
        assert store.get("espresso") == 1476
        assert store.get("total_espressos") == 2472

    def test_undated_entry_is_unknown_not_fresh(self):
        store = _store()
        _seed(store, {"espresso": 1476})
        asyncio.run(store.async_load())
        assert store.get_set_at("espresso") == UNKNOWN_DATE
        assert store.age_days("espresso") is None

    def test_missing_key_reports_unknown_too(self):
        store = _store()
        _seed(store, {"espresso": 1476})
        asyncio.run(store.async_load())
        assert store.get_set_at("coffee") == UNKNOWN_DATE


class TestDatedPayload:
    def test_set_many_stamps_a_date(self):
        store = _store()
        asyncio.run(store.async_set_many({"espresso": 1476}))
        assert store.get_set_at("espresso") != UNKNOWN_DATE
        assert store.age_days("espresso") == 0

    def test_dates_survive_a_save_load_round_trip(self):
        store = _store()
        asyncio.run(store.async_set_many({"espresso": 1476, "tea": 35}))
        raw = store._store._data
        assert raw[_SET_AT_KEY]["espresso"]

        reloaded = _store()
        _seed(reloaded, raw)
        asyncio.run(reloaded.async_load())
        assert reloaded.get("espresso") == 1476
        assert reloaded.get_set_at("espresso") == store.get_set_at("espresso")

    def test_partial_dates_load_with_the_rest_unknown(self):
        stamp = datetime.now(UTC).isoformat(timespec="seconds")
        store = _store()
        _seed(store, {"espresso": 1476, "tea": 35, _SET_AT_KEY: {"espresso": stamp}})
        asyncio.run(store.async_load())
        assert store.get_set_at("espresso") == stamp
        assert store.get_set_at("tea") == UNKNOWN_DATE

    def test_metadata_never_leaks_into_counter_values(self):
        """The v1 loader keeps int/float only, so the dict is ignored — and an
        older build reading a new file loses the dates, not the counters."""
        store = _store()
        _seed(
            store,
            {"espresso": 1476, _SET_AT_KEY: {"espresso": "2026-05-14T10:00:00+00:00"}},
        )
        asyncio.run(store.async_load())
        assert store.all_keys() == ["espresso"]
        assert _SET_AT_KEY not in store.all_keys()

    def test_garbage_metadata_is_dropped(self):
        store = _store()
        _seed(store, {"espresso": 1476, _SET_AT_KEY: ["not", "a", "dict"]})
        asyncio.run(store.async_load())
        assert store.get("espresso") == 1476
        assert store.get_set_at("espresso") == UNKNOWN_DATE

    def test_unparsable_date_reports_undatable(self):
        store = _store()
        _seed(store, {"espresso": 1476, _SET_AT_KEY: {"espresso": "14 mai, environ"}})
        asyncio.run(store.async_load())
        assert store.age_days("espresso") is None
        assert store.get_set_at("espresso") == "14 mai, environ"

    def test_age_is_computed_from_the_stored_date(self):
        store = _store()
        old = (datetime.now(UTC) - timedelta(days=191)).isoformat(timespec="seconds")
        _seed(store, {"espresso": 1476, _SET_AT_KEY: {"espresso": old}})
        asyncio.run(store.async_load())
        assert store.age_days("espresso") == 191

    def test_naive_timestamp_is_treated_as_utc(self):
        store = _store()
        old = (datetime.now(UTC) - timedelta(days=3)).isoformat(timespec="seconds")
        _seed(store, {"espresso": 1, _SET_AT_KEY: {"espresso": old.replace("+00:00", "")}})
        asyncio.run(store.async_load())
        assert store.age_days("espresso") == 3


class TestClearing:
    def test_clear_all_removes_the_file(self):
        store = _store()
        asyncio.run(store.async_set_many({"espresso": 1476, "tea": 35}))
        removed = asyncio.run(store.async_clear())
        assert sorted(removed) == ["espresso", "tea"]
        assert store.all_keys() == []
        assert store._store._data is None

    def test_clear_subset_keeps_the_rest(self):
        store = _store()
        asyncio.run(store.async_set_many({"espresso": 1476, "tea": 35}))
        removed = asyncio.run(store.async_clear(["tea"]))
        assert removed == ["tea"]
        assert store.get("espresso") == 1476
        assert store.get_set_at("tea") == UNKNOWN_DATE
        # The file is rewritten, not removed, while entries remain.
        assert store._store._data is not None
        assert store._store._data["espresso"] == 1476

    def test_clear_last_key_removes_the_file(self):
        store = _store()
        asyncio.run(store.async_set_many({"espresso": 1476}))
        asyncio.run(store.async_clear(["espresso"]))
        assert store._store._data is None

    def test_clear_unknown_key_is_a_no_op(self):
        store = _store()
        asyncio.run(store.async_set_many({"espresso": 1476}))
        before = dict(store._store._data)
        assert asyncio.run(store.async_clear(["nope"])) == []
        assert store._store._data == before


class TestMergeUnchanged:
    """The peer's one explicit constraint: keep max(). It is still the right
    guard against a legitimate counter going down after a component swap."""

    def test_max_rule_intact(self):
        store = _store()
        asyncio.run(store.async_set_many({"espresso": 1476}))
        assert store.merge("espresso", 1406) == 1476
        assert store.merge("espresso", 1500) == 1500
        assert store.merge("espresso", None) == 1476

    def test_no_baseline_is_invisible(self):
        store = _store()
        assert store.merge("espresso", 1406) == 1406
