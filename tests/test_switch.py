"""Test DeLonghiPowerSwitch — retry task lifecycle + power flow + state inference."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.delonghi_coffee import switch as switch_mod  # noqa: E402
from custom_components.delonghi_coffee.api import DeLonghiApiError  # noqa: E402
from custom_components.delonghi_coffee.switch import DeLonghiPowerSwitch  # noqa: E402


def _make_switch() -> DeLonghiPowerSwitch:
    """Construct a switch with stubbed HA internals."""
    api = MagicMock()
    coordinator = MagicMock()
    coordinator.data = {"machine_state": "Off"}
    # LAN path unavailable by default — switch falls back to cloud send_command,
    # which is what the existing tests assert on.
    coordinator.send_command_lan = AsyncMock(return_value=False)
    return DeLonghiPowerSwitch(
        api=api,
        coordinator=coordinator,
        dsn="DSN-TEST",
        model="DL-striker-cb",
        device_name="Test Machine",
        sw_version="1.0",
    )


class TestRetryTaskLifecycle:
    """Regression tests for F6 — orphan task cleanup on entity removal."""

    def test_retry_task_field_initialised(self):
        """__init__ exposes a _retry_task slot defaulting to None."""
        sw = _make_switch()
        assert sw._retry_task is None

    def test_retry_task_cancelled_on_removal(self):
        """async_will_remove_from_hass cancels the pending retry task."""

        async def _scenario() -> None:
            sw = _make_switch()

            async def _never() -> None:
                await asyncio.sleep(3600)

            sw._retry_task = asyncio.create_task(_never())
            # Give the event loop a tick to schedule the task.
            await asyncio.sleep(0)
            assert not sw._retry_task.done()

            await sw.async_will_remove_from_hass()

            # Slot cleared after teardown.
            assert sw._retry_task is None

        asyncio.run(_scenario())

    def test_removal_is_noop_when_no_task(self):
        """Removing an entity that never launched a retry is safe."""

        async def _scenario() -> None:
            sw = _make_switch()
            await sw.async_will_remove_from_hass()
            assert sw._retry_task is None

        asyncio.run(_scenario())

    def test_removal_ignores_already_completed_task(self):
        """A retry task that finished normally is cleaned up without error."""

        async def _scenario() -> None:
            sw = _make_switch()

            async def _done() -> None:
                return

            sw._retry_task = asyncio.create_task(_done())
            await sw._retry_task  # wait for completion
            await sw.async_will_remove_from_hass()
            assert sw._retry_task is None

        asyncio.run(_scenario())


def _make_hass():
    """Hass with awaitable executor + sync task scheduler."""
    hass = MagicMock()

    async def _run_executor(func, *args, **kwargs):
        return func(*args, **kwargs)

    hass.async_add_executor_job = _run_executor

    def _create_task(coro):
        loop = asyncio.get_event_loop()
        return loop.create_task(coro)

    hass.async_create_task = _create_task
    return hass


class TestAssumedAndIsOn:
    def test_assumed_state_always_true(self):
        sw = _make_switch()
        assert sw.assumed_state is True

    def test_is_on_unknown_state_uses_assumed(self):
        sw = _make_switch()
        sw.coordinator.data = {"machine_state": "Unknown"}
        sw._assumed_on = True
        assert sw.is_on is True
        sw._assumed_on = False
        assert sw.is_on is False

    def test_is_on_off_state_returns_false(self):
        sw = _make_switch()
        sw.coordinator.data = {"machine_state": "Off"}
        assert sw.is_on is False

    def test_is_on_going_to_sleep_returns_false(self):
        sw = _make_switch()
        sw.coordinator.data = {"machine_state": "Going to sleep"}
        assert sw.is_on is False

    def test_is_on_brewing_returns_true(self):
        sw = _make_switch()
        sw.coordinator.data = {"machine_state": "Brewing"}
        assert sw.is_on is True

    def test_is_on_ready_returns_true(self):
        sw = _make_switch()
        sw.coordinator.data = {"machine_state": "Ready"}
        assert sw.is_on is True

    def test_monitor_confirms_command_clears_assumed(self):
        sw = _make_switch()
        sw._last_commanded_on = True
        sw.coordinator.data = {"machine_state": "Ready"}
        assert sw.is_on is True
        # Once the monitor confirms, _last_commanded_on is cleared
        assert sw._last_commanded_on is None
        assert sw._monitor_stale_count == 0

    def test_stale_monitor_falls_back_to_assumed(self):
        """3+ consecutive contradictions from monitor → trust assumed state."""
        sw = _make_switch()
        sw._assumed_on = True
        sw._last_commanded_on = True
        # Monitor keeps saying Off — contradicts our command
        sw.coordinator.data = {"machine_state": "Off"}
        # Tick 1: contradiction starts
        sw.is_on
        assert sw._monitor_stale_count == 1
        # Tick 2
        sw.is_on
        assert sw._monitor_stale_count == 2
        # Tick 3: stale threshold reached, assumed state takes over
        result = sw.is_on
        assert sw._monitor_stale_count == 3
        assert result is True  # assumed_on=True wins

    def test_monitor_state_change_resets_stale_count(self):
        """If monitor state changes, the stale counter resets."""
        sw = _make_switch()
        sw._last_commanded_on = True
        sw.coordinator.data = {"machine_state": "Off"}
        sw.is_on  # count=1
        sw.coordinator.data = {"machine_state": "Going to sleep"}
        sw.is_on  # state changed, count resets to 1
        assert sw._monitor_stale_count == 1

    def test_no_command_pending_assumed_tracks_monitor(self):
        sw = _make_switch()
        sw._last_commanded_on = None
        sw.coordinator.data = {"machine_state": "Ready"}
        assert sw.is_on is True
        assert sw._assumed_on is True


class TestAsyncTurnOnFlow:
    """Power on flow with sleep patched to no-op."""

    def test_full_sequence_on_success(self):
        sw = _make_switch()
        hass = _make_hass()
        sw.hass = hass
        sw.async_write_ha_state = MagicMock()
        sw._api.ping_connected = MagicMock(return_value=True)
        sw._api.send_command = MagicMock()

        async def _go():
            with patch("custom_components.delonghi_coffee.switch.asyncio.sleep", new=_noop_sleep):
                await sw.async_turn_on()
            # Cancel the bg retry to avoid leak
            if sw._retry_task is not None:
                sw._retry_task.cancel()
                try:
                    await sw._retry_task
                except (asyncio.CancelledError, Exception):
                    pass

        asyncio.run(_go())
        sw._api.send_command.assert_called_once()
        assert sw._assumed_on is True
        assert sw._last_commanded_on is True

    def test_concurrent_call_skipped_when_locked(self):
        sw = _make_switch()
        hass = _make_hass()
        sw.hass = hass
        sw.async_write_ha_state = MagicMock()
        sw._api.send_command = MagicMock()

        async def _go():
            await sw._cmd_lock.acquire()
            try:
                await sw.async_turn_on()
            finally:
                sw._cmd_lock.release()

        asyncio.run(_go())
        sw._api.send_command.assert_not_called()

    def test_send_command_failure_raises(self):
        from homeassistant.exceptions import HomeAssistantError

        sw = _make_switch()
        hass = _make_hass()
        sw.hass = hass
        sw.async_write_ha_state = MagicMock()
        sw._api.ping_connected = MagicMock(return_value=True)
        sw._api.send_command = MagicMock(side_effect=DeLonghiApiError("boom"))

        async def _go():
            with patch("custom_components.delonghi_coffee.switch.asyncio.sleep", new=_noop_sleep):
                await sw.async_turn_on()

        with pytest.raises(HomeAssistantError, match="Failed to power on"):
            asyncio.run(_go())

    def test_ping_failure_falls_back_to_request_monitor(self):
        sw = _make_switch()
        hass = _make_hass()
        sw.hass = hass
        sw.async_write_ha_state = MagicMock()
        sw._api.ping_connected = MagicMock(return_value=False)
        sw._api.request_monitor = MagicMock()
        sw._api.send_command = MagicMock()

        async def _go():
            with patch("custom_components.delonghi_coffee.switch.asyncio.sleep", new=_noop_sleep):
                await sw.async_turn_on()
            if sw._retry_task is not None:
                sw._retry_task.cancel()
                try:
                    await sw._retry_task
                except (asyncio.CancelledError, Exception):
                    pass

        asyncio.run(_go())
        # Wake phase + post-command phase both fall back to request_monitor
        assert sw._api.request_monitor.call_count >= 1
        sw._api.send_command.assert_called_once()


class TestAsyncTurnOffFlow:
    def test_off_command_succeeds(self):
        sw = _make_switch()
        hass = _make_hass()
        sw.hass = hass
        sw.async_write_ha_state = MagicMock()
        sw._api.ping_connected = MagicMock(return_value=True)
        sw._api.send_command = MagicMock()

        asyncio.run(sw.async_turn_off())

        sw._api.send_command.assert_called_once()
        assert sw._assumed_on is False
        assert sw._last_commanded_on is False

    def test_off_concurrent_call_skipped(self):
        sw = _make_switch()
        hass = _make_hass()
        sw.hass = hass
        sw.async_write_ha_state = MagicMock()
        sw._api.send_command = MagicMock()

        async def _go():
            await sw._cmd_lock.acquire()
            try:
                await sw.async_turn_off()
            finally:
                sw._cmd_lock.release()

        asyncio.run(_go())
        sw._api.send_command.assert_not_called()

    def test_off_send_command_failure_raises(self):
        from homeassistant.exceptions import HomeAssistantError

        sw = _make_switch()
        hass = _make_hass()
        sw.hass = hass
        sw.async_write_ha_state = MagicMock()
        sw._api.send_command = MagicMock(side_effect=DeLonghiApiError("boom"))

        with pytest.raises(HomeAssistantError, match="Failed to power off"):
            asyncio.run(sw.async_turn_off())


class TestAsyncSetupEntry:
    def test_adds_one_switch_entity(self):
        hass = MagicMock()
        entry = MagicMock()
        entry.entry_id = "eid"
        coord = MagicMock()
        coord.data = {"machine_state": "Off"}
        hass.data = {
            "delonghi_coffee": {
                entry.entry_id: {
                    "api": MagicMock(),
                    "coordinator": coord,
                    "dsn": "DSN",
                    "model": "ECAM",
                    "device_name": "Test",
                    "sw_version": "1.0",
                }
            }
        }
        added: list = []
        async_add = MagicMock(side_effect=lambda ents: added.extend(ents))
        asyncio.run(switch_mod.async_setup_entry(hass, entry, async_add))
        assert len(added) == 1
        assert isinstance(added[0], DeLonghiPowerSwitch)


async def _noop_sleep(*_args, **_kwargs):
    return None


class TestRetryPowerOn:
    """Cover _retry_power_on background task — both confirmed and retry paths."""

    def test_retry_skipped_when_machine_confirms_on(self):
        sw = _make_switch()
        hass = _make_hass()
        sw.hass = hass
        sw._api.ping_connected = MagicMock(return_value=True)
        sw._api.send_command = MagicMock()
        sw.coordinator.data = {"machine_state": "Ready"}

        async def _go():
            with patch("custom_components.delonghi_coffee.switch.asyncio.sleep", new=_noop_sleep):
                await sw._retry_power_on()

        asyncio.run(_go())
        # No retry sent because monitor confirmed ON
        sw._api.send_command.assert_not_called()

    def test_retry_sent_when_machine_still_off(self):
        sw = _make_switch()
        hass = _make_hass()
        sw.hass = hass
        sw._api.ping_connected = MagicMock(return_value=True)
        sw._api.send_command = MagicMock()
        sw.coordinator.data = {"machine_state": "Off"}

        async def _go():
            with patch("custom_components.delonghi_coffee.switch.asyncio.sleep", new=_noop_sleep):
                await sw._retry_power_on()

        asyncio.run(_go())
        sw._api.send_command.assert_called_once()
        # ping called twice (pre + post)
        assert sw._api.ping_connected.call_count == 2

    def test_retry_falls_back_to_request_monitor(self):
        sw = _make_switch()
        hass = _make_hass()
        sw.hass = hass
        sw._api.ping_connected = MagicMock(return_value=False)
        sw._api.request_monitor = MagicMock()
        sw._api.send_command = MagicMock()
        sw.coordinator.data = {"machine_state": "Going to sleep"}

        async def _go():
            with patch("custom_components.delonghi_coffee.switch.asyncio.sleep", new=_noop_sleep):
                await sw._retry_power_on()

        asyncio.run(_go())
        assert sw._api.request_monitor.call_count >= 1

    def test_retry_swallows_api_error(self):
        sw = _make_switch()
        hass = _make_hass()
        sw.hass = hass
        sw._api.ping_connected = MagicMock(side_effect=DeLonghiApiError("boom"))
        sw._api.send_command = MagicMock()
        sw.coordinator.data = {"machine_state": "Off"}

        async def _go():
            with patch("custom_components.delonghi_coffee.switch.asyncio.sleep", new=_noop_sleep):
                # Must NOT raise — error is logged + swallowed
                await sw._retry_power_on()

        asyncio.run(_go())


class TestPowerOnExceptionPaths:
    def test_wake_failure_continues_to_send_command(self):
        sw = _make_switch()
        hass = _make_hass()
        sw.hass = hass
        sw.async_write_ha_state = MagicMock()
        sw._api.ping_connected = MagicMock(side_effect=DeLonghiApiError("wake fail"))
        sw._api.send_command = MagicMock()

        async def _go():
            with patch("custom_components.delonghi_coffee.switch.asyncio.sleep", new=_noop_sleep):
                await sw.async_turn_on()
            if sw._retry_task is not None:
                sw._retry_task.cancel()
                try:
                    await sw._retry_task
                except (asyncio.CancelledError, Exception):
                    pass

        asyncio.run(_go())
        sw._api.send_command.assert_called_once()

    def test_post_command_ping_failure_is_swallowed(self):
        sw = _make_switch()
        hass = _make_hass()
        sw.hass = hass
        sw.async_write_ha_state = MagicMock()
        # Wake ping OK, but post-command ping fails
        sw._api.ping_connected = MagicMock(side_effect=[True, DeLonghiApiError("post")])
        sw._api.send_command = MagicMock()

        async def _go():
            with patch("custom_components.delonghi_coffee.switch.asyncio.sleep", new=_noop_sleep):
                await sw.async_turn_on()
            if sw._retry_task is not None:
                sw._retry_task.cancel()
                try:
                    await sw._retry_task
                except (asyncio.CancelledError, Exception):
                    pass

        asyncio.run(_go())
        # Still completes successfully — _assumed_on flips
        assert sw._assumed_on is True

    def test_existing_retry_task_cancelled_before_new_one(self):
        """Phase 4: a pending retry from a previous power-on is cancelled."""
        sw = _make_switch()
        hass = _make_hass()
        sw.hass = hass
        sw.async_write_ha_state = MagicMock()
        sw._api.ping_connected = MagicMock(return_value=True)
        sw._api.send_command = MagicMock()

        async def _go():
            # Pre-load a long-running retry task that should be cancelled
            async def _slow():
                await asyncio.sleep(3600)

            sw._retry_task = asyncio.create_task(_slow())
            await asyncio.sleep(0)  # let it schedule
            assert not sw._retry_task.done()

            old_task = sw._retry_task
            with patch("custom_components.delonghi_coffee.switch.asyncio.sleep", new=_noop_sleep):
                await sw.async_turn_on()

            # Yield so the cancellation finishes propagating before we check.
            try:
                await old_task
            except (asyncio.CancelledError, Exception):
                pass
            assert old_task.cancelled() or old_task.done()
            # New retry task installed and different from the old one
            assert sw._retry_task is not None
            assert sw._retry_task is not old_task
            # Cleanup
            sw._retry_task.cancel()
            try:
                await sw._retry_task
            except (asyncio.CancelledError, Exception):
                pass

        asyncio.run(_go())


class TestPowerOffPostCommandFailure:
    def test_off_post_command_ping_swallowed(self):
        sw = _make_switch()
        hass = _make_hass()
        sw.hass = hass
        sw.async_write_ha_state = MagicMock()
        sw._api.send_command = MagicMock()
        sw._api.ping_connected = MagicMock(side_effect=DeLonghiApiError("post off"))

        asyncio.run(sw.async_turn_off())
        # Still completes, assumed_on = False
        assert sw._assumed_on is False
        sw._api.send_command.assert_called_once()

    def test_off_post_command_ping_unsupported_falls_back(self):
        sw = _make_switch()
        hass = _make_hass()
        sw.hass = hass
        sw.async_write_ha_state = MagicMock()
        sw._api.send_command = MagicMock()
        sw._api.ping_connected = MagicMock(return_value=False)
        sw._api.request_monitor = MagicMock()

        asyncio.run(sw.async_turn_off())
        sw._api.request_monitor.assert_called_once()
