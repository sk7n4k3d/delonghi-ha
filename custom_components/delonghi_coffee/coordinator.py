"""Data update coordinator for De'Longhi Coffee."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import DeLonghiApi, DeLonghiApiError, DeLonghiAuthError
from .const import DOMAIN, SCAN_INTERVAL_SECONDS

_LOGGER = logging.getLogger(__name__)


class DeLonghiCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator to fetch data from De'Longhi machine."""

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

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from API."""
        try:
            # Ping machine to force it to push fresh data
            await self.hass.async_add_executor_job(
                self.api.ping_connected, self.dsn
            )

            status: dict[str, Any] = await self.hass.async_add_executor_job(
                self.api.get_status, self.dsn
            )
            counters: dict[str, Any] = await self.hass.async_add_executor_job(
                self.api.get_counters, self.dsn
            )

            if not self.beverages:
                self.beverages = await self.hass.async_add_executor_job(
                    self.api.get_available_beverages, self.dsn
                )

            return {
                "status": status.get("status", "UNKNOWN"),
                "machine_state": status.get("machine_state", "Unknown"),
                "alarms": status.get("alarms", []),
                "profile": status.get("profile", 0),
                "counters": counters,
                "beverages": self.beverages,
            }
        except DeLonghiAuthError as err:
            raise UpdateFailed(f"Authentication error: {err}") from err
        except DeLonghiApiError as err:
            raise UpdateFailed(f"Error fetching data: {err}") from err
        except Exception as err:
            raise UpdateFailed(f"Unexpected error: {err}") from err
