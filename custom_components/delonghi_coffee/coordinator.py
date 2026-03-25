"""Data update coordinator for De'Longhi Coffee."""

from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import DeLonghiApi, DeLonghiApiError, DeLonghiAuthError
from .const import DOMAIN, FULL_REFRESH_INTERVAL, SCAN_INTERVAL_SECONDS

_LOGGER = logging.getLogger(__name__)


class DeLonghiCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator to fetch data from De'Longhi machine.

    Light poll every 60s: only status/monitor (1-2 API calls)
    Full refresh every 5min: ping + counters + profiles + beans (~4-5 API calls)

    This avoids Ayla cloud rate limiting while keeping status responsive.
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

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from API."""
        try:
            now = time.time()
            need_full = (now - self._last_full_refresh) >= FULL_REFRESH_INTERVAL

            # Always get status (lightweight — 1-2 API calls)
            status: dict[str, Any] = await self.hass.async_add_executor_job(
                self.api.get_status, self.dsn
            )

            # Full refresh: ping + counters + profiles + beans
            if need_full:
                _LOGGER.debug("Full refresh (counters, profiles, beans)")

                # Ping to force data push
                await self.hass.async_add_executor_job(
                    self.api.ping_connected, self.dsn
                )

                self._cached_counters = await self.hass.async_add_executor_job(
                    self.api.get_counters, self.dsn
                )

                if not self.beverages:
                    self.beverages = await self.hass.async_add_executor_job(
                        self.api.get_available_beverages, self.dsn
                    )

                self._cached_profiles = await self.hass.async_add_executor_job(
                    self.api.get_profiles, self.dsn
                )

                self._cached_beans = await self.hass.async_add_executor_job(
                    self.api.get_bean_systems, self.dsn
                )

                self._last_full_refresh = now

            return {
                "status": status.get("status", "UNKNOWN"),
                "machine_state": status.get("machine_state", "Unknown"),
                "alarms": status.get("alarms", []),
                "profile": status.get("profile", 0),
                "counters": self._cached_counters,
                "beverages": self.beverages,
                "active_profile": self._cached_profiles.get("active", 1),
                "profiles": self._cached_profiles.get("profiles", {}),
                "beans": self._cached_beans,
            }
        except DeLonghiAuthError as err:
            raise UpdateFailed(f"Authentication error: {err}") from err
        except DeLonghiApiError as err:
            raise UpdateFailed(f"Error fetching data: {err}") from err
        except Exception as err:
            raise UpdateFailed(f"Unexpected error: {err}") from err
