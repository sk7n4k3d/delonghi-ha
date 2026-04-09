"""Local LAN communication for De'Longhi WiFi coffee machines.

Implements the Ayla Networks local LAN protocol as spoken by De'Longhi
devices (PrimaDonna Soul, Eletta Explore, Dinamica, …) so this integration
can talk to the machine directly on the LAN — bypassing the Ayla cloud.

Fixes:
    * #9  — IP rate limit on the Ayla cloud (no more 403s at 200 req/h).
    * #10 — the tracking issue for LAN mode itself.
    * #16 — stale monitor on PrimaDonna Soul where app_device_connected
            is unsupported and the MQTT keepalive hack can't rescue us.

Protocol overview (reverse-engineered via cremalink-ha / ECAMpy and
verified against a live machine by @miditkl and @duckwc):

    1. Integration fetches lan_enabled + lan_ip + lan_key from the Ayla
       cloud (already implemented in api.get_lan_config).
    2. Integration starts an embedded aiohttp server (default port 10280).
    3. Integration issues ``PUT http://<lan_ip>/local_reg.json`` announcing
       its own IP, port, and /local_lan notification path. This is what
       tells the machine to start pushing to us instead of Ayla.
    4. Machine POSTs to ``/local_lan/key_exchange.json`` with
       ``{"key_exchange": {"random_1": <str>, "time_1": <int>}}``.
    5. Server responds HTTP **202 Accepted** with
       ``{"random_2": <str>, "time_2": <int>}``.
    6. Both sides run the (non-standard) cremalink key derivation:
            app_sign_key  = H(k, H(k, r1+r2+t1+t2+0x30) || r1+r2+t1+t2+0x30)
            app_crypto    = H(k, H(k, r1+r2+t1+t2+0x31) || r1+r2+t1+t2+0x31)
            app_iv_seed   = H(k, H(k, r1+r2+t1+t2+0x32) || r1+r2+t1+t2+0x32)[:16]
            dev_crypto    = H(k, H(k, r2+r1+t2+t1+0x31) || r2+r1+t2+t1+0x31)
            dev_iv_seed   = H(k, H(k, r2+r1+t2+t1+0x32) || r2+r1+t2+t1+0x32)[:16]
       Integration → device traffic uses (app_crypto, app_iv).
       Device → integration traffic uses (dev_crypto, dev_iv).
       Each direction has its own IV chain: the next IV is the last 16
       bytes of the *previous* ciphertext.
    7. Machine polls ``GET /local_lan/commands.json`` for pending commands.
       Server returns ``{"enc": <b64>, "sign": <b64>, "seq": <int>}``.
       When the queue is empty, an empty heartbeat payload is served so
       the IV chain keeps advancing on both sides.
    8. Machine pushes datapoints to ``/local_lan/property/datapoint.json``
       with ``{"enc": <b64>}`` (no signature — cremalink skips it too).

Credit: cremalink-ha (@miditkl) and ECAMpy (@duckwc) for first public
PrimaDonna Soul LAN implementations — used as the protocol reference.
The crypto in this file is deliberately bit-compatible with cremalink so
we can talk to the same devices without a protocol re-negotiation.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

try:  # aiohttp ships with Home Assistant; unit tests may run without it.
    from aiohttp import web
except ImportError:  # pragma: no cover
    web = None  # type: ignore[assignment]

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# Dedicated logger namespace so users can enable only LAN debug output:
#   logger:
#     default: warning
#     logs:
#       custom_components.delonghi_coffee.lan: debug
#
# Both names point at the same logger; _LAN_LOGGER exists so the namespace
# is obvious to readers.
_LOGGER = logging.getLogger(__name__)
_LAN_LOGGER = _LOGGER

LAN_SERVER_DEFAULT_PORT: int = 10280
LAN_HANDSHAKE_PATH: str = "/local_lan/key_exchange.json"
LAN_COMMAND_PATH: str = "/local_lan/commands.json"
LAN_PROPERTY_PATH: str = "/local_lan/property/datapoint.json"
LAN_STATUS_PATH: str = "/local_lan/status.json"
LAN_REG_PATH: str = "/local_reg.json"


class LanError(Exception):
    """Base exception for LAN errors."""


class LanHandshakeError(LanError):
    """Raised when the key exchange with the machine fails."""


class LanCryptoError(LanError):
    """Raised on encryption, decryption, or signature errors."""


# ─────────────────────────────────────────────────────────────────────────
# Primitives — cremalink-compatible pure functions. No I/O, no state.
#
# Idioms that differ from textbook Ayla LAN v1:
#   * Inputs (lan_key, randoms, times) are UTF-8 strings — not decoded.
#   * Padding is zero-padding (\x00), not PKCS7. Strings that end in \x00
#     do NOT round-trip. De'Longhi payloads are JSON, so this is fine.
#   * Key derivation is nested HMAC: H(k, H(k, m) || m).
#   * IV chain: next IV = last 16 bytes of the previous ciphertext.
# ─────────────────────────────────────────────────────────────────────────


def _hmac_sha256(key: bytes, data: bytes) -> bytes:
    """Raw HMAC-SHA256 digest (32 bytes)."""
    return hmac.new(key, data, hashlib.sha256).digest()


def _pad_zero(data: bytes, block_size: int = 16) -> bytes:
    """Right-pad ``data`` to a block_size multiple with 0x00 bytes.

    Always appends at least one byte (so plain 16-byte input grows by 16).
    """
    padding_length = block_size - (len(data) % block_size)
    return data + (padding_length * b"\x00")


def _unpad_zero(data: bytes) -> bytes:
    """Strip trailing 0x00 padding. Loses data that genuinely ends in \\x00."""
    return data.rstrip(b"\x00")


def _aes_encrypt(message: str, key: bytes, iv: bytes) -> str:
    """AES-CBC encrypt a UTF-8 string. Zero-padded. Returns base64.

    Key may be 16, 24, or 32 bytes — cremalink keeps the full 32-byte HMAC
    digest as the key, so this is effectively AES-256-CBC.
    """
    raw = _pad_zero(message.encode("utf-8"))
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    encryptor = cipher.encryptor()
    enc = encryptor.update(raw) + encryptor.finalize()
    return base64.b64encode(enc).decode("utf-8")


def _aes_decrypt(enc: str, key: bytes, iv: bytes) -> bytes:
    """Decrypt a base64 AES-CBC ciphertext and strip zero padding."""
    decoded = base64.b64decode(enc)
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    return _unpad_zero(decryptor.update(decoded) + decryptor.finalize())


def _rotate_iv_from_ciphertext(enc: str) -> bytes:
    """Next IV = last 16 bytes of the raw ciphertext."""
    raw = base64.b64decode(enc)
    return raw[-16:]


# ─────────────────────────────────────────────────────────────────────────
# Session and key derivation.
# ─────────────────────────────────────────────────────────────────────────


@dataclass
class LanSession:
    """Derived keys + mutable IV state for a single LAN session.

    The two sides have independent IV chains so both can encrypt and both
    can decrypt without stepping on each other. ``app_iv`` rotates on every
    call to :func:`encrypt_app_to_device`; ``dev_iv`` rotates on every call
    to :func:`decrypt_device_to_app`.
    """

    app_sign_key: bytes      # 32 bytes (HMAC-SHA256 digest)
    app_crypto_key: bytes    # 32 bytes (→ AES-256)
    app_iv: bytes            # 16 bytes — current IV, mutates
    dev_crypto_key: bytes    # 32 bytes
    dev_iv: bytes            # 16 bytes — current IV, mutates
    random_1: str
    random_2: str
    time_1: int
    time_2: int
    created_at: float = field(default_factory=time.time)


def derive_session(
    lan_key: str,
    random_1: str,
    random_2: str,
    time_1: int,
    time_2: int,
) -> LanSession:
    """Derive the full set of session keys from the handshake nonces.

    Bit-compatible with cremalink ``derive_keys()``: nested HMAC with a
    trailing byte tag that differs per key role. Inputs stay as UTF-8
    strings because that is how the device firmware concatenates them —
    do NOT base64-decode the randoms or the lan_key first.

    Raises :class:`LanCryptoError` on empty inputs.
    """
    if not lan_key:
        raise LanCryptoError("lan_key is empty")
    if not random_1:
        raise LanCryptoError("random_1 is empty")
    if not random_2:
        raise LanCryptoError("random_2 is empty")

    lan_bytes = lan_key.encode("utf-8")
    r1 = random_1.encode("utf-8")
    r2 = random_2.encode("utf-8")
    t1 = str(time_1).encode("utf-8")
    t2 = str(time_2).encode("utf-8")

    def _derive(parts: bytes) -> bytes:
        """Nested HMAC: H(k, H(k, parts) || parts)."""
        inner = _hmac_sha256(lan_bytes, parts)
        return _hmac_sha256(lan_bytes, inner + parts)

    app_sign_key = _derive(r1 + r2 + t1 + t2 + b"\x30")
    app_crypto_key = _derive(r1 + r2 + t1 + t2 + b"\x31")
    app_iv_full = _derive(r1 + r2 + t1 + t2 + b"\x32")
    dev_crypto_key = _derive(r2 + r1 + t2 + t1 + b"\x31")
    dev_iv_full = _derive(r2 + r1 + t2 + t1 + b"\x32")

    _LAN_LOGGER.debug(
        "derive_session: r1=%s r2=%s t1=%d t2=%d "
        "app_crypto_fp=%s dev_crypto_fp=%s app_iv_fp=%s dev_iv_fp=%s",
        random_1,
        random_2,
        time_1,
        time_2,
        app_crypto_key[:4].hex(),
        dev_crypto_key[:4].hex(),
        app_iv_full[:4].hex(),
        dev_iv_full[:4].hex(),
    )

    return LanSession(
        app_sign_key=app_sign_key,
        app_crypto_key=app_crypto_key,
        app_iv=app_iv_full[:16],
        dev_crypto_key=dev_crypto_key,
        dev_iv=dev_iv_full[:16],
        random_1=random_1,
        random_2=random_2,
        time_1=time_1,
        time_2=time_2,
    )


# ─────────────────────────────────────────────────────────────────────────
# Signing — over the *plaintext*, never over the ciphertext.
# ─────────────────────────────────────────────────────────────────────────


def sign_payload(sign_key: bytes, payload: str) -> str:
    """Base64 HMAC-SHA256 over a plaintext payload string.

    The machine signs the plaintext (not the ciphertext). Do not swap the
    order — wire compatibility breaks silently if you do.
    """
    return base64.b64encode(
        _hmac_sha256(sign_key, payload.encode("utf-8"))
    ).decode("utf-8")


def verify_signature(sign_key: bytes, payload: str, signature_b64: str) -> bool:
    """Constant-time signature verification. Returns False on any error."""
    try:
        expected = _hmac_sha256(sign_key, payload.encode("utf-8"))
        provided = base64.b64decode(signature_b64)
    except Exception:  # noqa: BLE001 — any decode error is a failed verify
        return False
    return hmac.compare_digest(expected, provided)


# ─────────────────────────────────────────────────────────────────────────
# Payload shaping — exact wire format from cremalink (no spaces, str seq).
# ─────────────────────────────────────────────────────────────────────────


def build_command_payload(seq: int, data: dict[str, Any]) -> str:
    """Wrap an ECAM command body as ``{"seq_no": "<seq>", "data": {...}}``."""
    return json.dumps(
        {"seq_no": str(seq), "data": data}, separators=(",", ":")
    )


def build_heartbeat_payload(seq: int) -> str:
    """Empty payload used when the command queue is drained."""
    return json.dumps(
        {"seq_no": str(seq), "data": {}}, separators=(",", ":")
    )


def encrypt_app_to_device(session: LanSession, payload: str) -> tuple[str, str]:
    """Encrypt + sign an app → device payload. Advances ``session.app_iv``.

    Returns ``(enc_b64, sign_b64)`` — the caller puts these in the command
    poll response under the keys ``enc`` and ``sign``.
    """
    prev_iv_fp = session.app_iv[-4:].hex()
    enc = _aes_encrypt(payload, session.app_crypto_key, session.app_iv)
    session.app_iv = _rotate_iv_from_ciphertext(enc)
    sign = sign_payload(session.app_sign_key, payload)
    _LAN_LOGGER.debug(
        "encrypt_app_to_device: payload_len=%d prev_iv_tail=%s next_iv_tail=%s",
        len(payload),
        prev_iv_fp,
        session.app_iv[-4:].hex(),
    )
    return enc, sign


def decrypt_device_to_app(session: LanSession, enc_b64: str) -> bytes:
    """Decrypt a device → app payload. Advances ``session.dev_iv``.

    Returns the raw plaintext. The caller is responsible for UTF-8 / JSON
    decoding — and for swallowing decode errors gracefully, because a
    500 response here makes the device back off and we lose telemetry.
    """
    prev_iv_fp = session.dev_iv[-4:].hex()
    plaintext = _aes_decrypt(enc_b64, session.dev_crypto_key, session.dev_iv)
    session.dev_iv = _rotate_iv_from_ciphertext(enc_b64)
    _LAN_LOGGER.debug(
        "decrypt_device_to_app: plaintext_len=%d prev_iv_tail=%s next_iv_tail=%s",
        len(plaintext),
        prev_iv_fp,
        session.dev_iv[-4:].hex(),
    )
    return plaintext


# ─────────────────────────────────────────────────────────────────────────
# Embedded aiohttp server — the integration plays the role of Ayla cloud
# on the LAN. Machine pushes properties to us and polls us for commands.
# ─────────────────────────────────────────────────────────────────────────


PropertyHandler = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class LanServerConfig:
    """Configuration for the embedded LAN server."""

    dsn: str
    lan_key: str
    advertised_ip: str
    bind_host: str = "0.0.0.0"  # noqa: S104 — HA local network, not public
    port: int = LAN_SERVER_DEFAULT_PORT


class DeLonghiLanServer:
    """Embedded HTTP server that impersonates Ayla cloud on the LAN.

    The machine POSTs property updates and GETs pending commands. Commands
    are queued by the coordinator via :meth:`enqueue_command`.

    Not yet wired into ``__init__.py`` — cloud mode remains the default
    until the protocol has been validated on real PrimaDonna Soul hardware.
    """

    def __init__(
        self,
        config: LanServerConfig,
        on_property: PropertyHandler | None = None,
    ) -> None:
        if web is None:  # pragma: no cover — defensive, HA always has aiohttp
            raise RuntimeError("aiohttp is required to use DeLonghiLanServer")
        self._config = config
        self._on_property = on_property
        self._session: LanSession | None = None
        self._seq = 0
        self._pending: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._app: Any | None = None
        self._runner: Any | None = None
        self._site: Any | None = None
        self._lock = asyncio.Lock()

    @property
    def session(self) -> LanSession | None:
        return self._session

    @property
    def seq(self) -> int:
        return self._seq

    @property
    def port(self) -> int:
        return self._config.port

    async def enqueue_command(self, data: dict[str, Any]) -> None:
        """Queue an ECAM command body (dict) to be served on the next poll."""
        await self._pending.put(data)

    async def start(self) -> None:
        """Bind and start the embedded server."""
        self._app = web.Application()
        self._app.router.add_post(LAN_HANDSHAKE_PATH, self._handle_handshake)
        self._app.router.add_get(LAN_COMMAND_PATH, self._handle_command_poll)
        self._app.router.add_post(LAN_COMMAND_PATH, self._handle_command_poll)
        self._app.router.add_post(LAN_PROPERTY_PATH, self._handle_property_push)
        self._app.router.add_get(LAN_STATUS_PATH, self._handle_status)

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(
            self._runner, self._config.bind_host, self._config.port
        )
        await self._site.start()
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
        _LOGGER.info("LAN server stopped")

    # ── HTTP handlers ───────────────────────────────────────────────────

    async def _handle_handshake(self, request: web.Request) -> web.Response:
        """POST /local_lan/key_exchange.json — machine initiates key exchange.

        Body shape: ``{"key_exchange": {"random_1": <str>, "time_1": <int>}}``.
        Response: HTTP 202 with ``{"random_2": <str>, "time_2": <int>}``.
        """
        peer = request.remote or "unknown"
        _LAN_LOGGER.debug("handshake: inbound from peer=%s", peer)
        if not self._config.lan_key:
            _LAN_LOGGER.error("handshake: reject (peer=%s) — lan_key not configured", peer)
            return web.json_response({"error": "not_configured"}, status=400)
        try:
            body = await request.json()
            exchange = body["key_exchange"]
            random_1 = str(exchange["random_1"])
            time_1 = int(exchange["time_1"])
        except (KeyError, ValueError, TypeError) as err:
            _LAN_LOGGER.warning("handshake: malformed request from %s: %s", peer, err)
            return web.json_response({"error": "bad_request"}, status=400)

        _LAN_LOGGER.debug(
            "handshake: received random_1=%s time_1=%d from %s",
            random_1,
            time_1,
            peer,
        )

        # cremalink uses 12 bytes of urandom → base64 without padding.
        random_2 = base64.b64encode(os.urandom(12)).decode("utf-8").rstrip("=")
        time_2 = int(time.time())
        try:
            self._session = derive_session(
                self._config.lan_key, random_1, random_2, time_1, time_2
            )
        except LanCryptoError as err:
            _LAN_LOGGER.error("handshake: derive failed for %s: %s", peer, err)
            return web.json_response({"error": "crypto"}, status=400)

        async with self._lock:
            self._seq = 0

        _LAN_LOGGER.info(
            "handshake ok (dsn=%s peer=%s time_1=%d time_2=%d)",
            self._config.dsn, peer, time_1, time_2,
        )
        return web.json_response(
            {"random_2": random_2, "time_2": time_2}, status=202
        )

    async def _handle_command_poll(self, request: web.Request) -> web.Response:
        """GET/POST /local_lan/commands.json — machine polls for work.

        Returns ``{"enc": <b64>, "sign": <b64>, "seq": <int>}``. When the
        queue is empty we serve an empty heartbeat so the IV chain keeps
        advancing — otherwise the first real command after a quiet period
        would desync with the device's dev_iv.
        """
        if self._session is None:
            return web.json_response(
                {"enc": "", "sign": "", "seq": self._seq}
            )

        try:
            data = self._pending.get_nowait()
        except asyncio.QueueEmpty:
            async with self._lock:
                self._seq += 1
                current_seq = self._seq
            payload = build_heartbeat_payload(current_seq)
        else:
            async with self._lock:
                self._seq += 1
                current_seq = self._seq
            payload = build_command_payload(current_seq, data)

        try:
            enc, sign = encrypt_app_to_device(self._session, payload)
        except Exception as err:  # noqa: BLE001 — never 500 the device
            _LOGGER.error("LAN command poll: encrypt failed: %s", err)
            return web.json_response(
                {"enc": "", "sign": "", "seq": current_seq}
            )

        return web.json_response(
            {"enc": enc, "sign": sign, "seq": current_seq}
        )

    async def _handle_property_push(self, request: web.Request) -> web.Response:
        """POST /local_lan/property/datapoint.json — encrypted device → app.

        Any decode / crypto / JSON failure returns HTTP 200. A non-2xx
        response makes the device back off its push schedule and we lose
        live telemetry — the cure is worse than the disease.
        """
        if self._session is None:
            return web.json_response({}, status=200)

        try:
            envelope = await request.json()
            enc = envelope["enc"]
        except (KeyError, ValueError, TypeError):
            return web.json_response({}, status=200)

        try:
            plaintext = decrypt_device_to_app(self._session, enc)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("LAN datapoint: decrypt failed: %s", err)
            return web.json_response({}, status=200)

        try:
            data = json.loads(plaintext.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as err:
            _LOGGER.debug(
                "LAN datapoint: decode failed (%s); returning 200", err
            )
            return web.json_response({}, status=200)

        if self._on_property is not None:
            try:
                await self._on_property(data)
            except Exception:  # noqa: BLE001 — never let handler kill server
                _LOGGER.exception("LAN datapoint handler raised")

        return web.json_response({}, status=200)

    async def _handle_status(self, request: web.Request) -> web.Response:
        """Health endpoint for debugging. Never exposes the lan_key."""
        return web.json_response(
            {
                "session": self._session is not None,
                "dsn": self._config.dsn,
                "seq": self._seq,
                "queue_depth": self._pending.qsize(),
            }
        )


# ─────────────────────────────────────────────────────────────────────────
# Outbound: tell the device where to push. This is the missing piece that
# lets the embedded server actually receive traffic — otherwise the device
# only talks to Ayla cloud. Equivalent to cremalink's device_adapter.
# ─────────────────────────────────────────────────────────────────────────


async def register_with_device(
    session_factory: Callable[[], Any],
    device_ip: str,
    advertised_ip: str,
    advertised_port: int,
    *,
    scheme: str = "http",
    timeout: float = 5.0,
) -> None:
    """``PUT /local_reg.json`` so the device pushes updates to our server.

    ``session_factory`` is typically ``aiohttp.ClientSession`` — pass it as
    a callable so callers can inject mocks in tests. Raises :class:`LanError`
    on any transport / HTTP failure so the coordinator can schedule a retry.
    """
    if web is None:  # pragma: no cover
        raise RuntimeError("aiohttp is required for register_with_device")

    url = f"{scheme}://{device_ip}{LAN_REG_PATH}"
    body = {
        "local_reg": {
            "ip": advertised_ip,
            "notify": 1,
            "port": advertised_port,
            "uri": "/local_lan",
        }
    }
    try:
        async with (
            session_factory() as client,
            client.put(url, json=body, timeout=timeout) as resp,
        ):
            if resp.status >= 400:
                raise LanError(f"local_reg HTTP {resp.status}")
    except LanError:
        raise
    except Exception as err:  # noqa: BLE001
        raise LanError(f"local_reg failed: {err}") from err


# ─────────────────────────────────────────────────────────────────────────
# Diagnostic — button-triggered in-process roundtrip. Exercises every
# stage of the LAN pipeline without requiring real hardware so we can
# confirm crypto, framing, IV chaining and the embedded server all agree.
# ─────────────────────────────────────────────────────────────────────────


_DIAG_STAGES: tuple[str, ...] = (
    "cloud_config",
    "server_start",
    "handshake",
    "app_to_device",
    "device_to_app",
    "teardown",
)


@dataclass
class LanDiagnosticResult:
    """Outcome of a single :func:`run_lan_diagnostic` invocation."""

    success: bool
    stage: str
    reason: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> str:
        if self.success:
            return f"success at stage={self.stage}"
        return f"failed at stage={self.stage}: {self.reason}"


async def run_lan_diagnostic(
    lan_key: str | None,
    lan_ip: str | None,
    dsn: str,
    *,
    bind_host: str = "127.0.0.1",
) -> LanDiagnosticResult:
    """Run an end-to-end LAN pipeline check and log every step.

    This is the hook wired into the ``Run LAN Diagnostic`` button. It
    never talks to real hardware — instead it spins up the embedded LAN
    server on an ephemeral loopback port, simulates the device-side
    handshake + datapoint push + command poll from an in-process
    aiohttp client, and verifies the full crypto / framing stack.

    The routine is intentionally defensive: **any** failure is caught
    and returned as a :class:`LanDiagnosticResult` so the Home Assistant
    button press never raises.
    """
    _LAN_LOGGER.info("===== LAN DIAGNOSTIC START (dsn=%s) =====", dsn)

    # -- Stage: cloud_config ---------------------------------------------
    details: dict[str, Any] = {
        "dsn": dsn,
        "lan_ip": lan_ip or "<missing>",
        "lan_key_present": bool(lan_key),
    }
    _LAN_LOGGER.debug(
        "cloud_config: lan_ip=%s lan_key_present=%s",
        lan_ip or "<missing>",
        bool(lan_key),
    )
    if not lan_key:
        result = LanDiagnosticResult(
            success=False,
            stage="cloud_config",
            reason="lan_key missing — Ayla cloud did not return a LAN key",
            details=details,
        )
        _LAN_LOGGER.error("cloud_config: %s", result.reason)
        _LAN_LOGGER.info("===== LAN DIAGNOSTIC END (%s) =====", result.summary())
        return result

    if web is None:  # pragma: no cover — HA always ships aiohttp
        result = LanDiagnosticResult(
            success=False,
            stage="cloud_config",
            reason="aiohttp not installed in this environment",
            details=details,
        )
        _LAN_LOGGER.error("cloud_config: %s", result.reason)
        _LAN_LOGGER.info("===== LAN DIAGNOSTIC END (%s) =====", result.summary())
        return result

    # -- Stage: server_start ---------------------------------------------
    server: DeLonghiLanServer | None = None
    stage = "server_start"
    try:
        config = LanServerConfig(
            dsn=dsn,
            lan_key=lan_key,
            advertised_ip=bind_host,
            bind_host=bind_host,
            port=0,  # OS picks a free port on loopback
        )
        server = DeLonghiLanServer(config)
        await server.start()
        chosen_port = _server_actual_port(server)
        details["diagnostic_port"] = chosen_port
        _LAN_LOGGER.debug(
            "server_start: listening on %s:%s (ephemeral loopback)",
            bind_host,
            chosen_port,
        )
        base_url = f"http://{bind_host}:{chosen_port}"

        # -- Stage: handshake --------------------------------------------
        stage = "handshake"
        random_1 = base64.b64encode(os.urandom(12)).decode("utf-8").rstrip("=")
        time_1 = int(time.time())
        _LAN_LOGGER.debug(
            "handshake: simulated device random_1=%s time_1=%d", random_1, time_1
        )

        from aiohttp import ClientSession

        async with ClientSession() as client:
            async with client.post(
                base_url + LAN_HANDSHAKE_PATH,
                json={"key_exchange": {"random_1": random_1, "time_1": time_1}},
                timeout=5.0,
            ) as resp:
                handshake_status = resp.status
                handshake_body = await resp.json()

            if handshake_status != 202:
                raise LanHandshakeError(
                    f"HTTP {handshake_status} body={handshake_body}"
                )
            random_2 = str(handshake_body["random_2"])
            time_2 = int(handshake_body["time_2"])
            _LAN_LOGGER.debug(
                "handshake: server responded 202 random_2=%s time_2=%d",
                random_2,
                time_2,
            )

            # The simulated client plays the DEVICE role. Both sides
            # derive the same session material, but the CONVENTION is:
            #
            #   * server encrypts outgoing commands with app_* / app_iv
            #     → client DECRYPTS incoming commands with app_* / app_iv
            #   * client encrypts outgoing datapoints with dev_* / dev_iv
            #     → server DECRYPTS incoming datapoints with dev_* / dev_iv
            #
            # Both sides rotate the matching IV in lock-step after each
            # message in that direction.
            client_session = derive_session(
                lan_key, random_1, random_2, time_1, time_2
            )

            # -- Stage: app_to_device (command poll) ---------------------
            stage = "app_to_device"
            async with client.get(
                base_url + LAN_COMMAND_PATH, timeout=5.0
            ) as resp:
                poll_status = resp.status
                poll_body = await resp.json()
            if poll_status != 200:
                raise LanError(f"command poll HTTP {poll_status}")
            if not poll_body.get("enc"):
                raise LanError("command poll returned empty enc")
            _LAN_LOGGER.debug(
                "app_to_device: poll seq=%s enc_len=%d",
                poll_body.get("seq"),
                len(poll_body.get("enc", "")),
            )
            try:
                # Decrypt using our app_* — mirrors the server's encrypt.
                plaintext = _aes_decrypt(
                    poll_body["enc"],
                    client_session.app_crypto_key,
                    client_session.app_iv,
                )
                client_session.app_iv = _rotate_iv_from_ciphertext(
                    poll_body["enc"]
                )
                parsed = json.loads(plaintext.decode("utf-8"))
                if "seq_no" not in parsed or "data" not in parsed:
                    raise LanError(f"unexpected poll payload shape: {parsed!r}")
                _LAN_LOGGER.debug(
                    "app_to_device: decrypted poll seq_no=%s data_keys=%s",
                    parsed["seq_no"],
                    list(parsed["data"].keys()),
                )
            except LanError:
                raise
            except Exception as err:  # noqa: BLE001
                raise LanError(f"poll decrypt failed: {err}") from err

            # -- Stage: device_to_app (datapoint push) ------------------
            stage = "device_to_app"
            datapoint = {"property": {"name": "diagnostic_ping", "value": 1}}
            payload = json.dumps(datapoint, separators=(",", ":"))
            # Encrypt using our dev_* — server decrypts with its dev_*.
            enc = _aes_encrypt(
                payload, client_session.dev_crypto_key, client_session.dev_iv
            )
            client_session.dev_iv = _rotate_iv_from_ciphertext(enc)
            async with client.post(
                base_url + LAN_PROPERTY_PATH,
                json={"enc": enc},
                timeout=5.0,
            ) as resp:
                push_status = resp.status
            if push_status != 200:
                raise LanError(f"datapoint push HTTP {push_status}")
            _LAN_LOGGER.debug(
                "device_to_app: datapoint accepted enc_len=%d", len(enc)
            )

            # -- Stage: teardown -----------------------------------------
            stage = "teardown"

    except Exception as err:  # noqa: BLE001 — button press must never raise
        _LAN_LOGGER.exception("diagnostic failed at stage=%s", stage)
        result = LanDiagnosticResult(
            success=False,
            stage=stage,
            reason=f"{type(err).__name__}: {err}",
            details=details,
        )
    else:
        result = LanDiagnosticResult(success=True, stage="teardown", details=details)
    finally:
        if server is not None:
            try:
                await server.stop()
            except Exception as err:  # noqa: BLE001
                _LAN_LOGGER.warning("teardown: server.stop raised %s", err)

    _LAN_LOGGER.info("===== LAN DIAGNOSTIC END (%s) =====", result.summary())
    return result


def _server_actual_port(server: DeLonghiLanServer) -> int:
    """Return the OS-assigned port of a started server, or -1 if unknown."""
    site = getattr(server, "_site", None)
    if site is None:
        return -1
    # aiohttp TCPSite exposes the bound sockets via ``_server.sockets``;
    # fall back to the configured port when introspection fails.
    try:
        sockets = site._server.sockets  # type: ignore[attr-defined]
        if sockets:
            return sockets[0].getsockname()[1]
    except Exception:  # noqa: BLE001
        pass
    return server.port
