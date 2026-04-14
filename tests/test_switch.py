"""Test DeLonghiPowerSwitch — retry task lifecycle."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

from custom_components.delonghi_coffee.switch import DeLonghiPowerSwitch


def _make_switch() -> DeLonghiPowerSwitch:
    """Construct a switch with stubbed HA internals."""
    api = MagicMock()
    coordinator = MagicMock()
    coordinator.data = {"machine_state": "Off"}
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
