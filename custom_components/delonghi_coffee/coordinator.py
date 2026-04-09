"""Data update coordinator for De'Longhi Coffee."""

from __future__ import annotations

import logging
from datetime import timedelta
from time import monotonic
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import DeLonghiApi, DeLonghiApiError, DeLonghiAuthError
from .const import DOMAIN, FULL_REFRESH_INTERVAL, MQTT_KEEPALIVE_INTERVAL, SCAN_INTERVAL_SECONDS
from .contentstack import fetch_bean_adapt, fetch_coffee_beans, fetch_drink_catalog
from .logger import get_diagnostic_dump

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
        self._last_keepalive: float = 0
        self._cached_counters: dict[str, Any] = {}
        self._cached_profiles: dict[str, Any] = {}
        self._cached_beans: list[dict[str, Any]] = []
        self._lan_config: dict[str, Any] | None = None
        self.selected_profile: int | None = None  # Set from machine on first refresh
        self.seen_alarm_bits: set[int] = set()  # Track which inverted bits the machine supports
        self.custom_recipe_names: dict[str, str] = {}  # custom_1 → "café midi"
        self.drink_catalog: dict[int, dict] = {}  # ContentStack drink_id → {name, clusters, ingredients}
        self.bean_adapt: dict[str, Any] | None = None  # ContentStack bean adapt calibration
        self.coffee_beans: list[dict[str, Any]] = []  # ContentStack coffee bean catalog
        self._contentstack_loaded: bool = False
        self._last_monitor_raw: str | None = None
        self._monitor_stale_count: int = 0
        self._monitor_last_changed: float = monotonic()
        self._monitor_stale_timeout: int = 2700  # 45 minutes (machine auto-sleep is 30min)
        self.diagnostic_mode: bool = False
        self._last_diagnostic: dict[str, Any] = {}
        self._last_all_props: dict[str, Any] = {}

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from API."""
        try:
            now = monotonic()
            need_full = (now - self._last_full_refresh) >= FULL_REFRESH_INTERVAL

            # Read status (monitor property from cloud cache)
            status: dict[str, Any] = await self.hass.async_add_executor_job(self.api.get_status, self.dsn)

            # Sync selected_profile from monitor (actual active profile)
            monitor_profile = status.get("profile", 0)
            if monitor_profile > 0:
                self.selected_profile = monitor_profile

            # MQTT keepalive — ping every 4 min to prevent cloud session expiry.
            # Without this, the MQTT session dies after ~5 min and commands
            # (including power on) never reach the machine.
            #
            # On models where app_device_connected is unsupported (PrimaDonna Soul
            # et al.), ping_connected returns False immediately. We fall back to
            # request_monitor (ECAM 0x84) which is idempotent and works on all
            # models — it asks the machine to push its monitor data, which also
            # keeps the cloud session alive and refreshes sensor values.
            need_keepalive = (now - self._last_keepalive) >= MQTT_KEEPALIVE_INTERVAL
            if need_keepalive and not need_full:  # full refresh already pings
                try:
                    ping_ok = await self.hass.async_add_executor_job(self.api.ping_connected, self.dsn)
                    if not ping_ok:
                        _LOGGER.debug("ping_connected unsupported, falling back to request_monitor")
                        await self.hass.async_add_executor_job(self.api.request_monitor, self.dsn)
                    self._last_keepalive = now
                    _LOGGER.debug("MQTT keepalive sent")
                except (DeLonghiApiError, DeLonghiAuthError):
                    _LOGGER.debug("MQTT keepalive failed, will retry next cycle")

            # Full refresh: ping + properties fetch for everything
            if need_full:
                _LOGGER.debug("Full refresh (single properties fetch)")

                # Force the machine to push ALL properties (not just monitor).
                # ping_connected on supported models, request_monitor as universal fallback.
                try:
                    ping_ok = await self.hass.async_add_executor_job(self.api.ping_connected, self.dsn)
                    if not ping_ok:
                        _LOGGER.debug("ping_connected unsupported, falling back to request_monitor")
                        await self.hass.async_add_executor_job(self.api.request_monitor, self.dsn)
                except (DeLonghiApiError, DeLonghiAuthError):
                    _LOGGER.debug("Refresh wake failed, continuing with cached data")

                # Single fetch of ALL properties — shared by counters, profiles, beans, beverages
                all_props: dict[str, Any] = await self.hass.async_add_executor_job(self.api.get_properties, self.dsn)
                self._last_all_props = all_props

                # Parse everything from the single fetch
                self._cached_counters = self.api.parse_counters(all_props)

                if not self.beverages:
                    self.beverages = self.api.parse_available_beverages(all_props)
                    self.custom_recipe_names = self.api.get_custom_recipe_names()

                self._cached_profiles = self.api.parse_profiles(all_props)
                self._cached_beans = self.api.parse_bean_systems(all_props)

                # Fetch LAN config once (first full refresh only)
                if self._lan_config is None:
                    self._lan_config = await self.hass.async_add_executor_job(self.api.get_lan_config, self.dsn)

                # ContentStack: fetch drink catalog + bean adapt once
                if not self._contentstack_loaded:
                    await self._load_contentstack()

                self._last_full_refresh = now
                self._last_keepalive = now  # full refresh counts as keepalive

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
            # - timed out (30+ min unchanged) AND machine assumed off, OR
            # - no cloud status (model has no app_device_status — monitor always cached)
            # Note: stale monitor alone is normal when machine is idle (cloud=RUN)
            alarms = status.get("alarms", [])
            alarm_word = status.get("alarm_word")
            cloud_status = status.get("status", "UNKNOWN")
            no_cloud = cloud_status == "UNKNOWN"
            if (monitor_timed_out or no_cloud) and alarms:
                _LOGGER.debug(
                    "Suppressing %d alarms (timed_out=%s, no_cloud=%s)",
                    len(alarms),
                    monitor_timed_out,
                    no_cloud,
                )
                alarms = []
                alarm_word = None

            # Override machine state to Off if monitor hasn't changed in 30+ min
            machine_state = status.get("machine_state", "Unknown")
            if monitor_timed_out and machine_state not in ("Unknown", "Off"):
                _LOGGER.debug(
                    "Monitor unchanged for %.0f min, assuming machine is Off",
                    stale_duration / 60,
                )
                machine_state = "Off"

            # Pre-seed inverted alarm bits when machine is in a normal state.
            # If the machine is Ready/Brewing/etc, inverted bits that are SET
            # mean the component is present (tank, grid). This avoids the
            # cold-boot problem where HA starts with tank already missing
            # and never sees bit=1 to "validate" the sensor.
            if alarm_word is not None and machine_state in ("Ready", "Brewing", "Heating"):
                for bit in (13, 18):  # Water Tank Missing, Grid Missing
                    if alarm_word & (1 << bit):
                        self.seen_alarm_bits.add(bit)

            # Build diagnostic dump if enabled
            if self.diagnostic_mode and self._last_all_props:
                self._last_diagnostic = get_diagnostic_dump(self._last_all_props, self._cached_counters, status)

            return {
                "status": status.get("status", "UNKNOWN"),
                "machine_state": machine_state,
                "alarms": alarms,
                "alarm_word": alarm_word,
                "monitor_stale": monitor_timed_out,
                "profile": status.get("profile", 0),
                "counters": self._cached_counters,
                "beverages": self.beverages,
                "active_profile": self._cached_profiles.get("active", 1),
                "profiles": self._cached_profiles.get("profiles", {}),
                "beans": self._cached_beans,
                "lan_config": self._lan_config or {},
                "api_rate": self.api.rate_tracker.current_rate,
                "api_total_calls": self.api.rate_tracker.total_calls,
                "diagnostic": self._last_diagnostic if self.diagnostic_mode else {},
                "drink_catalog": self.drink_catalog,
                "bean_adapt": self.bean_adapt or {},
                "coffee_beans_count": len(self.coffee_beans),
            }
        except DeLonghiAuthError as err:
            raise UpdateFailed(f"Authentication error: {err}") from err
        except DeLonghiApiError as err:
            raise UpdateFailed(f"Error fetching data: {err}") from err
        except Exception as err:
            raise UpdateFailed(f"Unexpected error: {err}") from err

    async def _load_contentstack(self) -> None:
        """Load drink catalog and bean adapt from ContentStack CMS (once).

        ContentStack prod_drink titles follow the pattern:
        "{DrinkName} {ECAM_MODEL} {SKU} {FW_VERSION}"
        e.g. "Espresso ECAM63050 0132250181 1.1-1.0-2.5"

        We search by ECAM model name (e.g. "ECAM63050") extracted from
        the serial number or DSN. Falls back to OEM model keywords.
        """
        # Try to get ECAM model from serial number (d270_serialnumber)
        model_pattern = ""
        if self._last_all_props:
            serial_prop = self._last_all_props.get("d270_serialnumber", {})
            serial_val = serial_prop.get("value", "")
            if serial_val:
                # Extract "ECAM" + digits pattern from serial
                import re
                match = re.search(r"ECAM\d+", serial_val, re.IGNORECASE)
                if match:
                    model_pattern = match.group(0)

        if not model_pattern:
            # Map OEM model to ECAM pattern
            oem = self.api._oem_model or ""
            oem_ecam_map = {
                "DL-striker-cb": "ECAM63050",
                "DL-striker-best": "ECAM63075",
                "DL-pd-soul": "ECAM61",
                "DL-dinamica-plus": "ECAM37",
            }
            model_pattern = oem_ecam_map.get(oem, "")

        if not model_pattern:
            _LOGGER.debug("ContentStack: cannot determine ECAM model, skipping")
            self._contentstack_loaded = True
            return

        try:
            self.drink_catalog = await self.hass.async_add_executor_job(
                fetch_drink_catalog, model_pattern
            )
            self.bean_adapt = await self.hass.async_add_executor_job(
                fetch_bean_adapt, model_pattern
            )
            self.coffee_beans = await self.hass.async_add_executor_job(
                fetch_coffee_beans
            )
            self._contentstack_loaded = True
            _LOGGER.info(
                "ContentStack loaded: %d drinks, bean_adapt=%s, %d coffee beans",
                len(self.drink_catalog),
                bool(self.bean_adapt),
                len(self.coffee_beans),
            )
        except Exception:
            _LOGGER.warning("ContentStack load failed, will retry next refresh")
