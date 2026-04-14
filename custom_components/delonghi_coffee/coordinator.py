"""Data update coordinator for De'Longhi Coffee."""

from __future__ import annotations

import logging
import socket
from datetime import timedelta
from time import monotonic
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import DeLonghiApi, DeLonghiApiError, DeLonghiAuthError
from .const import DOMAIN, FULL_REFRESH_INTERVAL, MQTT_KEEPALIVE_INTERVAL, SCAN_INTERVAL_SECONDS
from .contentstack import fetch_bean_adapt, fetch_coffee_beans, fetch_drink_catalog
from .lan import _LAN_LOGGER, DeLonghiLanServer, LanError, LanServerConfig, register_with_device
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
        self._cached_bean_system_par: dict[str, Any] = {}
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
        # LAN server (Phase 1 — observe mode)
        self._lan_server: DeLonghiLanServer | None = None
        self._lan_active: bool = False
        self._lan_start_attempted: bool = False
        self._lan_properties: dict[str, Any] = {}

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
                self._cached_bean_system_par = self.api.parse_bean_system_par(all_props)

                # Fetch LAN config once (first full refresh only)
                if self._lan_config is None:
                    self._lan_config = await self.hass.async_add_executor_job(self.api.get_lan_config, self.dsn)

                # Start LAN server once if the machine supports it
                if not self._lan_start_attempted and self._lan_config:
                    await self._try_start_lan()

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
                "bean_system_par": self._cached_bean_system_par,
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

    # ── LAN server lifecycle ─────────────────────────────────────────────

    async def _try_start_lan(self) -> None:
        """Attempt to start the embedded LAN server (once).

        LAN failures MUST NEVER crash the integration — cloud mode remains
        the fallback. All errors are caught and logged.
        """
        self._lan_start_attempted = True
        lan = self._lan_config or {}
        if not lan.get("lan_enabled"):
            _LAN_LOGGER.debug("LAN not enabled in cloud config, skipping")
            return
        lan_key = lan.get("lanip_key")
        lan_ip = lan.get("lan_ip")
        if not lan_key or not lan_ip:
            _LAN_LOGGER.debug("LAN key or IP missing in cloud config, skipping")
            return

        local_ip = self._get_local_ip(lan_ip)
        if not local_ip:
            _LAN_LOGGER.warning("Cannot determine local IP to reach device %s, LAN server not started", lan_ip)
            return

        try:
            config = LanServerConfig(
                dsn=self.dsn,
                lan_key=lan_key,
                advertised_ip=local_ip,
            )
            server = DeLonghiLanServer(config, on_property=self._on_lan_property)
            await server.start()
            self._lan_server = server
            _LAN_LOGGER.info(
                "LAN server started on port %d, local_ip=%s, device_ip=%s",
                server.port,
                local_ip,
                lan_ip,
            )
        except Exception:  # noqa: BLE001
            _LAN_LOGGER.exception("LAN server start failed, falling back to cloud")
            return

        # Tell the device to push to our server
        try:
            import aiohttp

            await register_with_device(
                aiohttp.ClientSession,
                device_ip=lan_ip,
                advertised_ip=local_ip,
                advertised_port=server.port,
            )
            self._lan_active = True
            _LAN_LOGGER.info("Registered with device %s — LAN observe mode active", lan_ip)
        except LanError:
            _LAN_LOGGER.exception("LAN registration failed, server running but device won't push to us")
        except Exception:  # noqa: BLE001
            _LAN_LOGGER.exception("Unexpected error during LAN registration")

    async def _on_lan_property(self, data: dict[str, Any]) -> None:
        """Callback invoked when the machine pushes a decrypted datapoint.

        Phase 1 (observe): log everything, cache property values, trigger
        entity updates so sensors reflect LAN data alongside cloud data.
        """
        _LAN_LOGGER.debug("LAN property received: %s", data)

        # Cache individual properties if the payload has the expected shape
        prop = data.get("property")
        if isinstance(prop, dict):
            name = prop.get("name")
            value = prop.get("value")
            if name is not None:
                self._lan_properties[name] = value
                _LAN_LOGGER.debug("LAN property cached: %s = %s", name, value)

        # Trigger entity update so sensors can pick up LAN data
        self.async_set_updated_data(self.data if self.data else {})

    @staticmethod
    def _get_local_ip(device_ip: str) -> str | None:
        """Determine the local IP that can reach *device_ip*.

        Opens a UDP socket toward the device (never actually sends) and
        reads back the source address the OS chose — this is the correct
        interface on multi-homed hosts.
        """
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect((device_ip, 80))
                return sock.getsockname()[0]
        except OSError:
            return None

    async def async_stop_lan(self) -> None:
        """Cleanly stop the LAN server (called from async_unload_entry)."""
        if self._lan_server is not None:
            try:
                await self._lan_server.stop()
            except Exception:  # noqa: BLE001
                _LAN_LOGGER.exception("Error stopping LAN server")
            self._lan_server = None
            self._lan_active = False
            _LAN_LOGGER.info("LAN server stopped")

    async def _load_contentstack(self) -> None:
        """Load drink catalog and bean adapt from ContentStack CMS (once).

        ContentStack prod_drink titles follow the pattern:
        "{DrinkName} {ECAM_MODEL} {SKU} {FW_VERSION}"
        e.g. "Espresso ECAM63050 0132250181 1.1-1.0-2.5"

        A full enumeration of the catalog (2026-04-09) returned only
        ECAM47 and ECAM63 families. Machines in other families
        (PrimaDonna Soul ECAM61, Dinamica Plus ECAM37, …) still get a
        lookup attempt so future data updates are picked up automatically,
        but the contentstack module itself skips the HTTP fetch for
        families it knows are not indexed. See issue #6.
        """
        import re

        model_pattern = self._detect_contentstack_pattern()
        if not model_pattern:
            _LOGGER.debug(
                "ContentStack: cannot determine ECAM model (oem=%s, serial=%s), skipping",
                self.api._oem_model or "?",
                (self._last_all_props.get("d270_serialnumber", {}) or {}).get("value", "?"),
            )
            self._contentstack_loaded = True
            return

        # Derive a model_name hint from TranscodeTable for cases where the
        # dotted variant ("ECAM610.75") might one day appear in title data.
        model_name = ""
        if self.api.model_info:
            raw_name = self.api.model_info.get("name", "")
            if isinstance(raw_name, str):
                compact = re.sub(r"[^A-Za-z0-9]", "", raw_name.replace(".", ""))
                m = re.search(r"ECAM\d+", compact, re.IGNORECASE)
                if m:
                    model_name = m.group(0)

        try:
            self.drink_catalog = await self.hass.async_add_executor_job(
                fetch_drink_catalog, model_pattern, model_name
            )
            self.bean_adapt = await self.hass.async_add_executor_job(
                fetch_bean_adapt, model_pattern, model_name
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
        except Exception:  # noqa: BLE001 — CMS failures must not block refresh
            _LOGGER.exception("ContentStack load failed, will retry next refresh")

    def _detect_contentstack_pattern(self) -> str:
        """Return the best ECAM pattern for ContentStack title lookups.

        Order of precedence:
        1. ``d270_serialnumber`` — hardware serial, e.g. ``ECAM61075MB...``.
        2. TranscodeTable model info (``appModelId``/``product_code``/``name``).
        3. Static OEM → ECAM family map for known CMS-indexed models.
        """
        import re

        if self._last_all_props:
            serial_prop = self._last_all_props.get("d270_serialnumber", {})
            serial_val = serial_prop.get("value", "") if isinstance(serial_prop, dict) else ""
            if serial_val:
                match = re.search(r"ECAM\d+", serial_val, re.IGNORECASE)
                if match:
                    return match.group(0).upper()

        if self.api.model_info:
            info = self.api.model_info
            for field in ("appModelId", "product_code", "name"):
                value = info.get(field, "")
                if isinstance(value, str):
                    # Drop punctuation so "ECAM610.75" collapses to "ECAM61075".
                    cleaned = value.replace(".", "")
                    match = re.search(r"ECAM\d+", cleaned, re.IGNORECASE)
                    if match:
                        return match.group(0).upper()

        oem = self.api._oem_model or ""
        # Only list families actually indexed in ContentStack. Everything
        # else falls through to an empty pattern and skips the fetch — we
        # used to map DL-pd-* → ECAM61, which silently returned zero drinks
        # on every startup and caused issue #6 to appear unfixed.
        oem_ecam_map = {
            "DL-striker-cb": "ECAM63050",
            "DL-striker-best": "ECAM63075",
        }
        return oem_ecam_map.get(oem, "")
