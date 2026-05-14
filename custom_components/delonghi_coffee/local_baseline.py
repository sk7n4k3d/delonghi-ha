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

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

_LOGGER = logging.getLogger(__name__)

# Storage version: bump when the schema changes. Stored under
# .storage/delonghi_coffee_baseline_<DSN>.
STORAGE_VERSION = 1


class LocalBaselineStore:
    """Per-DSN persistent baseline for counters.

    Counter keys match those produced by ``DeLonghiApiClient.parse_counters``
    (e.g. ``total_espressos``, ``descale_count``, ``filter_replacements``,
    ``beverages_since_descale``, ``total_water_ml``). The value stored is the
    one read off the machine's UI, in the same unit the cloud counter uses
    (ml for water_ml counters, raw integer for cup counters).
    """

    def __init__(self, hass: HomeAssistant, dsn: str) -> None:
        self._hass = hass
        self._dsn = dsn
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"delonghi_coffee_baseline_{dsn}"
        )
        self._values: dict[str, int] = {}
        self._loaded = False

    async def async_load(self) -> None:
        """Load persisted baselines. Idempotent."""
        if self._loaded:
            return
        data = await self._store.async_load()
        if isinstance(data, dict):
            # Filter to int-only values; we never store anything else.
            self._values = {k: int(v) for k, v in data.items() if isinstance(v, (int, float))}
            _LOGGER.info(
                "LocalBaselineStore[%s] loaded %d entries: %s",
                self._dsn, len(self._values), sorted(self._values.keys()),
            )
        self._loaded = True

    async def async_save(self) -> None:
        await self._store.async_save(self._values)

    def get(self, counter_key: str) -> int | None:
        """Return baseline for a counter key, or None if not set."""
        return self._values.get(counter_key)

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
                    self._dsn, k, v,
                )
        if not accepted:
            return
        self._values.update(accepted)
        await self.async_save()
        _LOGGER.info("LocalBaselineStore[%s] updated: %s", self._dsn, accepted)

    async def async_clear(self) -> None:
        """Drop all baselines and remove the storage file."""
        self._values = {}
        await self._store.async_remove()
        _LOGGER.info("LocalBaselineStore[%s] cleared", self._dsn)

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
                self._dsn, counter_key, baseline, cloud_value,
            )
            return cloud_value
