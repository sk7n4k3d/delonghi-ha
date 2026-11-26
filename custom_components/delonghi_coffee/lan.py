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
    3. Integration issues ``POST http://<lan_ip>/local_reg.json?dsn=<DSN>``
       announcing its own IP, port, and /local_lan notification path.
       POST OPENS a session (always triggers the key exchange); ``PUT`` is
       keep-alive only and never re-keys a live module (issue #50). The
       module keeps a single registration slot and expects a keep-alive
       every ~20 s while the session should stay up.
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
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

try:  # aiohttp ships with Home Assistant; unit tests may run without it.
    from aiohttp import ClientTimeout, web
except ImportError:  # pragma: no cover
    web = None  # type: ignore[assignment]
    ClientTimeout = None  # type: ignore[assignment]

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# Dedicated logger namespace so users can enable only LAN debug output:
#   logger:
#     default: warning
#     logs:
#       custom_components.delonghi_coffee.lan: debug
#
# Both names point at the same logger; _LAN_LOGGER exists so the namespace
# is obvious to readers.
from .const import LAN_SESSION_STALE_SECONDS
from .logger import get_logger

_LOGGER = get_logger(__name__)
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

    app_sign_key: bytes  # 32 bytes (HMAC-SHA256 digest)
    app_crypto_key: bytes  # 32 bytes (→ AES-256)
    app_iv: bytes  # 16 bytes — current IV, mutates
    dev_crypto_key: bytes  # 32 bytes
    dev_iv: bytes  # 16 bytes — current IV, mutates
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
        "derive_session: r1=%s r2=%s t1=%d t2=%d app_crypto_fp=%s dev_crypto_fp=%s app_iv_fp=%s dev_iv_fp=%s",
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
    return base64.b64encode(_hmac_sha256(sign_key, payload.encode("utf-8"))).decode("utf-8")


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
    return json.dumps({"seq_no": str(seq), "data": data}, separators=(",", ":"))


def build_heartbeat_payload(seq: int) -> str:
    """Empty payload used when the command queue is drained."""
    return json.dumps({"seq_no": str(seq), "data": {}}, separators=(",", ":"))


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
    """Configuration for the embedded LAN server.

    ``bind_host`` defaults to loopback so unit tests and accidental
    instantiations don't silently bind on every interface. Production
    code (coordinator.DeLonghiCoordinator._try_start_lan) overrides this
    with the local interface IP — narrow enough to reach the device, wide
    enough that the OS actually accepts the device's packets.

    ``allowed_peers`` is an optional ingress allowlist. When set, inbound
    requests whose remote address is not in the set are rejected with a
    403 so a hostile LAN neighbour can't clobber the active session by
    firing an unsolicited key_exchange. Always includes ``127.0.0.1`` /
    ``::1`` for diagnostic loopback — the :func:`run_lan_diagnostic`
    helper spins up a full pipeline locally.

    ``handshake_max_skew`` caps how far ``time_1`` (the device clock) may
    drift from our own ``time.time()``. cremalink picks its own time_1 so
    any huge delta is either a badly reset clock or a stale replay. 30
    minutes is conservative; real devices are within a few seconds.

    Non-wallclock firmwares (PrimaDonna Soul "millcore", issue #10) ship
    ``time_1`` values that are not UNIX seconds. Some can combine uptime and
    truncated wall-clock digits, so the numeric value may decrease at digit
    boundaries or after reboot. The skew check is bypassed above
    :data:`NON_WALLCLOCK_TIME1_THRESHOLD`; exact accepted nonce pairs provide
    replay protection without assuming those values are monotonic.
    """

    dsn: str
    lan_key: str
    advertised_ip: str
    bind_host: str = "127.0.0.1"
    port: int = LAN_SERVER_DEFAULT_PORT
    allowed_peers: frozenset[str] = field(default_factory=frozenset)
    handshake_max_skew: int = 30 * 60  # seconds


# Any UNIX-second timestamp we'd legitimately see is < 5e9 (year ~2128).
# Firmwares that ship non-wallclock or hybrid time_1 values (uptime and
# truncated clock digits) produce values far above UNIX seconds. Picking 5e9
# as the cutoff leaves a 130-year safety margin for real wall clocks while
# catching the non-wallclock formats observed so far.
NON_WALLCLOCK_TIME1_THRESHOLD: int = 5_000_000_000

# Remember a bounded sliding window per allowlisted peer. Repeated exact
# key-exchange pairs are replays; distinct pairs can legitimately have a
# lower non-wallclock time_1 after a digit-boundary rollover or machine reboot.
MAX_REMEMBERED_HANDSHAKES_PER_PEER: int = 512


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
        # Last time the device proved it's still there — either by
        # completing a handshake or by polling for commands. A session
        # that's merely "set" but never re-confirmed is indistinguishable
        # from a device that silently dropped off the LAN (see
        # is_session_alive).
        self._last_activity: float | None = None
        self._activity_epoch = 0
        self.last_activity_utc: str | None = None
        self._seq = 0
        self._pending: asyncio.Queue[tuple[dict[str, Any], asyncio.Event]] = asyncio.Queue()
        self._app: Any | None = None
        self._runner: Any | None = None
        self._site: Any | None = None
        self._lock = asyncio.Lock()
        # Wall-clock rollback state is per peer; non-wallclock time_1 values
        # are not numerically monotonic on every firmware (issue #97).
        # H-coffee-4 (audit 2026-05-08): a single global counter let one
        # rogue handshake (from any IP that matched the allowlist, e.g.
        # via ARP spoofing the device) pin the floor at 2^31-1 and
        # permanently reject the legitimate device's smaller time_1
        # until the integration reloaded. Per-peer state localises the
        # blast radius — a rogue peer's wall-clock floor only affects itself.
        self._last_handshake_time1: dict[str, int] = {}
        # Exact accepted (random_1, time_1) pairs reject captured handshakes
        # without rejecting fresh handshakes whose non-wallclock time_1 fell.
        self._seen_handshake_pairs: dict[str, dict[tuple[str, int], None]] = {}
        # Loopback is always trusted so the in-process diagnostic works
        # regardless of what the caller passed in ``allowed_peers``.
        peers = set(config.allowed_peers)
        peers.update({"127.0.0.1", "::1"})
        self._trusted_peers: frozenset[str] = frozenset(peers) if config.allowed_peers else frozenset()

    def _is_peer_allowed(self, peer: str | None) -> bool:
        """Return True when peer may access privileged LAN endpoints.

        Empty allowlist = legacy behaviour (allow everyone). Once an
        allowlist is configured, unknown peers are rejected.
        """
        if not self._trusted_peers:
            return True
        if peer is None:
            return False
        # Strip IPv6 zone identifier (``fe80::1%eth0``) — rare on HA hosts
        # but aiohttp's ``request.remote`` can carry them through.
        return peer.split("%", 1)[0] in self._trusted_peers

    @property
    def advertised_ip(self) -> str:
        """The IP we announced to the module (where it dials back to)."""
        return self._config.advertised_ip

    def reset_session(self) -> None:
        """Drop the current session so the next registration is a fresh POST.

        The millcore module keeps its side of a session until IT decides to
        drop it; if we drop ours without re-opening via POST, the module
        keep-alives against keys we no longer hold — the "hollow session"
        documented in issue #50. Callers must follow up with a POST.
        """
        self._session = None
        self._seq = 0
        self._last_activity = 0.0
        self._activity_epoch += 1

    @property
    def activity_epoch(self) -> int:
        """Identify interruptions in the LAN session and its activity."""
        return self._activity_epoch

    def _record_activity(self) -> None:
        now = time.monotonic()
        if self._last_activity is not None and now - self._last_activity > LAN_SESSION_STALE_SECONDS:
            self._activity_epoch += 1
        self._last_activity = now
        self.last_activity_utc = datetime.now(UTC).isoformat()

    @property
    def has_pending(self) -> bool:
        """True while commands are queued — drives the local_reg notify flag."""
        return not self._pending.empty()

    @property
    def session(self) -> LanSession | None:
        return self._session

    def is_session_alive(self, max_age: float) -> bool:
        """Return True when the session exists AND the device was seen
        within the last ``max_age`` seconds (handshake or command poll).

        A session that's set but hasn't been confirmed recently means the
        device stopped talking to us — trusting it would silently drop
        commands into a queue nobody drains.
        """
        if self._session is None or self._last_activity is None:
            return False
        return (time.monotonic() - self._last_activity) <= max_age

    @property
    def seq(self) -> int:
        return self._seq

    @property
    def port(self) -> int:
        return self._config.port

    async def enqueue_command(self, data: dict[str, Any], *, fetched: asyncio.Event | None = None) -> asyncio.Event:
        """Queue a command and return an event set when the machine fetches it."""
        if fetched is None:
            fetched = asyncio.Event()
        await self._pending.put((data, fetched))
        return fetched

    async def start(self) -> None:
        """Bind and start the embedded server."""
        self._app = web.Application()
        self._app.router.add_post(LAN_HANDSHAKE_PATH, self._handle_handshake)
        # Any method on commands.json (lodzen does the same): a firmware
        # polling with an unexpected verb used to hit a silent 405.
        self._app.router.add_route("*", LAN_COMMAND_PATH, self._handle_command_poll)
        self._app.router.add_route("*", LAN_PROPERTY_PATH, self._handle_property_push)
        self._app.router.add_get(LAN_STATUS_PATH, self._handle_status)

        # Route aiohttp's access log INTO our component logger: testers
        # send logs filtered to custom_components.delonghi_coffee, and the
        # default `aiohttp.access` logger gets dropped with everything else
        # — which made "the machine never connects back" undecidable (issue
        # #50). This captures every inbound request, including 404/405.
        self._runner = web.AppRunner(self._app, access_log=_LAN_LOGGER, logger=_LOGGER)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self._config.bind_host, self._config.port)
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

        Rejects requests from peers outside the allowlist (when set), stale
        wall-clock values, and exact replays of an accepted key exchange.
        """
        peer = request.remote or "unknown"
        _LAN_LOGGER.debug("handshake: inbound from peer=%s", peer)
        if not self._is_peer_allowed(peer):
            _LAN_LOGGER.warning("handshake: reject peer=%s (not in allowlist)", peer)
            return web.json_response({"error": "forbidden"}, status=403)
        if not self._config.lan_key:
            _LAN_LOGGER.error("handshake: reject (peer=%s) — lan_key not configured", peer)
            return web.json_response({"error": "not_configured"}, status=400)
        try:
            body = await request.json()
            exchange = body["key_exchange"]
            # Pass random_1 through VERBATIM: the reference implementations
            # (cremalink, ECAMpy) never strip base64 padding on the inbound
            # value, and the device derives its session material from
            # exactly what it sent. Canonicalizing here desyncs the KDF if
            # the firmware ever leaves padding in (issue #50 audit).
            random_1 = str(exchange["random_1"])
            time_1 = int(exchange["time_1"])
            # Fields the Soul sends and we do not use (doc ecam-lan-protocol
            # §3): log them instead of dropping them silently — `sec`
            # non-empty would mean an unsupported secure-setup handshake.
            _LAN_LOGGER.debug(
                "handshake: fields from %s: proto=%s ver=%s key_id=%s sec=%r",
                peer,
                exchange.get("proto"),
                exchange.get("ver"),
                exchange.get("key_id"),
                exchange.get("sec"),
            )
            if exchange.get("sec"):
                _LAN_LOGGER.warning(
                    "handshake: peer=%s requests secure setup (sec=%r) — unsupported by this integration",
                    peer,
                    exchange.get("sec"),
                )
        except (KeyError, ValueError, TypeError) as err:
            _LAN_LOGGER.warning("handshake: malformed request from %s: %s", peer, err)
            return web.json_response({"error": "bad_request"}, status=400)

        # Wall-clock timestamps are expected to be monotonic within the skew
        # window. Non-wallclock values can fall at concatenation digit
        # boundaries or after reboot, so those use exact-pair de-duplication
        # below instead of a numeric ratchet (issue #97).
        now = int(time.time())
        peer_norm = peer.split("%", 1)[0]  # strip IPv6 zone for stable key
        peer_floor = self._last_handshake_time1.get(peer_norm, 0)
        if (
            time_1 < NON_WALLCLOCK_TIME1_THRESHOLD
            and peer_floor
            and peer_floor < NON_WALLCLOCK_TIME1_THRESHOLD
            and time_1 < peer_floor
        ):
            _LAN_LOGGER.warning(
                "handshake: reject peer=%s time_1=%d older than last accepted %d for that peer",
                peer,
                time_1,
                peer_floor,
            )
            return web.json_response({"error": "stale"}, status=400)

        # Wall-clock skew check only applies to wall-clock-style time_1.
        # Above the threshold the value is structurally not a UNIX epoch
        # (cf. NON_WALLCLOCK_TIME1_THRESHOLD docstring); exact pair replay
        # protection below applies to both timestamp styles.
        if time_1 < NON_WALLCLOCK_TIME1_THRESHOLD:
            if abs(now - time_1) > self._config.handshake_max_skew:
                _LAN_LOGGER.warning(
                    "handshake: reject peer=%s time_1=%d vs local now=%d (max skew %ds)",
                    peer,
                    time_1,
                    now,
                    self._config.handshake_max_skew,
                )
                return web.json_response({"error": "clock_skew"}, status=400)
        else:
            _LAN_LOGGER.debug(
                "handshake: peer=%s time_1=%d looks non-wallclock/hybrid, skipping wall-clock skew check",
                peer,
                time_1,
            )

        _LAN_LOGGER.debug(
            "handshake: received random_1=%s time_1=%d from %s",
            random_1,
            time_1,
            peer,
        )

        # cremalink uses 12 bytes of urandom → base64 without padding.
        random_2 = base64.b64encode(os.urandom(12)).decode("utf-8").rstrip("=")
        # Milliseconds, matching both reference servers (cremalink state.py,
        # ECAMpy constants). The device re-derives with whatever we return,
        # so this is alignment not a fix — see issue #50 audit.
        time_2 = int(time.time() * 1000)
        try:
            session = derive_session(self._config.lan_key, random_1, random_2, time_1, time_2)
        except LanCryptoError as err:
            _LAN_LOGGER.error("handshake: derive failed for %s: %s", peer, err)
            return web.json_response({"error": "crypto"}, status=400)

        async with self._lock:
            seen = self._seen_handshake_pairs.setdefault(peer_norm, {})
            pair = (random_1, time_1)
            if pair in seen:
                _LAN_LOGGER.warning(
                    "handshake: reject exact replay from peer=%s time_1=%d",
                    peer,
                    time_1,
                )
                return web.json_response({"error": "replay"}, status=400)
            seen[pair] = None
            while len(seen) > MAX_REMEMBERED_HANDSHAKES_PER_PEER:
                del seen[next(iter(seen))]
            self._session = session
            self._activity_epoch += 1
            self._seq = 0
            self._last_handshake_time1[peer_norm] = time_1
            self._record_activity()

        _LAN_LOGGER.info(
            "handshake ok (dsn=%s peer=%s time_1=%d time_2=%d)",
            self._config.dsn,
            peer,
            time_1,
            time_2,
        )
        return web.json_response({"random_2": random_2, "time_2": time_2}, status=202)

    async def _handle_command_poll(self, request: web.Request) -> web.Response:
        """GET/POST /local_lan/commands.json — machine polls for work.

        Returns ``{"enc": <b64>, "sign": <b64>, "seq": <int>}``. When the
        queue is empty we serve an empty heartbeat so the IV chain keeps
        advancing — otherwise the first real command after a quiet period
        would desync with the device's dev_iv.
        """
        peer = request.remote or "unknown"
        # Log BEFORE any state check: a machine polling with no live
        # session must still leave a trace, otherwise "never connects back"
        # can never be distinguished from "connects back, we ignore it"
        # (issue #50).
        _LAN_LOGGER.debug("command_poll: %s from %s", request.method, peer)
        if not self._is_peer_allowed(peer):
            _LAN_LOGGER.debug("command_poll: reject peer=%s (not in allowlist)", peer)
            return web.json_response({"error": "forbidden"}, status=403)
        # H-coffee-1 (audit 2026-05-08): keep the encrypt call INSIDE the
        # lock alongside the seq/queue mutation. Earlier behaviour released
        # the lock before encrypt_app_to_device, which mutates session.app_iv
        # — two concurrent polls could each capture the same IV, encrypt
        # different plaintexts under the same key+IV (CBC IV reuse leaks
        # plaintext XOR for block-coincident prefixes), and the IV chain
        # would desync from the device until the next handshake. Encrypt
        # is microsecond-level on small payloads so holding the lock is
        # cheap. The response build is outside since it touches no state.
        async with self._lock:
            session = self._session
            if session is None:
                return web.json_response({"enc": "", "sign": "", "seq": self._seq})
            self._record_activity()
            try:
                queued = self._pending.get_nowait()
            except asyncio.QueueEmpty:
                queued = None
            self._seq += 1
            current_seq = self._seq

            if queued is not None:
                _, fetched = queued
                valid = getattr(fetched, "should_deliver", None)
                if valid is not None and not valid():
                    queued = None

            if queued is None:
                payload = build_heartbeat_payload(current_seq)
            else:
                data, fetched = queued
                payload = build_command_payload(current_seq, data)

            try:
                enc, sign = encrypt_app_to_device(session, payload)
            except Exception as err:  # noqa: BLE001 — never 500 the device
                _LOGGER.error("LAN command poll: encrypt failed: %s", err)
                return web.json_response({"enc": "", "sign": "", "seq": current_seq})

            if queued is not None:
                fetched.set()

        return web.json_response({"enc": enc, "sign": sign, "seq": current_seq})

    async def _handle_property_push(self, request: web.Request) -> web.Response:
        """POST /local_lan/property/datapoint.json — encrypted device → app.

        Any decode / crypto / JSON failure returns HTTP 200. A non-2xx
        response makes the device back off its push schedule and we lose
        live telemetry — the cure is worse than the disease.
        """
        peer = request.remote or "unknown"
        _LAN_LOGGER.debug("property_push: %s from %s", request.method, peer)
        if not self._is_peer_allowed(peer):
            _LAN_LOGGER.debug("property_push: reject peer=%s (not in allowlist)", peer)
            return web.json_response({"error": "forbidden"}, status=403)

        # Read the body BEFORE taking the lock — request.json() involves
        # async I/O and we must not stall the rest of the LAN server while
        # waiting for it.
        try:
            envelope = await request.json()
            enc = envelope["enc"]
        except (KeyError, ValueError, TypeError):
            return web.json_response({}, status=200)

        # H-coffee-1 (audit 2026-05-08): decrypt INSIDE the lock so two
        # concurrent property pushes don't race on session.dev_iv. Earlier
        # behaviour: each handler captured `session = self._session`,
        # released, then mutated dev_iv outside any lock — second handler
        # to finish overwrote dev_iv last and the IV chain desynced from
        # the device on subsequent decrypts. JSON parse + handler
        # invocation stay outside since they touch no session state.
        async with self._lock:
            session = self._session
            if session is None:
                return web.json_response({}, status=200)
            try:
                plaintext = decrypt_device_to_app(session, enc)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("LAN datapoint: decrypt failed: %s", err)
                return web.json_response({}, status=200)
            # A successfully decrypted push proves the device is alive and
            # the session is current — count it as activity so
            # is_session_alive() doesn't declare a live-but-pushing device
            # stale (observed on ECAM610.75: pushes every 12s-minutes while
            # command polls aren't observable in logs, audit 2026-10-03).
            self._record_activity()

        try:
            data = json.loads(plaintext.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as err:
            _LOGGER.debug("LAN datapoint: decode failed (%s); returning 200", err)
            return web.json_response({}, status=200)

        if self._on_property is not None:
            try:
                await self._on_property(data)
            except Exception:  # noqa: BLE001 — never let handler kill server
                _LOGGER.exception("LAN datapoint handler raised")

        return web.json_response({}, status=200)

    async def _handle_status(self, request: web.Request) -> web.Response:
        """Health endpoint for debugging. Never exposes the lan_key."""
        peer = request.remote or "unknown"
        if not self._is_peer_allowed(peer):
            _LAN_LOGGER.debug("status: reject peer=%s (not in allowlist)", peer)
            return web.json_response({"error": "forbidden"}, status=403)
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
    method: str = "POST",
    dsn: str | None = None,
    notify: bool = False,
    scheme: str = "http",
    timeout: float = 5.0,
) -> None:
    """``POST/PUT /local_reg.json`` so the device pushes updates to our server.

    The HTTP method is load-bearing on the DL-millcore firmware (issue #50,
    measured against the decompiled Coffee Link ``AylaLanModule``):

    - **POST opens a session** and always triggers a key exchange; the app
      appends ``?dsn=<DSN>`` when opening.
    - **PUT is keep-alive only** — on a live module it never re-keys, so
      PUTing after we dropped our own keys produces a "hollow session":
      the device keeps old keys we can no longer derive.

    Therefore: call with ``method="POST"`` (and ``dsn``) to open or reopen,
    ``method="PUT"`` only as periodic keep-alive while a session is alive.
    ``notify`` mirrors the official app: 1 only while commands are pending.

    ``session_factory`` is typically ``aiohttp.ClientSession`` — pass it as
    a callable so callers can inject mocks in tests. Raises :class:`LanError`
    on any transport / HTTP failure so the coordinator can schedule a retry.
    """
    if web is None:  # pragma: no cover
        raise RuntimeError("aiohttp is required for register_with_device")

    url = f"{scheme}://{device_ip}{LAN_REG_PATH}"
    if method.upper() == "POST" and dsn:
        url = f"{url}?dsn={dsn}"
    body = {
        "local_reg": {
            "ip": advertised_ip,
            "notify": 1 if notify else 0,
            "port": advertised_port,
            "uri": "/local_lan",
        }
    }
    # Compact separators, byte-identical to the httpx/cremalink body: a
    # 2020 firmware JSON parser should not have to deal with pretty spacing.
    payload = json.dumps(body, separators=(",", ":"))
    try:
        async with (
            session_factory() as client,
            client.request(
                method.upper(),
                url,
                data=payload,
                headers={"Content-Type": "application/json"},
                timeout=ClientTimeout(total=timeout),
            ) as resp,
        ):
            text = (await resp.text())[:200]
            _LAN_LOGGER.debug("local_reg %s %s -> HTTP %s body=%r", method.upper(), url, resp.status, text)
            if resp.status >= 400:
                raise LanError(f"local_reg HTTP {resp.status}: {text!r}")
    except LanError:
        raise
    except Exception as err:  # noqa: BLE001
        # str(TimeoutError()) is empty — always include the type name.
        raise LanError(f"local_reg failed: {type(err).__name__}: {err}") from err


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


DIAG_TOTAL_TIMEOUT: float = 30.0


async def run_lan_diagnostic(
    lan_key: str | None,
    lan_ip: str | None,
    dsn: str,
    *,
    bind_host: str = "127.0.0.1",
    total_timeout: float = DIAG_TOTAL_TIMEOUT,
) -> LanDiagnosticResult:
    """Run an end-to-end LAN pipeline check and log every step.

    This is the hook wired into the ``Run LAN Diagnostic`` button. It
    never talks to real hardware — instead it spins up the embedded LAN
    server on an ephemeral loopback port, simulates the device-side
    handshake + datapoint push + command poll from an in-process
    aiohttp client, and verifies the full crypto / framing stack.

    The routine is intentionally defensive: **any** failure is caught
    and returned as a :class:`LanDiagnosticResult` so the Home Assistant
    button press never raises. ``total_timeout`` hard-caps the whole
    pipeline so a stuck aiohttp client (or a teardown bug) can't hang
    the button indefinitely — per-stage timeouts already exist on each
    client call, this is belt-and-suspenders.
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
        async with asyncio.timeout(total_timeout):
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
            _LAN_LOGGER.debug("handshake: simulated device random_1=%s time_1=%d", random_1, time_1)

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
                    raise LanHandshakeError(f"HTTP {handshake_status} body={handshake_body}")
                # Canonicalize incoming random_2 (see _handle_handshake for rationale).
                random_2 = str(handshake_body["random_2"]).rstrip("=")
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
                client_session = derive_session(lan_key, random_1, random_2, time_1, time_2)

                # -- Stage: app_to_device (command poll) ---------------------
                stage = "app_to_device"
                async with client.get(base_url + LAN_COMMAND_PATH, timeout=5.0) as resp:
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
                    client_session.app_iv = _rotate_iv_from_ciphertext(poll_body["enc"])
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
                enc = _aes_encrypt(payload, client_session.dev_crypto_key, client_session.dev_iv)
                client_session.dev_iv = _rotate_iv_from_ciphertext(enc)
                async with client.post(
                    base_url + LAN_PROPERTY_PATH,
                    json={"enc": enc},
                    timeout=5.0,
                ) as resp:
                    push_status = resp.status
                if push_status != 200:
                    raise LanError(f"datapoint push HTTP {push_status}")
                _LAN_LOGGER.debug("device_to_app: datapoint accepted enc_len=%d", len(enc))

                # -- Stage: teardown -----------------------------------------
                stage = "teardown"

    except TimeoutError:
        _LAN_LOGGER.error("diagnostic timed out after %.1fs at stage=%s", total_timeout, stage)
        result = LanDiagnosticResult(
            success=False,
            stage=stage,
            reason=f"timeout after {total_timeout:.1f}s",
            details={**details, "timeout_s": total_timeout},
        )
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
        # Teardown status is tracked separately so a stop() failure doesn't
        # silently drop the port reservation — users who re-press the
        # button would otherwise get "address in use" on the next run.
        if server is not None:
            try:
                # shield() protects teardown from the outer timeout scope
                # being in a cancelled state on TimeoutError — without it
                # ``await server.stop()`` would immediately raise
                # CancelledError and leave the socket lingering until GC.
                await asyncio.shield(server.stop())
                result.details["teardown_ok"] = True
            except Exception as err:  # noqa: BLE001
                _LAN_LOGGER.warning("teardown: server.stop raised %s", err)
                result.details["teardown_ok"] = False
                result.details["teardown_error"] = f"{type(err).__name__}: {err}"

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
