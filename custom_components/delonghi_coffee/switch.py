"""Switch platform for De'Longhi Coffee — power on/off toggle."""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import DeLonghiApi, DeLonghiApiError, DeLonghiAuthError
from .const import (
    BLOCKING_ALARM_BITS,
    DOMAIN,
    POWER_CONFIRM_CLOCK_SKEW,
    POWER_CONFIRM_FAST_POLL_DURATION,
    POWER_CONFIRM_FAST_POLL_INTERVAL,
    POWER_CONFIRM_POLL,
    POWER_CONFIRM_WINDOWS,
    POWER_OFF_CMD,
    POWER_ON_CMD,
    POWER_ON_RETRIES,
    POWER_RETRY_DELAY,
    POWER_STALE_THRESHOLD,
    POWER_WAKE_DELAY,
)
from .coordinator import DeLonghiCoordinator
from .logger import get_logger
from .sensor import _device_info

_LOGGER = get_logger(__name__)

# Re-exported under legacy names so existing tests and private callers still
# resolve. ``_RETRY_DELAY`` is gone: issue #47 replaced the single blind retry
# with the confirmation ladder, which reads POWER_RETRY_DELAY directly.
_WAKE_DELAY: float = POWER_WAKE_DELAY
_STALE_THRESHOLD: int = POWER_STALE_THRESHOLD

# States that mean "the machine is not awake" for the issue #47 confirmation
# ladder (review finding 1). ``Error`` (state 4) belongs here: a machine in
# error is not usefully on, and shortcutting the ladder for it would claim a
# power-on the machine did not complete.
_NOT_ON_STATES: tuple[str, ...] = ("Off", "Going to sleep", "Error")


def _state_is_not_on(state: str) -> bool:
    """True when a monitor state is not evidence of an awake machine.

    ``Unknown`` arrives in two renderings and both mean "no proof": bare
    ``Unknown`` (no monitor frame at all) and ``Unknown (N)`` — the actual
    rendering of an unmapped state byte (``api.py`` builds
    ``MACHINE_STATES.get(v, f"Unknown ({v})")``, e.g. ``"Unknown (29)"``).
    An exact-match tuple silently treated every unmapped byte as "on", which
    shortcut the ladder on exactly the frames that prove nothing.
    """
    return state in _NOT_ON_STATES or state.startswith("Unknown")


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up switch entity."""
    data: dict[str, Any] = hass.data[DOMAIN][entry.entry_id]
    api: DeLonghiApi = data["api"]
    coordinator: DeLonghiCoordinator = data["coordinator"]
    dsn: str = data["dsn"]
    model: str = data["model"]
    device_name: str = data["device_name"]
    sw_version: str | None = data.get("sw_version")

    async_add_entities([DeLonghiPowerSwitch(api, coordinator, dsn, model, device_name, sw_version)])


class DeLonghiPowerSwitch(CoordinatorEntity[DeLonghiCoordinator], SwitchEntity):
    """Switch to power on/off the coffee machine."""

    _attr_device_class = SwitchDeviceClass.SWITCH

    def __init__(
        self,
        api: DeLonghiApi,
        coordinator: DeLonghiCoordinator,
        dsn: str,
        model: str,
        device_name: str,
        sw_version: str | None,
    ) -> None:
        super().__init__(coordinator)
        self._api = api
        self._dsn = dsn
        self._assumed_on = False
        self._last_commanded_on: bool | None = None
        self._monitor_stale_count: int = 0
        self._last_monitor_state: str | None = None
        self._cmd_lock = asyncio.Lock()
        # Cooperative cancel signal for the wake-delay window in
        # ``async_turn_on``. ``async_turn_off`` sets this to abort an
        # in-progress turn_on before POWER_ON is even sent (H-logic-2).
        self._cancel_turn_on = asyncio.Event()
        # Cached state (B1): the staleness machine advances exactly once
        # per coordinator refresh in ``_handle_coordinator_update``; the
        # ``is_on`` property reads ``_cached_is_on`` and does NOT mutate
        # any of ``_monitor_stale_count`` / ``_last_commanded_on`` /
        # ``_assumed_on`` / ``_last_monitor_state``. Sentinel below tracks
        # which ``coordinator.data`` identity has been processed so that
        # a test (or any caller) that swaps the data dict without firing
        # the coordinator hook still produces a consistent answer.
        self._cached_is_on: bool = False
        # Identity sentinel replacing raw id() — id() values get recycled
        # after GC and a fresh dict can land on the same address, skipping
        # the reconcile (audit m3). Holding the previous data dict for one
        # extra cycle is negligible; identity comparison via `is` is exact.
        self._processed_data_obj: dict | None = None
        # Track the background retry coroutine so it can be cancelled on
        # entity removal — otherwise it survives reloads as an orphan task.
        # Since issue #47 this slot holds the *confirmation* ladder
        # (``_confirm_power_on``), which also owns the retries.
        self._retry_task: asyncio.Task | None = None
        # Issue #47 — evidence baseline for the confirmation ladder: the
        # monitor frame as seen *before* POWER_ON was sent (a frame that moves
        # away from it is proof the machine answered) and the wall-clock time
        # of the wake ping, which the embedded monitor clock is compared
        # against. ``time.time`` (not monotonic) because the comparator is the
        # coffee machine's own clock, not ours.
        self._power_baseline_raw: str | None = None
        self._power_ping_wall: float = 0.0
        # True while the "machine ignored POWER_ON" persistent notification is
        # up (review finding 3). ``_reconcile_monitor_state`` uses it to take
        # the banner down as soon as the machine wakes anyway — a late answer,
        # the physical button, or a later poll must not leave the user staring
        # at an alarm that is no longer true.
        self._no_response_notified = False
        self._attr_unique_id = f"{dsn}_power"
        self._attr_has_entity_name = True
        self._attr_translation_key = "power"
        self._attr_icon = "mdi:coffee-maker"
        self._attr_device_info = _device_info(dsn, model, device_name, sw_version)

    @property
    def assumed_state(self) -> bool:
        """Always True — cloud monitor is unreliable and can show stale state."""
        return True

    def _handle_coordinator_update(self) -> None:
        """Called once per coordinator refresh. Advance the staleness
        machine and write the cached ``is_on`` value the property reads.

        Bug history (B1, fixed 2026-05-08): the staleness logic used to
        live inside the ``is_on`` property body and mutated counters on
        every read. HA reads ``is_on`` multiple times per refresh — the
        counter advanced too fast, ``_last_commanded_on`` was cleared
        prematurely, and ``Switch: monitor confirmed`` could fire on
        spurious reads.
        """
        self._reconcile_monitor_state()
        # CoordinatorEntity's hook calls async_write_ha_state(); replicate
        # that explicitly so the test harness (which stubs base classes)
        # exercises the same signal flow as production.
        self.async_write_ha_state()

    def _reconcile_monitor_state(self) -> None:
        """Advance the staleness state machine for one update cycle and
        write the resulting on/off into ``_cached_is_on``.

        Idempotency contract: callers may invoke this at most once per
        coordinator refresh. The ``is_on`` property uses
        ``_processed_data_obj`` to guarantee that property reads do not
        re-enter the machine within the same cycle.
        """
        state = self.coordinator.data.get("machine_state", "Unknown")
        self._processed_data_obj = self.coordinator.data

        if self._no_response_notified and not _state_is_not_on(state):
            # The machine woke up after we told the user it ignored us — the
            # banner is stale news (review finding 3). Best-effort: a harness
            # without a task scheduler must not break the reconcile path.
            with contextlib.suppress(Exception):
                self.hass.async_create_task(self._dismiss_no_response_notification())

        if state == "Unknown":
            self._cached_is_on = self._assumed_on
            return

        if self._last_commanded_on is not None:
            # Same predicate as the #47 ladder (re-review finding 1): ``Error``
            # and ``Unknown (N)`` are not evidence of an awake machine. With
            # the old exact-tuple check, one refresh carrying either rendering
            # "confirmed" the command, cleared ``_last_commanded_on`` and
            # silently disarmed the ladder underneath its own feet.
            monitor_says_on = not _state_is_not_on(state)

            if monitor_says_on != self._last_commanded_on:
                if state == self._last_monitor_state:
                    self._monitor_stale_count += 1
                else:
                    self._monitor_stale_count = 1
            else:
                self._monitor_stale_count = 0
                self._last_commanded_on = None
                _LOGGER.debug("Switch: monitor confirmed %s state", state)

        self._last_monitor_state = state

        if self._monitor_stale_count >= _STALE_THRESHOLD and self._last_commanded_on is not None:
            self._cached_is_on = self._assumed_on
            return

        result = state not in ("Off", "Going to sleep")
        if self._last_commanded_on is None:
            self._assumed_on = result
        self._cached_is_on = result

    @property
    def is_on(self) -> bool:
        """Return cached on/off computed by ``_reconcile_monitor_state``.

        Reading is *pure*: no state mutation. If a caller swaps
        ``coordinator.data`` without going through
        ``_handle_coordinator_update`` (test fixtures, manual harness),
        a single reconcile pass runs lazily so the answer reflects the
        new data — but subsequent reads against the same data identity
        return the cached value with zero side effects.

        Special case: when the coordinator state is ``Unknown`` we have
        no monitor signal at all, so we return the live ``_assumed_on``
        value (which a turn_on/turn_off updates synchronously). Caching
        the assumed value would lag behind a state change applied
        between two refreshes.
        """
        state = self.coordinator.data.get("machine_state", "Unknown")
        if state == "Unknown":
            return self._assumed_on
        # Identity sentinel — see __init__ (audit m3).
        if self._processed_data_obj is not self.coordinator.data:
            self._reconcile_monitor_state()
        return self._cached_is_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Power on the machine.

        Sequence captured from official Coffee Link app via MITM:
        1. Wake ping (app_device_connected)
        2. Wait 15s for WiFi module to become receptive
        3. Send power ON command (app_data_request)
        4. Post-command ping (keeps MQTT session alive)
        5. Confirm in the background that the machine actually answered
           (:meth:`_confirm_power_on`, issue #47) — re-sending POWER_ON up to
           ``POWER_ON_RETRIES`` times and, if nothing ever answers, leaving the
           switch OFF instead of claiming a state we never got.

        The switch is deliberately *not* flipped to ON here any more: the old
        ``_assumed_on = True`` made HA report "on" for three minutes after a
        command the machine silently dropped (issue #47 reporter: switch on at
        13:20:34, machine cold). Only the confirmation sets it — except when
        the monitor already said the machine was awake (the switch shows ON
        immediately, but the ladder is still armed as a net: the frame may be
        stale, and a fresh one confirms instantly without re-sending), or when
        the model has no confirmable witness at all (frozen cloud monitor and
        no LAN session), which keeps the pre-#47 assumed-state contract.
        """
        if self._cmd_lock.locked():
            _LOGGER.warning("Power command already in progress, ignoring")
            return

        # Surface blocking alarms upfront — without this, a turn_on with
        # Water Tank Empty (or any other blocking alarm) silently sends the
        # wake/POWER_ON commands and the machine sits in Turning On forever
        # while the user wonders why nothing happens.
        self._announce_blocking_alarms_for_power_on()

        # Arm the cooperative cancel signal so async_turn_off can abort
        # this turn_on if the user changes their mind during the wake delay.
        self._cancel_turn_on.clear()

        async with self._cmd_lock:
            _LOGGER.info("Powering on %s", self._dsn)
            # Evidence baseline for the confirmation ladder, captured *before*
            # anything is sent: whatever the monitor looked like beforehand is
            # what a real answer has to differ from.
            pre_state = self.coordinator.data.get("machine_state", "Unknown")
            self._power_baseline_raw = self.coordinator.data.get("monitor_raw")
            try:
                # Phase 1: Wake ping + cancellable delay (like app: ping → 15s wait)
                # Fallback to request_monitor if ping unsupported (PrimaDonna Soul et al.)
                try:
                    self._power_ping_wall = time.time()
                    ping_ok = await self.hass.async_add_executor_job(self._api.ping_connected, self._dsn)
                    if not ping_ok:
                        await self.hass.async_add_executor_job(self._api.request_monitor, self._dsn)
                    _LOGGER.debug("Wake sent, waiting %.0fs", _WAKE_DELAY)
                except (DeLonghiApiError, DeLonghiAuthError):
                    _LOGGER.debug("Wake failed, sending power ON anyway")

                # Cancellable wake delay (H-logic-2): wait_for returns when
                # the cancel event fires (user issued turn_off), and times out
                # normally otherwise. Either path proceeds — but the
                # event-fire path aborts the rest of turn_on so POWER_ON is
                # never sent against the user's intent.
                try:
                    await asyncio.wait_for(self._cancel_turn_on.wait(), timeout=_WAKE_DELAY)
                    _LOGGER.info("Power ON aborted by user during wake delay")
                    return
                except TimeoutError:
                    pass  # wake delay completed normally (asyncio.TimeoutError aliased since 3.11)

                # Phase 2: Power ON command
                if not await self.coordinator.send_command_lan(POWER_ON_CMD):
                    await self.hass.async_add_executor_job(self._api.send_command, self._dsn, POWER_ON_CMD)
                _LOGGER.info("Power ON command sent")

                # Phase 3: Post-command refresh (force the machine to push its new state)
                try:
                    ping_ok = await self.hass.async_add_executor_job(self._api.ping_connected, self._dsn)
                    if not ping_ok:
                        await self.hass.async_add_executor_job(self._api.request_monitor, self._dsn)
                except (DeLonghiApiError, DeLonghiAuthError):
                    pass

                self._last_commanded_on = True
                self._monitor_stale_count = 0
                # Issue #47: ``_assumed_on`` is NOT set here any more — only the
                # confirmation task sets it. With it False, the existing
                # staleness machine keeps ``is_on`` at False while the monitor
                # contradicts us, which is exactly what the reporter asked for.
                already_on = not _state_is_not_on(pre_state)
                if already_on:
                    # The monitor said the machine was already awake, so the
                    # switch may show ON right away — but this is a shortcut to
                    # the *display*, NOT a skip of the confirmation (review
                    # finding 1): ``pre_state`` may come from a stale frame
                    # (auto-sleep between the frame and now), and HEAD armed
                    # its retry unconditionally here. The ladder is armed
                    # regardless; when the frame really is fresh it confirms on
                    # its first tick without re-sending anything.
                    self._assumed_on = True
            except (DeLonghiApiError, DeLonghiAuthError) as err:
                raise HomeAssistantError(f"Failed to power on: {err}") from err
            self.async_write_ha_state()

            # Can the ladder confirm anything at all (review finding 4)? A
            # frozen cloud monitor with no live LAN session (PrimaDonna Soul
            # without LAN, issue #50) has no witness: the ladder could never
            # confirm, would burn 4 POWER_ON per turn_on instead of HEAD's 2,
            # and end in a false alarm. Court-circuit to the HEAD contract.
            confirmable = bool(self._api.cloud_monitor_reliable) or self.coordinator.lan_session_alive()

            # Reactive refresh: capture Turning On → Ready transitions
            # immediately instead of waiting up to 60s for the next poll.
            # While the ladder is running we poll finer (5s) so a cloud push is
            # seen the second it lands; the confirmation re-arms the 240/10
            # window that used to be armed here.
            if confirmable and not already_on:
                self.coordinator.request_fast_poll(
                    duration_s=POWER_CONFIRM_FAST_POLL_DURATION,
                    interval_s=POWER_CONFIRM_FAST_POLL_INTERVAL,
                )
            else:
                self.coordinator.request_fast_poll(duration_s=240.0, interval_s=10.0)
            self.hass.async_create_task(self.coordinator.async_request_refresh())

            # Phase 4: background confirmation (re-sends POWER_ON if the
            # machine stays mute). Cancel any ladder still pending from a prior
            # command so we never have two of them racing against the machine.
            if self._retry_task is not None and not self._retry_task.done():
                self._retry_task.cancel()
            if confirmable:
                self._retry_task = self.hass.async_create_background_task(
                    self._confirm_power_on(already_on),
                    name=f"delonghi_power_confirm_{self._dsn}",
                )
            else:
                # Frozen monitor, no LAN: assumed state now + one blind retry
                # at 180 s, exactly the pre-#47 contract, no notification.
                self._assumed_on = True
                self._retry_task = self.hass.async_create_background_task(
                    self._legacy_power_on_retry(),
                    name=f"delonghi_power_retry_{self._dsn}",
                )

    def _announce_blocking_alarms_for_power_on(self) -> None:
        """Surface blocking alarms before sending POWER_ON_CMD.

        The De'Longhi firmware accepts the wake-up + POWER_ON sequence even
        when a blocking alarm is active (Water Tank Empty, Hydraulic Problem,
        etc.). It just sits in Turning On indefinitely — never reaching Ready
        — leaving the user with no visible reason. We emit:
          - WARNING in the HA log with the explicit list of blocking alarms;
          - persistent_notification in the HA UI so the issue surfaces in the
            user's notification tray instead of buried in logs.
        We do *not* abort the command: some alarm states clear themselves
        once the wake sequence runs (e.g. transient probe blips after a
        long sleep), and short-circuiting power-on would regress users who
        actually want to wake the machine to clear an old alarm.
        """
        alarms = self.coordinator.data.get("alarms") or []
        blocking = [a for a in alarms if isinstance(a, dict) and a.get("bit") in BLOCKING_ALARM_BITS]
        if not blocking:
            return

        names = ", ".join(a.get("name", f"bit{a.get('bit')}") for a in blocking)
        _LOGGER.warning(
            "Power on requested with blocking alarms active — machine may stay in 'Turning On': %s",
            names,
        )

        # The persistent notification puts the same info in the HA UI so the
        # user sees it without reading logs. Failing to schedule the
        # notification (test harness, missing service) must never break the
        # power-on path.
        try:
            self.hass.async_create_task(
                self.hass.services.async_call(
                    "persistent_notification",
                    "create",
                    {
                        "title": f"Cafetière bloquée — {self._dsn}",
                        "message": (
                            "La machine ne pourra pas finir de s'allumer tant que ces "
                            "alarmes restent actives :\n\n- "
                            + "\n- ".join(a.get("name", f"bit{a.get('bit')}") for a in blocking)
                        ),
                        "notification_id": f"delonghi_blocking_alarms_{self._dsn}",
                    },
                    blocking=False,
                )
            )
        except Exception as err:  # noqa: BLE001 — defensive, must never abort turn_on
            _LOGGER.debug("Skipping persistent_notification (harness or unsupported): %s", err)

    @property
    def _no_response_notification_id(self) -> str:
        """Id of the persistent notification warning about an ignored POWER_ON."""
        return f"delonghi_power_no_response_{self._dsn}"

    @staticmethod
    def _monitor_is_fresh(data: dict[str, Any], floor: float) -> bool:
        """True when ``data`` carries evidence that the machine answered POWER_ON.

        One witness and one gate (issue #47, review finding 5):

        * the monitor frame embeds a unix clock at or after the wake ping
          (minus the tolerated skew). The trailer is real — CRC-verified on
          both real captures in the test suite, the millcore dump of #3/#10
          *and* a live Eletta Explore frame — but it is the *machine's* clock
          and we assume it is UTC (finding 6). UTC vs local time is not
          settled: a firmware stamping local time (EU offsets are ahead of
          UTC) makes this gate pass trivially, and the state gate below then
          carries the discrimination alone;
        * the frame must report a state that is not off-ish. That gate is what
          makes the witness mean anything: the wake ping itself
          (``app_device_connected``, ``request_monitor`` on the models without
          it) *provokes* a monitor push, so a frame stamped after the ping only
          proves the machine is talking — not that it obeyed. In the ignored
          run of #47 nothing arrived at all; in the runs that worked the frames
          reported state 1 (Turning On).

        The old second witness — "the raw frame moved away from the pre-command
        baseline" — is gone on purpose: the timestamp lives *inside*
        ``monitor_raw``, so every live machine's raw always differs from the
        baseline and the "independent" witness fired for any living device.
        Discrimination is the state gate's job alone; keeping the raw witness
        was a false tell that made the evidence look stronger than it is.
        """
        state = data.get("machine_state", "Unknown")
        if _state_is_not_on(state):
            return False
        ts = data.get("monitor_ts")
        return isinstance(ts, (int, float)) and ts >= floor

    def _fresh_off_evidence(self, floor: float) -> bool:
        """True when a *fresh* monitor frame proves the machine is off.

        Only used in ``already_on`` mode: the pre-command frame claimed the
        machine was awake, so the ladder must not spam POWER_ON at a machine
        that is merely idle with an old timestamp. A fresh Off/Going to sleep
        frame means the pre_state was wrong (auto-sleep after the frame) and
        re-sending is justified; mere staleness is not.
        """
        data = self.coordinator.data or {}
        if data.get("machine_state", "Unknown") not in ("Off", "Going to sleep"):
            return False
        # NB: not _monitor_is_fresh — that helper gates on an on-ish state.
        ts = data.get("monitor_ts")
        return isinstance(ts, (int, float)) and ts >= floor

    async def _confirm_power_on(self, already_on: bool = False) -> None:
        """Confirm — or deny — the POWER_ON that ``async_turn_on`` just sent.

        Replaces the blind "sleep 180 s, re-send once" retry (issue #47). After
        each send we watch ``coordinator.data`` for the window assigned to it
        (``POWER_CONFIRM_WINDOWS`` — sends leave at t=0/20/45/75 s so the last
        one lands past the 70 s latency the #47 reporter actually measured as
        *successful*); a mute machine gets POWER_ON again, up to
        ``POWER_ON_RETRIES`` re-sends, and the failure notification is raised
        only after the last window burns out. ``async_turn_on`` never waits for
        any of this: the ladder runs in the ``_retry_task`` slot, so the
        cancellation points that already existed (turn_off, entity removal)
        cover it unchanged.

        Aborts silently whenever the user or the monitor changed the question:
        ``_last_commanded_on`` no longer True means either a turn_off (the
        machine being Off is then the *desired* state — re-illuminating it three
        minutes after every quick on→off was a real bug, observed 2026-05-01)
        or the staleness machine having seen the monitor agree with us already.

        The wait loops are bounded by an iteration count instead of a
        wall-clock deadline on purpose: the test suite patches
        ``asyncio.sleep`` to a no-op, and ``while monotonic() < deadline``
        would then spin forever.
        """
        baseline_raw = self._power_baseline_raw
        floor = (self._power_ping_wall or time.time()) - POWER_CONFIRM_CLOCK_SKEW
        confirmed = False
        attempt = 0

        for attempt in range(len(POWER_CONFIRM_WINDOWS)):
            if attempt:
                if self._last_commanded_on is not True:
                    _LOGGER.debug("Power ON confirmation aborted (last_commanded_on=%s)", self._last_commanded_on)
                    return
                # Review finding 7: hold ``_cmd_lock`` across check+send. A
                # turn_off that queued behind this ladder flips
                # ``_last_commanded_on`` to False *while holding the same
                # lock*, so re-checking inside it closes the window where a
                # POWER_OFF could be followed by a delayed POWER_ON.
                async with self._cmd_lock:
                    if self._last_commanded_on is not True:
                        _LOGGER.debug("Power ON confirmation aborted (last_commanded_on=%s)", self._last_commanded_on)
                        return
                    if already_on and not self._fresh_off_evidence(floor):
                        # Re-review finding 2: the pre-command frame claimed
                        # the machine was awake and nothing *fresh* proves
                        # otherwise (idle machine, old timestamp). HEAD sent
                        # zero retries in this situation — do not spam
                        # POWER_ON at a machine that may well be on.
                        _LOGGER.debug("Power ON ladder: already_on without fresh off-evidence, not re-sending")
                    else:
                        try:
                            # Re-send without the 15 s wake delay: the machine
                            # was just pinged and the first command already
                            # woke it, and the reporter's own successful retry
                            # (13:21:44) came 70 s after the first attempt,
                            # not 195 s.
                            # [À CONFIRMER E] — if fast re-sends turn out to
                            # be ignored where the old delayed retry worked,
                            # re-insert ``await asyncio.sleep(_WAKE_DELAY)``
                            # here (+30 s worst case).
                            if not await self.coordinator.send_command_lan(POWER_ON_CMD):
                                await self.hass.async_add_executor_job(self._api.send_command, self._dsn, POWER_ON_CMD)
                            with contextlib.suppress(DeLonghiApiError, DeLonghiAuthError):
                                if not await self.hass.async_add_executor_job(self._api.ping_connected, self._dsn):
                                    await self.hass.async_add_executor_job(self._api.request_monitor, self._dsn)
                            _LOGGER.info(
                                "Power ON retry %d/%d sent (no fresh monitor within %.0fs)",
                                attempt,
                                POWER_ON_RETRIES,
                                POWER_CONFIRM_WINDOWS[attempt - 1],
                            )
                        except (DeLonghiApiError, DeLonghiAuthError) as err:
                            _LOGGER.warning("Power ON retry failed: %s", err)

            checks = max(1, int(POWER_CONFIRM_WINDOWS[attempt] / POWER_CONFIRM_POLL))
            for _ in range(checks):
                await asyncio.sleep(POWER_CONFIRM_POLL)
                if self._last_commanded_on is not True:
                    _LOGGER.debug("Power ON confirmation aborted (last_commanded_on=%s)", self._last_commanded_on)
                    return
                if self._monitor_is_fresh(self.coordinator.data or {}, floor):
                    confirmed = True
                    break
            if confirmed:
                break

        if confirmed:
            self._assumed_on = True
            self._monitor_stale_count = 0
            _LOGGER.info("Power ON confirmed by fresh monitor (attempt %d)", attempt + 1)
            self.async_write_ha_state()
            # Long reactive window again so Turning On → Ready is captured.
            self.coordinator.request_fast_poll(duration_s=240.0, interval_s=10.0)
            await self._dismiss_no_response_notification()
            return

        if baseline_raw is None:
            # No monitor frame was ever observed on this machine, so the ladder
            # has no witness at all: it must not accuse the machine of ignoring
            # anything. Legacy assumed-on, and stay quiet.
            _LOGGER.info("Power ON unconfirmable (no monitor frame ever seen) — keeping assumed state")
            self._assumed_on = True
            self.async_write_ha_state()
        elif already_on:
            # Re-review finding 2: the pre-command frame claimed the machine
            # was awake, the ladder never confirmed via a fresh frame but also
            # never saw fresh *off* evidence (an idle machine with an old
            # timestamp). Trust the last known state — HEAD parity, no
            # parasitic re-sends, no banner.
            _LOGGER.info("Power ON unconfirmed, but pre-command state was awake — keeping switch on")
            self._assumed_on = True
            self.async_write_ha_state()
        elif self._api.cloud_monitor_reliable:
            # Striker family: the machine pushes its monitor by itself, so a
            # monitor that never moved means it never heard us. Say so instead
            # of pretending — the switch stays OFF, which is the honest answer.
            self._assumed_on = False
            _LOGGER.warning(
                "Power ON ignored by the machine after %d attempts — switch kept off",
                len(POWER_CONFIRM_WINDOWS),
            )
            await self._notify_no_response()
        else:
            # Frozen cloud monitor that went unconfirmable mid-ladder (the LAN
            # died after turn_on judged it confirmable): muteness proves
            # nothing there, so keep the pre-#47 behaviour rather than
            # regressing those models to a stuck-off switch.
            _LOGGER.info("Power ON unconfirmable on a frozen-monitor model — keeping assumed state")
            self._assumed_on = True
            self.async_write_ha_state()

        # Safety net: a machine that is off the Wi-Fi entirely answers no
        # monitor at all, and the only sequence that ever woke such a one in the
        # old flow was ping → 15 s → send. Keep it, minus the time the ladder
        # already spent, and only if nothing has been confirmed since.
        await asyncio.sleep(max(POWER_RETRY_DELAY - sum(POWER_CONFIRM_WINDOWS), 0))
        await self._power_on_safety_net()

    async def _legacy_power_on_retry(self) -> None:
        """Pre-#47 contract for models the ladder cannot confirm (finding 4).

        Frozen cloud monitor and no live LAN session: muteness proves nothing,
        so the honest answer is the old one — assumed state (already set by
        ``async_turn_on``) and a single blind retry after 180 s. No ladder, no
        extra POWER_ON per turn, no notification.
        """
        await asyncio.sleep(POWER_RETRY_DELAY)
        await self._power_on_safety_net()

    async def _power_on_safety_net(self) -> None:
        """The legacy 180 s full-sequence retry, shared by the ladder tail and
        the frozen-monitor short-circuit.

        Sends the old ping → 15 s → POWER_ON sequence only if the user still
        wants the machine on and the monitor still says it is not.
        """
        if self._last_commanded_on is not True:
            _LOGGER.debug("Power ON safety retry aborted: user turned off in between")
            return

        state = (self.coordinator.data or {}).get("machine_state", "Unknown")
        if not _state_is_not_on(state):
            _LOGGER.debug("Power ON confirmed by monitor (%s), no retry needed", state)
            return

        _LOGGER.info("Power ON not confirmed after %.0fs, retrying", POWER_RETRY_DELAY)
        # Same lock discipline as the ladder re-sends (review finding 7): a
        # turn_off must never be followed by a delayed POWER_ON from here.
        async with self._cmd_lock:
            if self._last_commanded_on is not True:
                _LOGGER.debug("Power ON safety retry aborted: user turned off in between")
                return
            try:
                ping_ok = await self.hass.async_add_executor_job(self._api.ping_connected, self._dsn)
                if not ping_ok:
                    await self.hass.async_add_executor_job(self._api.request_monitor, self._dsn)
                await asyncio.sleep(_WAKE_DELAY)
                if not await self.coordinator.send_command_lan(POWER_ON_CMD):
                    await self.hass.async_add_executor_job(self._api.send_command, self._dsn, POWER_ON_CMD)
                ping_ok = await self.hass.async_add_executor_job(self._api.ping_connected, self._dsn)
                if not ping_ok:
                    await self.hass.async_add_executor_job(self._api.request_monitor, self._dsn)
                _LOGGER.info("Power ON retry sent")
            except (DeLonghiApiError, DeLonghiAuthError) as err:
                _LOGGER.warning("Power ON retry failed: %s", err)

    async def _notify_no_response(self) -> None:
        """Tell the user the machine ignored every POWER_ON we sent.

        Same shape as :meth:`_announce_blocking_alarms_for_power_on`: a WARNING
        in the log (already emitted by the caller) plus a persistent
        notification in the UI, and a notification service we cannot reach must
        never break the power path — hence the blanket ``except`` (the test
        harness hands us a MagicMock hass whose ``async_create_task`` rejects a
        non-coroutine).
        """
        self._no_response_notified = True
        try:
            self.hass.async_create_task(
                self.hass.services.async_call(
                    "persistent_notification",
                    "create",
                    {
                        "title": f"Cafetière restée éteinte — {self._dsn}",
                        "message": (
                            f"La machine n'a répondu à aucune commande d'allumage "
                            f"({len(POWER_CONFIRM_WINDOWS)} essais en "
                            f"{int(sum(POWER_CONFIRM_WINDOWS))} s). "
                            "Elle est probablement hors du réseau Wi-Fi : vérifiez son "
                            "écran, puis réessayez."
                        ),
                        "notification_id": self._no_response_notification_id,
                    },
                    blocking=False,
                )
            )
        except Exception as err:  # noqa: BLE001 — defensive, must never abort the power path
            _LOGGER.debug("Skipping persistent_notification (harness or unsupported): %s", err)

    async def _dismiss_no_response_notification(self) -> None:
        """Remove the 'machine ignored POWER_ON' notification, if one is up."""
        self._no_response_notified = False
        try:
            self.hass.async_create_task(
                self.hass.services.async_call(
                    "persistent_notification",
                    "dismiss",
                    {"notification_id": self._no_response_notification_id},
                    blocking=False,
                )
            )
        except Exception as err:  # noqa: BLE001 — defensive, must never abort the power path
            _LOGGER.debug("Skipping persistent_notification dismiss (harness or unsupported): %s", err)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Power off the machine (standby).

        H-logic-2: if a turn_on is in progress (parked in its 15 s wake
        delay), set the cooperative cancel event so the wake aborts and
        POWER_ON is never sent. Then acquire the lock and send POWER_OFF.
        """
        # Signal any in-flight turn_on to abort its wake delay. If no
        # turn_on is in flight this is a no-op.
        self._cancel_turn_on.set()

        async with self._cmd_lock:
            _LOGGER.info("Powering off %s", self._dsn)

            # Cancel any pending power-on retry — the user just changed their
            # mind. Without this, the retry would fire 3 min later, see
            # machine_state=Off (because we just turned it off), and renvoie
            # POWER_ON_CMD, re-allumant la machine contre la volonté du user.
            if self._retry_task is not None and not self._retry_task.done():
                self._retry_task.cancel()

            # Issue #47: the user is now doing something about the machine, so
            # the "it ignored our power-on" banner is no longer news.
            # Best-effort — a notification service that cannot be reached must
            # not turn a working power-off into an error.
            await self._dismiss_no_response_notification()

            try:
                if not await self.coordinator.send_command_lan(POWER_OFF_CMD):
                    await self.hass.async_add_executor_job(self._api.send_command, self._dsn, POWER_OFF_CMD)

                # Post-command refresh — force the machine to push its new state
                # so the HA monitor reflects Off immediately instead of relying on
                # the next full refresh cycle.
                try:
                    ping_ok = await self.hass.async_add_executor_job(self._api.ping_connected, self._dsn)
                    if not ping_ok:
                        await self.hass.async_add_executor_job(self._api.request_monitor, self._dsn)
                except (DeLonghiApiError, DeLonghiAuthError):
                    pass

                self._assumed_on = False
                self._last_commanded_on = False
                self._monitor_stale_count = 0
            except (DeLonghiApiError, DeLonghiAuthError) as err:
                raise HomeAssistantError(f"Failed to power off: {err}") from err
            self.async_write_ha_state()
            # Reactive refresh: confirm the Off state quickly.
            self.coordinator.request_fast_poll(duration_s=60.0, interval_s=10.0)
            self.hass.async_create_task(self.coordinator.async_request_refresh())

    async def async_will_remove_from_hass(self) -> None:
        """Cancel the pending power-on retry task, if any, before teardown."""
        if self._retry_task is not None and not self._retry_task.done():
            self._retry_task.cancel()
            # Retry body already swallows API errors; swallow cancel too.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._retry_task
        self._retry_task = None
        await super().async_will_remove_from_hass()
