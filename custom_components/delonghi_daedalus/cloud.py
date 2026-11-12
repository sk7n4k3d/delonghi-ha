"""Read-only cloud telemetry for the Daedalus stack (issue #18, mimasek71).

Two hops, both JWT-authenticated:

1. REST device directory (AWS API Gateway, ``AWS_REST_BASE_URL_PROD``):
   resolve the cloud ``machineName`` (AWS IoT thing name) from the serial
   printed on the machine.
2. AWS IoT Core device shadows over MQTT 3.1.1 / WebSocket (WSS:443):
   read the named shadows ``MachineStatus`` and ``MachineCapabilities``.
   Read-only — the only publishes are the standard ``$aws/things/.../get``
   requests, which only ask the shadow service for the stored document.

EXPERIMENTAL and opt-in (``CONF_CLOUD_TELEMETRY``): the wire details were
described from a single contributor's Eletta Ultra (My Coffee Lounge
1.5.0, EU pool). Every failure here degrades to a ``cloud_error`` string
in the coordinator data — the LAN path is never affected.

The MQTT codec below is a deliberately minimal subset (CONNECT / SUBSCRIBE
/ PUBLISH QoS0 / CONNACK / SUBACK) — enough for a shadow read, nothing
else.
"""

from __future__ import annotations

import contextlib
import json
import re
import secrets
import time
from typing import Any

import aiohttp

from .const import AWS_IOT_BROKER_PROD, AWS_REST_BASE_URL_PROD
from .redact import get_logger, mask_serial, sanitize_text

_LOGGER = get_logger(__name__)

SHADOWS: tuple[str, ...] = ("MachineStatus", "MachineCapabilities")

_CONNECT = 1
_CONNACK = 2
_PUBLISH = 3
_SUBSCRIBE = 8
_SUBACK = 9

_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)


class CloudTelemetryError(RuntimeError):
    """Any failure on the cloud path. Callers must treat it as optional."""


# --------------------------------------------------------------------- REST --


def extract_machine_name(payload: Any, serial: str) -> str:
    """Pull the cloud machineName for ``serial`` out of a /devices payload.

    Defensive on purpose: the endpoint shape was captured from one app
    version. Accepts a bare list or a wrapper dict (``devices`` /
    ``items`` / ``result`` / ``data``), and matches the serial
    case-insensitively against any key containing ``serial``.
    """
    if isinstance(payload, dict):
        for wrapper in ("devices", "items", "result", "data"):
            inner = payload.get(wrapper)
            if isinstance(inner, list):
                payload = inner
                break
    if not isinstance(payload, list):
        raise CloudTelemetryError("/devices response was neither a list nor a known wrapper")
    target = serial.strip().lower()
    for device in payload:
        if not isinstance(device, dict):
            continue
        serials = {
            str(v).strip().lower()
            for k, v in device.items()
            if "serial" in str(k).lower() and isinstance(v, (str, int))
        }
        if target and target in serials:
            for key in ("machineName", "machine_name", "thingName", "thing_name", "name"):
                value = device.get(key)
                if isinstance(value, str) and value:
                    return value
    raise CloudTelemetryError(f"serial {mask_serial(serial)} not found in /devices response")


async def lookup_machine_name(
    session: aiohttp.ClientSession,
    *,
    serial: str,
    jwt: str,
    base_url: str = AWS_REST_BASE_URL_PROD,
) -> str:
    """Resolve the AWS IoT thing name for ``serial`` via the device directory."""
    url = f"{base_url.rstrip('/')}/devices"
    try:
        async with session.get(url, headers={"Authorization": f"Bearer {jwt}"}, timeout=_REQUEST_TIMEOUT) as resp:
            if resp.status != 200:
                raise CloudTelemetryError(f"/devices returned HTTP {resp.status}")
            payload = await resp.json(content_type=None)
    except aiohttp.ClientError as exc:
        raise CloudTelemetryError(f"/devices request failed: {sanitize_text(str(exc))}") from exc
    return extract_machine_name(payload, serial)


# ------------------------------------------------------------- MQTT codec --


def _encode_remaining_length(length: int) -> bytes:
    out = bytearray()
    while True:
        byte = length % 128
        length //= 128
        if length:
            byte |= 0x80
        out.append(byte)
        if not length:
            return bytes(out)


def _encode_mqtt_string(value: str) -> bytes:
    raw = value.encode("utf-8")
    return len(raw).to_bytes(2, "big") + raw


def mqtt_connect_packet(
    *,
    client_id: str,
    username: str | None = None,
    password: str | None = None,
    keepalive: int = 60,
) -> bytes:
    """CONNECT (protocol level 4 / clean session) with optional credentials."""
    flags = 0x02  # clean session
    if username is not None:
        flags |= 0x80
    if password is not None:
        flags |= 0x40
    body = _encode_mqtt_string("MQTT") + bytes([4, flags, keepalive >> 8, keepalive & 0xFF])
    body += _encode_mqtt_string(client_id)
    if username is not None:
        body += _encode_mqtt_string(username)
    if password is not None:
        body += _encode_mqtt_string(password)
    return bytes([_CONNECT << 4]) + _encode_remaining_length(len(body)) + body


def mqtt_subscribe_packet(packet_id: int, topics: list[str], qos: int = 0) -> bytes:
    body = packet_id.to_bytes(2, "big")
    for topic in topics:
        body += _encode_mqtt_string(topic) + bytes([qos])
    return bytes([_SUBSCRIBE << 4 | 0x02]) + _encode_remaining_length(len(body)) + body


def mqtt_publish_packet(topic: str, payload: str) -> bytes:
    body = _encode_mqtt_string(topic) + payload.encode("utf-8")
    return bytes([_PUBLISH << 4]) + _encode_remaining_length(len(body)) + body


class MqttPacket:
    """One decoded MQTT control packet (fixed header split out)."""

    def __init__(self, ptype: int, flags: int, payload: bytes) -> None:
        self.type = ptype
        self.flags = flags
        self.payload = payload

    def publish_parts(self) -> tuple[str, str]:
        """(topic, payload text) of a QoS-0 PUBLISH."""
        if len(self.payload) < 2:
            raise CloudTelemetryError("truncated PUBLISH packet")
        topic_len = int.from_bytes(self.payload[:2], "big")
        topic = self.payload[2 : 2 + topic_len].decode("utf-8", errors="replace")
        body = self.payload[2 + topic_len :].decode("utf-8", errors="replace")
        return topic, body


class MqttStreamReader:
    """Incremental MQTT packet parser: feed() bytes, next_packet() decodes."""

    # A shadow document is tens of KB; anything asking for more assembly
    # buffer is hostile or broken. Cap = 1 MiB.
    _MAX_BUFFER = 1 << 20

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> None:
        if len(self._buf) + len(data) > self._MAX_BUFFER:
            raise CloudTelemetryError("MQTT assembly buffer overflow (hostile or broken broker)")
        self._buf.extend(data)

    def next_packet(self) -> MqttPacket | None:
        if len(self._buf) < 2:
            return None
        ptype_flags = self._buf[0]
        multiplier = 1
        remaining = 0
        pos = 1
        while True:
            if pos >= len(self._buf):
                return None  # need more bytes
            byte = self._buf[pos]
            remaining += (byte & 0x7F) * multiplier
            multiplier *= 128
            pos += 1
            if not byte & 0x80:
                break
            if multiplier > 128**3:
                raise CloudTelemetryError("malformed MQTT remaining length")
        if len(self._buf) < pos + remaining:
            return None
        payload = bytes(self._buf[pos : pos + remaining])
        del self._buf[: pos + remaining]
        return MqttPacket(ptype_flags >> 4, ptype_flags & 0x0F, payload)


def shadow_get_topic(thing: str, shadow: str) -> str:
    return f"$aws/things/{thing}/shadow/name/{shadow}/get"


def shadow_accepted_topic(thing: str, shadow: str) -> str:
    return f"$aws/things/{thing}/shadow/name/{shadow}/get/accepted"


def shadow_rejected_topic(thing: str, shadow: str) -> str:
    return f"$aws/things/{thing}/shadow/name/{shadow}/get/rejected"


def _shadow_from_topic(topic: str) -> str | None:
    """Which of SHADOWS does this $aws topic belong to?"""
    parts = topic.split("/")
    # $aws/things/<thing>/shadow/name/<shadow>/get[/suffix]
    if len(parts) >= 6 and parts[:2] == ["$aws", "things"] and parts[3] == "shadow":
        shadow = parts[5]
        return shadow if shadow in SHADOWS else None
    return None


# ----------------------------------------------------------- shadow client --


class ShadowClient:
    """One-shot MQTT/WSS session reading the named shadows for one thing.

    The custom Lambda authorizer (``dlg-prod-token-authorizer``) accepts the
    Gigya JWT; two credential spellings are known in the wild (query-style
    ``?authorizer=`` username vs plain password field), so a refused CONNACK
    triggers exactly one retry with the other form before giving up.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        thing_name: str,
        jwt: str,
        broker: str = AWS_IOT_BROKER_PROD,
        scheme: str = "wss",
        port: int = 443,
    ) -> None:
        self._session = session
        self._thing = thing_name
        self._jwt = jwt
        self._broker = broker
        # scheme/port are seams for tests (local plain-ws stub); production
        # always uses wss:443, the only endpoint AWS IoT exposes.
        self._scheme = scheme
        self._port = port

    async def fetch(self, *, timeout: float = 20.0) -> dict[str, dict[str, Any]]:
        """Return {shadow name: parsed shadow document} (partial OK)."""
        results: dict[str, dict[str, Any]] = {}
        errors: dict[str, str] = {}
        ws = await self._connect()
        try:
            reader = MqttStreamReader()
            topics: list[str] = []
            for shadow in SHADOWS:
                topics.append(shadow_accepted_topic(self._thing, shadow))
                topics.append(shadow_rejected_topic(self._thing, shadow))
            await ws.send_bytes(mqtt_subscribe_packet(1, topics))
            for shadow in SHADOWS:
                await ws.send_bytes(mqtt_publish_packet(shadow_get_topic(self._thing, shadow), "{}"))
            deadline = time.monotonic() + timeout
            while len(results) + len(errors) < len(SHADOWS):
                packet = reader.next_packet()
                if packet is None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    msg = await ws.receive(timeout=remaining)
                    if msg.type == aiohttp.WSMsgType.BINARY:
                        reader.feed(msg.data)
                    elif msg.type in (
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.CLOSING,
                        aiohttp.WSMsgType.ERROR,
                    ):
                        raise CloudTelemetryError("MQTT WS closed by broker")
                    continue
                if packet.type != _PUBLISH:
                    continue
                self._route_publish(packet, results, errors)
            if errors and not results:
                raise CloudTelemetryError("; ".join(errors.values()))
            if not results:
                raise CloudTelemetryError("no shadow document received before timeout")
            return results
        except (aiohttp.ClientError, TimeoutError, OSError) as exc:
            # receive() timeouts and WS resets must surface as the optional
            # cloud error the coordinator logs, never as a raw crash.
            raise CloudTelemetryError(f"MQTT session failed: {exc}") from exc
        finally:
            with contextlib.suppress(Exception):
                await ws.close()

    def _route_publish(
        self,
        packet: MqttPacket,
        results: dict[str, dict[str, Any]],
        errors: dict[str, str],
    ) -> None:
        topic, body = packet.publish_parts()
        shadow = _shadow_from_topic(topic)
        if shadow is None or shadow in results or shadow in errors:
            return
        if topic.endswith("/get/accepted"):
            try:
                parsed = json.loads(body)
            except (ValueError, RecursionError) as exc:
                # RecursionError ⊄ ValueError: a pathologically nested shadow
                # document must degrade to cloud_error, not escape the
                # CloudTelemetryError contract (audit #18, B1).
                errors[shadow] = f"unparsable shadow: {exc}"
            else:
                results[shadow] = parsed if isinstance(parsed, dict) else {"value": parsed}
        elif topic.endswith("/get/rejected"):
            # The rejected payload is broker-controlled text — truncate hard.
            errors[shadow] = f"shadow get rejected by broker: {body[:200]}"

    async def _connect(self) -> aiohttp.ClientWebSocketResponse:
        url = f"{self._scheme}://{self._broker}:{self._port}/mqtt"
        attempts: list[dict[str, str]] = [
            {"username": f"?authorizer={self._jwt}"},
            {"password": self._jwt},
        ]
        last_rc: int | None = None
        for creds in attempts:
            try:
                # ClientWSTimeout, not the deprecated receive_timeout kwarg
                # (review #18, F8): 15 s to receive the CONNACK after CONNECT.
                ws = await self._session.ws_connect(url, heartbeat=30, timeout=aiohttp.ClientWSTimeout(ws_receive=15))
            except (aiohttp.ClientError, TimeoutError, OSError) as exc:
                # The aiohttp text embeds the request URL — which here carries
                # the JWT as ?authorizer= — scrub at birth instead of trusting
                # every downstream consumer to run sanitize_text (audit #18, C2).
                raise CloudTelemetryError(f"MQTT WS connect failed: {sanitize_text(str(exc))}") from exc
            opened = False
            try:
                client_id = f"ha-delonghi-{secrets.token_hex(4)}"
                await ws.send_bytes(mqtt_connect_packet(client_id=client_id, keepalive=60, **creds))
                msg = await ws.receive(timeout=10)
                if msg.type != aiohttp.WSMsgType.BINARY:
                    raise CloudTelemetryError(f"expected binary CONNACK, got {msg.type!r}")
                reader = MqttStreamReader()
                reader.feed(msg.data)
                packet = reader.next_packet()
                if packet is None or packet.type != _CONNACK:
                    raise CloudTelemetryError("no CONNACK from broker")
                if len(packet.payload) >= 2 and packet.payload[1] == 0:
                    opened = True
                    return ws
                last_rc = packet.payload[1] if len(packet.payload) >= 2 else -1
            finally:
                if not opened:
                    with contextlib.suppress(Exception):
                        await ws.close()
        raise CloudTelemetryError(f"MQTT broker refused the JWT (CONNACK rc={last_rc})")


# ------------------------------------------------------------- projection --


def flatten_shadow(document: dict[str, Any], *, max_entries: int = 80, max_depth: int = 8) -> dict[str, Any]:
    """Flatten ``state.reported`` into {dotted.key: scalar-or-json-string}.

    Unknown nested structures become compact JSON strings so nothing is
    silently dropped — raw codes stay visible until mapped for real. Depth
    is capped: a pathological document deeper than ``max_depth`` is
    stringified whole instead of recursing into a RecursionError.
    """
    reported = document.get("state", {}).get("reported")
    if not isinstance(reported, dict):
        reported = document
    flat: dict[str, Any] = {}

    def walk(prefix: str, node: Any, depth: int) -> None:
        if len(flat) >= max_entries:
            return
        if isinstance(node, dict) and depth < max_depth:
            for key, value in node.items():
                walk(f"{prefix}.{key}" if prefix else str(key), value, depth + 1)
        elif isinstance(node, (str, int, float, bool)) or node is None:
            flat[prefix] = node
        else:
            # deep structure, lists, anything else → compact JSON (default=str
            # keeps exotic scalars printable; depth already capped above).
            flat[prefix] = json.dumps(node, separators=(",", ":"), default=str)

    walk("", reported, 0)
    return flat


# AWS IoT thing names are [A-Za-z0-9_-]{1,128}; anything else coming from the
# /devices endpoint (or a corrupted entry.data) must never reach the MQTT
# topic builder — audit #18 (C1): wildcards, CRLF or 70 KB strings in
# `$aws/things/<thing>/...` are injection noise at best.
_THING_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")


async def fetch_cloud_state(
    session: aiohttp.ClientSession,
    *,
    serial: str,
    jwt: str,
    machine_name: str | None,
) -> tuple[dict[str, dict[str, Any]], str]:
    """Resolve the thing name if needed + read the shadows.

    Returns (shadow documents by name, machine_name actually used).
    Raises CloudTelemetryError on any failure.
    """
    thing = machine_name or await lookup_machine_name(session, serial=serial, jwt=jwt)
    if not _THING_NAME_RE.fullmatch(str(thing)):
        # Never echo the rejected value: it is the hostile input itself.
        raise CloudTelemetryError(f"cloud thing name rejected by shape check ({len(str(thing))} chars)")
    client = ShadowClient(session, thing_name=thing, jwt=jwt)
    docs = await client.fetch()
    return docs, thing
