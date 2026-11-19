"""Cloud telemetry layer tests: REST parsing, MQTT codec, shadow client."""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from aiohttp import web

from custom_components.delonghi_daedalus import cloud
from custom_components.delonghi_daedalus.cloud import (
    CloudTelemetryError,
    MqttPacket,
    MqttStreamReader,
    ShadowClient,
    _shadow_from_topic,
    extract_machine_name,
    fetch_brewing_statistics,
    fetch_cloud_state,
    flatten_shadow,
    lookup_machine_name,
    mqtt_connect_packet,
    mqtt_publish_packet,
    mqtt_subscribe_packet,
    shadow_accepted_topic,
)
from custom_components.delonghi_daedalus.const import (
    AWS_REST_BASE_URL_LEGACY_PROD,
    AWS_REST_BASE_URL_PROD,
    AWS_REST_SOURCE_HEADER,
)

# Wire literals repeated here on purpose (not imported from the module): these
# are the contract mimasek71 captured on the Eletta Ultra, and a test that
# imports them from the code under test could not catch a regression.
_APP_AUTHORIZER = "x-amz-customauthorizer-name=dlg-prod-token-authorizer"
_APP_WS_PATH = f"/mqtt?{_APP_AUTHORIZER}"
# RFC 6455 subprotocols the MQTT-over-WS upgrade must offer: the AWS IoT ATS
# refuses the upgrade itself (HTTP 426) when none is present.
_WS_SUBPROTOCOLS = ("mqttv5", "mqtt")
# Shaped like a real Gigya JWT so the redaction patterns actually fire.
FAKE_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyLTEifQ.ZmFrZXNpZ25hdHVyZTEyMzQ"


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


def test_extract_machine_name_grouped_owned_by_me() -> None:
    """Issue #18: the live answer is grouped under ownedByMe / sharedToMe."""
    payload = {
        "ownedByMe": [
            {"serialNumber": "AAA", "machineName": "m-aaa"},
            {"serialNumber": "SN1234", "machineName": "Eletta-Ultra_01"},
        ],
        "sharedToMe": [],
    }
    assert extract_machine_name(payload, "SN1234") == "Eletta-Ultra_01"


def test_extract_machine_name_grouped_shared_to_me() -> None:
    payload = {
        "ownedByMe": [{"serialNumber": "AAA", "machineName": "m-aaa"}],
        "sharedToMe": [{"serialNumber": "sn1234", "thingName": "shared-thing"}],
    }
    assert extract_machine_name(payload, "SN1234") == "shared-thing"


def test_extract_machine_name_grouped_beats_wrapper_keys() -> None:
    """A grouped body must not be mistaken for one of the older wrappers."""
    payload = {"ownedByMe": [{"serialNumber": "SN1234", "machineName": "t1"}], "data": "not-a-list"}
    assert extract_machine_name(payload, "SN1234") == "t1"


def test_extract_machine_name_grouped_but_absent_raises() -> None:
    with pytest.raises(CloudTelemetryError, match="not found"):
        extract_machine_name({"ownedByMe": [], "sharedToMe": [{"serialNumber": "zzz"}]}, "SN1234")


def test_extract_machine_name_not_found_raises() -> None:
    with pytest.raises(CloudTelemetryError, match="not found"):
        extract_machine_name([{"serialNumber": "other"}], "SN1234")


def test_extract_machine_name_bad_shape_raises() -> None:
    with pytest.raises(CloudTelemetryError):
        extract_machine_name({"unexpected": "shape"}, "SN1234")


# ------------------------------------------------------------------ REST hop --


def _devices_server(routes: dict[str, tuple[int, str]]) -> tuple[web.Application, list[dict]]:
    """Stub device directory: first matching path prefix wins, everything else 404."""
    seen: list[dict] = []
    app = web.Application()

    async def handler(request: web.Request) -> web.Response:
        seen.append({"path": request.path, "headers": dict(request.headers)})
        status, body = next((value for prefix, value in routes.items() if request.path.startswith(prefix)), (404, "{}"))
        return web.Response(status=status, text=body)

    app.router.add_route("*", "/{tail:.*}", handler)
    return app, seen


def _grouped(serial: str, name: str) -> str:
    return json.dumps({"ownedByMe": [{"serialNumber": serial, "machineName": name}], "sharedToMe": []})


def test_rest_constants_match_the_working_capture() -> None:
    assert AWS_REST_BASE_URL_PROD == "https://8q8c9xktb0.execute-api.eu-central-1.amazonaws.com/dlg-prod/"
    assert AWS_REST_BASE_URL_LEGACY_PROD == "https://bm5vp76k69.execute-api.eu-central-1.amazonaws.com/dlg-prod/"
    assert AWS_REST_SOURCE_HEADER == "mycoffeelounge"


def test_lookup_machine_name_defaults_to_both_stages() -> None:
    """The fallback must stay wired by default, not only when a caller asks."""
    signature = inspect.signature(lookup_machine_name)
    assert signature.parameters["base_url"].default is AWS_REST_BASE_URL_PROD
    assert signature.parameters["fallback_base_url"].default is AWS_REST_BASE_URL_LEGACY_PROD


def test_lookup_machine_name_sends_bearer_and_source_header() -> None:
    async def _run() -> None:
        app, seen = _devices_server({"/new": (200, _grouped("SN1234", "thing-42"))})
        runner, port = await _serve(app)
        root = f"http://127.0.0.1:{port}"
        session = aiohttp_session()
        try:
            name = await lookup_machine_name(
                session, serial="SN1234", jwt="jwt", base_url=f"{root}/new", fallback_base_url=f"{root}/legacy"
            )
            assert name == "thing-42"
            assert [entry["path"] for entry in seen] == ["/new/devices"]
            headers = seen[0]["headers"]
            assert headers["Authorization"] == "Bearer jwt"
            assert headers["source"] == "mycoffeelounge"
        finally:
            await session.close()
            await runner.cleanup()

    asyncio.run(_run())


def test_lookup_machine_name_falls_back_to_legacy_stage_on_403() -> None:
    async def _run() -> None:
        app, seen = _devices_server(
            {
                "/new": (403, '{"message":"Forbidden"}'),
                "/legacy": (200, _grouped("SN1234", "thing-42")),
            }
        )
        runner, port = await _serve(app)
        root = f"http://127.0.0.1:{port}"
        session = aiohttp_session()
        try:
            name = await lookup_machine_name(
                session, serial="SN1234", jwt="jwt", base_url=f"{root}/new", fallback_base_url=f"{root}/legacy"
            )
            assert name == "thing-42"
            assert [entry["path"] for entry in seen] == ["/new/devices", "/legacy/devices"]
        finally:
            await session.close()
            await runner.cleanup()

    asyncio.run(_run())


def test_lookup_machine_name_does_not_retry_a_valid_answer() -> None:
    """A 200 that simply lacks the serial is an answer, not a transport failure."""

    async def _run() -> None:
        app, seen = _devices_server({"/new": (200, _grouped("other", "t"))})
        runner, port = await _serve(app)
        root = f"http://127.0.0.1:{port}"
        session = aiohttp_session()
        try:
            with pytest.raises(CloudTelemetryError, match="not found"):
                await lookup_machine_name(
                    session, serial="SN1234", jwt="jwt", base_url=f"{root}/new", fallback_base_url=f"{root}/legacy"
                )
            assert [entry["path"] for entry in seen] == ["/new/devices"]
        finally:
            await session.close()
            await runner.cleanup()

    asyncio.run(_run())


def test_lookup_machine_name_reports_every_failed_stage() -> None:
    async def _run() -> None:
        app, seen = _devices_server({"/new": (403, "{}"), "/legacy": (502, "{}")})
        runner, port = await _serve(app)
        root = f"http://127.0.0.1:{port}"
        session = aiohttp_session()
        try:
            with pytest.raises(CloudTelemetryError, match="HTTP 403") as excinfo:
                await lookup_machine_name(
                    session, serial="SN1234", jwt="jwt", base_url=f"{root}/new", fallback_base_url=f"{root}/legacy"
                )
            assert "HTTP 502" in str(excinfo.value)
            assert len(seen) == 2
        finally:
            await session.close()
            await runner.cleanup()

    asyncio.run(_run())


def test_lookup_machine_name_falls_back_on_unparsable_body() -> None:
    """A gateway answering 200 + HTML must degrade like any other failure."""

    async def _run() -> None:
        app, seen = _devices_server({"/new": (200, "<html>whoops</html>"), "/legacy": (200, _grouped("SN1234", "t"))})
        runner, port = await _serve(app)
        root = f"http://127.0.0.1:{port}"
        session = aiohttp_session()
        try:
            assert (
                await lookup_machine_name(
                    session, serial="SN1234", jwt="jwt", base_url=f"{root}/new", fallback_base_url=f"{root}/legacy"
                )
                == "t"
            )
            assert [entry["path"] for entry in seen] == ["/new/devices", "/legacy/devices"]
        finally:
            await session.close()
            await runner.cleanup()

    asyncio.run(_run())


def test_lookup_machine_name_falls_back_on_timeout() -> None:
    """Audit #18: aiohttp's total timeout raises a bare asyncio.TimeoutError;
    a hung primary stage must degrade to the fallback, not escape and skip it."""

    async def _run() -> None:
        seen: list[str] = []
        app = web.Application()

        async def handler(request: web.Request) -> web.Response:
            seen.append(request.path)
            if request.path.startswith("/new"):
                await asyncio.sleep(0.5)  # past the patched total timeout
                return web.Response(status=500, text="never reached")
            return web.Response(status=200, text=_grouped("SN1234", "thing-42"))

        app.router.add_route("*", "/{tail:.*}", handler)
        runner, port = await _serve(app)
        root = f"http://127.0.0.1:{port}"
        session = aiohttp_session()
        original = cloud._REQUEST_TIMEOUT
        cloud._REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=0.15)
        try:
            name = await lookup_machine_name(
                session, serial="SN1234", jwt="jwt", base_url=f"{root}/new", fallback_base_url=f"{root}/legacy"
            )
            assert name == "thing-42"
            assert seen == ["/new/devices", "/legacy/devices"]
        finally:
            cloud._REQUEST_TIMEOUT = original
            await session.close()
            await runner.cleanup()

    asyncio.run(_run())


def test_lookup_machine_name_timeout_message_when_every_stage_hangs() -> None:
    """Both stages timing out must surface an explicit CloudTelemetryError."""

    async def _run() -> None:
        app = web.Application()

        async def handler(request: web.Request) -> web.Response:
            await asyncio.sleep(0.5)
            return web.Response(status=200, text="{}")

        app.router.add_route("*", "/{tail:.*}", handler)
        runner, port = await _serve(app)
        root = f"http://127.0.0.1:{port}"
        session = aiohttp_session()
        original = cloud._REQUEST_TIMEOUT
        cloud._REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=0.15)
        try:
            with pytest.raises(CloudTelemetryError, match="timed out"):
                await lookup_machine_name(
                    session, serial="SN1234", jwt="jwt", base_url=f"{root}/new", fallback_base_url=f"{root}/legacy"
                )
        finally:
            cloud._REQUEST_TIMEOUT = original
            await session.close()
            await runner.cleanup()

    asyncio.run(_run())


# --------------------------------------------------------------- MQTT codec --


def _decode(packet: bytes, *, protocol_version: int = 4) -> MqttPacket:
    reader = MqttStreamReader(protocol_version=protocol_version)
    reader.feed(packet)
    pkt = reader.next_packet()
    assert pkt is not None
    return pkt


def _parse_connect(body: bytes) -> dict[str, object]:
    """Decode a CONNECT variable header (both levels) for assertions."""
    name_len = int.from_bytes(body[:2], "big")
    pos = 2 + name_len
    level = body[pos]
    flags = body[pos + 1]
    pos += 4  # protocol level, connect flags, keepalive
    if level >= 5:
        while body[pos] & 0x80:  # skip the Properties length varint
            pos += 1
        pos += 1

    def read_str() -> str:
        nonlocal pos
        length = int.from_bytes(body[pos : pos + 2], "big")
        value = body[pos + 2 : pos + 2 + length].decode()
        pos += 2 + length
        return value

    client_id = read_str()
    return {
        "level": level,
        "flags": flags,
        "client_id": client_id,
        "username": read_str() if flags & 0x80 else None,
        "password": read_str() if flags & 0x40 else None,
    }


def test_connect_packet_flag_bits() -> None:
    pkt = mqtt_connect_packet(client_id="c1")
    assert pkt[0] == 0x10
    # fixed header (2 bytes) + protocol name (6) + level (1) → flags at 9
    assert pkt[9] == 0x02
    with_creds = mqtt_connect_packet(client_id="c1", username="u", password="p")
    assert with_creds[9] == 0x02 | 0x80 | 0x40
    assert b"u" in with_creds and b"p" in with_creds


def test_connect_packet_mqtt5_inserts_empty_properties() -> None:
    """MQTT 5 = level 5 + a (here empty) Properties section after the keepalive."""
    v4 = mqtt_connect_packet(client_id="c1", username="u", password="p")
    v5 = mqtt_connect_packet(client_id="c1", username="u", password="p", protocol_version=5)
    assert (v4[8], v5[8]) == (4, 5)
    assert v5[9] == v4[9]  # same connect flags
    assert v5[12] == 0x00  # Properties length 0
    assert len(v5) == len(v4) + 1
    assert _parse_connect(_decode(v5).payload) == {
        "level": 5,
        "flags": 0x02 | 0x80 | 0x40,
        "client_id": "c1",
        "username": "u",
        "password": "p",
    }


def test_subscribe_packet_mqtt5_inserts_empty_properties() -> None:
    v4 = mqtt_subscribe_packet(1, ["a/b"])
    v5 = mqtt_subscribe_packet(1, ["a/b"], protocol_version=5)
    assert v5[0] == v4[0] == 0x82
    assert len(v5) == len(v4) + 1
    body = v5[2:]
    assert body[:2] == b"\x00\x01"  # packet id
    assert body[2] == 0x00  # Properties length 0
    assert body[3:5] == b"\x00\x03"  # first topic filter length
    assert _decode(v5, protocol_version=5).type == 8


def test_publish_packet_mqtt5_roundtrip() -> None:
    pkt = mqtt_publish_packet("t/x", '{"a":1}', protocol_version=5)
    assert _decode(pkt, protocol_version=5).publish_parts() == ("t/x", '{"a":1}')
    # Read as 3.1.1 the properties byte would pollute the payload — which is
    # exactly why the reader has to know the negotiated level.
    assert _decode(pkt).publish_parts()[1] == '\x00{"a":1}'


def test_publish_parts_skips_qos_and_property_sections() -> None:
    qos1 = MqttPacket(3, 0x02, b"\x00\x03t/x" + b"\x00\x07" + b'{"v":1}')
    assert qos1.publish_parts() == ("t/x", '{"v":1}')
    # MQTT 5 §3.3.2 Variable Header order: Topic → Packet Identifier → Properties.
    mqtt5_qos1 = MqttPacket(3, 0x02, b"\x00\x03t/x" + b"\x00\x07" + b"\x00" + b"{}", protocol_version=5)
    assert mqtt5_qos1.publish_parts() == ("t/x", "{}")


def test_publish_parts_mqtt5_qos1_property_order() -> None:
    """Non-empty properties prove the real §3.3.2 order (topic|pid|props).

    With empty properties (single 0x00 byte) the wrong order props-before-pid
    self-cancels and a naive test stays green; a Message Expiry Interval
    property (0x17, Four Byte Integer) makes the two orders differ by 4 bytes.
    """
    props = b"\x05\x17\x00\x00\x00\x0a"  # length 5: Message Expiry Interval (Four Byte Integer) = 10 s
    pkt = MqttPacket(3, 0x02, b"\x00\x03t/x" + b"\x00\x07" + props + b"{}", protocol_version=5)
    assert pkt.publish_parts() == ("t/x", "{}")


def test_publish_parts_rejects_truncated_packets() -> None:
    with pytest.raises(CloudTelemetryError, match="truncated"):
        MqttPacket(3, 0, b"\x01").publish_parts()
    with pytest.raises(CloudTelemetryError, match="truncated"):
        MqttPacket(3, 0, b"\x00\x09ab").publish_parts()  # topic longer than the packet
    with pytest.raises(CloudTelemetryError, match="truncated"):
        MqttPacket(3, 0, b"\x00\x01t\x05abc", protocol_version=5).publish_parts()  # properties past the end


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


def test_reader_tags_packets_with_the_negotiated_level() -> None:
    reader = MqttStreamReader(protocol_version=5)
    reader.feed(mqtt_publish_packet("t", "1", protocol_version=5))
    assert reader.next_packet().protocol_version == 5


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


async def _serve(app: web.Application) -> tuple[web.AppRunner, int]:
    """Start `app` on an ephemeral port; returns (runner, port)."""
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]


def _mqtt_server(
    *,
    reject_all: bool = False,
    reject_levels: set[int] | None = None,
    reject_usernames: set[str] | None = None,
    drop_socket: bool = False,
    answer_shadows: set[str] | None = None,
    require_subprotocol: bool = False,
    deny_subscriptions: bool = False,
    silent_suback: bool = False,
    reported_by_shadow: dict[str, dict] | None = None,
) -> tuple[web.Application, dict]:
    """Minimal MQTT-over-WS broker stub: CONNACK, SUBACK, then one accepted
    PUBLISH per shadow get, encoded in the level the client connected with.

    ``reject_*`` model the authorizer refusing a spelling, ``drop_socket``
    models the broker closing the WebSocket without any CONNACK (what
    mimasek71 saw for the non-app-style connects). ``answer_shadows`` limits
    which shadow gets are answered — a named shadow the thing does not have
    is silent on the real broker, and the client must keep the partial set.
    ``require_subprotocol`` mimics the AWS IoT ATS gate: a WS upgrade that
    offers no MQTT subprotocol is refused with HTTP 426 before MQTT starts.
    """
    seen: dict[str, list] = {"connects": [], "paths": [], "publishes": [], "subprotocols": []}

    app = web.Application()

    async def handler(request: web.Request) -> web.WebSocketResponse:
        # AWS IoT ATS on 443 refuses a WS upgrade that offers no MQTT
        # subprotocol (HTTP 426) — mimicked here so a client that forgets
        # Sec-WebSocket-Protocol fails exactly like the real broker.
        offered = [p.strip() for p in request.headers.get("Sec-WebSocket-Protocol", "").split(",") if p.strip()]
        chosen = next((p for p in offered if p in _WS_SUBPROTOCOLS), None)
        if require_subprotocol and chosen is None:
            return web.Response(status=426, headers={"Upgrade": "websocket"})
        ws = web.WebSocketResponse(autoping=False, protocols=(chosen,) if chosen else None)
        await ws.prepare(request)
        seen["paths"].append(str(request.rel_url))
        seen["subprotocols"].append(offered)
        reader = MqttStreamReader()
        async for msg in ws:
            if msg.type != web.WSMsgType.BINARY:
                continue
            reader.feed(msg.data)
            while (pkt := reader.next_packet()) is not None:
                if pkt.type == 1:  # CONNECT
                    connect = _parse_connect(pkt.payload)
                    seen["connects"].append(connect)
                    level = int(connect["level"])
                    reader.protocol_version = level
                    if drop_socket:
                        await ws.close()
                        return ws
                    refused = (
                        reject_all
                        or (reject_levels is not None and level in reject_levels)
                        or (reject_usernames is not None and connect["username"] in reject_usernames)
                    )
                    await ws.send_bytes(bytes([0x20, 0x02, 0x00, 5 if refused else 0]))
                elif pkt.type == 8:  # SUBSCRIBE → SUBACK
                    if silent_suback:
                        continue  # broker swallows the SUBSCRIBE entirely
                    pid = pkt.payload[:2]
                    # count the topic filters in the SUBSCRIBE payload:
                    # pid, [MQTT5 properties], then 2-byte-len + topic + qos
                    pos = 2
                    if level >= 5:
                        pos += 1 + pkt.payload[pos]  # skip properties length byte(s)
                    n = 0
                    while pos < len(pkt.payload):
                        tlen = int.from_bytes(pkt.payload[pos : pos + 2], "big")
                        pos += 2 + tlen + 1
                        n += 1
                    n = max(1, n)
                    if level >= 5:
                        codes = bytes([0x87] * n) if deny_subscriptions else bytes([0x00] * n)
                        body = pid + bytes([0x00]) + codes  # empty properties
                    else:
                        codes = bytes([0x80] * n) if deny_subscriptions else bytes([0x00] * n)
                        body = pid + codes
                    await ws.send_bytes(bytes([0x90, len(body)]) + body)
                elif pkt.type == 3:  # PUBLISH get → answer accepted
                    topic, _ = pkt.publish_parts()
                    seen["publishes"].append(topic)
                    if deny_subscriptions:
                        # policy-refused topics: the get goes nowhere, the
                        # shadow service never answers — like the real ATS.
                        continue
                    if topic.endswith("/get"):
                        # $aws/things/<thing>/shadow/name/<shadow>/get — answer
                        # on the very topic the client published, so the stub
                        # never depends on one hard-coded thing name.
                        parts = topic.split("/")
                        thing, shadow = parts[2], parts[5]
                        if answer_shadows is not None and shadow not in answer_shadows:
                            continue
                        reported = (reported_by_shadow or {}).get(shadow, {"shadow": shadow})
                        doc = {"state": {"reported": reported}}
                        await ws.send_bytes(
                            mqtt_publish_packet(
                                shadow_accepted_topic(thing, shadow),
                                json.dumps(doc),
                                protocol_version=reader.protocol_version,
                            )
                        )
        return ws

    app.router.add_get("/mqtt", handler)
    return app, seen


def test_shadow_client_fetches_both_shadows() -> None:
    async def _run() -> None:
        app, seen = _mqtt_server()
        runner, port = await _serve(app)
        session = aiohttp_session()
        try:
            client = ShadowClient(session, thing_name="thing1", jwt="jwt", broker="127.0.0.1", scheme="ws", port=port)
            docs = await client.fetch(timeout=5)
            assert docs["MachineStatus"]["state"]["reported"]["shadow"] == "MachineStatus"
            assert docs["MachineCapabilities"]["state"]["reported"]["shadow"] == "MachineCapabilities"
            assert [c["level"] for c in seen["connects"]] == [5]  # app style first, no retry needed
        finally:
            await session.close()
            await runner.cleanup()

    asyncio.run(_run())


@pytest.mark.parametrize(
    ("reported", "counters"),
    [
        (
            {"Brew": 12, "Stopped": 3, "InError": 0, "Undocumented": 99},
            {"brew_count": 12, "stopped_brew_count": 3, "brewing_error_count": 0},
        ),
        ({"Brew": True, "Stopped": -2, "InError": "3"}, {}),
        ({"Stopped": 0}, {"stopped_brew_count": 0}),
    ],
)
def test_fetch_brewing_statistics_reads_only_verified_counters(monkeypatch, reported, counters) -> None:
    async def _run() -> None:
        app, seen = _mqtt_server(reported_by_shadow={"BrewingStatisticsTotals": reported})
        runner, port = await _serve(app)
        monkeypatch.setattr(
            cloud,
            "ShadowClient",
            lambda session, **kwargs: ShadowClient(session, broker="127.0.0.1", scheme="ws", port=port, **kwargs),
        )
        try:
            async with aiohttp_session() as session:
                assert await fetch_brewing_statistics(session, thing_name="thing1", jwt="jwt") == counters
            assert seen["publishes"] == ["$aws/things/thing1/shadow/name/BrewingStatisticsTotals/get"]
        finally:
            await runner.cleanup()

    asyncio.run(_run())


def test_shadow_client_keeps_partial_result_when_one_shadow_is_silent() -> None:
    """Issue #18 retest: a named shadow the thing does not have stays silent
    (no accepted, no rejected). The receive deadline used to raise out of the
    wait loop and discard the shadow that DID answer, behind an empty
    "MQTT session failed: " error — the valueless cloud_error the tester saw.
    A partial set must now be returned."""

    async def _run() -> None:
        app, _seen = _mqtt_server(answer_shadows={"MachineStatus"})
        runner, port = await _serve(app)
        session = aiohttp_session()
        try:
            client = ShadowClient(session, thing_name="thing1", jwt="jwt", broker="127.0.0.1", scheme="ws", port=port)
            docs = await client.fetch(timeout=1)
            assert set(docs) == {"MachineStatus"}
            assert docs["MachineStatus"]["state"]["reported"]["shadow"] == "MachineStatus"
        finally:
            await session.close()
            await runner.cleanup()

    asyncio.run(_run())


def test_shadow_client_offers_mqtt_subprotocol_like_aws_ats() -> None:
    """The AWS IoT ATS answers HTTP 426 to a WS upgrade that offers no MQTT
    subprotocol (issue #18, mimasek71's 0.4.4 diagnostics). The stub gates on
    the same header, so this pins all three halves of the contract: an
    upgrade without it is refused (control), the client does offer
    mqttv5/mqtt, and the shadow read then completes."""
    app, seen = _mqtt_server(require_subprotocol=True)

    async def _run() -> dict:
        runner, port = await _serve(app)
        session = aiohttp_session()
        try:
            # Control: the gate bites before MQTT starts, exactly like the ATS.
            with pytest.raises(aiohttp.WSServerHandshakeError) as refused:
                await session.ws_connect(f"ws://127.0.0.1:{port}{_APP_WS_PATH}")
            assert refused.value.status == 426

            client = ShadowClient(
                session, thing_name="Eletta-Ultra_01", jwt="jwt", broker="127.0.0.1", scheme="ws", port=port
            )
            docs = await client.fetch(timeout=5)
            assert seen["subprotocols"] == [list(_WS_SUBPROTOCOLS)], "the WS upgrade carried no MQTT subprotocol"
            return docs
        finally:
            await session.close()
            await runner.cleanup()

    docs = asyncio.run(_run())
    assert docs["MachineStatus"]["state"]["reported"]["shadow"] == "MachineStatus"
    # One CONNECT, app style: the mqttv5 subprotocol went with the MQTT 5 form.
    assert [c["level"] for c in seen["connects"]] == [5]


def test_shadow_client_offers_mqtt_subprotocol_on_legacy_fallbacks() -> None:
    """The 3.1.1 fallbacks must offer the mqtt subprotocol too: the ATS gate
    is protocol-agnostic, so a bare fallback would take the same 426 as the
    app-style attempt (review of the 0.4.5 fix). Reject MQTT 5 at CONNACK
    level to force the client onto the legacy spellings."""
    app, seen = _mqtt_server(require_subprotocol=True, reject_levels={5})

    async def _run() -> dict:
        runner, port = await _serve(app)
        session = aiohttp_session()
        try:
            client = ShadowClient(
                session, thing_name="Eletta-Ultra_01", jwt="jwt", broker="127.0.0.1", scheme="ws", port=port
            )
            return await client.fetch(timeout=5)
        finally:
            await session.close()
            await runner.cleanup()

    docs = asyncio.run(_run())
    # attempt 1 (v5) refused at CONNACK, attempt 2 (v4) accepted — and both
    # upgrades carried an MQTT subprotocol, the fallback via the ("mqtt",) default.
    assert seen["subprotocols"] == [list(_WS_SUBPROTOCOLS), ["mqtt"]]
    assert [c["level"] for c in seen["connects"]] == [5, 4]
    assert docs["MachineStatus"]["state"]["reported"]["shadow"] == "MachineStatus"


def test_shadow_client_reports_missing_suback() -> None:
    """A broker that accepts the session but never acknowledges the SUBSCRIBE
    must be named as such — the 0.4.6 retest could not distinguish this from
    granted-but-silent shadows (mimasek71, issue #18)."""
    app, seen = _mqtt_server(silent_suback=True)

    async def _run() -> None:
        runner, port = await _serve(app)
        session = aiohttp_session()
        try:
            client = ShadowClient(
                session, thing_name="Eletta-Ultra_01", jwt="jwt", broker="127.0.0.1", scheme="ws", port=port
            )
            with pytest.raises(CloudTelemetryError) as swallowed:
                await client.fetch(timeout=3)
        finally:
            await session.close()
            await runner.cleanup()
        assert "no SUBACK received" in str(swallowed.value)
        assert not seen["publishes"], "gets must not be sent before the SUBACK"

    asyncio.run(_run())


def test_shadow_client_reports_refused_subscriptions() -> None:
    """A connected session whose $aws/... subscribes are all refused (wrong
    thing name or authorizer policy) must say so — not wait 20 s and blame
    the shadows (issue #18, 0.4.5 retest: connected, silent, undiagnosable)."""
    app, seen = _mqtt_server(deny_subscriptions=True)

    async def _run() -> None:
        runner, port = await _serve(app)
        session = aiohttp_session()
        try:
            client = ShadowClient(
                session, thing_name="Eletta-Ultra_01", jwt="jwt", broker="127.0.0.1", scheme="ws", port=port
            )
            with pytest.raises(CloudTelemetryError) as refused:
                await client.fetch(timeout=5)
        finally:
            await session.close()
            await runner.cleanup()
        assert "refused all" in str(refused.value)
        assert "0x87" in str(refused.value)

    asyncio.run(_run())


def test_shadow_client_silent_broker_reports_descriptive_timeout() -> None:
    """When nothing answers at all the error text must say so — never the
    empty "MQTT session failed: " that made the #18 retest undiagnosable."""

    async def _run() -> None:
        app, _seen = _mqtt_server(answer_shadows=set())
        runner, port = await _serve(app)
        session = aiohttp_session()
        try:
            client = ShadowClient(session, thing_name="thing1", jwt="jwt", broker="127.0.0.1", scheme="ws", port=port)
            with pytest.raises(CloudTelemetryError) as excinfo:
                await client.fetch(timeout=1)
            message = str(excinfo.value)
            assert message.strip()
            assert "no shadow document received" in message
            assert "MQTT session failed: " not in message
        finally:
            await session.close()
            await runner.cleanup()

    asyncio.run(_run())


def test_shadow_client_connects_app_style_first() -> None:
    """Issue #18 (mimasek71): MQTT 5, authorizer in the path *and* the username,
    JWT as password, app client-id prefix — that is the accepted spelling."""

    async def _run() -> None:
        app, seen = _mqtt_server()
        runner, port = await _serve(app)
        session = aiohttp_session()
        try:
            client = ShadowClient(session, thing_name="thing1", jwt="jwt", broker="127.0.0.1", scheme="ws", port=port)
            await client.fetch(timeout=5)
            assert seen["paths"] == [_APP_WS_PATH]
            connect = seen["connects"][0]
            assert connect["level"] == 5
            assert connect["username"] == _APP_AUTHORIZER
            assert connect["password"] == "jwt"
            assert str(connect["client_id"]).startswith("dlg-appliance-kit-android-")
            # the get topics survived the MQTT 5 property sections both ways
            assert seen["publishes"] == [
                "$aws/things/thing1/shadow/name/MachineStatus/get",
                "$aws/things/thing1/shadow/name/MachineCapabilities/get",
            ]
        finally:
            await session.close()
            await runner.cleanup()

    asyncio.run(_run())


def test_shadow_client_falls_back_to_legacy_username_form() -> None:
    async def _run() -> None:
        app, seen = _mqtt_server(reject_levels={5})
        runner, port = await _serve(app)
        session = aiohttp_session()
        try:
            client = ShadowClient(session, thing_name="thing1", jwt="jwt", broker="127.0.0.1", scheme="ws", port=port)
            docs = await client.fetch(timeout=5)
            assert "MachineStatus" in docs
            assert [c["level"] for c in seen["connects"]] == [5, 4]
            assert seen["connects"][1]["username"] == "?authorizer=jwt"
            assert seen["paths"] == [_APP_WS_PATH, "/mqtt"]
        finally:
            await session.close()
            await runner.cleanup()

    asyncio.run(_run())


def test_shadow_client_falls_back_to_password_form() -> None:
    async def _run() -> None:
        app, seen = _mqtt_server(reject_levels={5}, reject_usernames={"?authorizer=jwt"})
        runner, port = await _serve(app)
        session = aiohttp_session()
        try:
            client = ShadowClient(session, thing_name="thing1", jwt="jwt", broker="127.0.0.1", scheme="ws", port=port)
            docs = await client.fetch(timeout=5)
            assert "MachineStatus" in docs
            assert [c["level"] for c in seen["connects"]] == [5, 4, 4]
            assert seen["connects"][2]["username"] is None
            assert seen["connects"][2]["password"] == "jwt"
        finally:
            await session.close()
            await runner.cleanup()

    asyncio.run(_run())


def test_shadow_client_raises_when_broker_refuses_jwt() -> None:
    async def _run() -> None:
        app, seen = _mqtt_server(reject_all=True)
        runner, port = await _serve(app)
        session = aiohttp_session()
        try:
            client = ShadowClient(session, thing_name="thing1", jwt="jwt", broker="127.0.0.1", scheme="ws", port=port)
            with pytest.raises(CloudTelemetryError, match="refused the JWT"):
                await client.fetch(timeout=2)
            # every spelling tried, every one refused
            assert [c["level"] for c in seen["connects"]] == [5, 4, 4]
        finally:
            await session.close()
            await runner.cleanup()

    asyncio.run(_run())


def test_shadow_client_retries_when_broker_drops_socket() -> None:
    """No CONNACK at all (the old spellings' failure mode) must not abort the
    remaining attempts, and must surface as a cloud error, not a crash."""

    async def _run() -> None:
        app, seen = _mqtt_server(drop_socket=True)
        runner, port = await _serve(app)
        session = aiohttp_session()
        try:
            client = ShadowClient(session, thing_name="thing1", jwt="jwt", broker="127.0.0.1", scheme="ws", port=port)
            with pytest.raises(CloudTelemetryError, match="MQTT WS connect failed"):
                await client.fetch(timeout=2)
            assert len(seen["connects"]) == 3
        finally:
            await session.close()
            await runner.cleanup()

    asyncio.run(_run())


def test_connect_budget_bounds_a_black_holed_broker() -> None:
    """Audit #18: three spellings × (15 s ws_receive + 10 s receive) could
    burn 75 s on a broker that accepts the socket but never answers the
    CONNECT; the shared connect budget must stop the loop and say so."""

    async def _run() -> None:
        app = web.Application()

        async def silent(request: web.Request) -> web.WebSocketResponse:
            ws = web.WebSocketResponse(autoping=False)
            await ws.prepare(request)
            async for _ in ws:  # accepts the WebSocket, never sends a CONNACK
                pass
            return ws

        app.router.add_get("/mqtt", silent)
        runner, port = await _serve(app)
        session = aiohttp_session()
        try:
            client = ShadowClient(session, thing_name="thing1", jwt="jwt", broker="127.0.0.1", scheme="ws", port=port)
            started = time.monotonic()
            with pytest.raises(CloudTelemetryError, match="connect budget exhausted"):
                await client._connect(connect_budget=0.5)
            # bounded by the budget, not by 3 × (15 s + 10 s)
            assert time.monotonic() - started < 5.0
        finally:
            await session.close()
            await runner.cleanup()

    asyncio.run(_run())


def test_shadow_client_errors_never_leak_the_jwt() -> None:
    """Whatever the broker answers, the JWT stays out of the error text
    (it ends up in logs and in the diagnostics dump)."""

    async def _run(reject_all: bool) -> None:
        app, _seen = _mqtt_server(reject_all=reject_all, drop_socket=not reject_all)
        runner, port = await _serve(app)
        session = aiohttp_session()
        try:
            client = ShadowClient(
                session, thing_name="thing1", jwt=FAKE_JWT, broker="127.0.0.1", scheme="ws", port=port
            )
            with pytest.raises(CloudTelemetryError) as excinfo:
                await client.fetch(timeout=2)
            assert FAKE_JWT not in str(excinfo.value)
            assert "eyJ" not in str(excinfo.value)
        finally:
            await session.close()
            await runner.cleanup()

    asyncio.run(_run(reject_all=True))
    asyncio.run(_run(reject_all=False))


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
                state = await fetch_cloud_state(session, serial="SN1", jwt="jwt", machine_name="Eletta-Ultra_01")
                assert state.machine_name == "Eletta-Ultra_01"
                assert state.shadows == {"MachineStatus": {}}
        finally:
            await session.close()

    asyncio.run(_run())


@pytest.mark.parametrize(
    ("record", "reported_at", "error"),
    [
        ({"status": " offline "}, 990, "reports the machine offline"),
        ({"status": "ONLINE"}, 1, None),
        ({}, 1, None),
        ({"status": FAKE_JWT}, 1, "unknown presence"),
        ({"status": "UNKNOWN"}, 990, None),
    ],
)
def test_cloud_machine_status_uses_directory_presence(record, reported_at, error) -> None:
    async def _run() -> None:
        stats = {"MachineCapabilities": {"state": {"reported": {"version": "1.2.0"}}}}
        status = {
            "state": {"reported": {"Status": "Idle"}},
            "metadata": {"reported": {"Status": {"timestamp": reported_at}}},
        }
        full = {**stats, "MachineStatus": status}
        thing_name = "Aromatic_70"
        payload = {"ownedByMe": [{"serialNumber": "SN1", "machineName": thing_name, **record}]}
        async with aiohttp.ClientSession() as session:
            with (
                patch.object(cloud, "_fetch_devices", return_value=payload),
                patch.object(cloud, "ShadowClient") as client,
                patch.object(cloud.time, "time", return_value=1000),
            ):
                client.return_value.fetch = AsyncMock(return_value=full)
                state = await fetch_cloud_state(
                    session, serial="SN1", jwt="jwt", machine_name=thing_name, check_device_status=True
                )
                assert state.machine_name == thing_name
                assert state.shadows == (stats if error else full)
                assert full["MachineStatus"] == status
                if error:
                    assert error in state.status_error
                    assert FAKE_JWT not in state.status_error
                else:
                    assert state.status_error is None
                client.return_value.fetch.assert_awaited_once_with()

    asyncio.run(_run())


def test_cloud_directory_prefers_explicit_offline_for_duplicate_records() -> None:
    async def _run() -> None:
        record = {"serialNumber": "SN1", "machineName": "thing-1"}
        payload = {"ownedByMe": [record], "sharedToMe": [{**record, "status": "OFFLINE"}]}
        docs = {"MachineStatus": {"state": {"reported": {"Status": "Idle"}}}}
        async with aiohttp.ClientSession() as session:
            with (
                patch.object(cloud, "_fetch_devices", return_value=payload),
                patch.object(cloud, "ShadowClient") as client,
            ):
                client.return_value.fetch = AsyncMock(return_value=docs)
                state = await fetch_cloud_state(
                    session, serial="SN1", jwt="jwt", machine_name=None, check_device_status=True
                )
        assert "MachineStatus" not in state.shadows
        assert state.status_error == "Device directory reports the machine offline"

    asyncio.run(_run())


@pytest.mark.parametrize(
    ("reported_at", "accepted"),
    [
        (880, True),
        (879.999, False),
        (990.5, True),
        (1001.5, True),
        (1120, True),
        (1120.001, False),
        (float("nan"), False),
        (float("inf"), False),
        (float("-inf"), False),
        ("1000", False),
        (True, False),
        (None, False),
    ],
)
def test_cloud_directory_failure_requires_recent_report_with_clock_tolerance(reported_at, accepted) -> None:
    async def _run() -> None:
        docs = {
            "MachineCapabilities": {"state": {"reported": {"version": "1.2.0"}}},
            "MachineStatus": {
                "timestamp": 1000,
                "state": {"reported": {"Status": "Idle"}},
                "metadata": {"reported": {"Status": {"timestamp": reported_at}}},
            },
        }
        async with aiohttp.ClientSession() as session:
            with (
                patch.object(cloud, "_lookup_machine_record", side_effect=CloudTelemetryError(FAKE_JWT)),
                patch.object(cloud, "ShadowClient") as client,
                patch.object(cloud.time, "time", return_value=1000),
            ):
                client.return_value.fetch = AsyncMock(return_value=docs)
                state = await fetch_cloud_state(
                    session, serial="SN1", jwt="jwt", machine_name="prior", check_device_status=True
                )
        assert state.shadows["MachineCapabilities"] == docs["MachineCapabilities"]
        if accepted:
            assert state.shadows["MachineStatus"] == docs["MachineStatus"]
            assert state.status_error is None
        else:
            assert "MachineStatus" not in state.shadows
            assert (
                state.status_error == "Device directory lookup failed and MachineStatus has no recent Status timestamp"
            )

    asyncio.run(_run())


@pytest.mark.parametrize("outcome", ["unreachable", "removed", "remapped"])
def test_cloud_handles_directory_changes_without_reusing_old_status(outcome):
    async def _run():
        prior = "Previous_Thing"
        replacement = "Replacement_Thing"
        caps = {"MachineCapabilities": {}}
        docs = {**caps, "MachineStatus": {"timestamp": int(time.time())}}
        async with aiohttp.ClientSession() as session:
            with (
                patch.object(cloud, "_lookup_machine_record") as directory,
                patch.object(cloud, "ShadowClient") as client,
            ):
                client.return_value.fetch = AsyncMock(return_value=docs)
                if outcome == "removed":
                    directory.side_effect = cloud.CloudMachineNotFound("removed")
                    with pytest.raises(cloud.CloudMachineNotFound):
                        await fetch_cloud_state(
                            session, serial="SN1", jwt="jwt", machine_name=prior, check_device_status=True
                        )
                    client.assert_not_called()
                else:
                    if outcome == "unreachable":
                        directory.side_effect = CloudTelemetryError("unreachable")
                    else:
                        directory.return_value = {"status": "ONLINE"}, replacement
                    result = await fetch_cloud_state(
                        session, serial="SN1", jwt="jwt", machine_name=prior, check_device_status=True
                    )
                    client.assert_called_once_with(session, thing_name=result.machine_name, jwt="jwt")
                    if outcome == "unreachable":
                        assert (result.shadows, result.machine_name) == (caps, prior)
                    else:
                        assert (result.shadows, result.machine_name) == (docs, replacement)

    asyncio.run(_run())
