"""Data update coordinator for De'Longhi Coffee."""

from __future__ import annotations

import logging
import time
from datetime import timedelta
from time import monotonic
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import DeLonghiApi, DeLonghiApiError, DeLonghiAuthError
from .const import DOMAIN, FULL_REFRESH_INTERVAL, SCAN_INTERVAL_SECONDS

_LOGGER = logging.getLogger(__name__)


class DeLonghiCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator to fetch data from De'Longhi machine.

    Light poll every 60s: only status/monitor (1-2 API calls)
    Full refresh every 10min: ping + single properties fetch (2 API calls)

    Before optimization: ~186-267 API calls/hour
    After: ~70 API calls/hour (3x reduction)
    """

    def __init__(self, hass: HomeAssistant, api: DeLonghiApi, dsn: str) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=SCAN_INTERVAL_SECONDS),
        )
        self.api = api
        self.dsn = dsn
        self.beverages: list[str] = []
        self._last_full_refresh: float = 0
        self._cached_counters: dict[str, Any] = {}
        self._cached_profiles: dict[str, Any] = {}
        self._cached_beans: list[dict[str, Any]] = []
        self._lan_config: dict[str, Any] | None = None
        self.selected_profile: int = 2  # Default to profile 2 (user defaults)
        self.custom_recipe_names: dict[str, str] = {}  # custom_1 → "café midi"
        self._last_monitor_raw: str | None = None
        self._monitor_stale_count: int = 0
        self._monitor_last_changed: float = monotonic()
        self._monitor_stale_timeout: int = 1800  # 30 minutes

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from API."""
        try:
            now = monotonic()
            need_full = (now - self._last_full_refresh) >= FULL_REFRESH_INTERVAL

            # Always get status (lightweight — 1-2 API calls)
            status: dict[str, Any] = await self.hass.async_add_executor_job(
                self.api.get_status, self.dsn
            )

            # Full refresh: ping + ONE properties fetch for everything
            if need_full:
                _LOGGER.debug("Full refresh (single properties fetch)")

                # Ping to force data push
                await self.hass.async_add_executor_job(
                    self.api.ping_connected, self.dsn
                )

                # Single fetch of ALL properties — shared by counters, profiles, beans, beverages
                all_props: dict[str, Any] = await self.hass.async_add_executor_job(
                    self.api.get_properties, self.dsn
                )

                # Parse everything from the single fetch
                self._cached_counters = self.api.parse_counters(all_props)

                if not self.beverages:
                    self.beverages = self.api.parse_available_beverages(all_props)
                    self.custom_recipe_names = self.api.get_custom_recipe_names()

                self._cached_profiles = self.api.parse_profiles(all_props)
                self._cached_beans = self.api.parse_bean_systems(all_props)

                # Fetch LAN config once (first full refresh only)
                if self._lan_config is None:
                    self._lan_config = await self.hass.async_add_executor_job(
                        self.api.get_lan_config, self.dsn
                    )

                self._last_full_refresh = now

            # Track monitor staleness — if raw data never changes,
            # alarms from this data are unreliable
            monitor_raw = status.get("monitor_raw")
            if monitor_raw and monitor_raw == self._last_monitor_raw:
                self._monitor_stale_count += 1
            else:
                self._monitor_stale_count = 0
                self._monitor_last_changed = monotonic()
            self._last_monitor_raw = monitor_raw

            # Detect prolonged staleness — machine probably off
            stale_duration = now - self._monitor_last_changed
            monitor_timed_out = stale_duration > self._monitor_stale_timeout

            # Suppress alarms when monitor is unreliable:
            # - stale (3+ identical polls), OR
            # - no cloud status (model has no app_device_status — monitor always cached)
            alarms = status.get("alarms", [])
            cloud_status = status.get("status", "UNKNOWN")
            monitor_stale = self._monitor_stale_count >= 3
            no_cloud = cloud_status == "UNKNOWN"
            if (monitor_stale or no_cloud) and alarms:
                _LOGGER.debug(
                    "Suppressing %d alarms (stale=%s, no_cloud=%s)",
                    len(alarms), monitor_stale, no_cloud,
                )
                alarms = []

            # Override machine state to Off if monitor hasn't changed in 30+ min
            machine_state = status.get("machine_state", "Unknown")
            if monitor_timed_out and machine_state not in ("Unknown", "Off"):
                _LOGGER.debug(
                    "Monitor unchanged for %.0f min, assuming machine is Off",
                    stale_duration / 60,
                )
                machine_state = "Off"

            return {
                "status": status.get("status", "UNKNOWN"),
                "machine_state": machine_state,
                "alarms": alarms,
                "monitor_stale": monitor_stale,
                "profile": status.get("profile", 0),
                "counters": self._cached_counters,
                "beverages": self.beverages,
                "active_profile": self._cached_profiles.get("active", 1),
                "profiles": self._cached_profiles.get("profiles", {}),
                "beans": self._cached_beans,
                "lan_config": self._lan_config or {},
            }
        except DeLonghiAuthError as err:
            raise UpdateFailed(f"Authentication error: {err}") from err
        except DeLonghiApiError as err:
            raise UpdateFailed(f"Error fetching data: {err}") from err
        except Exception as err:
            raise UpdateFailed(f"Unexpected error: {err}") from err
