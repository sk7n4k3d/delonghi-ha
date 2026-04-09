"""Local LAN communication for De'Longhi WiFi coffee machines.

Implements the Ayla Networks local LAN protocol so the integration can talk
to the machine directly on the LAN — bypassing the Ayla cloud entirely.
This eliminates IP rate limits (#9), the MQTT keepalive hack (#16), and the
stale monitor issue on models like PrimaDonna Soul where app_device_connected
is unsupported.

Protocol overview (Ayla LAN v1):
    1. Integration fetches lan_enabled + lan_ip + lan_key from Ayla cloud
       (already implemented in api.get_lan_config).
    2. Integration starts an embedded aiohttp server (default port 10280).
    3. Integration POSTs to http://<lan_ip>/local_lan/key_exchange.json with
       {"random_1": <base64>, "time_1": <ts>, "proto": 1, "key_id": <local_id>}.
    4. Machine replies with its own random_2 + the lan_key_id.
    5. Both sides derive:
          app_key  = HMAC_SHA256(lan_key, random_1 + random_2).digest()[:16]
          app_iv   = HMAC_SHA256(lan_key, random_2 + random_1).digest()[:16]
          dev_key  = HMAC_SHA256(lan_key, random_2 + random_1).digest()[16:32]
                     (truncated to 16 bytes)
          dev_iv   = HMAC_SHA256(lan_key, random_1 + random_2).digest()[16:32]
          sign_key = HMAC_SHA256(lan_key, b"sign" + random_1 + random_2).digest()
       Integration-to-device traffic is encrypted with (app_key, app_iv),
       device-to-integration traffic with (dev_key, dev_iv). The sign_key is
       used to HMAC each payload for integrity.
    6. Machine then pushes property datapoints to
       POST http://<ha>/local_lan/property/datapoint.json and polls
       GET  http://<ha>/local_lan/commands.json for pending commands.

This module provides the crypto primitives and an embedded aiohttp server.
It is NOT yet wired into __init__.py — cloud mode stays the default. Wiring
happens in a follow-up commit after the protocol has been validated against
a real PrimaDonna Soul on someone else's network (thanks @lodzen).

Credit: cremalink-ha (@miditkl) and ECAMpy (@duckwc) for the first public
PrimaDonna Soul LAN implementations — used as protocol reference.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

try:  # Home Assistant already ships aiohttp; fall back to None in unit tests.
    from aiohttp import web
except ImportError:  # pragma: no cover - aiohttp missing is only possible in tests.
    web = None  # type: ignore[assignment]

from cryptography.hazmat.primitives import padding as crypto_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

_LOGGER = logging.getLogger(__name__)

LAN_SERVER_DEFAULT_PORT: int = 10280
LAN_PROTO_VERSION: int = 1
LAN_HANDSHAKE_PATH: str = "/local_lan/key_exchange.json"
LAN_COMMAND_PATH: str = "/local_lan/commands.json"
LAN_PROPERTY_PATH: str = "/local_lan/property/datapoint.json"
LAN_STATUS_PATH: str = "/local_lan/status.json"

_HANDSHAKE_TIMEOUT_S: float = 5.0
_COMMAND_POLL_TIMEOUT_S: float = 30.0


class LanError(Exception):
    """Base exception for LAN errors."""


class LanHandshakeError(LanError):
    """Raised when the key exchange with the machine fails."""


class LanCryptoError(LanError):
    """Raised on encryption / decryption / signature errors."""


# ─────────────────────────────────────────────────────────────────────────
# Crypto — pure functions, no I/O, trivially unit-testable.
# ─────────────────────────────────────────────────────────────────────────


@dataclass
class LanSession:
    """Derived keys and state for a single LAN session."""

    app_key: bytes
    app_iv: bytes
    dev_key: bytes
    dev_iv: bytes
    sign_key: bytes
    key_id: int
    random_1: bytes
    random_2: bytes
    seq_out: int = 0  # counter for app → device
    seq_in: int = 0  # counter for device → app
    created_at: float = field(default_factory=time.time)

    def fresh_iv(self, base_iv: bytes, seq: int) -> bytes:
        """Derive a per-message IV from the base IV and the sequence number."""
        if len(base_iv) != 16:
            raise LanCryptoError(f"IV must be 16 bytes, got {len(base_iv)}")
        # Rotate IV by XORing the last 4 bytes with the sequence counter.
        tail = (int.from_bytes(base_iv[12:16], "big") ^ seq).to_bytes(4, "big")
        return base_iv[:12] + tail


def derive_session(lan_key: bytes, random_1: bytes, random_2: bytes, key_id: int) -> LanSession:
    """Derive the AES keys, IVs, and sign key from the LAN master key.

    Follows the Ayla LAN v1 derivation:
        app_key  = HMAC(lan_key, r1 + r2)[:16]
        dev_key  = HMAC(lan_key, r2 + r1)[:16]
        app_iv   = HMAC(lan_key, r2 + r1)[16:32]
        dev_iv   = HMAC(lan_key, r1 + r2)[16:32]
        sign_key = HMAC(lan_key, b"sign" + r1 + r2)

    Raises LanCryptoError if the lengths are wrong.
    """
    if len(lan_key) < 16:
        raise LanCryptoError(f"lan_key must be >= 16 bytes, got {len(lan_key)}")
    if len(random_1) != 16 or len(random_2) != 16:
        raise LanCryptoError("random_1 / random_2 must each be 16 bytes")

    h_12 = hmac.new(lan_key, random_1 + random_2, hashlib.sha256).digest()
    h_21 = hmac.new(lan_key, random_2 + random_1, hashlib.sha256).digest()
    sign = hmac.new(lan_key, b"sign" + random_1 + random_2, hashlib.sha256).digest()

    return LanSession(
        app_key=h_12[:16],
        app_iv=h_21[16:32],
        dev_key=h_21[:16],
        dev_iv=h_12[16:32],
        sign_key=sign,
        key_id=key_id,
        random_1=random_1,
        random_2=random_2,
    )


def _aes_encrypt(key: bytes, iv: bytes, plaintext: bytes) -> bytes:
    """AES-128-CBC with PKCS7 padding."""
    padder = crypto_padding.PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    encryptor = cipher.encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def _aes_decrypt(key: bytes, iv: bytes, ciphertext: bytes) -> bytes:
    """Reverse of _aes_encrypt. Raises on padding errors."""
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    unpadder = crypto_padding.PKCS7(128).unpadder()
    try:
        return unpadder.update(padded) + unpadder.finalize()
    except ValueError as err:
        raise LanCryptoError(f"PKCS7 unpadding failed: {err}") from err


def sign_payload(session: LanSession, payload: bytes) -> str:
    """Produce a base64 HMAC-SHA256 signature for a payload."""
    sig = hmac.new(session.sign_key, payload, hashlib.sha256).digest()
    return base64.b64encode(sig).decode()


def verify_signature(session: LanSession, payload: bytes, signature_b64: str) -> bool:
    """Constant-time comparison against the expected signature."""
    try:
        expected = hmac.new(session.sign_key, payload, hashlib.sha256).digest()
        provided = base64.b64decode(signature_b64)
    except Exception:  # noqa: BLE001 - any decode error is a failure
        return False
    return hmac.compare_digest(expected, provided)


def encrypt_app_to_device(session: LanSession, plaintext: bytes) -> dict[str, Any]:
    """Encrypt an app → device payload. Returns the wire dict (base64 fields)."""
    session.seq_out += 1
    iv = session.fresh_iv(session.app_iv, session.seq_out)
    ciphertext = _aes_encrypt(session.app_key, iv, plaintext)
    sig = sign_payload(session, ciphertext)
    return {
        "seq_no": session.seq_out,
        "data": base64.b64encode(ciphertext).decode(),
        "sign": sig,
    }


def decrypt_device_to_app(session: LanSession, envelope: dict[str, Any]) -> bytes:
    """Decrypt a device → app payload. Verifies signature + seq monotonicity."""
    try:
        seq = int(envelope["seq_no"])
        ciphertext = base64.b64decode(envelope["data"])
        signature = envelope["sign"]
    except (KeyError, TypeError, ValueError) as err:
        raise LanCryptoError(f"Malformed LAN envelope: {err}") from err

    if seq <= session.seq_in:
        raise LanCryptoError(f"Replay detected: seq_no={seq}, last={session.seq_in}")
    if not verify_signature(session, ciphertext, signature):
        raise LanCryptoError("Signature mismatch")

    iv = session.fresh_iv(session.dev_iv, seq)
    plaintext = _aes_decrypt(session.dev_key, iv, ciphertext)
    session.seq_in = seq
    return plaintext


# ─────────────────────────────────────────────────────────────────────────
# Embedded aiohttp server — receives property pushes, serves command queue.
# ─────────────────────────────────────────────────────────────────────────

PropertyHandler = Callable[[str, Any], Awaitable[None]]


@dataclass
class LanServerConfig:
    """Configuration for the embedded LAN server."""

    bind_host: str = "0.0.0.0"  # noqa: S104 - HA local network, no external exposure
    port: int = LAN_SERVER_DEFAULT_PORT
    dsn: str = ""
    lan_key: bytes = b""
    key_id: int = 1


class DeLonghiLanServer:
    """Embedded aiohttp server that plays the role of Ayla cloud on the LAN.

    The machine will POST property updates to us and GET pending commands.
    Commands are queued by the coordinator via enqueue_command().

    Not yet wired into __init__.py — use only from tests or manual integration
    until the protocol has been validated on real hardware.
    """

    def __init__(self, config: LanServerConfig, on_property: PropertyHandler | None = None) -> None:
        if web is None:  # pragma: no cover - defensive
            raise RuntimeError("aiohttp is required to use DeLonghiLanServer")
        self._config = config
        self._on_property = on_property
        self._session: LanSession | None = None
        self._command_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._app: Any | None = None
        self._runner: Any | None = None
        self._site: Any | None = None
        self._started = asyncio.Event()

    @property
    def session(self) -> LanSession | None:
        return self._session

    @property
    def port(self) -> int:
        return self._config.port

    async def enqueue_command(self, ecam_bytes: bytes) -> None:
        """Queue an ECAM command to be handed to the machine on its next poll."""
        await self._command_queue.put(ecam_bytes)

    async def start(self) -> None:
        """Bind and start the embedded server."""
        self._app = web.Application()
        self._app.router.add_post(LAN_HANDSHAKE_PATH, self._handle_handshake)
        self._app.router.add_get(LAN_COMMAND_PATH, self._handle_command_poll)
        self._app.router.add_post(LAN_PROPERTY_PATH, self._handle_property_push)
        self._app.router.add_get(LAN_STATUS_PATH, self._handle_status)

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self._config.bind_host, self._config.port)
        await self._site.start()
        self._started.set()
        _LOGGER.info(
            "LAN server listening on %s:%d for dsn=%s",
            self._config.bind_host,
            self._config.port,
            self._config.dsn,
        )

    async def stop(self) -> None:
        """Tear down the server."""
        if self._site is not None:
            await self._site.stop()
        if self._runner is not None:
            await self._runner.cleanup()
        self._site = None
        self._runner = None
        self._app = None
        self._started.clear()
        _LOGGER.info("LAN server stopped")

    # ── HTTP handlers ───────────────────────────────────────────────────

    async def _handle_handshake(self, request: web.Request) -> web.Response:
        """Machine initiates key exchange. We respond with our random_2."""
        try:
            payload = await request.json()
            random_1 = base64.b64decode(payload["random_1"])
            key_id = int(payload.get("key_id", self._config.key_id))
        except (KeyError, ValueError, TypeError) as err:
            _LOGGER.warning("Handshake: malformed request: %s", err)
            return web.json_response({"error": "bad_request"}, status=400)

        random_2 = os.urandom(16)
        try:
            self._session = derive_session(self._config.lan_key, random_1, random_2, key_id)
        except LanCryptoError as err:
            _LOGGER.error("Handshake: key derivation failed: %s", err)
            return web.json_response({"error": "crypto"}, status=500)

        _LOGGER.info("Handshake completed, session established (key_id=%d)", key_id)
        return web.json_response(
            {
                "random_2": base64.b64encode(random_2).decode(),
                "time_2": int(time.time()),
                "proto": LAN_PROTO_VERSION,
                "key_id": key_id,
            }
        )

    async def _handle_command_poll(self, request: web.Request) -> web.Response:
        """Machine polls for pending commands. Long-poll with timeout."""
        if self._session is None:
            return web.json_response({"error": "no_session"}, status=403)

        try:
            ecam = await asyncio.wait_for(self._command_queue.get(), timeout=_COMMAND_POLL_TIMEOUT_S)
        except TimeoutError:
            return web.json_response({"cmds": []})

        envelope = encrypt_app_to_device(self._session, ecam)
        return web.json_response({"cmds": [envelope]})

    async def _handle_property_push(self, request: web.Request) -> web.Response:
        """Machine pushes a property datapoint. Decrypt + dispatch to coordinator."""
        if self._session is None:
            return web.json_response({"error": "no_session"}, status=403)

        try:
            envelope = await request.json()
            plaintext = decrypt_device_to_app(self._session, envelope)
            body = plaintext.decode("utf-8")
        except LanCryptoError as err:
            _LOGGER.warning("Property push: crypto failure: %s", err)
            return web.json_response({"error": "crypto"}, status=400)
        except UnicodeDecodeError as err:
            _LOGGER.warning("Property push: non-utf8 payload: %s", err)
            return web.json_response({"error": "encoding"}, status=400)

        # Payload is expected to be a JSON datapoint like
        # {"property": "d302_monitor_machine", "value": "<b64>", "baseType": "string"}.
        import json

        try:
            data = json.loads(body)
        except json.JSONDecodeError as err:
            _LOGGER.warning("Property push: invalid JSON: %s", err)
            return web.json_response({"error": "json"}, status=400)

        prop_name = data.get("property") or data.get("name")
        prop_value = data.get("value")
        if prop_name and self._on_property is not None:
            try:
                await self._on_property(prop_name, prop_value)
            except Exception:  # noqa: BLE001 - we never let handler errors kill the server
                _LOGGER.exception("Property handler raised for %s", prop_name)

        return web.json_response({"status": "ok"})

    async def _handle_status(self, request: web.Request) -> web.Response:
        """Health check endpoint for debugging."""
        return web.json_response(
            {
                "session": self._session is not None,
                "key_id": self._config.key_id,
                "queue_depth": self._command_queue.qsize(),
                "dsn": self._config.dsn,
            }
        )


# ─────────────────────────────────────────────────────────────────────────
# Outbound handshake — we can also initiate the handshake towards the
# machine's LAN IP instead of waiting for it to call us first. This is the
# cremalink-ha approach and works well when the machine is already online.
# ─────────────────────────────────────────────────────────────────────────


async def initiate_handshake(
    session_factory: Callable[[], Any],
    lan_ip: str,
    lan_key: bytes,
    key_id: int = 1,
    timeout: float = _HANDSHAKE_TIMEOUT_S,
) -> LanSession:
    """Trigger the key exchange from our side (app → machine).

    session_factory is typically ``aiohttp.ClientSession`` — pass it as a
    callable so callers can inject mocks in tests.
    """
    if web is None:  # pragma: no cover
        raise RuntimeError("aiohttp is required for initiate_handshake")

    random_1 = os.urandom(16)
    url = f"http://{lan_ip}{LAN_HANDSHAKE_PATH}"
    payload = {
        "random_1": base64.b64encode(random_1).decode(),
        "time_1": int(time.time()),
        "proto": LAN_PROTO_VERSION,
        "key_id": key_id,
    }

    async with session_factory() as client:
        try:
            async with client.post(url, json=payload, timeout=timeout) as resp:
                if resp.status != 200:
                    raise LanHandshakeError(f"Handshake HTTP {resp.status}")
                data = await resp.json()
        except TimeoutError as err:
            raise LanHandshakeError(f"Handshake timed out after {timeout}s") from err
        except Exception as err:  # noqa: BLE001
            raise LanHandshakeError(f"Handshake failed: {err}") from err

    try:
        random_2 = base64.b64decode(data["random_2"])
        remote_key_id = int(data.get("key_id", key_id))
    except (KeyError, ValueError, TypeError) as err:
        raise LanHandshakeError(f"Malformed handshake response: {err}") from err

    return derive_session(lan_key, random_1, random_2, remote_key_id)
