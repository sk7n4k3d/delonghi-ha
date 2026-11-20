"""Data update coordinator for De'Longhi Coffee."""

from __future__ import annotations

import asyncio
import socket
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from time import monotonic
from typing import Any

import requests
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import DeLonghiApi, DeLonghiApiError, DeLonghiAuthError, DeLonghiUnsupportedError
from .const import (
    ACTIVE_MACHINE_STATES,
    ACTIVE_SCAN_INTERVAL_SECONDS,
    CONF_SELECTED_PROFILE,
    CONTENTSTACK_RETRY_MAX,
    DOMAIN,
    FULL_REFRESH_INTERVAL,
    LAN_KEEPALIVE_SECONDS,
    LAN_SESSION_STALE_SECONDS,
    MONITOR_STALE_TIMEOUT,
    MQTT_KEEPALIVE_INTERVAL,
    SCAN_INTERVAL_SECONDS,
)
from .contentstack import fetch_bean_adapt, fetch_coffee_beans, fetch_drink_catalog
from .lan import _LAN_LOGGER, DeLonghiLanServer, LanError, LanServerConfig, register_with_device
from .local_baseline import LocalBaselineStore
from .logger import get_diagnostic_dump, get_logger, sanitize

_LOGGER = get_logger(__name__)

# Property names that carry the MonitorDataV2 datapoint. The cloud exposes
# ``d302_monitor_machine`` (Eletta) or ``d302_monitor`` (PrimaDonna Soul),
# and the machine pushes the same names on the LAN socket.
LAN_MONITOR_PROPERTIES: tuple[str, ...] = ("d302_monitor_machine", "d302_monitor")

# Issue #46: one transient cloud hiccup (requests ConnectionError seen
# ~1.1x/day in user logs) used to mark every entity unavailable for a full
# poll cycle. Before giving up, retry the failed fetch once after a short
# pause — long enough to ride out a Wi-Fi/AP blip, short enough to stay well
# inside the 60s poll budget. This applies to the full-refresh
# ``get_properties`` fetch — the only cloud call in the poll cycle that can
# still raise (``api.get_status`` swallows transport errors internally and
# returns ``status=UNKNOWN``, so wrapping it would be dead code).
TRANSIENT_RETRY_DELAY_SECONDS: float = 2.0

# Keys a parsed monitor frame may override in the status / data dicts.
_LAN_MONITOR_STATUS_KEYS: tuple[str, ...] = (
    "machine_state",
    "action",
    "progress",
    "alarms",
    "alarm_word",
    "profile",
    "monitor_raw",
    # Issue #47: a frame the machine pushed itself carries its own clock, and
    # it must win over whatever the cloud cache says — the LAN is the fresher
    # witness of the two.
    "monitor_ts",
)


def _valid_slot(value: Any) -> bool:
    """True when ``value`` is a usable profile slot: a whole number in 1..5.

    Issue #78 review (PR #79): the old ``> 0`` test was too loose — it accepted
    ids the machine never offers (7) and let a half-parsed value pass for a
    choice. Anything else (``None``, 0, 7, ``"3"``) means *no slot*, so the
    resolver stays unresolved and the caller must ask instead of brewing on an
    invented slot 1.
    """
    return isinstance(value, int) and 1 <= value <= 5


class DeLonghiCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator to fetch data from De'Longhi machine.

    Light poll every 60s: only status/monitor (1-2 API calls)
    Full refresh every 10min: ping + single properties fetch (2 API calls)

    Before optimization: ~186-267 API calls/hour
    After: ~70 API calls/hour (3x reduction)
    """

    def __init__(
        self, hass: HomeAssistant, api: DeLonghiApi, dsn: str, config_entry: ConfigEntry | None = None
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            # Explicit config_entry: without it the base class falls back to
            # the `current_entry` ContextVar, an implicit dependency HA
            # discourages for custom components — and the ConfigEntryAuthFailed
            # reauth escalation depends on `self.config_entry` being set
            # (audit 2026-11-05).
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=SCAN_INTERVAL_SECONDS),
        )
        self.api = api
        self.dsn = dsn
        self.beverages: list[str] = []
        self._last_full_refresh: float = 0
        self._last_keepalive: float = 0
        self._keepalive_failures: int = 0
        self._cached_counters: dict[str, Any] = {}
        self._cached_profiles: dict[str, Any] = {}
        self._cached_beans: list[dict[str, Any]] = []
        self._cached_bean_system_par: dict[str, Any] = {}
        self._lan_config: dict[str, Any] | None = None
        # Issue #50 stickiness — the last LAN monitor overlay, when it was
        # applied, and the cloud monitor raw it was applied over. While the
        # cloud raw stays unchanged (frozen cache) the overlay is kept even
        # if the LAN session goes stale, so a push gap cannot flicker
        # machine_state back to the frozen cloud value.
        self._lan_sticky: dict[str, Any] | None = None
        self._lan_sticky_at: float = 0.0  # monotonic() of the last LAN application
        self._lan_sticky_cloud_raw: str | None = None  # None = cloud raw not observed yet
        self._last_cloud_monitor_raw: str | None = None  # raw from get_status, pre-overlay
        # Issue #46 — pause before the single retry of a failed cloud fetch.
        # Instance attribute (not just the module constant) so tests can
        # collapse the wait to 0 without patching asyncio.sleep.
        self._transient_retry_delay: float = TRANSIENT_RETRY_DELAY_SECONDS
        # Issue #78 / PR #79 review — two separate things, never conflated:
        #
        # ``selected_profile`` is the user's EXPLICIT choice only: restored from
        # the config entry, set by ``mark_profile_selection``, and changed only
        # when the user changes or clears it. Observation never writes it.
        #
        # ``_auto_profile`` is the AUTOMATIC default derived from what the
        # machine reports (fresh cloud ``active_profile`` / monitor byte). It is
        # computed only while there is no explicit choice, is never persisted and
        # is never promoted to ``selected_profile``.
        _saved = config_entry.data.get(CONF_SELECTED_PROFILE) if config_entry else None
        self.selected_profile: int | None = _saved if _valid_slot(_saved) else None
        self._auto_profile: int | None = None
        # last observed fresh cloud profile id; only a change observed in
        # session follows, and only in automatic mode (issue #78)
        self._last_d286: int | None = None
        self.seen_alarm_bits: set[int] = set()  # Track which inverted bits the machine supports
        self.custom_recipe_names: dict[str, str] = {}  # custom_1 → "café midi"
        self.drink_catalog: dict[int, dict] = {}  # ContentStack drink_id → {name, clusters, ingredients}
        self.bean_adapt: dict[str, Any] | None = None  # ContentStack bean adapt calibration
        self.coffee_beans: list[dict[str, Any]] = []  # ContentStack coffee bean catalog
        self._contentstack_loaded: bool = False
        # Backoff state for the one-shot ContentStack load: consecutive fetch
        # failures push the next attempt further out (see _load_contentstack)
        # so an unreachable CMS cannot spam the HA error log every 10 minutes.
        self._cs_failures: int = 0
        self._cs_next_retry: float = 0.0  # monotonic(); 0.0 = attempt allowed
        self._model_identified: bool = False
        self._last_monitor_raw: str | None = None
        self._monitor_stale_count: int = 0
        self._monitor_last_changed: float = monotonic()
        self._monitor_stale_timeout: int = MONITOR_STALE_TIMEOUT
        self.diagnostic_mode: bool = False
        self._last_diagnostic: dict[str, Any] = {}
        self._last_all_props: dict[str, Any] = {}
        # LAN server (Phase 1 — observe mode)
        self._lan_server: DeLonghiLanServer | None = None
        self._lan_active: bool = False
        self._lan_start_attempted: bool = False
        # LAN session state machine (issue #50): periodic keep-alive/re-open
        # job, when the last POST was accepted, and a one-shot warning flag.
        self._lan_session_unsub: Callable[[], None] | None = None
        self._lan_open_pending_since: float = 0.0
        self._lan_no_handshake_warned: bool = False
        self._lan_properties: dict[str, Any] = {}
        # Fast-poll window: when a brew button is pressed the cloud Ayla
        # 60s polling rate misses the transient Brewing / Pre-brewing /
        # Frothing milk / Rinsing / Steaming states (each lasts ~10-30s).
        # Callers may invoke request_fast_poll() to shorten the interval
        # for a bounded window (default 60s window @ 5s interval) so
        # automations that watch machine_state actually see the brew flow.
        self._fast_poll_until: float | None = None
        # Active-state polling: when the machine reports a transient state
        # (Brewing, Turning On, …) we automatically shorten the interval to
        # ACTIVE_SCAN_INTERVAL_SECONDS so state transitions surface in
        # near-real-time without the user pressing anything. Reverts to the
        # normal 60s once the machine settles.
        self._active_polling: bool = False
        # Cache the canonical normal interval so we can revert from a
        # fast-poll window even if the test harness stubs out
        # DataUpdateCoordinator.__init__ (where update_interval lives).
        self._normal_update_interval: timedelta = timedelta(seconds=SCAN_INTERVAL_SECONDS)
        # User-supplied counter baselines persisted in .storage. Used to
        # mask cloud counters that the firmware silently stops pushing.
        # See local_baseline.py docstring for the full diagnostic trail.
        self.local_baseline: LocalBaselineStore = LocalBaselineStore(hass, dsn)
        self._baseline_loaded: bool = False

    def request_fast_poll(self, duration_s: float = 60.0, interval_s: float = 5.0) -> None:
        """Shorten the polling interval for a bounded window.

        Idempotent: a second call within the existing window extends the
        deadline to ``monotonic() + duration_s`` (does not stack). The
        normal 60s interval is restored automatically by the next
        ``_async_update_data`` after the deadline.
        """
        if duration_s <= 0 or interval_s <= 0:
            return
        deadline = monotonic() + duration_s
        # Extend or arm the window
        self._fast_poll_until = max(self._fast_poll_until or 0.0, deadline)
        # An explicit fast-poll window supersedes active-state polling
        self._active_polling = False
        self.update_interval = timedelta(seconds=interval_s)
        _LOGGER.debug(
            "Fast poll requested: interval=%ds for %.0fs (until monotonic=%.1f)",
            interval_s,
            duration_s,
            deadline,
        )

    async def async_refresh_after_command(
        self,
        duration_s: float = 90.0,
        interval_s: float = 5.0,
    ) -> None:
        """Refresh immediately after a user command, then fast-poll.

        Ayla 201 means "cloud accepted", NOT "machine received". The machine
        pushes its fresh monitor datapoint within seconds when awake, but a
        lazy 60s poll hides that. This helper fires a refresh right away so
        the UI reflects the command, and arms the fast-poll window so the
        follow-up transitions are captured.
        """
        self.request_fast_poll(duration_s=duration_s, interval_s=interval_s)
        await self.async_request_refresh()

    # ── transient-error retry (issue #46) ──────────────────────────────

    async def _async_fetch_with_retry(self, label: str, func: Callable[..., Any], *args: Any) -> Any:
        """Run a blocking cloud fetch, retrying one transient failure.

        A single ``requests`` ConnectionError on the Ayla ADS used to bubble
        out of the whole refresh and mark every entity unavailable for the
        next 60s (issue #46). One extra attempt after a short pause absorbs
        those one-off hiccups.

        Only the full-refresh ``get_properties`` fetch goes through here:
        ``api.get_status`` converts every transport failure into a
        ``DeLonghiApiError`` inside its own ``@_retry`` decorator and then
        swallows it (returning ``status=UNKNOWN``), so a retry wrap around
        it could never fire.

        Auth errors are never retried: they must reach
        :class:`ConfigEntryAuthFailed` immediately so HA opens the reauth
        flow. ``DeLonghiUnsupportedError`` (definitive 404) is likewise not
        worth a second attempt.
        """
        try:
            return await self.hass.async_add_executor_job(func, *args)
        except (DeLonghiApiError, requests.RequestException) as err:
            if isinstance(err, DeLonghiUnsupportedError):
                raise
            _LOGGER.debug(
                "%s failed (%s) — retrying once in %.0fs before declaring the cloud unreachable",
                label,
                sanitize(err),
                self._transient_retry_delay,
            )
            await asyncio.sleep(self._transient_retry_delay)
            return await self.hass.async_add_executor_job(func, *args)

    # ── LAN monitor overlay (issue #50) ────────────────────────────────

    def _lan_monitor_status(self) -> dict[str, Any] | None:
        """Parse the monitor the machine pushed on the LAN socket.

        Returns the parsed datapoint (machine_state / alarms / profile /
        action / progress / monitor_raw) or ``None`` when there is nothing
        trustworthy to use: no server, stale session, no monitor pushed yet,
        or an undecodable payload. A bad LAN frame must never break a refresh
        cycle — the caller keeps the cloud values.
        """
        server = self._lan_server
        if server is None:
            return None
        try:
            if not server.is_session_alive(LAN_SESSION_STALE_SECONDS):
                return None
            for name in LAN_MONITOR_PROPERTIES:
                value = self._lan_properties.get(name)
                if not value:
                    continue
                parsed = self.api.parse_monitor_value(value)
                if parsed:
                    return parsed
        except Exception:  # noqa: BLE001 — LAN data is an optimisation, never fatal
            _LOGGER.debug("LAN monitor overlay skipped (parse/session error)", exc_info=True)
        return None

    @staticmethod
    def _merge_lan_overlay(status: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
        """Copy the parsed LAN datapoint over a status dict (no mutation)."""
        merged = dict(status)
        for key in _LAN_MONITOR_STATUS_KEYS:
            if key in overlay:
                merged[key] = overlay[key]
        merged["monitor_source"] = "lan"
        return merged

    def _apply_lan_monitor(self, status: dict[str, Any]) -> dict[str, Any]:
        """Overlay a live LAN monitor on top of the (possibly stale) cloud status.

        When the LAN value matches the cloud value the result is identical to
        ``status``, so models with a healthy cloud monitor are unaffected.

        Stickiness (issue #50): once a LAN overlay has been applied it is
        kept on later polls whose LAN session went stale, as long as the
        cloud monitor raw has not moved since the overlay was applied. A
        frozen cloud datapoint is no evidence of a state change, and
        dropping the overlay there would flicker machine_state
        Ready→Off→Ready every time the machine's push gap crosses
        LAN_SESSION_STALE_SECONDS (one recorder row per flip — the #45
        regression this avoids). The overlay is dropped only when the cloud
        monitor changes again (fresh cloud evidence) or when a newer LAN
        frame supersedes it.
        """
        cloud_raw = status.get("monitor_raw")
        overlay = self._lan_monitor_status()
        if overlay:
            self._lan_sticky = overlay
            self._lan_sticky_at = monotonic()
            self._lan_sticky_cloud_raw = cloud_raw
            return self._merge_lan_overlay(status, overlay)
        sticky = self._lan_sticky
        if sticky is not None and (self._lan_sticky_cloud_raw is None or cloud_raw == self._lan_sticky_cloud_raw):
            # Session stale but the cloud has not moved since the last LAN
            # frame was applied: frozen cache, keep the LAN truth. A None
            # sticky raw means the overlay came from a socket push before
            # any poll observed the cloud — accept it and pin the raw now.
            self._lan_sticky_cloud_raw = cloud_raw
            return self._merge_lan_overlay(status, sticky)
        return status

    def _apply_lan_monitor_to_data(self, data: dict[str, Any], overlay: dict[str, Any] | None = None) -> dict[str, Any]:
        """Same overlay, mapped onto the coordinator ``data`` dict keys."""
        if overlay is None:
            overlay = self._lan_monitor_status()
        if not overlay:
            return data
        merged = dict(data)
        if "machine_state" in overlay:
            merged["machine_state"] = overlay["machine_state"]
        if "action" in overlay:
            merged["monitor_action"] = overlay["action"]
        if "progress" in overlay:
            merged["monitor_progress"] = overlay["progress"]
        for key in ("alarms", "alarm_word", "profile", "monitor_raw"):
            if key in overlay:
                merged[key] = overlay[key]
        # Issue #47: the switch's power-on confirmation reads the embedded
        # monitor clock straight off ``data``; a LAN frame is the freshest
        # source for it, so the overlay overwrites the cloud value.
        if "monitor_ts" in overlay:
            merged["monitor_ts"] = overlay["monitor_ts"]
        merged["monitor_source"] = "lan"
        # A datapoint that just arrived over the socket is by definition fresh.
        merged["monitor_stale"] = False
        return merged

    async def async_get_status(self) -> dict[str, Any]:
        """Fresh status dict with the LAN monitor overlaid when available.

        Never raises: a cloud wobble must not block a brew. The pre-brew
        safety check treats an unknown state as "send anyway", exactly like
        ``api.get_status`` does when its own fetch fails.
        """
        status: dict[str, Any] = {}
        try:
            status = await self.hass.async_add_executor_job(self.api.get_status, self.dsn)
        except (DeLonghiApiError, DeLonghiAuthError) as err:
            _LOGGER.debug("Status fetch failed (%s), falling back to LAN/unknown state", sanitize(err))
        except Exception:  # noqa: BLE001 — caller decides what to do with an unknown state
            _LOGGER.exception("Unexpected error fetching status")
        if not isinstance(status, dict):
            status = {}
        return self._apply_lan_monitor(status)

    # ── brew delivery (issue #50) ──────────────────────────────────────

    async def async_send_brew_command(self, brew_cmd: bytes) -> bool:
        """Deliver a brew command on the LAN when the machine polls us.

        On DL-millcore (PrimaDonna Soul) the machine ignores the brew pushed
        through the cloud ``data_request`` while it happily executes the same
        ECAM frame served on its LAN poll, so LAN wins whenever the session
        is alive. Cloud remains the fallback.
        """
        if await self.send_command_lan(brew_cmd):
            _LOGGER.info("Brew command queued on LAN socket (cloud data_request skipped)")
            return True
        return await self.hass.async_add_executor_job(self.api.send_command, self.dsn, brew_cmd)

    async def async_brew(self, beverage_key: str, profile: int) -> bool:
        """Build a brew command from the machine recipe and deliver it.

        The pre-brew safety check runs first, against the LAN-corrected
        status, so a stale cloud monitor can no longer refuse a valid brew
        (issue #50).
        """
        status = await self.async_get_status()
        brew_cmd = await self.hass.async_add_executor_job(
            self.api.build_brew_command, self.dsn, beverage_key, profile, status
        )
        return await self.async_send_brew_command(brew_cmd)

    def mark_profile_selection(self, pid: int) -> None:
        """Record an HA-side profile selection (called by select.py).

        This is the ONLY way ``selected_profile`` becomes an explicit choice
        besides a restore from the config entry, and it is persisted so the
        choice survives a restart without re-sending a selection command
        (issue #78). Nothing here needs a grace window any more: the poll
        tracking never writes ``selected_profile``, so the cloud echo of this
        very command cannot overwrite it (PR #79 review).
        """
        self.selected_profile = pid
        self._persist_selected_profile(pid)

    def _persist_selected_profile(self, pid: int) -> None:
        """Store the explicit profile choice on the config entry (best-effort)."""
        entry = getattr(self, "config_entry", None)
        if entry is None:
            return
        if entry.data.get(CONF_SELECTED_PROFILE) == pid:
            return
        new_data = {**entry.data, CONF_SELECTED_PROFILE: pid}
        try:
            self.hass.config_entries.async_update_entry(entry, data=new_data)
        except Exception:  # noqa: BLE001 — persistence is best-effort; selection still works this session
            _LOGGER.debug("Could not persist selected_profile=%s", pid)

    def resolve_profile(self) -> int | None:
        """Single source of truth for the profile to use (issue #78).

        Precedence between the two distinct notions (PR #79 review):

        1. ``selected_profile`` — the user's EXPLICIT choice (persisted on the
           config entry, or set via ``mark_profile_selection``). It never
           changes on its own.
        2. ``_auto_profile`` — the AUTOMATIC default observed from the machine
           (fresh cloud ``active_profile`` agreeing with the monitor byte, or
           whichever of the two is the only usable signal). Only consulted while
           no explicit choice exists.

        Both must pass ``_valid_slot`` (a whole number in 1..5) to be returned,
        so a corrupt or out-of-range value can never reach a brew command.
        ``None`` when neither is known: callers must then ask for an explicit
        profile instead of silently defaulting to slot 1. The select entity and
        every implicit brew/recipe-sync path call this, so they can never
        disagree on the target slot.
        """
        if _valid_slot(self.selected_profile):
            return self.selected_profile
        if _valid_slot(self._auto_profile):
            return self._auto_profile
        return None

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from API."""
        try:
            now = monotonic()

            # Lazy-load persisted local baselines on the first refresh.
            # async_load is idempotent so this is safe to call every cycle,
            # but the explicit flag avoids a no-op await in the hot path.
            if not self._baseline_loaded:
                await self.local_baseline.async_load()
                self._baseline_loaded = True

            # Revert to the normal interval once the fast-poll window has
            # elapsed. We do it here (top of the cycle) so the deadline
            # check naturally aligns with refresh boundaries.
            if self._fast_poll_until is not None and now > self._fast_poll_until:
                _LOGGER.debug("Fast poll window expired, reverting to %ds interval", SCAN_INTERVAL_SECONDS)
                self.update_interval = self._normal_update_interval
                self._fast_poll_until = None

            # Adaptive active-state polling: transient machine states change
            # the monitor byte every few seconds, so we shorten the interval
            # automatically. Settled states (Ready/Off/…) revert to 60s. This
            # runs AFTER the fast-poll expiry check so an explicit
            # request_fast_poll() window (5s interval) always wins.
            prev = getattr(self, "data", None)
            machine_state_now: str = prev.get("machine_state", "Unknown") if isinstance(prev, dict) else "Unknown"
            should_poll_active = machine_state_now in ACTIVE_MACHINE_STATES
            if should_poll_active and not self._active_polling and self._fast_poll_until is None:
                self._active_polling = True
                self.update_interval = timedelta(seconds=ACTIVE_SCAN_INTERVAL_SECONDS)
                _LOGGER.debug(
                    "Machine in '%s' — switching to %ds active polling", machine_state_now, ACTIVE_SCAN_INTERVAL_SECONDS
                )
            elif not should_poll_active and self._active_polling:
                self._active_polling = False
                if self._fast_poll_until is None:
                    self.update_interval = self._normal_update_interval
                    _LOGGER.debug(
                        "Machine settled ('%s') — reverting to %ds polling", machine_state_now, SCAN_INTERVAL_SECONDS
                    )

            need_full = (now - self._last_full_refresh) >= FULL_REFRESH_INTERVAL

            # Read status (monitor property from cloud cache), then overlay
            # the monitor the machine pushes on the LAN socket when that
            # session is alive. On DL-millcore / PrimaDonna Soul the cloud
            # datapoint only refreshes while the Coffee Link app is open, so
            # cloud-only state is frozen (issue #50).
            #
            # Deliberately NOT wrapped in _async_fetch_with_retry (#46):
            # api.get_status swallows every transport/parse failure inside
            # its own except block and returns status=UNKNOWN, so the retry
            # could never fire here. The #46 retry lives on the full-refresh
            # get_properties call below — the only fetch in this cycle that
            # can still raise.
            status: dict[str, Any] = await self.hass.async_add_executor_job(self.api.get_status, self.dsn)
            self._last_cloud_monitor_raw = status.get("monitor_raw")
            status = self._apply_lan_monitor(status)

            # Profile observation (issue #78, PR #79 review).
            #
            # What the machine reports feeds the AUTOMATIC default ONLY. An
            # explicit choice (``selected_profile``: restored from the config
            # entry, or set by ``mark_profile_selection``) is never seeded,
            # followed or cleared here — it changes only when the user changes
            # or clears it. That is why no grace window is needed any more: an
            # echo of our own command has nothing to overwrite.
            #
            # ``fresh`` is the per-poll cloud ``active_profile`` (d286), the only
            # signal of a touchscreen switch; the full-refresh cache is never a
            # transition signal. ``mon`` is the monitor byte, which lags and
            # reports the last-brewed profile (the original F-COORD-01 stomping
            # bug) — so it is a default on its own, never a follow trigger.
            fresh = status.get("active_profile")
            mon = status.get("profile", 0)
            fresh_ok = _valid_slot(fresh)
            mon_ok = _valid_slot(mon)
            if self.selected_profile is not None:
                pass  # explicit choice: observations must not touch it
            else:
                # Automatic mode: agree the two signals before trusting either.
                if fresh_ok and mon_ok:
                    # A conflicting pair is NOT resolved. Picking one here is
                    # how the wrong profile got brewed (#78); the caller must
                    # ask instead.
                    self._auto_profile = fresh if fresh == mon else None
                elif fresh_ok:
                    self._auto_profile = fresh
                elif mon_ok:
                    self._auto_profile = mon
                else:
                    # Nothing usable this poll: unresolved, NOT a fabricated
                    # slot 1.
                    self._auto_profile = None
                if fresh_ok:
                    if self._last_d286 is None:
                        self._last_d286 = fresh  # first sighting: baseline only
                    elif fresh != self._last_d286:
                        # A genuine in-session touchscreen switch: follow it in
                        # the automatic default (never in ``selected_profile``).
                        _LOGGER.info(
                            "Profile %d seen from machine (cloud changed from %s), automatic default was %s — following",
                            fresh,
                            self._last_d286,
                            self._auto_profile,
                        )
                        self._auto_profile = fresh
                        self._last_d286 = fresh
                else:
                    # Read gap (status UNKNOWN, or a firmware that never
                    # publishes d286): re-baseline, so the next sighting is a
                    # first observation and not a transition out of a value we
                    # stopped observing.
                    self._last_d286 = None

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
                    if self._keepalive_failures:
                        _LOGGER.info("MQTT keepalive recovered after %d failures", self._keepalive_failures)
                    self._keepalive_failures = 0
                    _LOGGER.debug("MQTT keepalive sent")
                except (DeLonghiApiError, DeLonghiAuthError, requests.RequestException) as err:
                    # requests.RequestException (e.g. a raw Ayla ReadTimeout from the
                    # ping POST) is best-effort here, not fatal: the status was already
                    # read this cycle, so a slow keepalive ping must not blank the
                    # entities (issue #73 — observed ~4/day, last log frame
                    # "ping_connected: trying …"). Counted as a keepalive miss below.
                    self._keepalive_failures += 1
                    # Ramp the log level once the cloud has stopped answering for a while.
                    # Single miss = debug (cloud wobbles happen); 3 in a row = warning
                    # (user-visible in HA logs); ≥5 = error, hints at expired token or
                    # Ayla outage. The full refresh path still runs independently so
                    # we do not raise UpdateFailed — sensors stay populated from cache.
                    if self._keepalive_failures == 3:
                        _LOGGER.warning(
                            "MQTT keepalive failing (3 consecutive) — cloud session may be dead: %s",
                            err,
                        )
                    elif self._keepalive_failures >= 5 and self._keepalive_failures % 5 == 0:
                        _LOGGER.error(
                            "MQTT keepalive still failing after %d attempts — commands will likely stall; check token/cloud status: %s",
                            self._keepalive_failures,
                            err,
                        )
                    else:
                        _LOGGER.debug("MQTT keepalive failed (%d), will retry next cycle", self._keepalive_failures)

            # Full refresh: ping + properties fetch for everything
            if need_full:
                _LOGGER.debug("Full refresh (single properties fetch)")

                # Force the machine to push ALL properties (not just monitor).
                # ping_connected on supported models, request_monitor as universal fallback.
                wake_ok = False
                try:
                    ping_ok = await self.hass.async_add_executor_job(self.api.ping_connected, self.dsn)
                    if not ping_ok:
                        _LOGGER.debug("ping_connected unsupported, falling back to request_monitor")
                        await self.hass.async_add_executor_job(self.api.request_monitor, self.dsn)
                    wake_ok = True
                except (DeLonghiApiError, DeLonghiAuthError, requests.RequestException):
                    # Same as the keepalive path: a ping transport timeout is best-effort,
                    # the properties fetch below still runs (issue #73).
                    _LOGGER.debug("Refresh wake failed, continuing with cached data")

                # Single fetch of ALL properties — shared by counters, profiles, beans, beverages
                all_props: dict[str, Any] = await self._async_fetch_with_retry(
                    "get_properties", self.api.get_properties, self.dsn
                )
                self._last_all_props = all_props

                # Parse everything from the single fetch
                self._cached_counters = self.api.parse_counters(all_props)

                if not self.beverages:
                    self.beverages = self.api.parse_available_beverages(all_props)
                    self.custom_recipe_names = self.api.get_custom_recipe_names()

                self._cached_profiles = self.api.parse_profiles(all_props)
                self._cached_beans = self.api.parse_bean_systems(all_props)
                self._cached_bean_system_par = self.api.parse_bean_system_par(all_props)

                # Identify the model once via TranscodeTable (audit m1) —
                # previously fetch_transcode_table/identify_model were never
                # called, leaving api.model_info None and the model_name hint
                # branch in _detect_contentstack_pattern dead. Runs on the
                # executor: it performs a blocking HTTP POST (HA strict mode
                # rejects blocking calls inside the event loop).
                if not self._model_identified:
                    await self.hass.async_add_executor_job(self.api.fetch_transcode_table)
                    self.api.identify_model(all_props)
                    self._model_identified = True

                # Fetch LAN config once (first full refresh only)
                if self._lan_config is None:
                    self._lan_config = await self.hass.async_add_executor_job(self.api.get_lan_config, self.dsn)

                # Start LAN server once if the machine supports it
                if not self._lan_start_attempted and self._lan_config:
                    await self._try_start_lan()

                # ContentStack: fetch drink catalog + bean adapt once
                # (gated by the failure backoff — see _load_contentstack)
                if not self._contentstack_loaded and now >= self._cs_next_retry:
                    await self._load_contentstack()

                self._last_full_refresh = now
                # Only a wake that actually reached the machine counts as a keepalive.
                # A timed-out wake must NOT reset the heartbeat freshness, or the next
                # poll skips an overdue keepalive (issue #73 review, numanx11).
                if wake_ok:
                    self._last_keepalive = now
                    if self._keepalive_failures:
                        _LOGGER.info(
                            "MQTT keepalive recovered via full refresh after %d failures",
                            self._keepalive_failures,
                        )
                    self._keepalive_failures = 0
                else:
                    self._keepalive_failures += 1

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
            # Neither rule applies when the monitor came from the LAN socket:
            # the machine pushed it itself, so it is fresh evidence regardless
            # of what the cloud cache or app_device_status says (issue #50).
            alarms = status.get("alarms", [])
            alarm_word = status.get("alarm_word")
            cloud_status = status.get("status", "UNKNOWN")
            monitor_from_lan = status.get("monitor_source") == "lan"
            no_cloud = cloud_status == "UNKNOWN"
            if not monitor_from_lan and (monitor_timed_out or no_cloud) and alarms:
                _LOGGER.debug(
                    "Suppressing %d alarms (timed_out=%s, no_cloud=%s)",
                    len(alarms),
                    monitor_timed_out,
                    no_cloud,
                )
                alarms = []
                alarm_word = None

            # Override machine state to Off only when the cloud session is live
            # (RUN) and the monitor has been idle past the timeout. With cloud=RUN
            # we trust that an idle monitor = idle machine. With cloud=SYNC the
            # MQTT keep-alive is dead and we have *no evidence* about the physical
            # state — last known monitor wins (likely "Ready" frozen from before
            # the session died). Pretending to know "Off" here misleads the
            # switch entity into logging fake "monitor confirmed Off state"
            # right after a turn_off, even though the command never reached the
            # machine (cloud accepted, MQTT never relayed).
            # Skipped when the monitor is LAN-sourced: a live LAN session
            # proves the machine is awake and its last pushed frame says
            # Ready/Brewing outright, so "unchanged for 45 min" means "idle",
            # not "off".
            machine_state = status.get("machine_state", "Unknown")
            if (
                monitor_timed_out
                and not monitor_from_lan
                and cloud_status == "RUN"
                and machine_state not in ("Unknown", "Off")
            ):
                _LOGGER.debug(
                    "Monitor unchanged for %.0f min (cloud=RUN), assuming machine is Off",
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

            # The published active profile must PRESERVE an absence rather than
            # invent slot 1 (issue #78): a fresh cloud slot that is a real one
            # wins, otherwise the full-refresh cache, otherwise ``None``.
            published_active = status.get("active_profile")
            if not _valid_slot(published_active):
                cached_active = self._cached_profiles.get("active")
                published_active = cached_active if _valid_slot(cached_active) else None

            return {
                "status": status.get("status", "UNKNOWN"),
                "machine_state": machine_state,
                "monitor_action": status.get("action"),
                "monitor_progress": status.get("progress"),
                "alarms": alarms,
                "alarm_word": alarm_word,
                "monitor_stale": monitor_timed_out,
                # "lan" when the state above came from the machine's own
                # socket push rather than the cloud cache (issue #50) —
                # surfaced for diagnostics and for the sensor entities.
                "monitor_source": status.get("monitor_source", "cloud"),
                # Issue #47 — the power-on confirmation needs to tell a fresh
                # monitor from a frozen one, and the only witnesses it has are
                # the frame itself and the clock embedded in its trailer.
                # Both were cloud-only internals of ``get_status`` until now.
                "monitor_raw": status.get("monitor_raw"),
                "monitor_ts": status.get("monitor_ts"),
                "profile": status.get("profile", 0),
                "counters": self._cached_counters,
                "beverages": self.beverages,
                "active_profile": published_active,
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
            # Drop the profile baseline so the next successful read re-establishes
            # it as a first sighting instead of comparing against a pre-outage
            # value and hijacking the HA selection (issue #78, coderabbit #79).
            self._last_d286 = None
            # HA only opens the reauth flow on ConfigEntryAuthFailed —
            # a plain UpdateFailed loops forever with last_update_success=False
            # and the user is never prompted to re-enter credentials (audit C1).
            # Text sanitized: HA logs it verbatim in its own ERROR logger,
            # outside the reach of our SanitizeFilter (audit 2026-11-05).
            raise ConfigEntryAuthFailed(f"Authentication error: {sanitize(err)}") from err
        except DeLonghiApiError as err:
            self._last_d286 = None  # re-baseline on recovery (issue #78)
            # Same: the API error text can carry the DSN-bearing Ayla URL.
            raise UpdateFailed(f"Error fetching data: {sanitize(err)}") from err
        except Exception as err:  # noqa: BLE001 — last-resort guard: coordinator must surface UpdateFailed
            self._last_d286 = None  # re-baseline on recovery (issue #78)
            _LOGGER.exception("Unexpected error during coordinator refresh")
            raise UpdateFailed(f"Unexpected error: {sanitize(err)}") from err

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
                # Narrow the bind to the interface that reaches the device
                # rather than 0.0.0.0 — keeps the LAN server off unrelated
                # interfaces (VPN, docker bridges, public WAN on weird HA
                # setups).
                bind_host=local_ip,
                # Only accept inbound requests from the device itself.
                # 127.0.0.1 / ::1 stays allowed automatically for diag.
                allowed_peers=frozenset({lan_ip}),
            )
            server = DeLonghiLanServer(config, on_property=self._on_lan_property)
            await server.start()
            self._lan_server = server
            _LAN_LOGGER.info(
                "LAN server started on %s:%d, device=%s (peer allowlist enforced)",
                local_ip,
                server.port,
                lan_ip,
            )
        except Exception:  # noqa: BLE001
            _LAN_LOGGER.exception("LAN server start failed, falling back to cloud")
            return

        # Open the LAN session: POST (never PUT) triggers the module's key
        # exchange; a periodic job then keep-alives (PUT) or re-opens (POST)
        # as the session lives or dies (issue #50).
        self._lan_open_pending_since = time.monotonic()
        await self._lan_open_session()
        hass = getattr(self, "hass", None)
        if hass is not None and self._lan_session_unsub is None:
            self._lan_session_unsub = async_track_time_interval(
                hass, self._lan_session_tick, timedelta(seconds=LAN_KEEPALIVE_SECONDS)
            )

    async def _lan_open_session(self) -> None:
        """POST local_reg to open (or reopen) the module's LAN session."""
        server = self._lan_server
        lan = self._lan_config or {}
        lan_ip = lan.get("lan_ip")
        if server is None or not lan_ip:
            return
        try:
            import aiohttp

            await register_with_device(
                aiohttp.ClientSession,
                device_ip=lan_ip,
                advertised_ip=server.advertised_ip,
                advertised_port=server.port,
                method="POST",
                dsn=self.dsn,
                notify=server.has_pending,
            )
            self._lan_open_pending_since = time.monotonic()
            _LAN_LOGGER.info("LAN registration POST accepted by %s — waiting for key exchange", lan_ip)
        except LanError:
            self._lan_open_pending_since = time.monotonic()
            _LAN_LOGGER.warning("LAN registration POST failed, will retry on next tick")
        except Exception as err:  # noqa: BLE001
            self._lan_open_pending_since = time.monotonic()
            _LAN_LOGGER.warning("Unexpected error during LAN registration: %s", err)

    async def _lan_session_tick(self, _now: datetime) -> None:
        """Keep-alive / re-open loop — the module expects a nudge every ~20 s.

        State machine (mirrors the official app and cremalink's nudger):
          no session            -> POST (open; retried every tick until handshake)
          live session          -> PUT  (keep-alive, notify=pending commands)
          stale session (>120s) -> reset + POST on next tick — never PUT
                                   after dropping our keys (hollow session)
        """
        server = self._lan_server
        lan = self._lan_config or {}
        lan_ip = lan.get("lan_ip")
        if server is None or not lan_ip:
            return
        try:
            import aiohttp

            if server.session is None:
                await self._lan_open_session()
            elif not server.is_session_alive(LAN_SESSION_STALE_SECONDS):
                _LAN_LOGGER.warning(
                    "LAN session stale (device silent for >%.0fs) — dropping keys, re-opening with POST on next tick",
                    LAN_SESSION_STALE_SECONDS,
                )
                server.reset_session()
                self._lan_active = False
                await self._lan_open_session()
            else:
                await register_with_device(
                    aiohttp.ClientSession,
                    device_ip=lan_ip,
                    advertised_ip=server.advertised_ip,
                    advertised_port=server.port,
                    method="PUT",
                    notify=server.has_pending,
                )
        except Exception as err:  # noqa: BLE001 — the tick must never die
            _LAN_LOGGER.debug("LAN session tick failed: %s", err)
        # lan_active reflects reality: a real handshake, never a bare 2xx.
        self._lan_active = server.session is not None and server.is_session_alive(LAN_SESSION_STALE_SECONDS)
        if server.session is not None:
            self._lan_no_handshake_warned = False
        if (
            server.session is None
            and self._lan_open_pending_since
            and time.monotonic() - self._lan_open_pending_since > 30
            and not self._lan_no_handshake_warned
        ):
            self._lan_no_handshake_warned = True
            _LAN_LOGGER.warning(
                "No key exchange received 30 s after local_reg POST — "
                "check that no other LAN client (cremalink, Coffee Link app) "
                "holds the module's single registration slot"
            )

    async def _on_lan_property(self, data: dict[str, Any]) -> None:
        """Callback invoked when the machine pushes a decrypted datapoint.

        Accepts the shapes seen in the wild:
          - ``{"property": {"name": ..., "value": ...}}``
          - ``{"properties": [{"property": {...}}, ...]}``
          - ``{"seq_no": N, "data": {"name": ..., "value": ...}}``
            (ECAM610.75 / DL-millcore — this is the shape in the issue #10
            logs; previously unrecognized, so all LAN telemetry was decoded
            then silently discarded.)
        """
        _LAN_LOGGER.debug("LAN property received: %s", data)

        cached: list[tuple[str, Any]] = []

        # ECAM610.75 / millcore envelope: {"seq_no": N, "data": {...}}
        inner = data.get("data")
        if isinstance(inner, dict) and inner.get("name"):
            cached.append((inner["name"], inner.get("value")))

        prop = data.get("property")
        if isinstance(prop, dict) and prop.get("name"):
            cached.append((prop["name"], prop.get("value")))

        for entry in data.get("properties") or []:
            if not isinstance(entry, dict):
                continue
            p = entry.get("property") if isinstance(entry.get("property"), dict) else entry
            if isinstance(p, dict) and p.get("name"):
                cached.append((p["name"], p.get("value")))

        for name, value in cached:
            self._lan_properties[name] = value
            _LAN_LOGGER.info("LAN property: %s = %s", name, value)

        # Trigger entity update so sensors can pick up LAN data
        if self._lan_properties:
            merged = dict(self.data) if isinstance(self.data, dict) else {}
            merged["lan_properties"] = dict(self._lan_properties)
            # A monitor pushed on the socket is the freshest state there is —
            # apply it immediately instead of waiting for the next 60s cloud
            # poll, which on DL-millcore would keep showing the frozen value
            # (issue #50: power-on via HA left the machine "Off" until the
            # Coffee Link app was opened).
            pushed_monitor = any(name in LAN_MONITOR_PROPERTIES for name, _ in cached)
            if pushed_monitor:
                before = merged.get("machine_state")
                overlay = self._lan_monitor_status()
                if overlay:
                    merged = self._apply_lan_monitor_to_data(merged, overlay)
                    # A socket push is the freshest LAN evidence — it becomes
                    # the sticky overlay, so a later poll whose session went
                    # stale keeps this state instead of flickering back to
                    # the frozen cloud value (issue #50).
                    self._lan_sticky = overlay
                    self._lan_sticky_at = monotonic()
                    self._lan_sticky_cloud_raw = self._last_cloud_monitor_raw
                    if overlay.get("monitor_raw"):
                        # Keep the staleness bookkeeping aligned with the
                        # LAN frame, otherwise the next cloud poll would
                        # compare against a cloud raw it never saw.
                        self._last_monitor_raw = overlay["monitor_raw"]
                        self._monitor_last_changed = monotonic()
                        self._monitor_stale_count = 0
                    if merged.get("machine_state") != before:
                        _LAN_LOGGER.info("LAN monitor: machine state %s → %s", before, merged.get("machine_state"))
            self.async_set_updated_data(merged)
        else:
            self.async_set_updated_data(self.data if self.data else {})

    def lan_session_alive(self) -> bool:
        """True when a LAN server is running and its session is fresh.

        Issue #47 (review finding 4): the power switch asks this before
        arming its confirmation ladder — a frozen cloud monitor is bearable
        while a live LAN session still pushes fresh frames, but with neither
        witness the ladder could never confirm and must not run at all.
        """
        server = self._lan_server
        if server is None:
            return False
        try:
            return bool(server.is_session_alive(LAN_SESSION_STALE_SECONDS))
        except Exception:  # noqa: BLE001 — a probe must never raise into the switch
            _LOGGER.debug("lan_session_alive probe failed", exc_info=True)
            return False

    async def send_command_lan(self, ecam_bytes: bytes) -> bool:
        """Enqueue an ECAM command for delivery on the next device LAN poll.

        Returns True when the LAN session is active and the command was
        queued. Returns False when the LAN path is unavailable — the caller
        is expected to fall back to the cloud ``send_command``.

        Packet shape matches what the device already receives over Ayla:
        a base64-encoded ECAM command + timestamp (± app_id), wrapped in
        the Ayla ``properties[].property`` envelope.
        """
        server = self._lan_server
        if server is None or server.session is None:
            return False
        if not server.is_session_alive(LAN_SESSION_STALE_SECONDS):
            _LAN_LOGGER.warning(
                "LAN session stale (device silent for >%.0fs), falling back to cloud",
                LAN_SESSION_STALE_SECONDS,
            )
            return False

        include_app_id = self.api._cmd_property != "data_request"  # noqa: SLF001
        property_name = self.api._cmd_property or "app_data_request"  # noqa: SLF001

        # data_request family (PD Soul / millcore): two firmware variants
        # observed in the wild act on OPPOSITE packet shapes (issues #10 vs
        # #34) and the machine polls its command queue infrequently — we
        # enqueue BOTH shapes so whichever firmware polls gets the one it
        # understands. Each firmware's strict ECAM parser ignores the other.
        if self.api._cmd_property == "data_request":  # noqa: SLF001
            props = []
            for app_id_flag in (False, True):
                b64 = self.api._build_packet(ecam_bytes, include_app_id=app_id_flag)  # noqa: SLF001
                props.append(
                    {
                        "property": {
                            "base_type": "string",
                            "dsn": self.dsn,
                            "name": property_name,
                            "value": b64,
                        }
                    }
                )
            payload = {"properties": props}
            await server.enqueue_command(payload)
            _LAN_LOGGER.info(
                "LAN command queued (both shapes): property=%s ecam=%s",
                property_name,
                ecam_bytes.hex(),
            )
            return True

        b64 = self.api._build_packet(ecam_bytes, include_app_id=include_app_id)  # noqa: SLF001

        payload = {
            "properties": [
                {
                    "property": {
                        "base_type": "string",
                        "dsn": self.dsn,
                        "name": property_name,
                        "value": b64,
                    }
                }
            ]
        }
        await server.enqueue_command(payload)
        _LAN_LOGGER.info(
            "LAN command queued: property=%s ecam=%s (app_id=%s)",
            property_name,
            ecam_bytes.hex(),
            include_app_id,
        )
        return True

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
        if self._lan_session_unsub is not None:
            self._lan_session_unsub()
            self._lan_session_unsub = None
        if self._lan_server is not None:
            try:
                await self._lan_server.stop()
            except Exception:  # noqa: BLE001
                _LAN_LOGGER.exception("Error stopping LAN server")
            self._lan_server = None
            self._lan_active = False
            # lan.py already logs "LAN server stopped" — the duplicate here
            # made testers hunt for a phantom port war (issue #50 audit).

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
            self.drink_catalog = await self.hass.async_add_executor_job(fetch_drink_catalog, model_pattern, model_name)
            self.bean_adapt = await self.hass.async_add_executor_job(fetch_bean_adapt, model_pattern, model_name)
            self.coffee_beans = await self.hass.async_add_executor_job(fetch_coffee_beans)
            self._contentstack_loaded = True
            if self._cs_failures:
                _LOGGER.info(
                    "ContentStack recovered after %d failed attempt(s): %d drinks, bean_adapt=%s, %d coffee beans",
                    self._cs_failures,
                    len(self.drink_catalog),
                    bool(self.bean_adapt),
                    len(self.coffee_beans),
                )
                self._cs_failures = 0
                self._cs_next_retry = 0.0
            else:
                _LOGGER.info(
                    "ContentStack loaded: %d drinks, bean_adapt=%s, %d coffee beans",
                    len(self.drink_catalog),
                    bool(self.bean_adapt),
                    len(self.coffee_beans),
                )
        except Exception:  # noqa: BLE001 — CMS failures must not block refresh
            # Exponential backoff: the catalog is auxiliary (brews run off the
            # machine's own recipes), so an unreachable CMS — DNS filter, offline
            # uplink — must not produce a full traceback every 10 minutes.
            # First failure keeps the traceback for diagnostics; after that,
            # single-line warnings as the retry window doubles toward the ceiling.
            self._cs_failures += 1
            backoff = min(FULL_REFRESH_INTERVAL * 2**self._cs_failures, CONTENTSTACK_RETRY_MAX)
            self._cs_next_retry = monotonic() + backoff
            if self._cs_failures == 1:
                _LOGGER.exception(
                    "ContentStack load failed (network/DNS to eu-cdn.contentstack.com?), next retry in ~%d min",
                    backoff // 60,
                )
            else:
                _LOGGER.warning(
                    "ContentStack still unavailable (attempt %d), next retry in ~%d min",
                    self._cs_failures,
                    backoff // 60,
                )

    def _detect_contentstack_pattern(self) -> str:
        """Return the best ECAM pattern for ContentStack title lookups.

        Order of precedence:
        1. ``d270_serialnumber`` raw — plaintext serials like ``ECAM61075MB…``.
        2. ``d270_serialnumber`` decoded — binary envelope SKU (issue #10).
        3. TranscodeTable model info (``appModelId``/``product_code``/``name``).
        4. Static OEM → ECAM family map (covers firmwares that surface no
           identifying string anywhere else, e.g. PD Soul ``DL-millcore``).

        Returning a non-empty pattern does not imply ContentStack actually
        indexes that family — only ECAM47/ECAM63 are populated as of
        2026-04. The downstream :func:`fetch_drink_catalog` path skips the
        HTTP call for unindexed families. Surfacing the correct family
        anyway lets users see their machine recognised in logs and gives
        future ContentStack data updates a working hook.
        """
        import re

        from .api import DeLonghiApi
        from .const import SKU_TO_ECAM_PATTERN

        if self._last_all_props:
            serial_prop = self._last_all_props.get("d270_serialnumber", {})
            serial_val = serial_prop.get("value", "") if isinstance(serial_prop, dict) else ""
            if serial_val:
                match = re.search(r"ECAM\d+", serial_val, re.IGNORECASE)
                if match:
                    return match.group(0).upper()
                # Binary envelope (PR #19) — decode and try a SKU-based map.
                serial_info = DeLonghiApi.parse_serial_number(serial_val)
                if serial_info and serial_info.get("format") == "binary":
                    sku = serial_info.get("sku", "")
                    if sku and sku in SKU_TO_ECAM_PATTERN:
                        return SKU_TO_ECAM_PATTERN[sku]

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
        # Map covers OEM identifiers we have hardware for. Families that
        # ContentStack does not yet index (ECAM61 PD Soul) are still listed
        # so the integration can surface the correct family in logs;
        # contentstack.fetch_drink_catalog short-circuits unknown families.
        oem_ecam_map = {
            "DL-striker-cb": "ECAM63050",
            "DL-striker-best": "ECAM63075",
            "DL-pd-soul": "ECAM61075",
            "DL-pd-soul-better": "ECAM61075",
            "DL-millcore": "ECAM61075",
        }
        return oem_ecam_map.get(oem, "")
