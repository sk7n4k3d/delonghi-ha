"""Read-only cloud telemetry for the Daedalus stack (issue #18, mimasek71).

Two hops, both JWT-authenticated:

1. REST device directory (AWS API Gateway, ``AWS_REST_BASE_URL_PROD`` plus
   the static ``source: mycoffeelounge`` header): resolve the cloud
   ``machineName`` (AWS IoT thing name) from the serial printed on the
   machine. The directory answers grouped under ``ownedByMe`` /
   ``sharedToMe``.
2. AWS IoT Core device shadows over MQTT 5 / WebSocket (WSS:443): read the
   named shadows ``MachineStatus`` and ``MachineCapabilities``. Read-only —
   the only publishes are the standard ``$aws/things/.../get`` requests,
   which only ask the shadow service for the stored document.

The MQTT connection mirrors what the De'Longhi Android app sends, because
that is the only spelling the custom authorizer accepts (issue #18,
mimasek71): MQTT 5, ``?x-amz-customauthorizer-name=<authorizer>`` on the
WebSocket path, the same ``key=value`` pair repeated verbatim as the MQTT
username, the JWT as password and a ``dlg-appliance-kit-android-`` client
id. The two older spellings (``?authorizer=<jwt>`` username, bare password,
MQTT 3.1.1, ``ha-delonghi-`` client id) make the broker drop the WebSocket
and are kept as fallbacks only.

EXPERIMENTAL and opt-in (``CONF_CLOUD_TELEMETRY``): the wire details were
described from a single contributor's Eletta Ultra (My Coffee Lounge
1.5.0, EU pool). Every failure here degrades to a ``cloud_error`` string
in the coordinator data — the LAN path is never affected.

The MQTT codec below is a deliberately minimal subset (CONNECT / SUBSCRIBE
/ PUBLISH QoS0 / CONNACK / SUBACK) speaking both MQTT 3.1.1 and MQTT 5 —
the only MQTT 5 addition is the empty Properties section the newer level
requires. Enough for a shadow read, nothing else.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import secrets
import time
from typing import Any

import aiohttp

from .const import (
    AWS_IOT_AUTHORIZER_PROD,
    AWS_IOT_BROKER_PROD,
    AWS_REST_BASE_URL_LEGACY_PROD,
    AWS_REST_BASE_URL_PROD,
    AWS_REST_SOURCE_HEADER,
)
from .redact import get_logger, mask_serial, sanitize_text

_LOGGER = get_logger(__name__)

SHADOWS: tuple[str, ...] = ("MachineStatus", "MachineCapabilities")

_CONNECT = 1
_CONNACK = 2
_PUBLISH = 3
_SUBSCRIBE = 8
_SUBACK = 9

# MQTT 5 inserts a Properties section where 3.1.1 has nothing; this client
# never sets a property, so every one of them is the single zero byte.
_MQTT5_NO_PROPERTIES = b"\x00"

_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)


class CloudTelemetryError(RuntimeError):
    """Any failure on the cloud path. Callers must treat it as optional."""


# --------------------------------------------------------------------- REST --

# The live /devices answer groups the machines by ownership (issue #18,
# mimasek71's capture); older captures were a bare list or a wrapper dict.
_GROUPED_DEVICE_KEYS: tuple[str, ...] = ("ownedByMe", "sharedToMe")


def extract_machine_name(payload: Any, serial: str) -> str:
    """Pull the cloud machineName for ``serial`` out of a /devices payload.

    Defensive on purpose: the endpoint shape was captured from one app
    version. Accepts the grouped live shape (``ownedByMe`` / ``sharedToMe``
    lists, flattened in that order), a bare list, or a wrapper dict
    (``devices`` / ``items`` / ``result`` / ``data``), and matches the serial
    case-insensitively against any key containing ``serial``.
    """
    if isinstance(payload, dict):
        grouped = [key for key in _GROUPED_DEVICE_KEYS if isinstance(payload.get(key), list)]
        if grouped:
            payload = [device for key in grouped for device in payload[key]]
        else:
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


async def _fetch_devices(session: aiohttp.ClientSession, *, url: str, jwt: str) -> Any:
    """GET one /devices URL. Raises CloudTelemetryError on any bad answer."""
    headers = {"Authorization": f"Bearer {jwt}", "source": AWS_REST_SOURCE_HEADER}
    try:
        async with session.get(url, headers=headers, timeout=_REQUEST_TIMEOUT) as resp:
            if resp.status != 200:
                raise CloudTelemetryError(f"/devices returned HTTP {resp.status}")
            try:
                return await resp.json(content_type=None)
            except (aiohttp.ClientError, ValueError, RecursionError) as exc:
                # A gateway in front of the real API can answer 200 with an
                # HTML error page; that must degrade to cloud_error like any
                # other failure instead of escaping as a raw JSONDecodeError.
                raise CloudTelemetryError(f"/devices body was not JSON: {sanitize_text(str(exc))}") from exc
    except TimeoutError as exc:
        # aiohttp's total timeout (connect or body read) raises a bare
        # asyncio.TimeoutError — since 3.11 the builtin TimeoutError, and
        # neither an aiohttp.ClientError nor caught by the clauses below.
        # Uncaught it would escape lookup_machine_name and skip the fallback
        # stage entirely (audit #18).
        raise CloudTelemetryError(f"/devices request timed out: {sanitize_text(str(exc))}") from exc
    except aiohttp.ClientError as exc:
        raise CloudTelemetryError(f"/devices request failed: {sanitize_text(str(exc))}") from exc


async def lookup_machine_name(
    session: aiohttp.ClientSession,
    *,
    serial: str,
    jwt: str,
    base_url: str = AWS_REST_BASE_URL_PROD,
    fallback_base_url: str = AWS_REST_BASE_URL_LEGACY_PROD,
) -> str:
    """Resolve the AWS IoT thing name for ``serial`` via the device directory.

    ``base_url`` (the stage the current app uses) is tried first; a transport
    failure, a non-200 status or an unparsable body falls through to
    ``fallback_base_url`` — the stage from the older APK dump answers 403
    today, but keeping it costs one extra request only in that broken case.
    A 200 that simply does not mention the serial is a real answer and is
    never retried against the other host.
    """
    candidates = [base_url]
    if fallback_base_url and fallback_base_url.rstrip("/") != base_url.rstrip("/"):
        candidates.append(fallback_base_url)
    failures: list[str] = []
    for base in candidates:
        url = f"{base.rstrip('/')}/devices"
        try:
            payload = await _fetch_devices(session, url=url, jwt=jwt)
        except CloudTelemetryError as exc:
            failures.append(str(exc))
            continue
        return extract_machine_name(payload, serial)
    raise CloudTelemetryError("; ".join(failures) if failures else "no REST device directory configured")


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
    protocol_version: int = 4,
) -> bytes:
    """CONNECT with optional credentials, MQTT 3.1.1 (level 4) or MQTT 5.

    MQTT 5 puts a Properties section between the keepalive and the payload;
    nothing here needs one, so it is the empty (single zero byte) section.
    """
    flags = 0x02  # clean session (3.1.1) / clean start (5)
    if username is not None:
        flags |= 0x80
    if password is not None:
        flags |= 0x40
    body = _encode_mqtt_string("MQTT") + bytes([protocol_version, flags, keepalive >> 8, keepalive & 0xFF])
    if protocol_version >= 5:
        body += _MQTT5_NO_PROPERTIES
    body += _encode_mqtt_string(client_id)
    if username is not None:
        body += _encode_mqtt_string(username)
    if password is not None:
        body += _encode_mqtt_string(password)
    return bytes([_CONNECT << 4]) + _encode_remaining_length(len(body)) + body


def mqtt_subscribe_packet(packet_id: int, topics: list[str], qos: int = 0, *, protocol_version: int = 4) -> bytes:
    body = packet_id.to_bytes(2, "big")
    if protocol_version >= 5:
        body += _MQTT5_NO_PROPERTIES  # no subscription identifier
    for topic in topics:
        body += _encode_mqtt_string(topic) + bytes([qos])
    return bytes([_SUBSCRIBE << 4 | 0x02]) + _encode_remaining_length(len(body)) + body


def mqtt_publish_packet(topic: str, payload: str, *, protocol_version: int = 4) -> bytes:
    """QoS-0 PUBLISH (no packet identifier in any level; MQTT 5 adds empty properties)."""
    body = _encode_mqtt_string(topic)
    if protocol_version >= 5:
        body += _MQTT5_NO_PROPERTIES
    body += payload.encode("utf-8")
    return bytes([_PUBLISH << 4]) + _encode_remaining_length(len(body)) + body


def _skip_properties(payload: bytes, pos: int) -> int:
    """Index just past the MQTT 5 Properties section starting at ``pos``."""
    length = 0
    multiplier = 1
    while True:
        if pos >= len(payload):
            raise CloudTelemetryError("truncated MQTT properties")
        byte = payload[pos]
        length += (byte & 0x7F) * multiplier
        multiplier *= 128
        pos += 1
        if not byte & 0x80:
            break
        if multiplier > 128**3:
            raise CloudTelemetryError("malformed MQTT properties length")
    if pos + length > len(payload):
        raise CloudTelemetryError("truncated MQTT properties")
    return pos + length


class MqttPacket:
    """One decoded MQTT control packet (fixed header split out)."""

    def __init__(self, ptype: int, flags: int, payload: bytes, *, protocol_version: int = 4) -> None:
        self.type = ptype
        self.flags = flags
        self.payload = payload
        # Level the session negotiated — MQTT 5 puts a Properties section in
        # PUBLISH that 3.1.1 does not have, so decoding needs to know it.
        self.protocol_version = protocol_version

    def publish_parts(self) -> tuple[str, str]:
        """(topic, payload text) of a PUBLISH; QoS 1/2 packet ids are skipped."""
        if len(self.payload) < 2:
            raise CloudTelemetryError("truncated PUBLISH packet")
        topic_len = int.from_bytes(self.payload[:2], "big")
        if len(self.payload) < 2 + topic_len:
            raise CloudTelemetryError("truncated PUBLISH topic")
        topic = self.payload[2 : 2 + topic_len].decode("utf-8", errors="replace")
        pos = 2 + topic_len
        if self.flags & 0x06:
            # QoS 1/2: a packet identifier follows (we only subscribe QoS 0,
            # but a broker that answers QoS 1 must not corrupt the payload).
            # MQTT 5 §3.3.2 orders the Variable Header Topic → Packet Identifier
            # → Properties, so the id comes *before* the properties section.
            pos += 2
        if self.protocol_version >= 5:
            pos = _skip_properties(self.payload, pos)
        if pos > len(self.payload):
            raise CloudTelemetryError("truncated PUBLISH packet")
        return topic, self.payload[pos:].decode("utf-8", errors="replace")


class MqttStreamReader:
    """Incremental MQTT packet parser: feed() bytes, next_packet() decodes."""

    # A shadow document is tens of KB; anything asking for more assembly
    # buffer is hostile or broken. Cap = 1 MiB.
    _MAX_BUFFER = 1 << 20

    def __init__(self, *, protocol_version: int = 4) -> None:
        self._buf = bytearray()
        # Set to the level the CONNACK came in on: it decides whether broker
        # PUBLISH packets carry a Properties section (MQTT 5) or not.
        self.protocol_version = protocol_version

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
        return MqttPacket(ptype_flags >> 4, ptype_flags & 0x0F, payload, protocol_version=self.protocol_version)


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

# The AWS IoT custom authorizer is selected by this query parameter, which the
# official SDK also repeats verbatim as the MQTT username (mimasek71's recipe,
# issue #18). The value is a compile-time constant of URL-safe characters, so
# the query string is built by plain interpolation.
_AUTHORIZER_QUERY = f"x-amz-customauthorizer-name={AWS_IOT_AUTHORIZER_PROD}"
_APP_STYLE_CLIENT_ID_PREFIX = "dlg-appliance-kit-android-"
_LEGACY_CLIENT_ID_PREFIX = "ha-delonghi-"


class ShadowClient:
    """One-shot MQTT/WSS session reading the named shadows for one thing.

    The custom Lambda authorizer (``dlg-prod-token-authorizer``) accepts the
    Gigya JWT, but only in the exact spelling the De'Longhi app uses: MQTT 5,
    authorizer in the query string *and* as the username, JWT as password,
    ``dlg-appliance-kit-android-`` client id (issue #18 — mimasek71 saw the
    broker drop the WebSocket for every other form). The two older spellings
    are still tried, in order, after it: a refused CONNACK, a dropped socket or
    a failed handshake moves on to the next one.
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
        ws, protocol_version = await self._connect()
        try:
            reader = MqttStreamReader(protocol_version=protocol_version)
            topics: list[str] = []
            for shadow in SHADOWS:
                topics.append(shadow_accepted_topic(self._thing, shadow))
                topics.append(shadow_rejected_topic(self._thing, shadow))
            await ws.send_bytes(mqtt_subscribe_packet(1, topics, protocol_version=protocol_version))
            _LOGGER.debug(
                "Daedalus cloud: subscribed %d shadow topics (MQTT %d), requesting get for %s",
                len(topics),
                protocol_version,
                ", ".join(SHADOWS),
            )
            for shadow in SHADOWS:
                await ws.send_bytes(
                    mqtt_publish_packet(shadow_get_topic(self._thing, shadow), "{}", protocol_version=protocol_version)
                )
            deadline = time.monotonic() + timeout
            while len(results) + len(errors) < len(SHADOWS):
                packet = reader.next_packet()
                if packet is None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    try:
                        msg = await ws.receive(timeout=remaining)
                    except TimeoutError:
                        # aiohttp *raises* at the receive deadline instead of
                        # returning: that is the shadow wait ending, not a
                        # session failure. It used to be caught by the outer
                        # clause below, discarding every shadow that did
                        # answer behind an empty "MQTT session failed: "
                        # (str(TimeoutError) is "") — the valueless
                        # cloud_error of the issue #18 retest.
                        break
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
            if results:
                missing = [s for s in SHADOWS if s not in results and s not in errors]
                if missing:
                    _LOGGER.debug(
                        "Daedalus cloud: partial shadow set after %.0fs, no answer for %s",
                        timeout,
                        ", ".join(missing),
                    )
                return results
            if errors:
                raise CloudTelemetryError("; ".join(errors.values()))
            raise CloudTelemetryError(
                f"no shadow document received within {timeout:.0f}s "
                f"({len(topics)} topics subscribed, no get/accepted or get/rejected answer)"
            )
        except (aiohttp.ClientError, TimeoutError, OSError) as exc:
            # WS resets must surface as the optional cloud error the
            # coordinator logs, never as a raw crash. The text falls back to
            # the exception type: several transport errors stringify empty.
            raise CloudTelemetryError(f"MQTT session failed: {sanitize_text(str(exc)) or type(exc).__name__}") from exc
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
                _LOGGER.debug("Daedalus cloud: %s shadow document unparsable: %s", shadow, exc)
            else:
                results[shadow] = parsed if isinstance(parsed, dict) else {"value": parsed}
                _LOGGER.debug("Daedalus cloud: %s shadow accepted (%d bytes)", shadow, len(body))
        elif topic.endswith("/get/rejected"):
            # The rejected payload is broker-controlled text — truncate hard,
            # and keep the raw body out of the log line (it can echo the
            # request context); the shadow name is what a retest needs.
            errors[shadow] = f"shadow get rejected by broker: {body[:200]}"
            _LOGGER.warning("Daedalus cloud: shadow get for %s rejected by the broker", shadow)

    def _ws_url(self, *, app_style: bool) -> str:
        """WebSocket endpoint; only the app-style one carries the authorizer."""
        path = f"/mqtt?{_AUTHORIZER_QUERY}" if app_style else "/mqtt"
        return f"{self._scheme}://{self._broker}:{self._port}{path}"

    def _connect_attempts(self) -> list[dict[str, Any]]:
        """Credential spellings, best-documented first (issue #18, mimasek71).

        The first is what the De'Longhi Android app sends and the only one the
        custom authorizer accepts; the two MQTT 3.1.1 forms are kept because
        they are what earlier captures of the protocol described.
        """
        return [
            {
                "url": self._ws_url(app_style=True),
                "username": _AUTHORIZER_QUERY,
                "password": self._jwt,
                "client_id": f"{_APP_STYLE_CLIENT_ID_PREFIX}{secrets.token_hex(8)}",
                "protocol_version": 5,
                "protocols": ("mqttv5", "mqtt"),
            },
            {
                "url": self._ws_url(app_style=False),
                "username": f"?authorizer={self._jwt}",
                "client_id": f"{_LEGACY_CLIENT_ID_PREFIX}{secrets.token_hex(4)}",
                "protocol_version": 4,
            },
            {
                "url": self._ws_url(app_style=False),
                "password": self._jwt,
                "client_id": f"{_LEGACY_CLIENT_ID_PREFIX}{secrets.token_hex(4)}",
                "protocol_version": 4,
            },
        ]

    async def _connect(self, *, connect_budget: float = 30.0) -> tuple[aiohttp.ClientWebSocketResponse, int]:
        """Open the WebSocket and CONNACK; returns (socket, negotiated level).

        Every spelling gets its own try: the broker is known to answer a wrong
        one with a refused CONNACK, a dropped socket or a failed handshake,
        and none of those should stop the next spelling from being tried.

        The three spellings share one total budget (audit #18): each attempt
        may wait at most the time left, so a black-holed broker cannot burn
        3 × (15 s ws_receive + 10 s receive) and eat the coordinator's whole
        cloud timeout before the shadow read even starts.
        """
        deadline = time.monotonic() + connect_budget
        last_rc: int | None = None
        failures: list[str] = []
        budget_exhausted = False
        for index, attempt in enumerate(self._connect_attempts(), start=1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                budget_exhausted = True
                break
            version = int(attempt["protocol_version"])
            try:
                # ClientWSTimeout, not the deprecated receive_timeout kwarg
                # (review #18, F8): at most 15 s to receive the CONNACK after
                # CONNECT, and never more than the shared connect budget.
                # The handshake itself (DNS + TCP + TLS + WS upgrade) is NOT
                # covered by ClientWSTimeout, so a black-holed broker used to
                # sit in ws_connect far past the budget on aiohttp's session
                # defaults — the wait_for bounds the whole attempt now.
                ws = await asyncio.wait_for(
                    self._session.ws_connect(
                        str(attempt["url"]),
                        # AWS IoT ATS answers 426 (Upgrade Required) to a WS
                        # upgrade that offers no MQTT subprotocol — the
                        # subprotocol IS the handshake gate on port 443
                        # (issue #18, mimasek71's 0.4.4 diagnostics).
                        protocols=attempt.get("protocols") or ("mqtt",),
                        heartbeat=30,
                        timeout=aiohttp.ClientWSTimeout(ws_receive=min(15.0, remaining)),
                    ),
                    timeout=remaining,
                )
            except (aiohttp.ClientError, TimeoutError, OSError) as exc:
                # The aiohttp text embeds the request URL — the legacy
                # spellings put the JWT in reach — so scrub at birth instead
                # of trusting downstream consumers (audit #18, C2). The label
                # and type fallback keep the aggregated error non-empty even
                # for exceptions that stringify empty (bare TimeoutError).
                failures.append(f"attempt {index}: WS connect failed: {sanitize_text(str(exc)) or type(exc).__name__}")
                continue
            opened = False
            try:
                await ws.send_bytes(
                    mqtt_connect_packet(
                        client_id=str(attempt["client_id"]),
                        username=attempt.get("username"),
                        password=attempt.get("password"),
                        keepalive=60,
                        protocol_version=version,
                    )
                )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    budget_exhausted = True
                    break
                msg = await ws.receive(timeout=min(10.0, remaining))
                if msg.type != aiohttp.WSMsgType.BINARY:
                    raise CloudTelemetryError(f"attempt {index}: expected binary CONNACK, got {msg.type!r}")
                reader = MqttStreamReader(protocol_version=version)
                reader.feed(msg.data)
                packet = reader.next_packet()
                if packet is None or packet.type != _CONNACK:
                    raise CloudTelemetryError(f"attempt {index}: no CONNACK from broker")
                # MQTT 5 moved the return code to a "reason code" but kept it
                # at payload[1] (payload[0] stays the acknowledge flags), so
                # the same success test covers both levels.
                if len(packet.payload) >= 2 and packet.payload[1] == 0:
                    opened = True
                    _LOGGER.debug("Daedalus cloud: MQTT connected on spelling %d (MQTT %d)", index, version)
                    return ws, version
                last_rc = packet.payload[1] if len(packet.payload) >= 2 else -1
                _LOGGER.debug("Daedalus cloud: spelling %d refused by the authorizer (CONNACK rc=%s)", index, last_rc)
            except TimeoutError as exc:
                failures.append(f"attempt {index}: no CONNACK in time: {sanitize_text(str(exc)) or type(exc).__name__}")
            except (aiohttp.ClientError, OSError) as exc:
                failures.append(f"attempt {index}: {sanitize_text(str(exc)) or type(exc).__name__}")
            except CloudTelemetryError as exc:
                failures.append(str(exc))
            finally:
                if not opened:
                    with contextlib.suppress(Exception):
                        await ws.close()
        if budget_exhausted:
            raise CloudTelemetryError("connect budget exhausted")
        if last_rc is not None:
            raise CloudTelemetryError(f"MQTT broker refused the JWT (CONNACK rc={last_rc})")
        raise CloudTelemetryError(f"MQTT WS connect failed: {'; '.join(failures) if failures else 'no attempt'}")


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
