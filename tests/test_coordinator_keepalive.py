"""Coordinator keepalive backoff — ensure consecutive failures escalate log level
and reset on recovery. Breaks fast if someone drops the counter logic.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from unittest.mock import MagicMock

from custom_components.delonghi_coffee.api import DeLonghiApiError
from custom_components.delonghi_coffee.coordinator import DeLonghiCoordinator


class _DirectExecutor:
    """Stand-in for hass.async_add_executor_job that runs the callable inline."""

    async def __call__(self, fn, *args, **kwargs):
        return fn(*args, **kwargs)


def _make_coord(api: MagicMock) -> DeLonghiCoordinator:
    hass = MagicMock()
    hass.async_add_executor_job = _DirectExecutor()
    coord = DeLonghiCoordinator(hass, api, dsn="DSN-KEEP")
    # conftest stub DataUpdateCoordinator doesn't persist hass — set it ourselves.
    coord.hass = hass
    # Force keepalive path (not full refresh) every call.
    coord._last_full_refresh = 1e12  # far in the future so need_full == False
    coord._last_keepalive = 0  # first call always hits keepalive branch
    return coord


def _make_api_status_ok() -> MagicMock:
    api = MagicMock()
    api.get_status.return_value = {"profile": 0, "monitor_raw": "00" * 20}
    return api


def test_keepalive_failure_counter_increments_and_resets(caplog) -> None:
    api = _make_api_status_ok()
    api.ping_connected.side_effect = DeLonghiApiError("cloud down")

    coord = _make_coord(api)

    async def hammer(n: int) -> None:
        for _ in range(n):
            coord._last_keepalive = 0  # force keepalive eligibility each cycle
            with contextlib.suppress(Exception):  # only care about the counter
                await coord._async_update_data()

    caplog.set_level(logging.DEBUG, logger="custom_components.delonghi_coffee.coordinator")
    asyncio.run(hammer(4))

    assert coord._keepalive_failures >= 3, (
        f"counter should increment on each failure, got {coord._keepalive_failures}"
    )

    # Warning emitted exactly when counter hits 3.
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "keepalive failing" in r.message]
    assert len(warnings) == 1, f"expected one warning at threshold, got {len(warnings)}"

    # Recovery resets the counter and emits an info line.
    api.ping_connected.side_effect = None
    api.ping_connected.return_value = True
    coord._last_keepalive = 0
    asyncio.run(coord._async_update_data())
    assert coord._keepalive_failures == 0, "successful keepalive must reset the counter"

    info_recovered = [r for r in caplog.records if r.levelno == logging.INFO and "recovered" in r.message]
    assert len(info_recovered) == 1, "recovery should log once at INFO"


def test_keepalive_error_escalates_at_multiples_of_five(caplog) -> None:
    api = _make_api_status_ok()
    api.ping_connected.side_effect = DeLonghiApiError("still down")
    coord = _make_coord(api)

    caplog.set_level(logging.DEBUG, logger="custom_components.delonghi_coffee.coordinator")

    async def hammer(n: int) -> None:
        for _ in range(n):
            coord._last_keepalive = 0
            with contextlib.suppress(Exception):
                await coord._async_update_data()

    asyncio.run(hammer(12))

    errors = [r for r in caplog.records if r.levelno == logging.ERROR and "still failing" in r.message]
    # Error fires at 5 and 10 (multiples of 5). 12 attempts → exactly 2 error logs.
    assert len(errors) == 2, f"expected 2 ERROR escalations at failure counts 5 and 10, got {len(errors)}"
