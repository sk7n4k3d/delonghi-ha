"""Issue #47 — fast power-on confirmation ladder.

The bug: ``async_turn_on`` flipped the switch to ON the moment the cloud
accepted POWER_ON, and the only re-send happened blindly three minutes later.
A machine that ignored the command therefore sat "on" in HA forever (reporter:
switch on at 13:20:34, machine stone cold, retry landed at 13:21:44).

These tests pin the replacement contract:

* ``async_turn_on`` records an evidence baseline and does **not** claim success
  (except on models with no confirmable witness at all, which keep the legacy
  assumed-on answer);
* a background ladder watches ``coordinator.data`` for a fresh monitor frame —
  the embedded clock at/after the wake ping combined with a not-off state, the
  raw-change witness was removed because the timestamp lives inside the raw and
  it proved nothing on its own — and re-sends POWER_ON once per
  ``POWER_CONFIRM_WINDOWS`` entry (sends at t=0/20/45/75 s, covering the 70 s
  latency the reporter measured as *successful*);
* on a model whose cloud monitor is known to be pushed by the machine (striker
  family) a mute machine leaves the switch OFF and raises a persistent
  notification — which is taken down again as soon as the machine wakes; on a
  frozen-monitor model (issue #50) with no live LAN session the ladder is not
  even armed: the legacy assumed-on answer + single 180 s retry is kept, because
  silence proves nothing there;
* the legacy 180 s full-sequence safety net still runs when nothing answered.

Harness is the one from ``tests/test_switch.py``: ``asyncio.sleep`` is patched
to a no-op, which is exactly why the ladder loops a bounded number of times
instead of watching a wall-clock deadline.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.delonghi_coffee.api import DeLonghiApiError
from custom_components.delonghi_coffee.const import (
    POWER_CONFIRM_FAST_POLL_DURATION,
    POWER_CONFIRM_FAST_POLL_INTERVAL,
    POWER_CONFIRM_POLL,
    POWER_CONFIRM_WINDOWS,
    POWER_ON_RETRIES,
    POWER_RETRY_DELAY,
)
from custom_components.delonghi_coffee.switch import DeLonghiPowerSwitch, _state_is_not_on

# Monitor raw frames used as baseline / "machine answered" witnesses. Only the
# bytes matter for comparison; the switch never decodes them.
BASE_RAW = "d00ca4f0010200000000000000000000"  # state byte 0 → Off
TURNING_ON_RAW = "d00ca4f0010200000001000000000000"  # state byte 1 → Turning On

# Number of 1 s reads the ladder performs per send window (mirrors the
# implementation), and the total across the whole ladder.
CHECKS = [int(w / POWER_CONFIRM_POLL) for w in POWER_CONFIRM_WINDOWS]
TOTAL_CHECKS = sum(CHECKS)


def _make_switch(cmd_property: str = "app_data_request", lan_alive: bool = False) -> DeLonghiPowerSwitch:
    """Switch stubbed the way ``tests/test_switch.py`` does.

    ``api._cmd_property`` is a real string here (not a MagicMock) because the
    ladder's failure branch discriminates on it: ``app_data_request`` is the
    striker family (healthy cloud monitor, issue #47 reporter), ``data_request``
    is PrimaDonna Soul / millcore (frozen cloud monitor, issue #50).

    ``lan_alive`` stubs ``coordinator.lan_session_alive`` — the second witness
    the switch checks before arming the ladder (a frozen cloud monitor is
    bearable while the LAN socket is still pushing frames).
    """
    api = MagicMock()
    api._cmd_property = cmd_property
    # ``cloud_monitor_reliable`` is a property on the real api; on a MagicMock
    # it would be an auto-attribute (always truthy) and silently route every
    # model down the striker branch, so it is stubbed from the same string.
    api.cloud_monitor_reliable = cmd_property == "app_data_request"
    coordinator = MagicMock()
    coordinator.data = {"machine_state": "Off", "monitor_raw": BASE_RAW}
    coordinator.async_request_refresh = AsyncMock()
    coordinator.request_fast_poll = MagicMock()
    coordinator.send_command_lan = AsyncMock(return_value=False)
    coordinator.lan_session_alive = MagicMock(return_value=lan_alive)
    sw = DeLonghiPowerSwitch(
        api=api,
        coordinator=coordinator,
        dsn="DSN-TEST",
        model="DL-striker-cb",
        device_name="Test Machine",
        sw_version="1.0",
    )
    sw.hass = _make_hass()
    sw.async_write_ha_state = MagicMock()
    sw._api.ping_connected = MagicMock(return_value=True)
    sw._api.send_command = MagicMock()
    sw._api.request_monitor = MagicMock()
    return sw


def _make_hass():
    """Hass with awaitable executor + sync task scheduler (as in test_switch)."""
    hass = MagicMock()

    async def _run_executor(func, *args, **kwargs):
        return func(*args, **kwargs)

    def _create_task(coro, *args, **kwargs):
        loop = asyncio.get_event_loop()
        return loop.create_task(coro)

    hass.async_add_executor_job = _run_executor
    hass.async_create_task = _create_task
    hass.async_create_background_task = _create_task
    return hass


async def _noop_sleep(*_args, **_kwargs):
    return None


@contextlib.contextmanager
def _no_delays():
    """Patch away every wait: the ladder's sleeps and turn_on's 15 s wake delay."""
    with (
        patch("custom_components.delonghi_coffee.switch._WAKE_DELAY", 0.01),
        patch("custom_components.delonghi_coffee.switch.asyncio.sleep", new=_noop_sleep),
    ):
        yield


def _sleep_that_fires(after_calls: int, action) -> object:
    """No-op sleep that runs ``action`` once, on the ``after_calls``-th tick."""
    state = {"n": 0}

    async def _sleep(*_args, **_kwargs):
        state["n"] += 1
        if state["n"] == after_calls:
            action()

    return _sleep


def _arm(sw: DeLonghiPowerSwitch, data: dict) -> None:
    """Put ``sw`` in the state ``async_turn_on`` leaves behind, minus the send."""
    sw._last_commanded_on = True
    sw._monitor_stale_count = 0
    sw._power_baseline_raw = BASE_RAW
    sw._power_ping_wall = time.time()
    sw.coordinator.data = data


async def _run_ladder(sw: DeLonghiPowerSwitch, sleep=_noop_sleep) -> None:
    with patch("custom_components.delonghi_coffee.switch.asyncio.sleep", new=sleep):
        await sw._confirm_power_on()


async def _run_turn_on_and_ladder(sw: DeLonghiPowerSwitch) -> None:
    """Full turn_on, then await the background task it armed."""
    with _no_delays():
        await sw.async_turn_on()
        await sw._retry_task


def _run(coro) -> None:
    asyncio.run(coro)


def _persistent_calls(hass) -> list[tuple[str, dict]]:
    """``(service, params)`` of every persistent_notification call made."""
    out: list[tuple[str, dict]] = []
    for call in hass.services.async_call.mock_calls:
        args = call.args
        if args and args[0] == "persistent_notification" and len(args) >= 3:
            out.append((args[1], args[2]))
    return out


# ── the not-on predicate (review finding 1) ─────────────────────────────


def test_state_is_not_on_covers_real_renderings():
    """``Unknown (N)`` (the real api rendering of an unmapped byte) and
    ``Error`` are not evidence of an awake machine; a bare exact-match tuple
    silently treated ``Unknown (29)`` as "on"."""
    assert _state_is_not_on("Off")
    assert _state_is_not_on("Going to sleep")
    assert _state_is_not_on("Error")
    assert _state_is_not_on("Unknown")
    assert _state_is_not_on("Unknown (29)")
    assert not _state_is_not_on("Ready")
    assert not _state_is_not_on("Turning On")
    assert not _state_is_not_on("Idle")
    assert not _state_is_not_on("Brewing")


# ── turn_on: honest switch, ladder armed ────────────────────────────────


def test_turn_on_defers_assumed_on():
    """turn_on must not claim the machine is on (the 13:20:34 lie)."""
    sw = _make_switch()

    async def _go():
        with _no_delays():
            await sw.async_turn_on()
        assert sw._retry_task is not None
        sw._retry_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await sw._retry_task

    _run(_go())
    sw._api.send_command.assert_called_once()
    assert sw._last_commanded_on is True
    assert sw._assumed_on is False
    assert sw._retry_task is not None

    # Three coordinator ticks with the monitor still saying Off: the staleness
    # machine must lock the switch OFF instead of trusting an assumption.
    for _ in range(3):
        sw.coordinator.data = {"machine_state": "Off", "monitor_raw": BASE_RAW}
        sw._handle_coordinator_update()
    assert sw.is_on is False


def test_turn_on_already_on_confirms_immediately_without_resending():
    """Already-on is a shortcut to the display, not a skip of the net: the
    ladder is armed, sees the fresh frame on its first tick and confirms with
    zero re-sends (review finding 1)."""
    sw = _make_switch()
    sw.coordinator.data = {
        "machine_state": "Ready",
        "monitor_raw": BASE_RAW,
        "monitor_ts": int(time.time()),
    }

    _run(_run_turn_on_and_ladder(sw))
    assert sw._assumed_on is True
    # Only turn_on's own POWER_ON went out — the ladder never re-sent.
    sw._api.send_command.assert_called_once()
    sw.coordinator.request_fast_poll.assert_called_with(duration_s=240.0, interval_s=10.0)


def test_turn_on_already_on_stale_frame_does_not_resend():
    """Re-review finding 2: an already-on shortcut resting on a stale frame
    (idle machine, old timestamp) must NOT spam POWER_ON — HEAD sent zero
    retries here. Only a *fresh* off frame proves the shortcut wrong."""
    sw = _make_switch()
    sw.coordinator.data = {
        "machine_state": "Ready",
        "monitor_raw": BASE_RAW,
        "monitor_ts": int(time.time()) - 10_000,  # hours old, no fresh Off
    }

    _run(_run_turn_on_and_ladder(sw))
    # turn_on's own send only — no ladder re-sends without fresh off-evidence.
    sw._api.send_command.assert_called_once()
    # Last known state trusted (HEAD parity), no false-alarm banner.
    assert sw._assumed_on is True
    assert not [svc for svc, _ in _persistent_calls(sw.hass) if svc == "create"]


def test_fresh_off_evidence_helper():
    """The already-on re-send gate: only a fresh Off/Going to sleep frame
    counts as evidence the shortcut was wrong."""
    sw = _make_switch()
    now = int(time.time())
    floor = now - 120

    sw.coordinator.data = {"machine_state": "Off", "monitor_raw": "x", "monitor_ts": now}
    assert sw._fresh_off_evidence(floor) is True
    sw.coordinator.data = {"machine_state": "Going to sleep", "monitor_raw": "x", "monitor_ts": now}
    assert sw._fresh_off_evidence(floor) is True
    # stale Off: the machine may have slept long ago, proves nothing now
    sw.coordinator.data = {"machine_state": "Off", "monitor_raw": "x", "monitor_ts": now - 10_000}
    assert sw._fresh_off_evidence(floor) is False
    # on-ish states are not off-evidence
    sw.coordinator.data = {"machine_state": "Ready", "monitor_raw": "x", "monitor_ts": now}
    assert sw._fresh_off_evidence(floor) is False
    # no timestamp at all
    sw.coordinator.data = {"machine_state": "Off", "monitor_raw": "x"}
    assert sw._fresh_off_evidence(floor) is False


def test_error_or_unknown_refresh_does_not_disarm_ladder():
    """Re-review finding 1: a coordinator refresh carrying ``Error`` or
    ``Unknown (N)`` must not count as monitor agreement — the old exact-tuple
    predicate cleared ``_last_commanded_on`` on such a frame and silently
    disarmed the ladder underneath its own feet."""
    sw = _make_switch()
    sw._last_commanded_on = True
    sw._monitor_stale_count = 0
    for state in ("Error", "Unknown (29)"):
        sw.coordinator.data = {"machine_state": state, "monitor_raw": "some-raw"}
        sw._handle_coordinator_update()
        assert sw._last_commanded_on is True, f"{state} must not confirm the command"


def test_turn_on_unknown_parenthesized_is_not_already_on():
    """``Unknown (29)`` must take the full ladder path, not the shortcut."""
    sw = _make_switch()
    sw.coordinator.data = {"machine_state": "Unknown (29)", "monitor_raw": BASE_RAW}

    async def _go():
        with _no_delays():
            await sw.async_turn_on()
        assert sw._retry_task is not None
        sw._retry_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await sw._retry_task

    _run(_go())
    assert sw._assumed_on is False
    sw.coordinator.request_fast_poll.assert_called_with(
        duration_s=POWER_CONFIRM_FAST_POLL_DURATION,
        interval_s=POWER_CONFIRM_FAST_POLL_INTERVAL,
    )


def test_turn_on_arms_short_fast_poll_while_ladder_runs():
    """While unconfirmed, polling is finer (5s) and covers the whole ladder."""
    sw = _make_switch()

    async def _go():
        with _no_delays():
            await sw.async_turn_on()
        sw._retry_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await sw._retry_task

    _run(_go())
    sw.coordinator.request_fast_poll.assert_called_with(
        duration_s=POWER_CONFIRM_FAST_POLL_DURATION,
        interval_s=POWER_CONFIRM_FAST_POLL_INTERVAL,
    )


def test_turn_on_short_circuits_unreliable_cloud_without_lan():
    """Frozen cloud monitor + no LAN session: the ladder could never confirm,
    so the HEAD contract is kept — assumed state, one blind retry at 180 s,
    no notification, no ladder re-sends (review finding 4)."""
    sw = _make_switch(cmd_property="data_request", lan_alive=False)

    _run(_run_turn_on_and_ladder(sw))
    assert sw._assumed_on is True
    # turn_on's send + the single legacy retry — 2 sends, like HEAD, not 5.
    assert sw._api.send_command.call_count == 2
    assert [svc for svc, _ in _persistent_calls(sw.hass)] == []
    sw.coordinator.request_fast_poll.assert_called_with(duration_s=240.0, interval_s=10.0)


def test_turn_on_frozen_model_with_lan_runs_ladder():
    """A live LAN session is a witness too: even a frozen-cloud model gets the
    ladder (its frames arrive over the socket)."""
    sw = _make_switch(cmd_property="data_request", lan_alive=True)

    async def _go():
        with _no_delays():
            await sw.async_turn_on()
        sw._retry_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await sw._retry_task

    _run(_go())
    sw.coordinator.request_fast_poll.assert_called_with(
        duration_s=POWER_CONFIRM_FAST_POLL_DURATION,
        interval_s=POWER_CONFIRM_FAST_POLL_INTERVAL,
    )


# ── the ladder: confirmation evidence ───────────────────────────────────


def test_ladder_confirms_via_timestamp():
    """A frame stamped after the wake ping, reporting a machine that is coming
    up, is proof the machine answered."""
    sw = _make_switch()
    _arm(
        sw,
        {"machine_state": "Turning On", "monitor_raw": BASE_RAW, "monitor_ts": int(time.time())},
    )

    _run(_run_ladder(sw))
    assert sw._assumed_on is True
    # Nothing was re-sent: the first command answered.
    sw._api.send_command.assert_not_called()
    sw.coordinator.send_command_lan.assert_not_awaited()
    sw.coordinator.request_fast_poll.assert_called_with(duration_s=240.0, interval_s=10.0)
    # The "ignored" banner is taken down on the way.
    assert [svc for svc, _ in _persistent_calls(sw.hass)] == ["dismiss"]


def test_ladder_ignores_raw_change_without_timestamp():
    """The raw-change witness is gone (review finding 5): the timestamp lives
    *inside* ``monitor_raw``, so every live machine's raw always differs from
    the baseline — it proved nothing. Without a clock, a frame is not an
    answer, even if its bytes moved."""
    sw = _make_switch()
    _arm(
        sw,
        {"machine_state": "Turning On", "monitor_raw": TURNING_ON_RAW, "monitor_ts": None},
    )

    _run(_run_ladder(sw))
    assert sw._assumed_on is False  # striker: silence = ignored
    # One re-send per window; the safety net skips because the (unconfirmable)
    # frame still claims Turning On, which is not an off-ish state.
    assert sw._api.send_command.call_count == POWER_ON_RETRIES


def test_lan_push_confirms():
    """A frame the machine pushed on its LAN socket carries its own clock and
    is the fastest witness there is."""
    sw = _make_switch()
    _arm(
        sw,
        {
            "machine_state": "Turning On",
            "monitor_raw": TURNING_ON_RAW,
            "monitor_ts": int(time.time()),
            "monitor_source": "lan",
        },
    )

    _run(_run_ladder(sw))
    assert sw._assumed_on is True
    sw._api.send_command.assert_not_called()


def test_ladder_ignores_fresh_frame_that_still_says_off():
    """The wake ping provokes a monitor push of its own.

    A frame stamped after the ping but still reporting Off proves the machine
    is talking, not that it obeyed — treating it as an answer would put the
    #47 lie straight back (the switch would claim ON while the machine is
    cold, and no retry would ever be sent).
    """
    sw = _make_switch()
    _arm(sw, {"machine_state": "Off", "monitor_raw": BASE_RAW, "monitor_ts": int(time.time())})

    _run(_run_ladder(sw))
    assert sw._assumed_on is False
    assert sw._api.send_command.call_count == POWER_ON_RETRIES + 1


def test_ladder_ignores_fresh_frame_with_unknown_parenthesized_state():
    """``Unknown (29)`` with a fresh stamp is a live machine in an unmapped
    state — not proof that the POWER_ON landed."""
    sw = _make_switch()
    _arm(
        sw,
        {"machine_state": "Unknown (29)", "monitor_raw": TURNING_ON_RAW, "monitor_ts": int(time.time())},
    )

    _run(_run_ladder(sw))
    assert sw._assumed_on is False
    assert sw._api.send_command.call_count == 1 + POWER_ON_RETRIES


def test_ladder_ignores_raw_change_to_an_off_state():
    """A frame that moved but still says Off is not an answer (e.g. a reboot push)."""
    sw = _make_switch()
    other_off_raw = "d00ca4f0020200000000000000000000"
    _arm(sw, {"machine_state": "Off", "monitor_raw": other_off_raw, "monitor_ts": None})
    sw._power_baseline_raw = BASE_RAW

    _run(_run_ladder(sw))
    assert sw._assumed_on is False  # striker: stays off, no false confirmation
    assert sw._api.send_command.call_count == 1 + POWER_ON_RETRIES


def test_ladder_ignores_stale_timestamp():
    """A frame stamped before the wake ping (minus skew) is the frozen cloud cache."""
    sw = _make_switch()
    _arm(
        sw,
        {"machine_state": "Off", "monitor_raw": BASE_RAW, "monitor_ts": int(time.time()) - 10_000},
    )

    _run(_run_ladder(sw))
    assert sw._assumed_on is False
    assert sw._api.send_command.call_count == 1 + POWER_ON_RETRIES


# ── the ladder: re-sends ────────────────────────────────────────────────


def test_ladder_budget_covers_the_measured_latency():
    """The reporter's successful answer took 70 s (13:20:34 → 13:21:44). The
    last ladder send must therefore leave at ≥70 s, and the ladder must still
    finish before the 180 s safety net (review finding 2)."""
    last_send = sum(POWER_CONFIRM_WINDOWS[:-1])
    assert last_send >= 70
    assert sum(POWER_CONFIRM_WINDOWS) <= POWER_RETRY_DELAY


def test_ladder_resends_power_on_once_per_window():
    """A mute machine gets POWER_ON again, LAN first, cloud as fallback."""
    sw = _make_switch()
    _arm(sw, {"machine_state": "Off", "monitor_raw": BASE_RAW, "monitor_ts": None})

    _run(_run_ladder(sw))
    # POWER_ON_RETRIES ladder re-sends + the 180 s safety net (the first
    # POWER_ON itself was sent by turn_on, which this test does not run), all
    # through the cloud because the LAN session is down in this harness.
    assert sw._api.send_command.call_count == POWER_ON_RETRIES + 1
    assert sw.coordinator.send_command_lan.await_count == POWER_ON_RETRIES + 1
    # One keepalive ping after each ladder re-send, plus the pre/post pair of
    # the safety net.
    assert sw._api.ping_connected.call_count == POWER_ON_RETRIES + 2


def test_ladder_stops_when_retry_succeeds():
    """The first re-send that is answered ends the ladder — no further sends."""
    sw = _make_switch()
    _arm(sw, {"machine_state": "Off", "monitor_raw": BASE_RAW, "monitor_ts": None})

    def _answer():
        sw.coordinator.data = {
            "machine_state": "Turning On",
            "monitor_raw": TURNING_ON_RAW,
            "monitor_ts": int(time.time()),
        }

    # First window burns CHECKS[0] ticks; the tick right after it is the first
    # tick of the window that follows re-send #1.
    sleep = _sleep_that_fires(CHECKS[0] + 1, _answer)
    _run(_run_ladder(sw, sleep))
    assert sw._assumed_on is True
    assert sw._api.send_command.call_count == 1


def test_ladder_aborts_on_turn_off():
    """A turn_off mid-window must stop every further re-send."""
    sw = _make_switch()
    _arm(sw, {"machine_state": "Off", "monitor_raw": BASE_RAW, "monitor_ts": None})

    def _user_turned_off():
        sw._last_commanded_on = False

    sleep = _sleep_that_fires(3, _user_turned_off)
    _run(_run_ladder(sw, sleep))
    sw._api.send_command.assert_not_called()
    sw.coordinator.send_command_lan.assert_not_awaited()
    assert sw._assumed_on is False


def test_ladder_aborts_when_turn_off_wins_the_lock():
    """Review finding 7: a turn_off that queues behind an in-flight ladder
    send must not be followed by a delayed POWER_ON. The ladder re-checks the
    desired state *under* ``_cmd_lock`` before every send — the same lock
    ``async_turn_off`` holds while it flips ``_last_commanded_on``."""
    sw = _make_switch()
    _arm(sw, {"machine_state": "Off", "monitor_raw": BASE_RAW, "monitor_ts": None})

    async def _go():
        with patch("custom_components.delonghi_coffee.switch.asyncio.sleep", new=_noop_sleep):
            # Pretend turn_off holds the lock mid-POWER_OFF.
            await sw._cmd_lock.acquire()
            task = asyncio.get_event_loop().create_task(sw._confirm_power_on())
            for _ in range(5):
                await asyncio.sleep(0)  # ladder burns window 0, blocks on the lock
            assert not task.done()
            # turn_off commits under the lock, then releases.
            sw._last_commanded_on = False
            sw._cmd_lock.release()
            await task

    _run(_go())
    sw._api.send_command.assert_not_called()
    sw.coordinator.send_command_lan.assert_not_awaited()
    assert sw._assumed_on is False


def test_ladder_treats_reconcile_confirmation_as_done():
    """If the staleness machine already saw the monitor agree, the ladder is moot."""
    sw = _make_switch()
    _arm(sw, {"machine_state": "Off", "monitor_raw": BASE_RAW, "monitor_ts": None})
    sw._last_commanded_on = None  # what _reconcile_monitor_state does on confirmation

    _run(_run_ladder(sw))
    sw._api.send_command.assert_not_called()
    assert sw._assumed_on is False


def test_ladder_swallows_send_failure_and_keeps_trying(caplog):
    """A rejected re-send is logged, not raised, and the ladder continues."""
    sw = _make_switch()
    _arm(sw, {"machine_state": "Off", "monitor_raw": BASE_RAW, "monitor_ts": None})
    sw._api.send_command = MagicMock(side_effect=DeLonghiApiError("cloud 500"))

    with caplog.at_level(logging.INFO):
        _run(_run_ladder(sw))
    assert "Power ON retry failed" in caplog.text
    assert sw._api.send_command.call_count == 1 + POWER_ON_RETRIES


# ── the ladder: what failure means, per model family ────────────────────


def test_ladder_striker_failure_notifies_and_stays_off(caplog):
    """Striker family (monitor pushed by the machine): silence = ignored = say so."""
    sw = _make_switch(cmd_property="app_data_request")
    _arm(sw, {"machine_state": "Off", "monitor_raw": BASE_RAW, "monitor_ts": None})

    with caplog.at_level(logging.WARNING):
        _run(_run_ladder(sw))

    assert sw._assumed_on is False
    assert "ignored by the machine" in caplog.text
    creates = [(svc, params) for svc, params in _persistent_calls(sw.hass) if svc == "create"]
    assert len(creates) == 1
    params = creates[0][1]
    assert params["notification_id"] == "delonghi_power_no_response_DSN-TEST"
    assert params["title"]


def test_ladder_frozen_model_assumes_on():
    """Frozen cloud monitor (issue #50): silence proves nothing, keep legacy answer."""
    sw = _make_switch(cmd_property="data_request")
    _arm(sw, {"machine_state": "Off", "monitor_raw": BASE_RAW, "monitor_ts": None})

    _run(_run_ladder(sw))
    assert sw._assumed_on is True
    assert [svc for svc, _ in _persistent_calls(sw.hass)] == []


def test_ladder_unknown_model_assumes_on():
    """Model not identified yet (``_cmd_property`` None) → no accusation, legacy answer."""
    sw = _make_switch(cmd_property="")
    _arm(sw, {"machine_state": "Off", "monitor_raw": BASE_RAW, "monitor_ts": None})

    _run(_run_ladder(sw))
    assert sw._assumed_on is True
    assert [svc for svc, _ in _persistent_calls(sw.hass)] == []


def test_ladder_no_monitor_ever_seen_falls_back_legacy():
    """No monitor frame ever observed → no witness at all → legacy, and stay quiet."""
    sw = _make_switch(cmd_property="app_data_request")
    sw._last_commanded_on = True
    sw._power_baseline_raw = None
    sw._power_ping_wall = time.time()
    sw.coordinator.data = {"machine_state": "Unknown"}

    _run(_run_ladder(sw))
    assert sw._assumed_on is True
    assert [svc for svc, _ in _persistent_calls(sw.hass)] == []


# ── the 180 s safety net ────────────────────────────────────────────────


def test_late_safety_retry_at_180s():
    """Full turn_on with a mute striker: 1 + POWER_ON_RETRIES ladder sends,
    then the legacy net."""
    sw = _make_switch()

    _run(_run_turn_on_and_ladder(sw))
    assert sw._api.send_command.call_count == 2 + POWER_ON_RETRIES
    assert sw._assumed_on is False


def test_no_safety_retry_when_confirmed():
    """A confirmed power-on never reaches the safety net."""
    sw = _make_switch()
    stamp = int(time.time())

    def _answer():
        # The machine answers the very first command: Turning On, freshly stamped.
        sw.coordinator.data = {
            "machine_state": "Turning On",
            "monitor_raw": BASE_RAW,
            "monitor_ts": stamp,
        }

    sleep = _sleep_that_fires(1, _answer)

    async def _go():
        with (
            patch("custom_components.delonghi_coffee.switch._WAKE_DELAY", 0.01),
            patch("custom_components.delonghi_coffee.switch.asyncio.sleep", new=sleep),
        ):
            await sw.async_turn_on()
            await sw._retry_task

    _run(_go())
    assert sw._api.send_command.call_count == 1
    assert sw._assumed_on is True


def test_safety_net_skipped_when_user_turned_off():
    """The net must not re-illuminate a machine the user switched off meanwhile."""
    sw = _make_switch()
    _arm(sw, {"machine_state": "Off", "monitor_raw": BASE_RAW, "monitor_ts": None})

    def _turn_off():
        # Fires on the safety net's own sleep, i.e. after the ladder failed.
        sw._last_commanded_on = False

    sleep = _sleep_that_fires(TOTAL_CHECKS + 1, _turn_off)
    _run(_run_ladder(sw, sleep))
    # The ladder re-sends happened, the safety net did not add one more.
    assert sw._api.send_command.call_count == POWER_ON_RETRIES


# ── notifications lifecycle ─────────────────────────────────────────────


def test_turn_off_dismisses_no_response_notification():
    sw = _make_switch()

    async def _go():
        await sw.async_turn_off()

    _run(_go())
    assert [svc for svc, _ in _persistent_calls(sw.hass)] == ["dismiss"]
    assert _persistent_calls(sw.hass)[0][1]["notification_id"] == "delonghi_power_no_response_DSN-TEST"


def test_late_wakeup_dismisses_no_response_notification():
    """Review finding 3: if the machine wakes *after* the ladder gave up (late
    answer, physical button, next poll), the banner must not stay up — the
    reconcile pass takes it down as soon as the state is not off-ish."""
    sw = _make_switch()

    async def _go():
        await sw._notify_no_response()
        await asyncio.sleep(0)  # let the create task run
        assert sw._no_response_notified is True
        # The machine woke up on its own; a coordinator refresh sees it.
        sw.coordinator.data = {"machine_state": "Ready", "monitor_raw": TURNING_ON_RAW}
        sw._handle_coordinator_update()
        await asyncio.sleep(0)  # let the dismiss task run

    _run(_go())
    svcs = [svc for svc, _ in _persistent_calls(sw.hass)]
    assert "create" in svcs
    assert "dismiss" in svcs
    assert sw._no_response_notified is False
