"""Authenticated EU PrimaDonna Aromatic warm-up, with one command per session.

Wire format and ownership check adapted from FA9US/mycoffeeHASS (MIT),
revision 7b34164d0a3a5ec1032c410c6f35253362732328.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
import string
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp

from . import cloud
from .const import AWS_REST_BASE_URL_PROD

COMMAND_EXPIRY_SECONDS = 15
_SHADOW = "MachineStatus"
_MQTT_VERSION = 5
_MQTT_DISCONNECT = 14
_SUPPORTED_MODEL = "ECAM63070"
_SUPPORTED_FIRMWARE = {"FWUIVersion": "1.2.0", "FWMainboardVersion": "1.2.0", "FWWiFiVersion": "2.5.3"}


class WarmupError(RuntimeError):
    """Warm-up could not be completed."""


class _BrokerDisconnected(Exception):
    """The EU MQTT session disconnected."""


def owned_device(payload: Any, serial: str) -> dict[str, Any]:
    """Resolve an unambiguous owner-directory record for the configured serial."""
    devices = payload.get("ownedByMe") if isinstance(payload, dict) else None
    if not serial.strip() or not isinstance(devices, list) or not all(isinstance(device, dict) for device in devices):
        raise WarmupError("The EU owner directory did not return a usable device list.")
    matches = [
        device
        for device in devices
        if isinstance(value := device.get("serialNumber"), str)
        and value.strip().casefold() == serial.strip().casefold()
    ]
    if len(matches) != 1:
        raise WarmupError("The EU account must own exactly one device with the configured serial number.")
    device = matches[0]
    thing = device.get("machineName")
    if not isinstance(thing, str) or not cloud._THING_NAME_RE.fullmatch(thing):
        raise WarmupError("The EU owner directory did not return a valid cloud name for this machine.")
    return device


async def fetch_owned_device(session: aiohttp.ClientSession, *, serial: str, jwt: str) -> dict[str, Any]:
    """Verify ownership on the current EU endpoint for every call."""
    try:
        payload = await cloud._fetch_devices(
            session, url=f"{AWS_REST_BASE_URL_PROD}devices", jwt=jwt, allow_redirects=False
        )
    except cloud.CloudTelemetryError:
        raise WarmupError("The EU owner directory could not be read.") from None
    return owned_device(payload, serial)


async def resolve_owned_aromatic(session: aiohttp.ClientSession, *, serial: str, jwt: str) -> str:
    """Return the cloud name only if the owner directory identifies the supported model."""
    device = await fetch_owned_device(session, serial=serial, jwt=jwt)
    if device.get("machineModel") != _SUPPORTED_MODEL:
        raise WarmupError("The EU owner directory did not identify a PrimaDonna Aromatic ECAM630.70.")
    return device["machineName"]


async def _next_packet(ws: aiohttp.ClientWebSocketResponse, reader: cloud.MqttStreamReader) -> cloud.MqttPacket:
    while (packet := reader.next_packet()) is None:
        message = await ws.receive()
        if message.type == aiohttp.WSMsgType.BINARY:
            reader.feed(message.data)
        elif message.type in (
            aiohttp.WSMsgType.CLOSE,
            aiohttp.WSMsgType.CLOSED,
            aiohttp.WSMsgType.CLOSING,
            aiohttp.WSMsgType.ERROR,
        ):
            raise _BrokerDisconnected
    if packet.type == _MQTT_DISCONNECT:
        raise _BrokerDisconnected
    return packet


async def _next_publication(
    ws: aiohttp.ClientWebSocketResponse, reader: cloud.MqttStreamReader
) -> tuple[str, dict[str, Any]]:
    while True:
        packet = await _next_packet(ws, reader)
        if packet.type != cloud._PUBLISH:
            continue
        topic, payload = packet.publish_parts()
        if packet.flags & 0x06 == 0x02:  # QoS 1 deliveries require a PUBACK.
            pos = 2 + int.from_bytes(packet.payload[:2], "big")
            packet_id = packet.payload[pos : pos + 2]
            await ws.send_bytes(b"\x40\x02" + packet_id)
        if packet.flags & 0x01 or len(payload) > 131_072:  # Retained messages cannot qualify.
            continue
        try:
            body = json.loads(payload)
        except (ValueError, RecursionError):
            continue
        if isinstance(body, dict):
            return topic, body


class WarmupClient(cloud.ShadowClient):
    """Reuse the app-aligned MQTT 5 transport for one explicit ON request."""

    def _connect_attempts(self) -> list[dict[str, Any]]:
        return super()._connect_attempts()[:1]

    async def verify_firmware(self) -> None:
        """Read the firmware versions from the current MachineCapabilities shadow."""
        try:
            async with asyncio.timeout(25):
                document = (await self.fetch(timeout=10)).get("MachineCapabilities")
        except (cloud.CloudTelemetryError, OSError, TimeoutError):
            raise WarmupError(
                "Could not read the machine firmware from the EU broker. No warm-up command was sent."
            ) from None
        if document is None:
            raise WarmupError("The EU broker returned no MachineCapabilities document. No warm-up command was sent.")
        reported = _reported(document)
        if any(reported.get(key) != value for key, value in _SUPPORTED_FIRMWARE.items()):
            raise WarmupError(
                "Warm-up requires UI 1.2.0, mainboard 1.2.0, and Wi-Fi 2.5.3. No warm-up command was sent."
            )

    async def warm_up(self, *, before_send: Callable[[], Awaitable[None] | None]) -> None:
        """Wait for Standby before sending, then require a machine reply."""
        ws = None
        sent = False
        try:
            async with asyncio.timeout(25):
                ws, _ = await self._connect(connect_budget=10)
                reader = cloud.MqttStreamReader(protocol_version=_MQTT_VERSION)
                accepted = cloud.shadow_accepted_topic(self._thing, _SHADOW)
                rejected = cloud.shadow_rejected_topic(self._thing, _SHADOW)
                reply = f"{self._thing}/commands/response"
                await ws.send_bytes(
                    cloud.mqtt_subscribe_packet(1, [accepted, rejected, reply], qos=1, protocol_version=_MQTT_VERSION)
                )
                while True:
                    packet = await _next_packet(ws, reader)
                    if packet.type == cloud._SUBACK and packet.payload[:2] == b"\x00\x01":
                        reasons = cloud._suback_reasons(packet, _MQTT_VERSION)
                        if len(reasons) != 3 or any(reason not in (0, 1) for reason in reasons):
                            raise WarmupError("The broker refused the status or command-reply subscription.")
                        break
                nonce = secrets.token_hex(12)
                await ws.send_bytes(
                    cloud.mqtt_publish_packet(
                        cloud.shadow_get_topic(self._thing, _SHADOW),
                        json.dumps({"clientToken": nonce}),
                        protocol_version=_MQTT_VERSION,
                        packet_id=2,
                    )
                )
                while True:
                    topic, body = await _next_publication(ws, reader)
                    if body.get("clientToken") != nonce:
                        continue
                    if topic == rejected:
                        raise WarmupError("The broker refused the current machine status request.")
                    if topic == accepted:
                        if _reported(body).get("Status") != "Standby":
                            raise WarmupError("Warm-up requires the machine to report Standby.")
                        break
                request_id = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(5))
                command = cloud.mqtt_publish_packet(
                    f"{self._thing}/commands/request",
                    json.dumps({"AppId": "MCL", "Message": "MachineUI", "TurnOnOff": 2, "RequestId": request_id}),
                    protocol_version=_MQTT_VERSION,
                    packet_id=3,
                    expiry_seconds=COMMAND_EXPIRY_SECONDS,
                )
                if (reservation := before_send()) is not None:
                    await reservation
                sent = True
                await ws.send_bytes(command)
                while True:
                    topic, body = await _next_publication(ws, reader)
                    if (
                        topic == reply
                        and body.get("Message") == "MachineUIResponse"
                        and body.get("RequestId") == request_id
                    ):
                        if body.get("Response") != "OK":
                            raise WarmupError(
                                "The machine did not acknowledge success. Check it before enabling warm-up again."
                            )
                        return
        except (_BrokerDisconnected, cloud.CloudTelemetryError, aiohttp.ClientError, OSError, TimeoutError):
            if sent:
                raise WarmupError(
                    "Warm-up delivery is uncertain. Check the machine before enabling warm-up again."
                ) from None
            raise WarmupError("Could not verify readiness on the EU broker. No warm-up command was sent.") from None
        finally:
            if ws is not None:
                with contextlib.suppress(Exception):
                    await ws.close()


def _reported(document: dict[str, Any]) -> dict[str, Any]:
    state = document.get("state")
    reported = state.get("reported") if isinstance(state, dict) else None
    return reported if isinstance(reported, dict) else {}
