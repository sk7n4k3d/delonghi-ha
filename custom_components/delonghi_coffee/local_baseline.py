"""Local baseline override for counters that the firmware silently stops syncing.

The Eletta Explore firmware (ESP-IDF v3.3.1 2020-05-20) ships counters
(``d5XX``/``d7XX`` family) to the Ayla cloud only on rare events — practical
observation on AC000W038925641: the last spontaneous push was on the boot
following the home WAN swap (Free → Starlink, 2026-04-08T04:17:30Z). The
machine has dispensed ~140 brews since, finished a descale cycle and a water
filter replacement on the user-facing UI, but ``d552_cnt_calc_tot``,
``d553_water_tot_qty``, ``d554_cnt_filter_tot``, ``d558_bev_cnt_desc_on`` and
the per-drink ``d7XX`` totals remain frozen at the April values.

What we tried before falling back to this module:

* LAN protocol (Ayla local mode) — ``lan_enabled=false`` cloud-side and the
  firmware does not run a LAN server (port 10275 closed). Cremalink approach
  rejected by the device.
* Writing idempotent values on output properties (e.g. ``d556_water_hardness``
  3 → 3) to trigger the bundled refresh seen on settings changes — Ayla returns
  201 but the firmware never echoes back fresh stats.
* ECAM ``ParameterRead`` (0x95), ``ParameterReadExt`` (0xA1) and
  ``StatisticsRead`` (0xA2) — Ayla accepts every packet (201), the machine
  silently drops them. These opcodes are BLE-only on longshot; this WiFi
  firmware build implements ``monitor``/``brew``/``power``/``recipe`` only.
* ``ping_connected``/MONITOR_REQUEST already in coordinator — refresh
  ``d302_monitor_machine`` but never push back counters.

So we expose a local baseline persisted in HA's ``.storage``. The user enters
the values shown on the touchscreen (``set_baseline_from_screen`` service) and
sensors return ``max(cloud_value, baseline_value)`` so they reflect reality.
If a future firmware event ever does push fresh higher values we transparently
switch back to cloud.
"""

from __future__ import annotations

from asyncio import Lock
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .logger import get_logger

_LOGGER = get_logger(__name__)

# Storage version: bump when the schema changes. Stored under
# .storage/delonghi_coffee_baseline_<DSN>.
STORAGE_VERSION = 1

# Per-key capture dates live in this metadata key of the same payload instead
# of a STORAGE_VERSION bump: HA's Store refuses a file whose version it does
# not recognise, which would silently drop every baseline users already set
# (the 1476-cup entry behind issue #90 among them). The value is a dict, and
# async_load only keeps int/float entries, so an older build still reads a
# new file and simply ignores the dates.
_SET_AT_KEY = "_set_at"

# Sentinel surfaced to the UI when a baseline predates per-key dating (or its
# date failed to parse). "unknown" is deliberately not treated as fresh.
UNKNOWN_DATE = "unknown"


class LocalBaselineStore:
    """Per-DSN persistent baseline for counters.

    Counter keys match those produced by ``DeLonghiApiClient.parse_counters``
    (e.g. ``total_espressos``, ``descale_count``, ``filter_replacements``,
    ``beverages_since_descale``, ``total_water_ml``). The value stored is the
    one read off the machine's UI, in the same unit the cloud counter uses
    (ml for water_ml counters, raw integer for cup counters).
    """

    def __init__(self, hass: HomeAssistant, dsn: str, *, on_change: Callable[[set[str]], None] | None = None) -> None:
        self._hass = hass
        self._dsn = dsn
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"delonghi_coffee_baseline_{dsn}")
        self._values: dict[str, int] = {}
        self._set_at: dict[str, str] = {}
        self._loaded = False
        self._lock = Lock()
        self._on_change = on_change

    async def async_load(self) -> None:
        """Load persisted baselines. Idempotent."""
        async with self._lock:
            if self._loaded:
                return
            data = await self._store.async_load()
            if isinstance(data, dict):
                # Dates sit under _SET_AT_KEY, so the numeric filter skips them.
                raw_dates = data.get(_SET_AT_KEY)
                self._set_at = {}
                if isinstance(raw_dates, dict):
                    self._set_at = {k: v for k, v in raw_dates.items() if isinstance(k, str) and isinstance(v, str)}
                self._values = {k: int(v) for k, v in data.items() if isinstance(v, (int, float))}
                undated = sorted(k for k in self._values if k not in self._set_at)
                _LOGGER.info(
                    "LocalBaselineStore[%s] loaded %d entries: %s%s",
                    self._dsn,
                    len(self._values),
                    sorted(self._values.keys()),
                    f" — no capture date for {undated}" if undated else "",
                )
            self._loaded = True

    async def _async_commit(self, values: dict[str, int], dates: dict[str, str], changed: set[str]) -> None:
        """Persist the edit before publishing the new baseline and invalidating its streak."""
        if values:
            payload: dict[str, Any] = dict(values)
            if dates:
                payload[_SET_AT_KEY] = dates
            await self._store.async_save(payload)
        else:
            await self._store.async_remove()
        self._values = values
        self._set_at = dates
        if changed and self._on_change is not None:
            self._on_change(changed)

    def get(self, counter_key: str) -> int | None:
        """Return baseline for a counter key, or None if not set."""
        return self._values.get(counter_key)

    def get_set_at(self, counter_key: str) -> str:
        """When this baseline was entered, ISO 8601, or ``UNKNOWN_DATE``.

        A baseline written before per-key dating existed has no date. That is
        reported as unknown, never as fresh — a number of unknown age is the
        exact failure mode issue #90 is about.
        """
        return self._set_at[counter_key] if self._capture_date(counter_key) is not None else UNKNOWN_DATE

    def age_days(self, counter_key: str) -> int | None:
        """Days since the baseline was entered, or None when undatable."""
        stamp = self._capture_date(counter_key)
        return max(0, (datetime.now(UTC) - stamp).days) if stamp is not None else None

    def _capture_date(self, counter_key: str) -> datetime | None:
        """Read the stored ISO timestamp, interpreting naive dates as UTC."""
        raw = self._set_at.get(counter_key)
        if not raw:
            return None
        try:
            stamp = datetime.fromisoformat(raw)
        except ValueError:
            _LOGGER.debug(
                "LocalBaselineStore[%s] unparsable capture date for %s",
                self._dsn,
                counter_key,
            )
            return None
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=UTC)
        return stamp

    def all_keys(self) -> list[str]:
        return list(self._values.keys())

    async def async_set_many(self, values: dict[str, int]) -> None:
        """Override one or more counters at once and persist.

        Only int-coercible values are kept; everything else is ignored with
        a WARNING log so the user knows the field was dropped.
        """
        accepted: dict[str, int] = {}
        for k, v in values.items():
            try:
                accepted[k] = int(v)
            except (TypeError, ValueError):
                _LOGGER.warning(
                    "LocalBaselineStore[%s] ignoring non-numeric value for %s: %r",
                    self._dsn,
                    k,
                    v,
                )
        if not accepted:
            return
        async with self._lock:
            changed = {key for key, value in accepted.items() if self._values.get(key) != value}
            # Re-entering an unchanged value still records a new capture date.
            stamp = datetime.now(UTC).isoformat(timespec="seconds")
            await self._async_commit(self._values | accepted, self._set_at | dict.fromkeys(accepted, stamp), changed)
        _LOGGER.info("LocalBaselineStore[%s] updated: %s", self._dsn, accepted)

    async def async_clear(self, keys: list[str] | None = None) -> list[str]:
        """Drop baselines and return the keys actually removed.

        With no argument every baseline goes and the storage file is removed
        (the original behaviour). With a key list only those entries go, so a
        wrong single value can be fixed without discarding the rest.
        """
        async with self._lock:
            if keys is None:
                removed = sorted(self._values)
                await self._async_commit({}, {}, set(removed))
            else:
                removed = [k for k in keys if k in self._values]
                if not removed:
                    return []
                changed = set(removed)
                values = {key: value for key, value in self._values.items() if key not in changed}
                dates = {key: stamp for key, stamp in self._set_at.items() if key not in changed}
                await self._async_commit(values, dates if values else {}, changed)
            _LOGGER.info("LocalBaselineStore[%s] cleared %s", self._dsn, removed)
            return removed

    def merge(self, counter_key: str, cloud_value: int | float | None) -> int | float | None:
        """Return the value a sensor should expose.

        Rule: if a baseline exists, return ``max(cloud, baseline)``. The cloud
        wins as soon as it catches up (TOTAL_INCREASING semantic preserved).
        If no baseline is set, return the cloud value untouched — this module
        is invisible until the user opts in.

        Works for both ints (cup counters) and floats (already-scaled values).
        Comparison falls back to cloud if types are incompatible.
        """
        baseline = self._values.get(counter_key)
        if baseline is None:
            return cloud_value
        if cloud_value is None:
            return baseline
        try:
            if cloud_value >= baseline:
                return cloud_value
            return baseline
        except TypeError:
            _LOGGER.debug(
                "LocalBaselineStore[%s] cannot compare %s: baseline=%r cloud=%r",
                self._dsn,
                counter_key,
                baseline,
                cloud_value,
            )
            return cloud_value
