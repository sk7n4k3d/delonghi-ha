"""Exercise the EU Aromatic action without contacting a broker or appliance."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiohttp
import pytest

from custom_components.delonghi_daedalus import cloud, warmup, warmup_action, warmup_state
from custom_components.delonghi_daedalus.const import (
    AWS_REST_BASE_URL_PROD,
    CONF_AROMATIC_WARMUP,
    CONF_POOL,
    CONF_SERIAL_NUMBER,
    CONF_SESSION_TOKEN,
    DOMAIN,
    GIGYA_POOL_CH,
    GIGYA_POOL_EU,
)
from custom_components.delonghi_daedalus.device import DaedalusDevice
from custom_components.delonghi_daedalus.warmup import WarmupClient, WarmupError

_SERIAL = "fixture-serial"
_THING = "fixture-machine"
_DEVICE = {"serialNumber": _SERIAL, "machineName": _THING, "machineModel": "ECAM63070"}
_ACCEPTED = cloud.shadow_accepted_topic(_THING, "MachineStatus")
_REJECTED = cloud.shadow_rejected_topic(_THING, "MachineStatus")
_REPLY = f"{_THING}/commands/response"


def test_resolve_only_owned_device_from_eu_endpoint(monkeypatch):
    fetch = AsyncMock(return_value={"ownedByMe": [_DEVICE]})
    monkeypatch.setattr(cloud, "_fetch_devices", fetch)
    session = object()

    assert asyncio.run(warmup.resolve_owned_aromatic(session, serial=_SERIAL, jwt="test-token")) == _THING
    fetch.assert_awaited_once_with(
        session, url=f"{AWS_REST_BASE_URL_PROD}devices", jwt="test-token", allow_redirects=False
    )


@pytest.mark.parametrize(
    "directory",
    [
        {"ownedByMe": [], "sharedToMe": [_DEVICE]},
        {"ownedByMe": [_DEVICE, {**_DEVICE, "machineName": "another-machine"}]},
        {"ownedByMe": [{**_DEVICE, "serialNumber": "other-serial"}]},
        {"ownedByMe": [{**_DEVICE, "machineModel": "ECAM63075"}]},
        {"ownedByMe": [{**_DEVICE, "machineModel": "PrimaDonna Aromatic"}]},
        {"ownedByMe": [{**_DEVICE, "machineName": "a/commands/+"}]},
    ],
)
def test_target_validation_refuses_ambiguous_unsupported_or_shared_device(monkeypatch, directory):
    monkeypatch.setattr(cloud, "_fetch_devices", AsyncMock(return_value=directory))
    with pytest.raises(WarmupError):
        asyncio.run(warmup.resolve_owned_aromatic(Mock(), serial=_SERIAL, jwt="test-token"))


class _Broker:
    def __init__(self, scenario):
        self.scenario = scenario
        self.messages = deque()
        self.commands = []
        self.close = AsyncMock()
        self.status_requested = False

    def _publish(self, topic, body, *, retain=False):
        packet = cloud.mqtt_publish_packet(topic, json.dumps(body), protocol_version=5, packet_id=10)
        if retain:
            packet = bytes([packet[0] | 0x01]) + packet[1:]
        self.messages.append(packet)

    async def receive(self):
        if not self.messages:
            return SimpleNamespace(type=aiohttp.WSMsgType.CLOSE)
        return SimpleNamespace(type=aiohttp.WSMsgType.BINARY, data=self.messages.popleft())

    async def send_bytes(self, data):
        reader = cloud.MqttStreamReader(protocol_version=5)
        reader.feed(data)
        packet = reader.next_packet()
        if packet.type == cloud._SUBSCRIBE:
            self.messages.append(b"\x90\x06\x00\x02\x00\x01\x01\x01")  # Unrelated SUBACK.
            reason = b"\x87" if self.scenario == "subscription_denied" else b"\x01"
            self.messages.append(b"\x90\x06\x00\x01\x00\x01\x01" + reason)
            return
        if packet.type != cloud._PUBLISH:
            return

        topic, text = packet.publish_parts()
        body = json.loads(text)
        if topic == cloud.shadow_get_topic(_THING, "MachineStatus"):
            self.status_requested = True
            status = {"state": {"reported": {"Status": self.scenario}}, "clientToken": body["clientToken"]}
            if self.scenario == "status_rejected":
                self._publish(_REJECTED, status)
            elif self.scenario == "retained":
                self._publish(_ACCEPTED, status, retain=True)
            elif self.scenario == "wrong_nonce":
                self._publish(_ACCEPTED, {**status, "clientToken": "unrelated"})
            elif self.scenario == "wrong_topic":
                self._publish(_ACCEPTED.replace(_THING, "other-machine"), status)
            else:
                if self.scenario in {"ok", "lost", "write_error", "bad_reply", "denied"}:
                    status["state"]["reported"]["Status"] = "Standby"
                self._publish(_ACCEPTED, status)
                self._publish(_ACCEPTED, status)
            return

        assert topic == f"{_THING}/commands/request"
        assert packet.flags == 0x02  # QoS 1, no RETAIN or DUP.
        topic_end = 2 + int.from_bytes(packet.payload[:2], "big")
        assert packet.payload[topic_end : topic_end + 8] == b"\x00\x03\x05\x02\x00\x00\x00\x0f"
        assert {key: body[key] for key in ("AppId", "Message", "TurnOnOff")} == {
            "AppId": "MCL",
            "Message": "MachineUI",
            "TurnOnOff": 2,
        }
        assert len(body["RequestId"]) == 5 and body["RequestId"].isascii() and body["RequestId"].isalnum()
        self.commands.append(body)
        if self.scenario == "write_error":
            raise OSError
        if self.scenario == "lost":
            return
        reply = {"Message": "MachineUIResponse", "RequestId": body["RequestId"], "Response": "OK"}
        self._publish(_REPLY.replace(_THING, "other-machine"), reply)
        self._publish(_REPLY, {**reply, "RequestId": "unrelated"})
        if self.scenario != "bad_reply":
            self._publish(_REPLY, {**reply, "Response": "ERROR"} if self.scenario == "denied" else reply)


@pytest.mark.parametrize("scenario", ["ok", "Standby"])
def test_warmup_publishes_once_after_correlated_resting_status(scenario):
    broker = _Broker(scenario)
    client = WarmupClient(Mock(), thing_name=_THING, jwt="test-token")
    client._connect = AsyncMock(return_value=(broker, 5))
    marker = Mock(return_value=None)

    asyncio.run(client.warm_up(before_send=marker))

    assert broker.status_requested
    assert len(broker.commands) == 1
    marker.assert_called_once()
    broker.close.assert_awaited_once()


@pytest.mark.parametrize(
    "scenario",
    ["subscription_denied", "status_rejected", "retained", "wrong_nonce", "wrong_topic", "Heating", "Idle", "Off", 2],
)
def test_warmup_does_not_send_when_readiness_is_unverified(scenario):
    broker = _Broker(scenario)
    client = WarmupClient(Mock(), thing_name=_THING, jwt="test-token")
    client._connect = AsyncMock(return_value=(broker, 5))
    marker = Mock(return_value=None)

    with pytest.raises(WarmupError):
        asyncio.run(client.warm_up(before_send=marker))

    assert not broker.commands
    marker.assert_not_called()
    broker.close.assert_awaited_once()
    if scenario == "subscription_denied":
        assert not broker.status_requested


@pytest.mark.parametrize("scenario", ["lost", "write_error", "bad_reply", "denied"])
def test_warmup_never_resends_after_an_unconfirmed_command(scenario):
    broker = _Broker(scenario)
    client = WarmupClient(Mock(), thing_name=_THING, jwt="test-token")
    client._connect = AsyncMock(return_value=(broker, 5))

    with pytest.raises(WarmupError, match="Check the machine|Check it"):
        asyncio.run(client.warm_up(before_send=Mock(return_value=None)))

    assert len(broker.commands) == 1
    client._connect.assert_awaited_once()
    broker.close.assert_awaited_once()


@pytest.fixture
def service(monkeypatch):
    entry = SimpleNamespace(
        domain=DOMAIN,
        entry_id="entry-fixture",
        disabled_by=None,
        state=warmup_action.ConfigEntryState.LOADED,
        data={
            CONF_AROMATIC_WARMUP: True,
            CONF_POOL: GIGYA_POOL_EU,
            CONF_SERIAL_NUMBER: _SERIAL,
            CONF_SESSION_TOKEN: "test-owner-session",
        },
    )
    config_entries = Mock()
    config_entries.async_reload = AsyncMock()
    config_entries.async_get_entry.side_effect = lambda entry_id: entry if entry_id == entry.entry_id else None
    config_entries.async_entries.return_value = [entry]
    config_entries.async_update_entry.side_effect = lambda target, data: setattr(target, "data", data)
    auth = SimpleNamespace(
        async_get_user=AsyncMock(side_effect=lambda user_id: SimpleNamespace(is_admin=user_id == "admin"))
    )
    hass = SimpleNamespace(data={}, config_entries=config_entries, services=Mock(), auth=auth)
    api = SimpleNamespace(refresh_jwt=AsyncMock(return_value="test-jwt"), http_session=object(), close=AsyncMock())
    monkeypatch.setattr(warmup_action, "DaedalusApi", Mock(return_value=api))
    resolve = AsyncMock(return_value=_THING)
    monkeypatch.setattr(warmup_action, "resolve_owned_aromatic", resolve)
    client = Mock(verify_firmware=AsyncMock(), warm_up=AsyncMock())
    monkeypatch.setattr(warmup_action, "WarmupClient", Mock(return_value=client))

    disk = {}

    async def write_marker(key, marker):
        disk[key] = marker

    persist = AsyncMock(side_effect=write_marker)

    class FakeStore:
        def __init__(self, hass, version, key, *, atomic_writes):
            assert atomic_writes
            self.key = key

        async def async_load(self):
            return disk.get(self.key)

        async def async_save(self, marker):
            await persist(self.key, marker)

    monkeypatch.setattr(warmup_state, "Store", FakeStore)

    def register_admin(hass, domain, name, handler, *, schema):
        # Mirror the native HA wrapper's user/admin gate in the offline harness.
        async def guarded(call):
            if call.context.user_id is not None:
                user = await hass.auth.async_get_user(call.context.user_id)
                if not user or not user.is_admin:
                    raise PermissionError("admin required")
            return await handler(call)

        hass.services.async_register(domain, name, guarded, schema=schema)

    admin_register = Mock(side_effect=register_admin)
    monkeypatch.setattr(warmup_action, "async_register_admin_service", admin_register)
    warmup_action.register_warmup_action(hass)
    call = hass.services.async_register.call_args
    admin_register.assert_called_once()
    assert call.args[:2] == (DOMAIN, "warm_up")
    return SimpleNamespace(
        entry=entry,
        hass=hass,
        resolve=resolve,
        client=client,
        handler=call.args[2],
        schema=call.kwargs["schema"],
        disk=disk,
        persist=persist,
        storage_key=f"{DOMAIN}.warmup.{entry.entry_id}",
    )


def _call(service, entry_id=None, *, user_id=None):
    return SimpleNamespace(
        data={"config_entry_id": entry_id or service.entry.entry_id}, context=SimpleNamespace(user_id=user_id)
    )


def _has_pending(service):
    return service.disk.get(service.storage_key, {}).get("pending") is True


def test_service_requires_an_explicit_single_entry(service):
    import voluptuous as vol

    for invalid in ({}, {"config_entry_id": ["entry-fixture"]}, {"config_entry_id": "", "device_id": "other"}):
        with pytest.raises(vol.Invalid):
            service.schema(invalid)

    with pytest.raises(warmup_action.HomeAssistantError, match="loaded"):
        asyncio.run(service.handler(_call(service, entry_id="other-entry")))

    service.resolve.assert_not_called()


@pytest.mark.parametrize("blocked_by", ["region", "opt-in", "duplicate", "disabled", "unloaded"])
def test_service_rejects_ineligible_entry_before_any_network_work(service, blocked_by):
    if blocked_by == "region":
        service.entry.data[CONF_POOL] = GIGYA_POOL_CH
    if blocked_by == "opt-in":
        service.entry.data[CONF_AROMATIC_WARMUP] = False
    if blocked_by == "duplicate":
        service.hass.config_entries.async_entries.return_value.append(
            SimpleNamespace(data={CONF_SERIAL_NUMBER: _SERIAL.upper()})
        )
    if blocked_by == "disabled":
        service.entry.disabled_by = "user"
    if blocked_by == "unloaded":
        service.entry.state = warmup_action.ConfigEntryState.NOT_LOADED
    with pytest.raises(warmup_action.HomeAssistantError):
        asyncio.run(service.handler(_call(service)))
    service.resolve.assert_not_called()


def test_uncertain_delivery_remains_blocked_for_later_calls(service):
    async def lose_reply(*, before_send):
        await before_send()
        raise WarmupError("Warm-up delivery is uncertain.")

    service.client.warm_up.side_effect = lose_reply
    with pytest.raises(warmup_action.HomeAssistantError, match="uncertain"):
        asyncio.run(service.handler(_call(service)))
    assert _has_pending(service)

    # A new HA instance has no cached locks or pending state and reads disk.
    service.hass.data = {}
    warmup_action.register_warmup_action(service.hass)
    handler = service.hass.services.async_register.call_args.args[2]
    with pytest.raises(warmup_action.HomeAssistantError, match="previous warm-up"):
        asyncio.run(handler(_call(service)))

    service.resolve.assert_awaited_once()
    service.client.warm_up.assert_awaited_once()


def test_service_deduplicates_concurrent_and_immediately_repeated_calls(service):
    async def exercise():
        started = asyncio.Event()
        finish = asyncio.Event()

        async def deliver(*, before_send):
            started.set()
            await finish.wait()
            await before_send()

        service.client.warm_up.side_effect = deliver
        first = asyncio.create_task(service.handler(_call(service)))
        await started.wait()
        with pytest.raises(warmup_action.HomeAssistantError, match="in progress"):
            await service.handler(_call(service))
        finish.set()
        await first
        assert not _has_pending(service)
        with pytest.raises(warmup_action.HomeAssistantError, match="just sent"):
            await service.handler(_call(service))

    asyncio.run(exercise())
    service.client.warm_up.assert_awaited_once()


def test_service_revalidates_after_target_entry_is_disabled(service):
    async def disable_before_send(*, before_send):
        service.entry.disabled_by = "user"
        await before_send()

    service.client.warm_up.side_effect = disable_before_send

    with pytest.raises(warmup_action.HomeAssistantError, match="entry changed"):
        asyncio.run(service.handler(_call(service)))

    assert not _has_pending(service)


def test_service_preserves_pending_if_cancelled_after_attempt(service):
    async def exercise():
        started = asyncio.Event()

        async def deliver(*, before_send):
            await before_send()
            started.set()
            await asyncio.Future()

        service.client.warm_up.side_effect = deliver
        first = asyncio.create_task(service.handler(_call(service)))
        await started.wait()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        with pytest.raises(warmup_action.HomeAssistantError, match="previous warm-up"):
            await service.handler(_call(service))

    asyncio.run(exercise())
    service.client.warm_up.assert_awaited_once()


def test_reset_requires_a_deliberate_opt_out_and_reenable(service):
    from custom_components.delonghi_daedalus.config_flow import DaedalusOptionsFlow

    entry = service.entry
    other_key = f"{DOMAIN}.warmup.other-entry"
    service.disk[service.storage_key] = {"pending": True}
    service.disk[other_key] = {"pending": True}
    flow = DaedalusOptionsFlow(entry)
    flow.hass = service.hass
    asyncio.run(flow.async_step_init({CONF_AROMATIC_WARMUP: True}))
    assert _has_pending(service)

    asyncio.run(flow.async_step_init({CONF_AROMATIC_WARMUP: False}))
    assert _has_pending(service)
    assert entry.data[CONF_AROMATIC_WARMUP] is False

    asyncio.run(flow.async_step_init({CONF_AROMATIC_WARMUP: True}))
    assert not _has_pending(service)
    assert service.disk[other_key]["pending"] is True
    assert entry.data[CONF_AROMATIC_WARMUP] is True
    assert service.hass.config_entries.async_reload.await_count == 2


@pytest.mark.parametrize("changed", [None, "FWUIVersion", "FWMainboardVersion", "FWWiFiVersion"])
def test_warmup_requires_each_physically_verified_firmware_version(changed):
    firmware = {"FWUIVersion": "1.2.0", "FWMainboardVersion": "1.2.0", "FWWiFiVersion": "2.5.3"}
    client = WarmupClient(Mock(), thing_name=_THING, jwt="test-token")
    client.fetch = AsyncMock(return_value={"MachineCapabilities": {"state": {"reported": firmware}}})
    if changed is None:
        asyncio.run(client.verify_firmware())
    else:
        firmware[changed] = "0.0.0"
        with pytest.raises(WarmupError, match=r"Warm-up requires UI 1\.2\.0.*No warm-up command was sent"):
            asyncio.run(client.verify_firmware())
    client.fetch.assert_awaited_once_with(timeout=10)


def test_firmware_check_reports_missing_capabilities_on_partial_fetch():
    client = WarmupClient(Mock(), thing_name=_THING, jwt="test-token")
    client.fetch = AsyncMock(return_value={"MachineStatus": {"state": {"reported": {"Status": "Standby"}}}})
    with pytest.raises(WarmupError, match="no MachineCapabilities document.*No warm-up command was sent"):
        asyncio.run(client.verify_firmware())


def test_firmware_check_reports_broker_failure_separately():
    client = WarmupClient(Mock(), thing_name=_THING, jwt="test-token")
    client.fetch = AsyncMock(side_effect=cloud.CloudTelemetryError("MQTT session failed"))
    with pytest.raises(WarmupError, match="Could not read the machine firmware.*No warm-up command was sent"):
        asyncio.run(client.verify_firmware())


def test_service_never_calls_command_when_firmware_is_unverified(service):
    service.client.verify_firmware.side_effect = WarmupError("Unexpected firmware.")
    with pytest.raises(warmup_action.HomeAssistantError, match="Unexpected firmware"):
        asyncio.run(service.handler(_call(service)))
    service.client.warm_up.assert_not_awaited()
    assert not _has_pending(service)


def test_non_admin_manual_call_is_denied_before_network_or_storage(service):
    with pytest.raises(PermissionError, match="admin required"):
        asyncio.run(service.handler(_call(service, user_id="non-admin")))
    service.resolve.assert_not_awaited()
    service.persist.assert_not_awaited()


@pytest.mark.parametrize("user_id", ["admin", None])
def test_admin_manual_call_and_home_assistant_automation_are_allowed(service, user_id):
    async def send(*, before_send):
        await before_send()
        assert _has_pending(service)

    service.client.warm_up.side_effect = send
    asyncio.run(service.handler(_call(service, user_id=user_id)))
    service.client.warm_up.assert_awaited_once()
    assert not _has_pending(service)


@pytest.mark.parametrize("silent_failure", [True, False])
def test_failed_write_prevents_the_command(service, silent_failure):
    sent = Mock()

    async def send(*, before_send):
        await before_send()
        sent()

    service.client.warm_up.side_effect = send
    service.persist.side_effect = None if silent_failure else OSError("disk unavailable")
    with pytest.raises(warmup_action.HomeAssistantError, match="Could not save"):
        asyncio.run(service.handler(_call(service)))

    sent.assert_not_called()
    assert not _has_pending(service)


def test_firmware_preflight_then_wire_awaits_the_reservation():
    async def exercise():
        broker = _Broker("Standby")
        client = WarmupClient(Mock(), thing_name=_THING, jwt="test-token")
        client._connect = AsyncMock(return_value=(broker, 5))
        writing = asyncio.Event()
        saved = asyncio.Event()

        async def reserve():
            writing.set()
            await saved.wait()

        action = asyncio.create_task(client.warm_up(before_send=reserve))
        await writing.wait()
        assert not broker.commands
        saved.set()
        await action
        assert len(broker.commands) == 1

    asyncio.run(exercise())


def test_unload_during_the_storage_write_prevents_send(service):
    sent = Mock()

    async def save_and_unload(key, marker):
        service.disk[key] = marker
        service.entry.state = warmup_action.ConfigEntryState.NOT_LOADED

    async def send(*, before_send):
        await before_send()
        sent()

    service.persist.side_effect = save_and_unload
    service.client.warm_up.side_effect = send
    with pytest.raises(warmup_action.HomeAssistantError, match="entry changed"):
        asyncio.run(service.handler(_call(service)))

    sent.assert_not_called()
    assert _has_pending(service)


def test_cancellation_during_write_keeps_lock_until_storage_finishes(service):
    async def exercise():
        started = asyncio.Event()
        release = asyncio.Event()
        sent = Mock()

        async def write_marker(key, marker):
            started.set()
            await release.wait()
            service.disk[key] = marker

        async def send(*, before_send):
            await before_send()
            sent()

        service.persist.side_effect = write_marker
        service.client.warm_up.side_effect = send

        action = asyncio.create_task(service.handler(_call(service)))
        await started.wait()
        action.cancel()
        await asyncio.sleep(0)
        assert not action.done()
        assert warmup_state.entry_lock(service.hass, service.entry.entry_id).locked()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await action

        sent.assert_not_called()
        assert _has_pending(service)
        assert not warmup_state.entry_lock(service.hass, service.entry.entry_id).locked()

    asyncio.run(exercise())


def test_failed_reset_keeps_warmup_disabled(service):
    from custom_components.delonghi_daedalus.config_flow import DaedalusOptionsFlow

    entry = service.entry
    entry.data[CONF_AROMATIC_WARMUP] = False
    service.disk[service.storage_key] = {"pending": True}
    service.persist.side_effect = OSError("disk unavailable")
    flow = DaedalusOptionsFlow(entry)
    flow.hass = service.hass
    result = asyncio.run(flow.async_step_init({CONF_AROMATIC_WARMUP: True}))

    assert result["errors"]["base"] == "warmup_storage_error"
    assert entry.data[CONF_AROMATIC_WARMUP] is False
    assert _has_pending(service)


def _button(service):
    from custom_components.delonghi_daedalus import button

    entry = service.entry
    coordinator = SimpleNamespace(entry=entry, serial_number=entry.data.get(CONF_SERIAL_NUMBER), data={})
    coordinator.device = DaedalusDevice(coordinator)
    service.hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    add = Mock()
    asyncio.run(button.async_setup_entry(service.hass, entry, add))
    return add.call_args.args[0][0] if add.called else None


def test_button_tracks_opt_in_on_reload_and_uses_machine_device(service):
    from hashlib import sha256

    from custom_components.delonghi_daedalus.config_flow import DaedalusOptionsFlow

    entry = service.entry
    entry.data[CONF_AROMATIC_WARMUP] = False
    assert _button(service) is None

    flow = DaedalusOptionsFlow(entry)
    flow.hass = service.hass
    asyncio.run(flow.async_step_init({CONF_AROMATIC_WARMUP: True}))
    service.hass.config_entries.async_reload.assert_awaited_once_with(entry.entry_id)
    button = _button(service)
    assert button.available
    assert button._attr_unique_id == f"{_SERIAL}_warm_up"
    assert button._attr_device_info["identifiers"] == {(DOMAIN, sha256(f"{DOMAIN}:{_SERIAL}".encode()).hexdigest())}

    asyncio.run(flow.async_step_init({CONF_AROMATIC_WARMUP: False}))
    assert service.hass.config_entries.async_reload.await_count == 2
    assert not button.available
    assert _button(service) is None
    service.client.warm_up.assert_not_awaited()


@pytest.mark.parametrize("excluded", ["duplicate", "disabled", "no_session", "non_eu"])
def test_button_requires_one_enabled_eu_target(service, excluded):
    if excluded == "duplicate":
        duplicate = SimpleNamespace(data={CONF_SERIAL_NUMBER: _SERIAL.lower()})
        service.hass.config_entries.async_entries.return_value = [service.entry, duplicate]
    elif excluded == "disabled":
        service.entry.disabled_by = "user"
    elif excluded == "no_session":
        service.entry.data.pop(CONF_SESSION_TOKEN)
    elif excluded == "non_eu":
        service.entry.data[CONF_POOL] = "CH"
    assert _button(service) is None
    service.client.warm_up.assert_not_awaited()


def test_button_delegates_with_same_context_and_admin_gate_without_second_send(service):
    button = _button(service)
    button.hass = service.hass
    button.enabled = True

    async def forward(domain, action, data, *, blocking, context):
        assert (domain, action) == (DOMAIN, "warm_up")
        assert blocking and context is button._context
        assert data == {"config_entry_id": service.entry.entry_id}
        await service.handler(SimpleNamespace(data=data, context=context))

    async def deliver(*, before_send):
        await before_send()

    button.hass.services.async_call = AsyncMock(side_effect=forward)
    service.client.warm_up.side_effect = deliver

    button._context = SimpleNamespace(user_id="nonadmin")
    with pytest.raises(PermissionError, match="admin"):
        asyncio.run(button.async_press())
    service.client.warm_up.assert_not_awaited()

    button._context = SimpleNamespace(user_id="admin")
    asyncio.run(button.async_press())
    service.client.verify_firmware.assert_awaited_once()
    service.resolve.assert_awaited_once()
    service.client.warm_up.assert_awaited_once()

    with pytest.raises(warmup_action.HomeAssistantError, match="just sent"):
        asyncio.run(button.async_press())
    service.client.warm_up.assert_awaited_once()
    assert button.hass.services.async_call.await_count == 3


@pytest.mark.parametrize("context_present", [False, True])
def test_button_does_not_delegate_without_context_or_when_entity_disabled(service, context_present):
    button = _button(service)
    button.hass = service.hass
    button.hass.services.async_call = AsyncMock()
    button.enabled = not context_present
    button._context = SimpleNamespace(user_id="admin") if context_present else None
    with pytest.raises(warmup_action.HomeAssistantError, match="unavailable"):
        asyncio.run(button.async_press())
    button.hass.services.async_call.assert_not_awaited()
    service.client.warm_up.assert_not_awaited()
