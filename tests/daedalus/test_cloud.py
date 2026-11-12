"""Cloud telemetry layer tests: REST parsing, MQTT codec, shadow client."""

from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp import web

from custom_components.delonghi_daedalus.cloud import (
    CloudTelemetryError,
    MqttStreamReader,
    ShadowClient,
    _shadow_from_topic,
    extract_machine_name,
    fetch_cloud_state,
    flatten_shadow,
    mqtt_connect_packet,
    mqtt_publish_packet,
    mqtt_subscribe_packet,
    shadow_accepted_topic,
)

# ---------------------------------------------------------- /devices parsing --


def test_extract_machine_name_bare_list() -> None:
    payload = [
        {"serialNumber": "AAA", "machineName": "m-aaa"},
        {"serialNumber": "SN1234", "machineName": "thing-42"},
    ]
    assert extract_machine_name(payload, "SN1234") == "thing-42"


def test_extract_machine_name_wrapper_and_key_variants() -> None:
    payload = {"devices": [{"serial_no": "sn1234", "thing_name": "t1"}]}
    assert extract_machine_name(payload, "SN1234") == "t1"


def test_extract_machine_name_not_found_raises() -> None:
    with pytest.raises(CloudTelemetryError, match="not found"):
        extract_machine_name([{"serialNumber": "other"}], "SN1234")


def test_extract_machine_name_bad_shape_raises() -> None:
    with pytest.raises(CloudTelemetryError):
        extract_machine_name({"unexpected": "shape"}, "SN1234")


# --------------------------------------------------------------- MQTT codec --


def test_connect_packet_flag_bits() -> None:
    pkt = mqtt_connect_packet(client_id="c1")
    assert pkt[0] == 0x10
    # fixed header (2 bytes) + protocol name (6) + level (1) → flags at 9
    assert pkt[9] == 0x02
    with_creds = mqtt_connect_packet(client_id="c1", username="u", password="p")
    assert with_creds[9] == 0x02 | 0x80 | 0x40
    assert b"u" in with_creds and b"p" in with_creds


def test_subscribe_and_publish_roundtrip() -> None:
    reader = MqttStreamReader()
    reader.feed(mqtt_subscribe_packet(1, ["a/b", "c/d"]))
    pkt = reader.next_packet()
    assert pkt is not None and pkt.type == 8
    reader.feed(mqtt_publish_packet("topic/x", '{"a":1}'))
    pkt = reader.next_packet()
    assert pkt is not None and pkt.type == 3
    assert pkt.publish_parts() == ("topic/x", '{"a":1}')


def test_reader_handles_fragmented_and_coalesced_frames() -> None:
    blob = mqtt_publish_packet("t", "1") + mqtt_publish_packet("t2", "2")
    reader = MqttStreamReader()
    out = []
    for i in range(len(blob)):
        reader.feed(blob[i : i + 1])  # one byte at a time
        while (pkt := reader.next_packet()) is not None:
            out.append(pkt)
    assert len(out) == 2
    assert out[0].publish_parts()[0] == "t"
    assert out[1].publish_parts()[1] == "2"


def test_reader_rejects_burst_of_continuation_bytes() -> None:
    """A hostile/buggy stream must not grow the buffer unbounded: a header
    claiming a huge remaining length stays None until it is capped."""
    reader = MqttStreamReader()
    reader.feed(bytes([0x30, 0xFF, 0xFF, 0xFF, 0xFF]))
    with pytest.raises(CloudTelemetryError):
        reader.next_packet()


def test_reader_caps_assembly_buffer() -> None:
    """Review #18 F4: feed() itself must refuse past 1 MiB — a broker (or a
    MITM on a plain-ws test seam) streaming endless fragments cannot OOM HA."""
    reader = MqttStreamReader()
    chunk = b"x" * (1 << 16)
    with pytest.raises(CloudTelemetryError, match="overflow"):
        for _ in range(32):  # 32 * 64 KiB = 2 MiB > cap
            reader.feed(chunk)


def test_shadow_topic_parsing() -> None:
    assert _shadow_from_topic("$aws/things/thing1/shadow/name/MachineStatus/get/accepted") == "MachineStatus"
    assert _shadow_from_topic("$aws/things/thing1/shadow/name/Other/get") is None
    assert _shadow_from_topic("garbage") is None


# ------------------------------------------------------------ flatten_shadow --


def test_flatten_shadow_reported() -> None:
    doc = {"state": {"reported": {"machineState": "Brewing", "tanks": {"water": 42}, "alarms": [1, 2]}}}
    flat = flatten_shadow(doc)
    assert flat["machineState"] == "Brewing"
    assert flat["tanks.water"] == 42
    assert flat["alarms"] == "[1,2]"


def test_flatten_shadow_without_reported() -> None:
    flat = flatten_shadow({"a": {"b": True}})
    assert flat == {"a.b": True}


def test_flatten_shadow_caps_entries() -> None:
    doc = {f"k{i}": i for i in range(200)}
    assert len(flatten_shadow(doc, max_entries=80)) == 80


def test_flatten_shadow_depth_capped() -> None:
    """Review #18 F3: nesting deeper than max_depth is stringified whole
    instead of recursing — no RecursionError path into the coordinator."""
    doc: dict = {"leaf": 1}
    for _ in range(10):
        doc = {"n": doc}
    flat = flatten_shadow(doc)
    assert list(flat) == ["n.n.n.n.n.n.n.n"]  # 8 segments = max_depth
    assert '"leaf"' in flat["n.n.n.n.n.n.n.n"]


def test_flatten_shadow_survives_pathological_depth() -> None:
    """A 150-deep document (near what json.loads itself can produce) must
    still flatten to one entry, not blow the stack."""
    doc: dict = {"leaf": 1}
    for _ in range(150):
        doc = {"n": doc}
    flat = flatten_shadow(doc)
    assert len(flat) == 1


# --------------------------------------------------------- ShadowClient e2e --


def _mqtt_server(reject_usernames: bool = False, reject_all: bool = False) -> tuple[web.Application, dict]:
    """Minimal MQTT-over-WS broker stub: CONNACK, SUBACK, then one accepted
    PUBLISH per shadow get."""
    seen: dict[str, object] = {"connects": []}

    app = web.Application()

    async def handler(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(autoping=False)
        await ws.prepare(request)
        reader = MqttStreamReader()
        shadows_answered = 0
        async for msg in ws:
            if msg.type != web.WSMsgType.BINARY:
                continue
            reader.feed(msg.data)
            while (pkt := reader.next_packet()) is not None:
                if pkt.type == 1:  # CONNECT
                    body = pkt.payload
                    # username/password presence via flags
                    flags = body[7]
                    seen["connects"].append(bool(flags & 0x80))
                    if reject_all:
                        rc = 5
                    else:
                        rc = 5 if (reject_usernames and flags & 0x80) else 0
                    await ws.send_bytes(bytes([0x20, 0x02, 0x00, rc]))
                elif pkt.type == 8:  # SUBSCRIBE → SUBACK
                    pid = pkt.payload[:2]
                    await ws.send_bytes(bytes([0x90, 0x03]) + pid + bytes([0x00]))
                elif pkt.type == 3:  # PUBLISH get → answer accepted
                    topic, _ = pkt.publish_parts()
                    if topic.endswith("/get"):
                        shadow = topic.split("/")[5]
                        doc = {"state": {"reported": {"shadow": shadow}}}
                        await ws.send_bytes(
                            mqtt_publish_packet(shadow_accepted_topic("thing1", shadow), json.dumps(doc))
                        )
                        shadows_answered += 1
        return ws

    app.router.add_get("/mqtt", handler)
    return app, seen


def test_shadow_client_fetches_both_shadows() -> None:
    async def _run() -> None:
        app, seen = _mqtt_server()
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        session = aiohttp_session()
        try:
            client = ShadowClient(session, thing_name="thing1", jwt="jwt", broker="127.0.0.1", scheme="ws", port=port)
            docs = await client.fetch(timeout=5)
            assert docs["MachineStatus"]["state"]["reported"]["shadow"] == "MachineStatus"
            assert docs["MachineCapabilities"]["state"]["reported"]["shadow"] == "MachineCapabilities"
            assert seen["connects"] == [True]  # first attempt used the username form
        finally:
            await session.close()
            await runner.cleanup()

    asyncio.run(_run())


def test_shadow_client_retries_with_password_form() -> None:
    async def _run() -> None:
        app, seen = _mqtt_server(reject_usernames=True)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        session = aiohttp_session()
        try:
            client = ShadowClient(session, thing_name="thing1", jwt="jwt", broker="127.0.0.1", scheme="ws", port=port)
            docs = await client.fetch(timeout=5)
            assert "MachineStatus" in docs
            # first (username) refused, second (password) accepted
            assert seen["connects"] == [True, False]
        finally:
            await session.close()
            await runner.cleanup()

    asyncio.run(_run())


def test_shadow_client_raises_when_broker_refuses_jwt() -> None:
    async def _run() -> None:
        app, seen = _mqtt_server(reject_all=True)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        session = aiohttp_session()
        try:
            client = ShadowClient(session, thing_name="thing1", jwt="jwt", broker="127.0.0.1", scheme="ws", port=port)
            with pytest.raises(CloudTelemetryError, match="refused the JWT"):
                await client.fetch(timeout=2)
            # both credential spellings tried, both refused
            assert seen["connects"] == [True, False]
        finally:
            await session.close()
            await runner.cleanup()

    asyncio.run(_run())


def aiohttp_session():  # noqa: ANN201 — tiny helper
    import aiohttp

    return aiohttp.ClientSession()


# --- audit #18 (blue-team): thing-name validation ---------------------------


def test_fetch_cloud_state_rejects_hostile_thing_name() -> None:
    """Audit #18 C1: a machineName from /devices (or entry.data) containing
    wildcards, CRLF or absurd length must never reach the MQTT topics."""
    import aiohttp

    async def _run(bad: str) -> None:
        session = aiohttp.ClientSession()
        try:
            with pytest.raises(CloudTelemetryError, match="shape check"):
                await fetch_cloud_state(session, serial="SN1", jwt="jwt", machine_name=bad)
        finally:
            await session.close()

    for bad in ("thing#all", "a\r\nb", "x" * 70000, "   "):
        asyncio.run(_run(bad))


def test_fetch_cloud_state_accepts_normal_thing_name() -> None:
    """The shape check must not reject legitimate AWS IoT names."""
    from unittest.mock import AsyncMock, patch

    async def _run() -> None:
        import aiohttp

        session = aiohttp.ClientSession()
        try:
            with patch("custom_components.delonghi_daedalus.cloud.ShadowClient") as client_cls:
                client_cls.return_value.fetch = AsyncMock(return_value={"MachineStatus": {}})
                docs, thing = await fetch_cloud_state(session, serial="SN1", jwt="jwt", machine_name="Eletta-Ultra_01")
                assert thing == "Eletta-Ultra_01"
                assert docs == {"MachineStatus": {}}
        finally:
            await session.close()

    asyncio.run(_run())
