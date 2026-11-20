"""Read-only cloud telemetry for the Daedalus stack (issue #18, mimasek71).

Two hops, both JWT-authenticated:

1. REST device directory (AWS API Gateway, ``AWS_REST_BASE_URL_PROD`` plus
   the static ``source: mycoffeelounge`` header): resolve the cloud
   ``machineName`` (AWS IoT thing name) from the serial printed on the
   machine. The directory answers grouped under ``ownedByMe`` /
   ``sharedToMe``.
2. AWS IoT Core device shadows over MQTT 5 / WebSocket (WSS:443): read the
   named shadows ``MachineStatus``, ``MachineCapabilities``, and optional
   ``BrewingStatisticsTotals``. Read-only —
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
1.5.0, EU pool). Cloud failures are reported separately from the LAN path.

The MQTT codec supports the control packets used by the telemetry reader
and the MQTT 5 publish properties used by the separate warm-up action.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from .const import (
    AUTO_OFF_DELAY,
    AWS_IOT_AUTHORIZER_PROD,
    AWS_IOT_BROKER_PROD,
    AWS_REST_BASE_URL_LEGACY_PROD,
    AWS_REST_BASE_URL_PROD,
    AWS_REST_SOURCE_HEADER,
    BREWING_STATISTICS_KEYS,
    CLOUD_POLL_SECONDS,
    MAINTENANCE_COUNTER_KEYS,
    MAINTENANCE_PERCENTAGE_KEYS,
    WATER_FILTER_INSTALLED,
)
from .lan_protocol import LanProtocolError, validate_lan_host
from .redact import get_logger, mask_serial, sanitize_text

_LOGGER = get_logger(__name__)

SHADOWS: tuple[str, ...] = ("MachineStatus", "MachineCapabilities")
_MACHINE_SETTINGS_SHADOW = "MachineSettings"
_BREWING_STATISTICS_SHADOW = "BrewingStatisticsTotals"
_DRINK_VISIBILITY_SHADOW = "DrinkVisibility"
_DRINK_VISIBILITY_KEY = re.compile(r"id_[0-9]+")

_CONNECT = 1
_CONNACK = 2
_PUBLISH = 3
_SUBSCRIBE = 8
_SUBACK = 9

# MQTT 5 inserts a Properties section where 3.1.1 has nothing. The empty
# section is encoded as a single zero byte.
_MQTT5_NO_PROPERTIES = b"\x00"

_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)


class CloudTelemetryError(RuntimeError):
    """Any failure on the cloud path. Callers must treat it as optional."""


class CloudMachineNotFound(CloudTelemetryError):
    """The directory did not confirm one machine for this serial."""


@dataclass(frozen=True)
class CloudState:
    """Shadows returned for this fetch and a reason for withholding status."""

    shadows: dict[str, dict[str, Any]]
    machine_name: str
    status_error: str | None = None


# --------------------------------------------------------------------- REST --

# The live /devices answer groups the machines by ownership (issue #18,
# mimasek71's capture); older captures were a bare list or a wrapper dict.
_GROUPED_DEVICE_KEYS: tuple[str, ...] = ("ownedByMe", "sharedToMe")
_DIRECTORY_SERIAL_KEYS = ("serialNumber", "serial_number", "serial", "serialNo", "SerialNo")
_DIRECTORY_NAME_KEYS = ("machineName", "machine_name", "thingName", "thing_name", "name")
_DIRECTORY_MODEL_RE = re.compile(r"(?:ECAM|EXAM)[0-9]{3}\.?[0-9]{2}[A-Z]{0,3}", re.ASCII | re.IGNORECASE)


@dataclass(frozen=True)
class DirectoryMachine:
    """Identity fields copied from one authenticated account directory record."""

    serial: str
    thing_name: str | None
    model: str | None


def _directory_records(payload: Any) -> list[Any]:
    if isinstance(payload, dict):
        grouped = [key for key in _GROUPED_DEVICE_KEYS if isinstance(payload.get(key), list)]
        if grouped:
            return [device for key in grouped for device in payload[key]]
        for wrapper in ("devices", "items", "result", "data"):
            inner = payload.get(wrapper)
            if isinstance(inner, list):
                return inner
    if isinstance(payload, list):
        return payload
    raise CloudTelemetryError("/devices response was neither a list nor a known wrapper")


def machine_model_code(value: object) -> str | None:
    """Validate the De'Longhi product code used in device metadata."""
    model = value.strip().upper() if isinstance(value, str) else ""
    return model if _DIRECTORY_MODEL_RE.fullmatch(model) else None


def _directory_machines(payload: Any) -> list[DirectoryMachine]:
    records = _directory_records(payload)
    machines: dict[str, DirectoryMachine] = {}
    ambiguous: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        serial = next(
            (
                str(value).strip()
                for field in _DIRECTORY_SERIAL_KEYS
                if isinstance(value := record.get(field), (str, int))
                and not isinstance(value, bool)
                and re.fullmatch(r"[A-Za-z0-9._-]{1,128}", str(value).strip())
            ),
            None,
        )
        if serial is None:
            continue
        thing = next(
            (
                value
                for field in _DIRECTORY_NAME_KEYS
                if isinstance(value := record.get(field), str) and _THING_NAME_RE.fullmatch(value)
            ),
            None,
        )
        model = machine_model_code(record.get("machineModel"))
        if model is not None and model.casefold() in (
            serial.casefold(),
            (thing or "").casefold(),
        ):
            model = None
        key = serial.casefold()
        machine = DirectoryMachine(serial, thing, model)
        existing = machines.get(key)
        if existing is None:
            machines[key] = machine
        elif existing.thing_name != machine.thing_name:
            ambiguous.add(key)
    result = [machine for key, machine in machines.items() if key not in ambiguous]
    if records and not result:
        raise CloudTelemetryError("/devices response contained no usable machine records")
    return result


async def list_machines(session: aiohttp.ClientSession, *, jwt: str) -> list[DirectoryMachine]:
    """List owner and shared machines from the current app directory."""
    payload = await _fetch_devices(
        session, url=f"{AWS_REST_BASE_URL_PROD.rstrip('/')}/devices", jwt=jwt, allow_redirects=False
    )
    return _directory_machines(payload)


def extract_machine_name(payload: Any, serial: str) -> str:
    """Pull the cloud machineName for ``serial`` out of a /devices payload.

    Defensive on purpose: the endpoint shape was captured from one app
    version. Accepts the grouped live shape (``ownedByMe`` / ``sharedToMe``
    lists, flattened in that order), a bare list, or a wrapper dict
    (``devices`` / ``items`` / ``result`` / ``data``), and matches the serial
    case-insensitively against any key containing ``serial``.
    """
    _, name = _extract_machine_record(payload, serial)
    return name


def _directory_presence(device: dict[str, Any]) -> str | None:
    """Normalize presence, treating a malformed field as unknown."""
    presence = device.get("status")
    if presence is None:
        return None
    return presence.strip().upper() if isinstance(presence, str) else ""


def _extract_machine_record(payload: Any, serial: str) -> tuple[dict[str, Any], str]:
    target = serial.strip().lower()
    match = None
    for device in _directory_records(payload):
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
                    if match is not None and match[1] != value:
                        raise CloudMachineNotFound(f"serial {mask_serial(serial)} is ambiguous in /devices response")
                    if match is None or _directory_presence(device) == "OFFLINE":
                        match = (device, value)
                    break
    if match is None:
        raise CloudMachineNotFound(f"serial {mask_serial(serial)} not found in /devices response")
    return match


async def _fetch_devices(session: aiohttp.ClientSession, *, url: str, jwt: str, allow_redirects: bool = False) -> Any:
    """GET one /devices URL. Raises CloudTelemetryError on any bad answer."""
    headers = {"Authorization": f"Bearer {jwt}", "source": AWS_REST_SOURCE_HEADER}
    try:
        async with session.get(url, headers=headers, timeout=_REQUEST_TIMEOUT, allow_redirects=allow_redirects) as resp:
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
    _, name = await _lookup_machine_record(
        session, serial=serial, jwt=jwt, base_url=base_url, fallback_base_url=fallback_base_url
    )
    return name


async def _lookup_machine_record(
    session: aiohttp.ClientSession,
    *,
    serial: str,
    jwt: str,
    base_url: str = AWS_REST_BASE_URL_PROD,
    fallback_base_url: str = AWS_REST_BASE_URL_LEGACY_PROD,
) -> tuple[dict[str, Any], str]:
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
        return _extract_machine_record(payload, serial)
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


def mqtt_publish_packet(
    topic: str,
    payload: str,
    *,
    protocol_version: int = 4,
    packet_id: int | None = None,
    expiry_seconds: int | None = None,
) -> bytes:
    """Non-retained PUBLISH, using QoS 1 when a packet identifier is supplied."""
    if expiry_seconds is not None and protocol_version < 5:
        raise ValueError("MQTT PUBLISH expiry requires MQTT 5")
    body = _encode_mqtt_string(topic)
    if packet_id is not None:
        if not 1 <= packet_id <= 65535:
            raise ValueError("invalid MQTT packet identifier")
        body += packet_id.to_bytes(2, "big")
    if protocol_version >= 5:
        # Five property bytes: the Message Expiry Interval identifier followed
        # by the four-byte unsigned interval in seconds.
        body += _MQTT5_NO_PROPERTIES if expiry_seconds is None else b"\x05\x02" + expiry_seconds.to_bytes(4, "big")
    body += payload.encode("utf-8")
    flags = 0x02 if packet_id is not None else 0
    return bytes([_PUBLISH << 4 | flags]) + _encode_remaining_length(len(body)) + body


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


def _shadow_from_topic(topic: str, shadows: tuple[str, ...] = SHADOWS) -> str | None:
    """Return the shadow name if the topic belongs to a requested shadow."""
    parts = topic.split("/")
    # $aws/things/<thing>/shadow/name/<shadow>/get[/suffix]
    if len(parts) >= 6 and parts[:2] == ["$aws", "things"] and parts[3] == "shadow":
        shadow = parts[5]
        return shadow if shadow in shadows else None
    return None


# ----------------------------------------------------------- shadow client --

# The AWS IoT custom authorizer is selected by this query parameter, which the
# official SDK also repeats verbatim as the MQTT username (mimasek71's recipe,
# issue #18). The value is a compile-time constant of URL-safe characters, so
# the query string is built by plain interpolation.
_AUTHORIZER_QUERY = f"x-amz-customauthorizer-name={AWS_IOT_AUTHORIZER_PROD}"
_APP_STYLE_CLIENT_ID_PREFIX = "dlg-appliance-kit-android-"
_LEGACY_CLIENT_ID_PREFIX = "ha-delonghi-"


def _suback_reasons(packet: MqttPacket, protocol_version: int) -> list[int]:
    """Per-topic reason codes of a SUBACK (MQTT 5) or return codes (3.1.1).

    Anything >= 0x80 is a refusal; the value names why (0x87 not authorised,
    0x83 quota, 0x80 unspecified). Empty list when the packet is too short to
    trust — never guess a refusal from a malformed frame.
    """
    payload = packet.payload
    if len(payload) < 3:
        return []
    pos = 2
    if protocol_version >= 5:
        pos = _skip_properties(payload, pos)
    return list(payload[pos:])


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

    async def fetch(self, *, timeout: float = 20.0, shadows: tuple[str, ...] = SHADOWS) -> dict[str, dict[str, Any]]:
        """Return {shadow name: parsed shadow document} (partial OK)."""
        results: dict[str, dict[str, Any]] = {}
        errors: dict[str, str] = {}
        ws, protocol_version = await self._connect()
        try:
            topics: list[str] = []
            for shadow in shadows:
                topics.append(shadow_accepted_topic(self._thing, shadow))
                topics.append(shadow_rejected_topic(self._thing, shadow))
            await ws.send_bytes(mqtt_subscribe_packet(1, topics, protocol_version=protocol_version))
            _LOGGER.debug(
                "Daedalus cloud: SUBSCRIBE sent for %d shadow topics on thing %s (MQTT %d), awaiting SUBACK",
                len(topics),
                mask_serial(self._thing),
                protocol_version,
            )
            # The gets must not outrun the SUBACK: a "subscribed" line printed
            # before the acknowledgment proves nothing (issue #18, mimasek71 on
            # 0.4.6 — no refusal error was also consistent with a SUBACK that
            # never arrived). Wait for it, log every reason code, fail loudly
            # when it is missing.
            reader = MqttStreamReader(protocol_version=protocol_version)
            suback_codes: list[int] = []
            suback_deadline = time.monotonic() + min(10.0, timeout)
            while not suback_codes:
                packet = reader.next_packet()
                if packet is None:
                    try:
                        msg = await ws.receive(timeout=max(0.1, suback_deadline - time.monotonic()))
                    except TimeoutError:
                        break
                    if msg.type == aiohttp.WSMsgType.BINARY:
                        reader.feed(msg.data)
                    elif msg.type in (
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.CLOSING,
                        aiohttp.WSMsgType.ERROR,
                    ):
                        raise CloudTelemetryError("MQTT WS closed by broker before SUBACK")
                    continue
                if packet.type == _SUBACK:
                    suback_codes += _suback_reasons(packet, protocol_version)
            if not suback_codes:
                raise CloudTelemetryError(
                    f"no SUBACK received within {min(10.0, timeout):.0f}s — the broker accepted the "
                    "session but never acknowledged the subscriptions"
                )
            _LOGGER.debug(
                "Daedalus cloud: SUBACK reason codes: %s",
                ", ".join(f"0x{code:02x}" for code in suback_codes),
            )
            if all(code >= 0x80 for code in suback_codes):
                raise CloudTelemetryError(
                    f"broker refused all {len(suback_codes)} shadow topic subscriptions "
                    f"(reason 0x{suback_codes[0]:02x}) — thing name or authorizer policy mismatch"
                )
            if any(code >= 0x80 for code in suback_codes):
                _LOGGER.warning(
                    "Daedalus cloud: %d of %d shadow topic subscriptions refused (first reason 0x%02x)",
                    sum(1 for code in suback_codes if code >= 0x80),
                    len(suback_codes),
                    next(code for code in suback_codes if code >= 0x80),
                )
            _LOGGER.debug("Daedalus cloud: requesting get for %s", ", ".join(shadows))
            for shadow in shadows:
                await ws.send_bytes(
                    mqtt_publish_packet(shadow_get_topic(self._thing, shadow), "{}", protocol_version=protocol_version)
                )
            deadline = time.monotonic() + timeout
            while len(results) + len(errors) < len(shadows):
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
                        aiohttp.WSMsgType.CLOSE,
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.CLOSING,
                        aiohttp.WSMsgType.ERROR,
                    ):
                        if results:
                            break
                        raise CloudTelemetryError("MQTT WS closed by broker")
                    continue
                if packet.type == _SUBACK:
                    suback_codes += _suback_reasons(packet, protocol_version)
                    continue
                if packet.type != _PUBLISH:
                    continue
                self._route_publish(packet, results, errors, shadows)
            if results:
                missing = [s for s in shadows if s not in results and s not in errors]
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
            if results:
                return results
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
        shadows: tuple[str, ...],
    ) -> None:
        topic, body = packet.publish_parts()
        shadow = _shadow_from_topic(topic, shadows)
        if shadow is None or shadow in results or shadow in errors:
            return
        if topic not in (shadow_accepted_topic(self._thing, shadow), shadow_rejected_topic(self._thing, shadow)):
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
            if shadow == _MACHINE_SETTINGS_SHADOW:
                errors[shadow] = "MachineSettings shadow get rejected by broker"
                _LOGGER.debug("Daedalus cloud: shadow get for %s rejected by the broker", shadow)
                return
            # The rejected payload is broker-controlled text — truncate hard,
            # and keep the raw body out of the log line (it can echo the
            # request context); the shadow name is what a retest needs.
            errors[shadow] = f"shadow get rejected by broker: {body[:200]}"
            log = _LOGGER.debug if shadow in (_BREWING_STATISTICS_SHADOW, _DRINK_VISIBILITY_SHADOW) else _LOGGER.warning
            log("Daedalus cloud: shadow get for %s rejected by the broker", shadow)

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
    check_device_status: bool = False,
) -> CloudState:
    """Read shadows and determine whether to publish the last reported status."""
    thing = machine_name
    directory_matched = False
    presence = None
    if check_device_status or not thing:
        try:
            device, name = await _lookup_machine_record(session, serial=serial, jwt=jwt)
        except CloudMachineNotFound:
            raise
        except CloudTelemetryError:
            if not thing:
                raise
        else:
            thing = name
            directory_matched = True
            presence = _directory_presence(device)
    if not _THING_NAME_RE.fullmatch(str(thing)):
        # Never echo the rejected value: it is the hostile input itself.
        raise CloudTelemetryError(f"cloud thing name rejected by shape check ({len(str(thing))} chars)")
    client = ShadowClient(session, thing_name=thing, jwt=jwt)
    docs = await client.fetch()
    status_error = None
    if check_device_status:
        if presence == "OFFLINE":
            status_error = "Device directory reports the machine offline"
        elif "MachineStatus" not in docs:
            status_error = "MachineStatus shadow was not returned"
        # A matching directory record with no presence field is normal. Its
        # shadow is the last reported state, which can stay unchanged for hours.
        # With only a stored thing name, require a recent Status report. An
        # old capabilities shadow can still be read after a machine disconnects.
        elif not (directory_matched and presence in (None, "ONLINE")) and not _recent_reported_status(
            docs["MachineStatus"]
        ):
            status_error = (
                "Device directory returned unknown presence and MachineStatus has no recent Status timestamp"
                if directory_matched
                else "Device directory lookup failed and MachineStatus has no recent Status timestamp"
            )
    if status_error is not None:
        docs = {name: doc for name, doc in docs.items() if name != "MachineStatus"}
    return CloudState(docs, thing, status_error)


async def fetch_brewing_statistics(session: aiohttp.ClientSession, *, thing_name: str, jwt: str) -> dict[str, int]:
    """Read the verified lifetime counters from the optional statistics shadow."""
    client = ShadowClient(session, thing_name=thing_name, jwt=jwt)
    documents = await client.fetch(shadows=(_BREWING_STATISTICS_SHADOW,), timeout=5)
    state = documents[_BREWING_STATISTICS_SHADOW].get("state")
    reported = state.get("reported") if isinstance(state, dict) else None
    return _nonnegative_integers(reported, BREWING_STATISTICS_KEYS)


@dataclass(slots=True)
class MachineSettingsTelemetry:
    """Verified read-only lifetime counters and settings from one shadow."""

    maintenance_counters: dict[str, int] = field(default_factory=dict)
    machine_settings: dict[str, int | bool] = field(default_factory=dict)


async def fetch_machine_settings(
    session: aiohttp.ClientSession, *, thing_name: str, jwt: str
) -> MachineSettingsTelemetry:
    """Read only verified fields from the reported MachineSettings shadow."""
    if not _THING_NAME_RE.fullmatch(thing_name):
        raise CloudTelemetryError("Invalid cloud thing name")
    client = ShadowClient(session, thing_name=thing_name, jwt=jwt)
    documents = await client.fetch(shadows=(_MACHINE_SETTINGS_SHADOW,), timeout=5)
    state = documents.get(_MACHINE_SETTINGS_SHADOW, {}).get("state")
    reported = state.get("reported") if isinstance(state, dict) else None
    if not isinstance(reported, dict):
        return MachineSettingsTelemetry()

    read_only = reported.get("NotEditable")
    counters = _nonnegative_integers(read_only, MAINTENANCE_COUNTER_KEYS)
    percentages = _nonnegative_integers(read_only, MAINTENANCE_PERCENTAGE_KEYS)
    settings: dict[str, int | bool] = {key: value for key, value in percentages.items() if value <= 100}
    editable = reported.get("Editable")
    if isinstance(editable, dict):
        settings.update(_nonnegative_integers(editable, {"AutoTurnOffTimeInMin": AUTO_OFF_DELAY}))
        user_config = editable.get("UserConf")
        if isinstance(user_config, dict) and type(installed := user_config.get("FilterInstall")) is bool:
            settings[WATER_FILTER_INSTALLED] = installed
            if not installed:
                settings.pop(MAINTENANCE_PERCENTAGE_KEYS["FilterUsagePerc"], None)
    return MachineSettingsTelemetry(counters, settings)


def _nonnegative_integers(reported: Any, keys: dict[str, str]) -> dict[str, int]:
    if not isinstance(reported, dict):
        return {}
    values: dict[str, int] = {}
    for source_key, key in keys.items():
        value = reported.get(source_key)
        if type(value) is int and value >= 0:
            values[key] = value
    return values


async def fetch_visible_drinks_count(session: aiohttp.ClientSession, *, thing_name: str, jwt: str) -> int | None:
    """Count the machine's reported visibility flags without keeping recipe identifiers."""
    client = ShadowClient(session, thing_name=thing_name, jwt=jwt)
    documents = await client.fetch(shadows=(_DRINK_VISIBILITY_SHADOW,), timeout=5)
    state = documents.get(_DRINK_VISIBILITY_SHADOW, {}).get("state")
    reported = state.get("reported") if isinstance(state, dict) else None
    if not isinstance(reported, dict) or not reported:
        return None
    if any(
        not _DRINK_VISIBILITY_KEY.fullmatch(key) or type(value) is not int or value not in (0, 1)
        for key, value in reported.items()
    ):
        return None
    return sum(reported.values())


async def fetch_reported_lan_ip(session: aiohttp.ClientSession, *, thing_name: str, jwt: str) -> str | None:
    """Get the selected machine's reported LAN address as an untrusted hint."""
    if not _THING_NAME_RE.fullmatch(thing_name):
        raise CloudTelemetryError("Invalid cloud thing name")
    client = ShadowClient(session, thing_name=thing_name, jwt=jwt)
    try:
        async with asyncio.timeout(12):
            documents = await client.fetch(timeout=3)
    except (aiohttp.ClientError, OSError) as exc:
        raise CloudTelemetryError("The machine's LAN address could not be read") from exc
    state = documents.get("MachineStatus", {}).get("state")
    reported = state.get("reported") if isinstance(state, dict) else None
    value = reported.get("LanIpAddress") if isinstance(reported, dict) else None
    if isinstance(value, str):
        with contextlib.suppress(LanProtocolError):
            return validate_lan_host(value.strip())
    return None


def _recent_reported_status(document: Any) -> bool:
    """Allow one polling interval of age or clock skew on the Status change."""
    for key in ("metadata", "reported", "Status", "timestamp"):
        if not isinstance(document, dict):
            return False
        document = document.get(key)
    now = time.time()
    # The bounded comparison also excludes NaN and infinities.
    return type(document) in (int, float) and now - CLOUD_POLL_SECONDS <= document <= now + CLOUD_POLL_SECONDS
