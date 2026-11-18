"""Budget review (finding 9): the cloud hop must not gate the LAN refresh.

``CLOUD_FETCH_TIMEOUT_SECONDS`` (75 s on a black-holed broker) is larger than
``DEFAULT_UPDATE_INTERVAL_SECONDS`` (30 s): awaited inline inside
``_async_update_data``, one lagging MQTT session delayed the LAN state by more
than two full poll cycles. The fetch is now a background task with an
anti-overlap guard — the refresh attaches the cached shadow and returns.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock, patch

from custom_components.delonghi_daedalus.cloud import CloudTelemetryError
from custom_components.delonghi_daedalus.const import (
    CONF_CLOUD_TELEMETRY,
    CONF_HOST,
    CONF_JWT,
    CONF_MACHINE_NAME,
    CONF_SERIAL_NUMBER,
)
from custom_components.delonghi_daedalus.coordinator import DaedalusCoordinator

_PATCH = "custom_components.delonghi_daedalus.coordinator.fetch_cloud_state"


def _make_coordinator() -> DaedalusCoordinator:
    entry = MagicMock()
    entry.data = {
        CONF_HOST: "192.168.1.50",
        CONF_JWT: "jwt",
        CONF_SERIAL_NUMBER: "SN1",
        CONF_MACHINE_NAME: "SN1",
        CONF_CLOUD_TELEMETRY: True,
    }
    hass = MagicMock()

    def _create_task(coro, *_args, **_kwargs):
        return asyncio.ensure_future(coro)

    hass.async_create_task = _create_task
    coord = DaedalusCoordinator(hass, entry)
    coord.hass = hass
    coord._api = MagicMock()
    # A live LAN connection: _ensure_connected short-circuits, so the refresh
    # duration measures only the cloud path.
    coord._lan = MagicMock(closed=False, connection_id="c1")
    coord.async_request_refresh = MagicMock()
    return coord


_DOCS = {"MachineStatus": {"a": 1}, "MachineCapabilities": {}}


def _slow_fetch(delay: float):
    async def _fetch(*_args, **_kwargs):
        await asyncio.sleep(delay)
        return _DOCS, "thing-1"

    return _fetch


def test_refresh_returns_without_waiting_for_the_cloud_fetch():
    """A 5 s cloud hop must not delay the LAN snapshot: the refresh returns
    immediately and the fetch keeps running in the background."""

    async def _go():
        coord = _make_coordinator()
        with patch(_PATCH, side_effect=_slow_fetch(5.0)):
            start = time.monotonic()
            data = await coord._async_update_data()
            elapsed = time.monotonic() - start
            assert elapsed < 1.0, f"refresh waited {elapsed:.1f}s for the cloud hop"
            assert "cloud" not in data  # nothing cached yet
            assert data["connected"] is True
            assert coord._cloud_task is not None
            # Let the background hop finish (it will not block anything).
            await asyncio.wait_for(coord._cloud_task, timeout=10)
        assert coord._cloud_data is not None

    asyncio.run(_go())


def test_one_cloud_hop_at_a_time():
    """The anti-overlap guard: repeated refreshes never stack cloud hops."""

    async def _go():
        coord = _make_coordinator()
        calls = {"n": 0}

        async def _fetch(*_args, **_kwargs):
            calls["n"] += 1
            await asyncio.sleep(5.0)
            return _DOCS, "thing-1"

        with patch(_PATCH, side_effect=_fetch):
            await coord._async_update_data()
            first = coord._cloud_task
            await asyncio.sleep(0)  # let hop #1 actually start
            await coord._async_update_data()  # next 30 s tick while hop #1 runs
            assert coord._cloud_task is first
            assert calls["n"] == 1
            first.cancel()

    asyncio.run(_go())


def test_fresh_cache_is_attached_without_a_new_fetch():
    """Within CLOUD_POLL_SECONDS the refresh only projects the cache."""

    async def _go():
        coord = _make_coordinator()
        coord._cloud_data = {"machine_name": "t", "status_flat": {}, "capabilities_flat": {}, "fetched_at": 0}
        coord._cloud_fetched_at = time.monotonic()
        with patch(_PATCH, side_effect=AssertionError("must not fetch")) as mocked:
            data = await coord._async_update_data()
        assert data["cloud"] is coord._cloud_data
        mocked.assert_not_called()
        assert coord._cloud_task is None

    asyncio.run(_go())


def test_failed_fetch_records_error_without_refresh_storm():
    """A failed hop stores cloud_error for the next refresh and does NOT
    trigger a follow-up refresh (error→refresh→fetch→error would hot-loop)."""

    async def _go():
        coord = _make_coordinator()
        with patch(_PATCH, side_effect=CloudTelemetryError("broker black-holed")):
            await coord._async_update_data()
            await asyncio.wait_for(coord._cloud_task, timeout=5)
            assert coord._cloud_error is not None
            assert coord._cloud_data is None
            coord.async_request_refresh.assert_not_called()
            # The next refresh surfaces the error from the cache.
            data = await coord._async_update_data()
            assert "cloud_error" in data
            # Drain the retry hop this refresh scheduled (same failure).
            await asyncio.wait_for(coord._cloud_task, timeout=5)

    asyncio.run(_go())


def test_successful_fetch_projects_via_one_extra_refresh():
    """Success asks for exactly one refresh so the fresh shadow lands without
    waiting for the next 30 s tick."""

    async def _go():
        coord = _make_coordinator()
        with patch(_PATCH, side_effect=_slow_fetch(0.0)):
            await coord._async_update_data()
            await asyncio.wait_for(coord._cloud_task, timeout=5)
        coord.async_request_refresh.assert_called_once()
        assert coord._cloud_data["machine_name"] == "thing-1"

    asyncio.run(_go())


def test_cloud_hop_timeout_sets_descriptive_error():
    """The wait_for budget used to fall into the generic ``except Exception``
    branch, and str(TimeoutError) is empty — the stored cloud_error was
    literally "unexpected cloud error: ", the valueless key the issue #18
    retest diagnostics showed."""

    async def _go():
        coord = _make_coordinator()

        async def _hang(*_args, **_kwargs):
            await asyncio.sleep(30)
            return _DOCS, "thing-1"

        with (
            patch(_PATCH, side_effect=_hang),
            patch("custom_components.delonghi_daedalus.coordinator.CLOUD_FETCH_TIMEOUT_SECONDS", 0.05),
        ):
            await coord._async_cloud_fetch()
        assert coord._cloud_error is not None
        assert coord._cloud_error.strip()
        assert "timed out" in coord._cloud_error
        assert coord._cloud_data is None

    asyncio.run(_go())
