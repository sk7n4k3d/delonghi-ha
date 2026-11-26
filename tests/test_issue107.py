"""Regression coverage for non-idempotent LAN command shapes."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.delonghi_coffee.api import DeLonghiApi
from custom_components.delonghi_coffee.coordinator import BrewReceipt, DeLonghiCoordinator
from tests.test_coordinator import LAN_BREWING, LAN_READY, _make_api, _make_coord, _make_hass, _make_lan_server


async def _setup():
    coord, _, api = _make_coord()
    coord.config_entry = None
    coord.async_set_updated_data = MagicMock()
    api._cmd_property = "data_request"
    api._build_packet = MagicMock(side_effect=lambda _cmd, *, include_app_id: f"shape-{include_app_id}")
    api.parse_monitor_value = DeLonghiApi.parse_monitor_value
    server = _make_lan_server()
    server.activity_epoch = 1
    server.enqueue_command = AsyncMock(side_effect=lambda _payload, **kw: kw.get("fetched", asyncio.Event()))
    coord._lan_server = server
    coord._lan_properties = {"d302_monitor": LAN_READY, "data_response": "previous"}
    parsed = api.parse_monitor_value(LAN_READY)
    coord.data = {"machine_state": "Ready", "monitor_raw": parsed["monitor_raw"]}
    coord._shape_ack_timeout = 0.012
    coord._shape_ack_poll = 0.001
    receipt = await coord.send_command_lan(b"brew", return_fetch_event=True, brew=True)
    assert isinstance(receipt, BrewReceipt)
    receipt.set()
    return coord, server, api, receipt


async def _ready(coord):
    await coord._on_lan_property({"property": {"name": "d302_monitor", "value": LAN_READY}})


def test_brew_single_default_shape_and_settings_keep_both():
    async def scenario():
        coord, server, api, _ = await _setup()
        assert api._build_packet.call_args.kwargs["include_app_id"] is False
        assert len(server.enqueue_command.call_args.args[0]["properties"]) == 1
        api._build_packet.reset_mock()
        await coord.send_command_lan(b"settings")
        assert [call.kwargs["include_app_id"] for call in api._build_packet.call_args_list] == [False, True]
        assert len(server.enqueue_command.call_args.args[0]["properties"]) == 2

    asyncio.run(scenario())


def test_one_alternate_only_after_fresh_ready_and_no_response():
    async def scenario():
        coord, server, api, receipt = await _setup()
        await _ready(coord)
        await coord._async_verify_lan_shape(b"brew", receipt)
        assert server.enqueue_command.await_count == 2
        assert api._build_packet.call_args.kwargs["include_app_id"] is True
        retry = server.enqueue_command.call_args.kwargs["fetched"]
        assert retry.should_deliver()
        await coord._async_verify_lan_shape(b"brew", receipt)
        assert server.enqueue_command.await_count == 2
        await coord._on_lan_property({"property": {"name": "data_response", "value": "late"}})
        assert not retry.should_deliver()

    asyncio.run(scenario())


@pytest.mark.parametrize("scenario", ["brewing", "response", "dead", "session_changed", "no_fresh", "unknown"])
def test_no_retry_when_acceptance_or_witness_is_uncertain(scenario):
    async def run():
        coord, server, _, receipt = await _setup()
        if scenario != "no_fresh":
            await _ready(coord)
        if scenario == "brewing":
            await coord._on_lan_property({"property": {"name": "d302_monitor", "value": LAN_BREWING}})
            coord._lan_properties["d302_monitor"] = LAN_READY
        elif scenario == "response":
            await coord._on_lan_property({"property": {"name": "data_response", "value": "accepted"}})
        elif scenario == "dead":
            server.is_session_alive.return_value = False
        elif scenario == "session_changed":
            server.activity_epoch += 1
        elif scenario == "unknown":
            receipt.ambiguous = True
        await coord._async_verify_lan_shape(b"brew", receipt)
        assert server.enqueue_command.await_count == 1

    asyncio.run(run())


@pytest.mark.parametrize("elapsed,value", [(1, "previous"), (6, "status")])
def test_late_or_unchanged_response_is_ambiguous(elapsed, value):
    async def scenario():
        coord, server, _, receipt = await _setup()
        coord._shape_ack_timeout = 45
        await _ready(coord)
        with patch("custom_components.delonghi_coffee.coordinator.monotonic", return_value=receipt.polled_at + elapsed):
            await coord._on_lan_property({"property": {"name": "data_response", "value": value}})
            assert receipt.ambiguous
            assert not coord._shape_retry_allowed(receipt)
        assert server.enqueue_command.await_count == 1

    asyncio.run(scenario())


def test_missing_response_baseline_fails_closed():
    async def scenario():
        coord, server, _, _ = await _setup()
        coord._lan_properties.pop("data_response")
        receipt = await coord.send_command_lan(b"brew", return_fetch_event=True, brew=True)
        receipt.set()
        await _ready(coord)
        await coord._async_verify_lan_shape(b"brew", receipt)
        assert receipt.ambiguous
        assert server.enqueue_command.await_count == 2

    asyncio.run(scenario())


def test_guard_error_never_delivers():
    def error():
        raise RuntimeError("missing witness")

    receipt = BrewReceipt(False, lambda _receipt: None, error)
    assert not receipt.should_deliver()


def test_successful_alternate_is_persisted_and_restored_per_dsn():
    async def scenario():
        coord, server, api, original = await _setup()
        entry = SimpleNamespace(data={"dsn": "DSN-1", "lan_brew_shapes": {"other": False}})
        coord.config_entry = entry
        coord.hass.config_entries.async_update_entry.side_effect = lambda _entry, *, data: setattr(entry, "data", data)
        await _ready(coord)
        await coord._async_verify_lan_shape(b"brew", original)
        alternate = server.enqueue_command.call_args.kwargs["fetched"]
        alternate.set()
        await coord._on_lan_property({"property": {"name": "data_response", "value": "new-acceptance"}})
        assert entry.data["lan_brew_shapes"] == {"DSN-1": True, "other": False}
        restored = DeLonghiCoordinator(_make_hass(), _make_api(), "DSN-1", config_entry=entry)
        assert restored._lan_brew_shape is True
        coord._active_shape_receipt = None
        api._build_packet.reset_mock()
        await coord.send_command_lan(b"next", brew=True)
        assert api._build_packet.call_args.kwargs["include_app_id"] is True

    asyncio.run(scenario())


@pytest.mark.parametrize("reason", ["brewing", "dead", "rekey", "other_command"])
def test_alternate_is_invalidated_until_it_is_polled(reason):
    async def scenario():
        coord, server, _, original = await _setup()
        await _ready(coord)
        await coord._async_verify_lan_shape(b"brew", original)
        alternate = server.enqueue_command.call_args.kwargs["fetched"]
        assert alternate.should_deliver()
        if reason == "brewing":
            await coord._on_lan_property({"property": {"name": "d302_monitor", "value": LAN_BREWING}})
        elif reason == "dead":
            server.is_session_alive.return_value = False
        elif reason == "rekey":
            server.activity_epoch += 1
        else:
            await coord.send_command_lan(b"settings")
        assert not alternate.should_deliver()

    asyncio.run(scenario())
