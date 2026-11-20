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
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed

from custom_components.delonghi_daedalus.api import (
    DaedalusAuthError,
    DaedalusConnectionError,
    DaedalusLanIdentityError,
    DaedalusLanSerialMismatchError,
)
from custom_components.delonghi_daedalus.cloud import CloudState, CloudTelemetryError, MachineSettingsTelemetry
from custom_components.delonghi_daedalus.const import (
    BREWING_STATISTICS_POLL_SECONDS,
    BREWING_STATISTICS_RETRY_SECONDS,
    CLOUD_POLL_SECONDS,
    CONF_CLOUD_TELEMETRY,
    CONF_DRINK_VISIBILITY,
    CONF_HOST,
    CONF_JWT,
    CONF_LAN_TLS_FINGERPRINT,
    CONF_MACHINE_NAME,
    CONF_SERIAL_NUMBER,
    CONF_SESSION_TOKEN,
    DRINK_VISIBILITY_POLL_SECONDS,
    DRINK_VISIBILITY_RETRY_SECONDS,
    MACHINE_SETTINGS_POLL_SECONDS,
    MACHINE_SETTINGS_RETRY_SECONDS,
)
from custom_components.delonghi_daedalus.coordinator import DaedalusCoordinator

_PATCH = "custom_components.delonghi_daedalus.coordinator.fetch_cloud_state"
_STATS_PATCH = "custom_components.delonghi_daedalus.coordinator.fetch_brewing_statistics"
_VISIBILITY_PATCH = "custom_components.delonghi_daedalus.coordinator.fetch_visible_drinks_count"


@pytest.fixture(autouse=True)
def _mock_statistics():
    with patch(_STATS_PATCH, return_value={}) as mocked:
        yield mocked


@pytest.fixture(autouse=True)
def _mock_machine_settings():
    with patch(
        "custom_components.delonghi_daedalus.coordinator.fetch_machine_settings",
        return_value=MachineSettingsTelemetry(),
    ) as mocked:
        yield mocked


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

    hass.async_create_background_task = _create_task
    coord = DaedalusCoordinator(hass, entry)
    coord.hass = hass
    coord.data = {}
    coord.async_update_listeners = MagicMock()
    coord._api = MagicMock()
    # A live LAN connection: _ensure_connected short-circuits, so the refresh
    # duration measures only the cloud path.
    coord._lan = MagicMock(closed=False, connection_id="c1")
    return coord


_DOCS = {"MachineStatus": {"state": {"reported": {"a": 1}}}, "MachineCapabilities": {}}
_CLOUD_STATE = CloudState(_DOCS, "thing-1")


def _slow_fetch(delay: float):
    async def _fetch(*_args, **_kwargs):
        await asyncio.sleep(delay)
        return _CLOUD_STATE

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
            return _CLOUD_STATE

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
    """A failure immediately removes previous values and backs off cloud retries."""

    async def _go():
        coord = _make_coordinator()
        with patch(_PATCH, side_effect=[_CLOUD_STATE, CloudTelemetryError("broker black-holed")]) as fetch:
            await coord._async_update_data()
            await asyncio.wait_for(coord._cloud_task, timeout=5)
            assert coord.data["cloud"]["status_flat"] == {"a": 1}
            coord._cloud_fetched_at -= CLOUD_POLL_SECONDS
            await coord._async_update_data()
            await asyncio.wait_for(coord._cloud_task, timeout=5)
            assert coord._cloud_error is not None
            assert coord._cloud_data is None
            assert "cloud_error" in coord.data
            assert "cloud" not in coord.data
            assert coord.data["connected"]
            data = await coord._async_update_data()
            assert "cloud_error" in data
            assert fetch.await_count == 2

    asyncio.run(_go())


def test_successful_fetch_publishes_without_a_lan_refresh():
    """New shadows land immediately and leave the LAN polling cadence intact."""

    async def _go():
        coord = _make_coordinator()
        with patch(_PATCH, side_effect=_slow_fetch(0.0)):
            await coord._async_update_data()
            fetch_task = coord._cloud_task
            await asyncio.wait_for(fetch_task, timeout=5)
        coord.async_update_listeners.assert_called_once()
        assert coord._cloud_task is fetch_task
        assert coord.data["cloud"] is coord._cloud_data
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
            return _CLOUD_STATE

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


@pytest.mark.parametrize("host", [None, "192.168.1.50"])
def test_cloud_is_available_during_missing_wifi_or_lan_failure(host):
    async def _go():
        coord = _make_coordinator()
        coord.entry.data[CONF_HOST] = host
        coord._lan = None
        coord._api.connect_lan = AsyncMock(side_effect=DaedalusConnectionError("LAN unavailable"))

        with patch(_PATCH, return_value=_CLOUD_STATE):
            coord.data = await coord._async_update_data()
            await coord._cloud_task

        assert coord._api.connect_lan.await_count == (1 if host else 0)
        assert not coord.data["connected"]
        assert "connection_id" not in coord.data
        assert coord.data["cloud"]["status_flat"] == {"a": 1}

    asyncio.run(_go())


@pytest.mark.parametrize("status_error", [None, "Device directory reports the machine offline"])
def test_partial_shadow_replaces_the_missing_slice(status_error):
    async def _go():
        coord = _make_coordinator()
        docs = {**_DOCS, "MachineCapabilities": {"state": {"reported": {"firmware": "1.2.0"}}}}
        partial = {
            "MachineCapabilities": {"state": {"reported": {"firmware": "1.3.0"}}},
        }
        if status_error is None:
            partial["MachineStatus"] = {"state": {"desired": {"Status": "Idle"}}}
        with patch(_PATCH, side_effect=[CloudState(docs, "thing-1"), CloudState(partial, "thing-1", status_error)]):
            await coord._async_update_data()
            await coord._cloud_task
            coord._cloud_fetched_at -= CLOUD_POLL_SECONDS
            await coord._async_update_data()
            await coord._cloud_task

        assert "status_flat" not in coord.data["cloud"]
        assert coord.data["cloud"]["capabilities_flat"] == {"firmware": "1.3.0"}
        assert coord.data["cloud"]["status_error"] == (
            status_error or "MachineStatus shadow has no usable reported data"
        )
        assert "cloud_error" not in coord.data
        assert coord.data["connected"]

    asyncio.run(_go())


def test_cloud_failure_preserves_status_error_when_no_capabilities_are_available():
    async def _go():
        coord = _make_coordinator()
        status_error = "Device directory reports the machine offline"
        with patch(_PATCH, return_value=CloudState({}, "thing-1", status_error)):
            await coord._async_cloud_fetch()
        assert coord.data["cloud_error"] == status_error
        assert "cloud" not in coord.data

    asyncio.run(_go())


def test_cloud_works_when_local_reauth_is_needed():
    async def _go():
        coord = _make_coordinator()
        coord._lan = None
        coord._ensure_connected = AsyncMock(side_effect=ConfigEntryAuthFailed("LAN rejected credentials"))

        with patch(_PATCH, return_value=_CLOUD_STATE):
            coord.data = await coord._async_update_data()
            await coord._cloud_task
            await coord._async_update_data()
        coord._ensure_connected.assert_awaited_once()
        coord.entry.async_start_reauth.assert_called_once_with(coord.hass)
        assert not coord.data["connected"]
        assert coord.data["cloud"]["status_flat"] == {"a": 1}

    asyncio.run(_go())


def test_missing_expected_lan_identity_raises_repair_then_success_clears_it():
    async def _go():
        coord = _make_coordinator()
        coord.entry.data[CONF_CLOUD_TELEMETRY] = False
        coord._lan = None
        connection = MagicMock(
            closed=False, connection_id=7, auth_message="AUTH", receive=AsyncMock(), close=AsyncMock()
        )
        coord._api.connect_lan = AsyncMock(side_effect=[DaedalusLanIdentityError("pin mismatch"), connection])
        coordinator_module = "custom_components.delonghi_daedalus.coordinator"
        with (
            patch(f"{coordinator_module}.async_raise_lan_issue") as raise_issue,
            patch(f"{coordinator_module}.async_clear_lan_issue") as clear_issue,
        ):
            coord.data = await coord._async_update_data()
            assert not coord.data["connected"]
            assert coord.data["lan_error"] == "pin mismatch"
            raise_issue.assert_called_once_with(coord.hass, coord.entry.entry_id)
            clear_issue.assert_not_called()

            coord.data = await coord._async_update_data()
            assert coord.data["connected"]
            assert "lan_error" not in coord.data
            clear_issue.assert_called_once_with(coord.hass, coord.entry.entry_id)
            await coord._stop_lan_listener()

    asyncio.run(_go())


def test_serial_mismatch_blocks_lan_polls_for_the_same_identity():
    async def run():
        coord = _make_coordinator()
        coord._lan = None
        coord.entry.data[CONF_LAN_TLS_FINGERPRINT] = "ab" * 32
        coord.entry.data[CONF_MACHINE_NAME] = "thing-1"
        connection = MagicMock(
            closed=False, connection_id=7, auth_message="AUTH", receive=AsyncMock(), close=AsyncMock()
        )
        coord._api.connect_lan = AsyncMock(side_effect=[DaedalusLanSerialMismatchError("serial mismatch"), connection])
        coord._api.refresh_jwt = AsyncMock()
        coordinator_module = "custom_components.delonghi_daedalus.coordinator"
        with (
            patch(f"{coordinator_module}.async_raise_lan_issue") as raise_issue,
            patch(f"{coordinator_module}.async_clear_lan_issue") as clear_issue,
            patch(_PATCH, return_value=_CLOUD_STATE),
        ):
            coord.data = await coord._async_update_data()
            assert coord.data["lan_error"] == "serial mismatch"
            await coord._cloud_task

            coord.entry.data[CONF_JWT] = "new-jwt"
            snapshot = await coord._async_update_data()
            assert not snapshot["connected"]
            assert snapshot["lan_error"] == "serial mismatch"
            assert "cloud" in snapshot
            coord._api.connect_lan.assert_awaited_once()
            coord._api.refresh_jwt.assert_not_awaited()
            coord.hass.config_entries.async_update_entry.assert_not_called()
            coord.entry.async_start_reauth.assert_not_called()
            raise_issue.assert_called_once_with(coord.hass, coord.entry.entry_id)
            clear_issue.assert_not_called()

            coord.entry.data[CONF_LAN_TLS_FINGERPRINT] = "fe" * 32
            snapshot = await coord._async_update_data()
            assert snapshot["connected"]
            assert "lan_error" not in snapshot
            assert coord._api.connect_lan.await_args.kwargs["fingerprint"] == "fe" * 32
            clear_issue.assert_called_once_with(coord.hass, coord.entry.entry_id)
            await coord._stop_lan_listener()

    asyncio.run(run())


@pytest.mark.parametrize("rotation_succeeds", [True, False])
def test_cloud_can_renew_an_expired_jwt_independently(rotation_succeeds):
    async def _go():
        coord = _make_coordinator()
        # Payload is {"exp": 1}. It is a scheduling hint; the real services
        # authenticate the token and determine whether it can be used.
        expired = "header.eyJleHAiOjF9.signature"
        coord.entry.data.update({CONF_JWT: expired, CONF_SESSION_TOKEN: "session"})
        if rotation_succeeds:
            coord.entry.data[CONF_HOST] = None
            coord._lan = None
        coord._api.refresh_jwt = AsyncMock(
            return_value="fresh-jwt",
            side_effect=None if rotation_succeeds else DaedalusConnectionError("network unavailable"),
        )

        def update_entry(entry, *, data):
            entry.data = data

        coord.hass.config_entries.async_update_entry.side_effect = update_entry
        with patch(_PATCH, return_value=_CLOUD_STATE) as fetch:
            coord.data = await coord._async_update_data()
            await coord._cloud_task

        assert coord.data["connected"] is not rotation_succeeds
        coord._api.refresh_jwt.assert_awaited_once()
        if rotation_succeeds:
            assert fetch.await_args.kwargs["jwt"] == "fresh-jwt"
            assert coord.entry.data[CONF_JWT] == "fresh-jwt"
            assert coord.data["cloud"]["status_flat"] == {"a": 1}
        else:
            fetch.assert_not_awaited()
            assert "cloud" not in coord.data
            assert coord.data["cloud_error"] == "network unavailable"
        coord.entry.async_start_reauth.assert_not_called()

    asyncio.run(_go())


@pytest.mark.parametrize("session_token", [None, "revoked"])
def test_cloud_waits_for_new_credentials_after_permanent_denial(session_token):
    async def _go():
        coord = _make_coordinator()
        coord.entry.data[CONF_JWT] = "header.eyJleHAiOjF9.signature"
        if session_token:
            coord.entry.data[CONF_SESSION_TOKEN] = session_token
        results = [DaedalusAuthError("revoked"), "fresh-jwt"] if session_token else ["fresh-jwt"]
        coord._api.refresh_jwt = AsyncMock(side_effect=results)

        def update_entry(entry, *, data):
            entry.data = data

        coord.hass.config_entries.async_update_entry.side_effect = update_entry
        with patch(_PATCH, return_value=_CLOUD_STATE) as fetch:
            coord.data = await coord._async_update_data()
            failed_task = coord._cloud_task
            await failed_task
            assert coord.data["connected"]
            assert "cloud_error" in coord.data

            # Normal retry backoff has elapsed. Unchanged credentials must
            # still stay blocked after Gigya denies refresh.
            coord._cloud_retry_at = 0.0
            await coord._async_update_data()
            await coord._async_update_data()
            assert coord._cloud_task is failed_task
            assert coord._api.refresh_jwt.await_count == bool(session_token)
            fetch.assert_not_awaited()
            coord.entry.async_start_reauth.assert_called_once_with(coord.hass)

            coord.entry.data[CONF_SESSION_TOKEN] = "new-session"
            await coord._async_update_data()
            await coord._cloud_task
            fetch.assert_awaited_once()
            assert coord.data["cloud"]["status_flat"] == {"a": 1}
            assert coord.data["connected"]
            assert "cloud_error" not in coord.data

    asyncio.run(_go())


def test_cloud_expiry_preserves_healthy_lan():
    async def _go():
        coord = _make_coordinator()
        with (
            patch(_PATCH, return_value=_CLOUD_STATE),
            patch("custom_components.delonghi_daedalus.coordinator._CLOUD_MAX_AGE", 0.02),
        ):
            await coord._async_update_data()
            await coord._cloud_task
            assert "cloud" in coord.data
            await asyncio.sleep(0.04)
            assert "cloud" not in coord.data
            assert coord.data["connected"]

    asyncio.run(_go())


def test_lan_listener_detects_disconnect_and_reconnects_without_commands():
    async def _go():
        coord = _make_coordinator()
        coord.entry.data.update({CONF_CLOUD_TELEMETRY: False, CONF_LAN_TLS_FINGERPRINT: "ab" * 32})
        coord._lan = None
        messages: asyncio.Queue = asyncio.Queue()

        async def receive(*, timeout):
            assert timeout is None
            frame = await messages.get()
            if isinstance(frame, Exception):
                raise frame
            return frame

        def connection(connection_id):
            return MagicMock(
                closed=False,
                connection_id=connection_id,
                auth_message="AUTH",
                receive=AsyncMock(side_effect=receive),
                close=AsyncMock(),
                send_command=AsyncMock(),
            )

        original = connection(7)
        replacement = connection(8)
        coord._api.connect_lan = AsyncMock(side_effect=[original, replacement])
        coord.data = await coord._async_update_data()
        assert coord.data["connection_id"] == 7

        # Authentication or server frames must never be interpreted as an
        # instruction to issue a command or mark the machine ready during heat.
        await messages.put({"Message": "StatusService", "Response": '{"Status":"Busy","StepDescr":"Heating"}'})
        await messages.put(DaedalusConnectionError("machine closed the socket"))
        await coord._lan_task
        assert not coord.data["connected"]
        assert "connection_id" not in coord.data
        assert "lan_status" not in coord.data
        coord._api.connect_lan.assert_awaited_once()

        coord.data = await coord._async_update_data()
        assert coord.data["connection_id"] == 8
        assert coord._api.connect_lan.await_count == 2
        assert coord._api.connect_lan.await_args.kwargs["fingerprint"] == "ab" * 32
        original.send_command.assert_not_awaited()
        replacement.send_command.assert_not_awaited()
        await coord._stop_lan_listener()

    asyncio.run(_go())


def test_machine_settings_back_off_without_delaying_regular_shadows(_mock_machine_settings, _mock_statistics):
    async def _go():
        coord = _make_coordinator()
        coord.entry.data[CONF_MACHINE_NAME] = "thing-1"
        started = asyncio.Event()
        totals = {"descale_count": 1, "water_filter_registration_count": 0}
        settings = {"descale_progress": 72, "auto_off_delay": 30, "water_filter_installed": False}

        async def _settings(_session, *, thing_name, jwt):
            assert thing_name == "thing-1"
            assert jwt == "jwt"
            if _mock_machine_settings.await_count == 1:
                started.set()
                await asyncio.Event().wait()
            if _mock_machine_settings.await_count == 3:
                raise CloudTelemetryError("settings shadow not found")
            return MachineSettingsTelemetry(totals, settings)

        _mock_machine_settings.side_effect = _settings
        _mock_statistics.return_value = {"brew_count": 12}
        with (
            patch(_PATCH, return_value=_CLOUD_STATE) as regular_fetch,
            patch("custom_components.delonghi_daedalus.coordinator.MACHINE_SETTINGS_FETCH_TIMEOUT_SECONDS", 0.05),
        ):
            await coord._async_update_data()
            await asyncio.wait_for(started.wait(), timeout=2)
            data = await coord._async_update_data()
            assert data["connected"]
            assert data["cloud"]["status_flat"] == {"a": 1}
            assert data["cloud"]["brewing_statistics"] == {"brew_count": 12}
            assert "maintenance_counters" not in data["cloud"]
            assert "machine_settings" not in data["cloud"]
            await asyncio.wait_for(coord._cloud_task, timeout=2)
            assert coord._machine_settings_due_at > time.monotonic() + MACHINE_SETTINGS_RETRY_SECONDS - 1

            coord._cloud_fetched_at -= CLOUD_POLL_SECONDS
            await coord._async_update_data()
            await coord._cloud_task
            _mock_machine_settings.assert_awaited_once()

            coord._cloud_fetched_at -= CLOUD_POLL_SECONDS
            coord._machine_settings_due_at = 0
            await coord._async_update_data()
            await coord._cloud_task
            assert coord.data["cloud"]["maintenance_counters"] == totals
            assert coord.data["cloud"]["machine_settings"] == settings
            assert coord._machine_settings_due_at > time.monotonic() + MACHINE_SETTINGS_POLL_SECONDS - 1

            coord._cloud_fetched_at -= CLOUD_POLL_SECONDS
            await coord._async_update_data()
            await coord._cloud_task
            assert coord.data["cloud"]["maintenance_counters"] == totals
            assert coord.data["cloud"]["machine_settings"] == settings
            assert _mock_machine_settings.await_count == 2

            coord._cloud_fetched_at -= CLOUD_POLL_SECONDS
            coord._machine_settings_due_at = 0
            await coord._async_update_data()
            await coord._cloud_task
            assert coord.data["cloud"]["maintenance_counters"] == {}
            assert coord.data["cloud"]["machine_settings"] == {}
            assert coord.data["cloud"]["brewing_statistics"] == {"brew_count": 12}
            assert "cloud_error" not in coord.data
            assert regular_fetch.await_count == 5

    asyncio.run(_go())


def test_offline_machine_clears_settings_and_counters_until_next_due_read(_mock_machine_settings):
    async def _go():
        coord = _make_coordinator()
        coord.entry.data[CONF_MACHINE_NAME] = "thing-1"
        offline = CloudState(
            {"MachineCapabilities": {"state": {"reported": {"Languages": ["en"]}}}},
            "thing-1",
            status_error="Device directory reports the machine offline",
        )
        _mock_machine_settings.side_effect = [
            MachineSettingsTelemetry({"descale_count": 1}, {"descale_progress": 95}),
            MachineSettingsTelemetry({"descale_count": 2}, {"descale_progress": 0}),
        ]
        with patch(_PATCH, side_effect=[_CLOUD_STATE, offline, _CLOUD_STATE, _CLOUD_STATE]):
            await coord._async_cloud_fetch()
            assert coord.data["cloud"]["maintenance_counters"] == {"descale_count": 1}
            assert coord.data["cloud"]["machine_settings"] == {"descale_progress": 95}
            due_at = coord._machine_settings_due_at

            await coord._async_cloud_fetch()
            assert coord.data["cloud"]["capabilities_flat"]
            assert "status_flat" not in coord.data["cloud"]
            assert "maintenance_counters" not in coord.data["cloud"]
            assert "machine_settings" not in coord.data["cloud"]

            await coord._async_cloud_fetch()
            assert coord.data["cloud"]["status_flat"] == {"a": 1}
            assert "maintenance_counters" not in coord.data["cloud"]
            assert "machine_settings" not in coord.data["cloud"]
            assert coord._machine_settings_due_at == due_at
            _mock_machine_settings.assert_awaited_once()

            coord._machine_settings_due_at = 0
            await coord._async_cloud_fetch()
            assert coord.data["cloud"]["maintenance_counters"] == {"descale_count": 2}
            assert coord.data["cloud"]["machine_settings"] == {"descale_progress": 0}

    asyncio.run(_go())


@pytest.mark.parametrize("invalidate", ["cloud expiry", "account change"])
def test_pending_machine_settings_do_not_restore_an_obsolete_report(_mock_machine_settings, invalidate):
    async def _go():
        coord = _make_coordinator()
        coord.entry.data[CONF_MACHINE_NAME] = "thing-1"
        started = asyncio.Event()
        release = asyncio.Event()

        async def _settings(*_args, **_kwargs):
            started.set()
            await release.wait()
            return MachineSettingsTelemetry({"descale_count": 1}, {"water_filter_installed": True})

        _mock_machine_settings.side_effect = _settings
        with patch(_PATCH, return_value=_CLOUD_STATE):
            await coord._async_update_data()
            await asyncio.wait_for(started.wait(), timeout=2)
            if invalidate == "cloud expiry":
                coord._cancel_cloud_expiry()
                coord._expire_cloud()
            else:
                coord.entry.data[CONF_SESSION_TOKEN] = "updated-session"
            release.set()
            await asyncio.wait_for(coord._cloud_task, timeout=2)
            assert "maintenance_counters" not in coord.data.get("cloud", {})
            assert "machine_settings" not in coord.data.get("cloud", {})
            assert coord._machine_settings_due_at == 0

    asyncio.run(_go())


@pytest.mark.parametrize(
    ("updated_key", "expected"),
    [(CONF_SESSION_TOKEN, {}), (CONF_JWT, {"descale_count": 1})],
)
def test_machine_settings_read_keeps_original_account_identity(
    _mock_statistics, _mock_machine_settings, updated_key, expected
):
    async def _go():
        coord = _make_coordinator()
        coord.entry.data[CONF_MACHINE_NAME] = "thing-1"
        started = asyncio.Event()
        release = asyncio.Event()

        async def _statistics(*_args, **_kwargs):
            started.set()
            await release.wait()
            return {"brew_count": 12}

        _mock_statistics.side_effect = _statistics
        _mock_machine_settings.return_value = MachineSettingsTelemetry({"descale_count": 1})
        with patch(_PATCH, return_value=_CLOUD_STATE):
            await coord._async_update_data()
            await asyncio.wait_for(started.wait(), timeout=2)
            coord.entry.data[updated_key] = "updated-value"
            release.set()
            await asyncio.wait_for(coord._cloud_task, timeout=2)

        assert coord.data["cloud"]["brewing_statistics"] == {"brew_count": 12}
        assert coord.data["cloud"].get("maintenance_counters", {}) == expected
        assert _mock_machine_settings.await_count == int(bool(expected))

    asyncio.run(_go())


def test_optional_statistics_timeout_leaves_regular_shadows_untouched(_mock_statistics):
    async def _go():
        coord = _make_coordinator()
        coord.entry.data[CONF_MACHINE_NAME] = "thing-1"
        await_stats = asyncio.Event()

        async def _statistics(*_args, **_kwargs):
            await_stats.set()
            await asyncio.Event().wait()

        _mock_statistics.side_effect = _statistics
        with (
            patch(_PATCH, return_value=_CLOUD_STATE) as regular_fetch,
            patch("custom_components.delonghi_daedalus.coordinator.BREWING_STATISTICS_FETCH_TIMEOUT_SECONDS", 0.05),
        ):
            start = time.monotonic()
            await coord._async_update_data()
            await asyncio.wait_for(await_stats.wait(), timeout=2)
            assert coord.data["cloud"]["status_flat"] == {"a": 1}
            assert coord.data["connected"] is True
            assert not coord._cloud_task.done()
            await asyncio.wait_for(coord._cloud_task, timeout=2)
            assert coord._cloud_error is None
            assert coord._brewing_statistics_due_at >= start + BREWING_STATISTICS_RETRY_SECONDS
            assert "brewing_statistics" not in coord._cloud_data
            regular_fetch.assert_awaited_once()
            coord.async_update_listeners.assert_called_once()

            coord._cloud_fetched_at -= CLOUD_POLL_SECONDS
            await coord._async_update_data()
            await coord._cloud_task
            assert regular_fetch.await_count == 2
            _mock_statistics.assert_awaited_once()

    asyncio.run(_go())


def test_statistics_survive_regular_fetches_and_clear_when_denied(_mock_statistics):
    async def _go():
        coord = _make_coordinator()
        coord.entry.data[CONF_MACHINE_NAME] = "thing-1"
        counters = {"brew_count": 12, "stopped_brew_count": 3, "brewing_error_count": 0}
        updated_counters = {**counters, "brew_count": 13}
        _mock_statistics.side_effect = [counters, CloudTelemetryError("broker refused subscriptions"), updated_counters]

        with patch(_PATCH, return_value=_CLOUD_STATE):
            start = time.monotonic()
            await coord._async_update_data()
            await coord._cloud_task
            assert coord.async_update_listeners.call_count == 2
            assert coord._brewing_statistics_due_at >= start + BREWING_STATISTICS_POLL_SECONDS
            assert coord.data["cloud"]["brewing_statistics"] == counters

            coord._cloud_fetched_at -= CLOUD_POLL_SECONDS
            await coord._async_update_data()
            await coord._cloud_task
            assert coord.data["cloud"]["brewing_statistics"] == counters
            _mock_statistics.assert_awaited_once()

            coord._cloud_fetched_at -= CLOUD_POLL_SECONDS
            coord._brewing_statistics_due_at -= BREWING_STATISTICS_POLL_SECONDS + 1
            await coord._async_update_data()
            await coord._cloud_task
            assert coord.data["cloud"]["brewing_statistics"] == {}
            assert coord.data["cloud"]["status_flat"] == {"a": 1}
            assert coord._cloud_error is None
            assert coord.async_update_listeners.call_count == 5
            assert _mock_statistics.await_count == 2

            coord._cloud_fetched_at -= CLOUD_POLL_SECONDS
            coord._brewing_statistics_due_at -= BREWING_STATISTICS_RETRY_SECONDS + 1
            await coord._async_update_data()
            await coord._cloud_task
            assert coord.data["cloud"]["brewing_statistics"] == updated_counters

    asyncio.run(_go())


def test_statistics_cannot_restore_expired_cloud_snapshot(_mock_statistics):
    async def _go():
        coord = _make_coordinator()
        started = asyncio.Event()
        release = asyncio.Event()

        async def _statistics(*_args, **_kwargs):
            started.set()
            await release.wait()
            return {"brew_count": 12}

        _mock_statistics.side_effect = _statistics
        with patch(_PATCH, return_value=_CLOUD_STATE):
            await coord._async_update_data()
            await asyncio.wait_for(started.wait(), timeout=2)
            assert coord.data["cloud"]["status_flat"] == {"a": 1}
            coord._cancel_cloud_expiry()
            coord._expire_cloud()
            release.set()
            await asyncio.wait_for(coord._cloud_task, timeout=2)

        assert "cloud" not in coord.data
        assert coord._cloud_data is None
        assert coord._brewing_statistics_due_at == 0.0

    asyncio.run(_go())


def test_statistics_repoll_after_thing_remap_without_reusing_old_counters(_mock_statistics):
    async def _go():
        coord = _make_coordinator()
        coord.entry.data[CONF_MACHINE_NAME] = "thing-1"

        async def _statistics(_session, *, thing_name, jwt):
            if thing_name == "thing-1":
                return {"brew_count": 12}
            assert thing_name == "thing-2"
            assert "brewing_statistics" not in coord.data["cloud"]
            return {"stopped_brew_count": 0}

        _mock_statistics.side_effect = _statistics
        with patch(_PATCH, side_effect=[_CLOUD_STATE, CloudState(_DOCS, "thing-2")]):
            await coord._async_update_data()
            await coord._cloud_task
            assert coord.data["cloud"]["brewing_statistics"] == {"brew_count": 12}
            assert coord._brewing_statistics_due_at > time.monotonic()

            coord._cloud_fetched_at -= CLOUD_POLL_SECONDS
            await coord._async_update_data()
            await coord._cloud_task

        assert _mock_statistics.await_count == 2
        assert coord.data["cloud"]["machine_name"] == "thing-2"
        assert coord.data["cloud"]["brewing_statistics"] == {"stopped_brew_count": 0}

    asyncio.run(_go())


def test_visibility_is_opt_in_and_keeps_regular_telemetry_on_failure():
    async def _go():
        coord = _make_coordinator()
        coord.entry.data[CONF_MACHINE_NAME] = "thing-1"
        waiting = asyncio.Event()

        async def visibility(*_args, **_kwargs):
            if fetch.await_count == 1:
                return 0
            waiting.set()
            await asyncio.Future()

        with (
            patch(_PATCH, return_value=_CLOUD_STATE),
            patch(_VISIBILITY_PATCH, side_effect=visibility) as fetch,
            patch("custom_components.delonghi_daedalus.coordinator.DRINK_VISIBILITY_FETCH_TIMEOUT_SECONDS", 0.05),
        ):
            await coord._async_update_data()
            await coord._cloud_task
            fetch.assert_not_awaited()

            coord.entry.data[CONF_DRINK_VISIBILITY] = True
            coord._cloud_fetched_at -= CLOUD_POLL_SECONDS
            await coord._async_update_data()
            await coord._cloud_task
            assert coord.data["cloud"]["visible_drinks_count"] == 0

            coord._cloud_fetched_at -= CLOUD_POLL_SECONDS
            await coord._async_update_data()
            await coord._cloud_task
            assert coord.data["cloud"]["visible_drinks_count"] == 0
            fetch.assert_awaited_once()

            coord._cloud_fetched_at -= CLOUD_POLL_SECONDS
            coord._drink_visibility_due_at -= DRINK_VISIBILITY_POLL_SECONDS + 1
            await coord._async_update_data()
            await asyncio.wait_for(waiting.wait(), timeout=2)
            assert coord.data["cloud"]["status_flat"] == {"a": 1}
            assert coord.data["connected"] is True
            assert not coord._cloud_task.done()
            await asyncio.wait_for(coord._cloud_task, timeout=2)
            assert coord._cloud_error is None
            assert "visible_drinks_count" not in coord.data["cloud"]
            assert coord._drink_visibility_due_at >= time.monotonic() + DRINK_VISIBILITY_RETRY_SECONDS - 1

    asyncio.run(_go())
