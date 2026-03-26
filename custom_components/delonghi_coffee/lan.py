"""Ayla LAN local server for De'Longhi Coffee machines.

The Ayla LAN protocol is inverted: we run an HTTP server and the machine
connects to us. Communication is encrypted with AES-128-CBC after a key
exchange handshake. This eliminates cloud round-trips for status polling
and command execution.

Protocol flow:
1. We PUT /local_reg.json on the machine to register our server
2. Machine POSTs /local_lan/key_exchange.json with its random + timestamp
3. We derive 5 session keys via HMAC-SHA256 from the shared LAN key
4. Machine GETs /local_lan/commands.json to fetch queued commands
5. Machine POSTs /local_lan/property/datapoint.json with property updates
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import socket
import struct
import time
from typing import Any, Callable

from aiohttp import ClientSession, web
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

_LOGGER = logging.getLogger(__name__)

DEFAULT_PORT = 10280
REGISTRATION_INTERVAL = 30  # seconds — keep-alive nudge
REKEY_INTERVAL = 60  # seconds — force new key exchange


def _pad_zero(data: bytes, block_size: int = 16) -> bytes:
    """Pad data with zero bytes to AES block boundary."""
    remainder = len(data) % block_size
    if remainder:
        data += b"\x00" * (block_size - remainder)
    return data


def _unpad_zero(data: bytes) -> bytes:
    """Remove trailing zero-byte padding."""
    return data.rstrip(b"\x00")


def _derive_key(lan_key_hex: str, concat: bytes, last_byte: int) -> bytes:
    """Derive a session key using double HMAC-SHA256.

    This is the Ayla LAN key derivation function:
      inner = HMAC(lan_key, concat + last_byte)
      result = HMAC(lan_key, inner + concat + last_byte)
    """
    lan_key = bytes.fromhex(lan_key_hex)
    data = concat + bytes([last_byte])
    inner = hmac.new(lan_key, data, hashlib.sha256).digest()
    return hmac.new(lan_key, inner + data, hashlib.sha256).digest()


def _detect_local_ip(target_ip: str) -> str:
    """Detect which local IP can reach the target machine.

    Opens a UDP socket (no actual packet sent) to determine
    which interface would be used to reach the machine.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((target_ip, 80))
        return sock.getsockname()[0]
    finally:
        sock.close()


class AylaLanServer:
    """Embedded HTTP server implementing the Ayla LAN protocol.

    The machine connects to this server after registration. All communication
    is encrypted with AES-128-CBC using session keys derived from a shared
    LAN key obtained from the Ayla cloud.
    """

    def __init__(
        self,
        lan_key: str,
        lan_key_id: int,
        machine_ip: str,
        port: int = DEFAULT_PORT,
    ) -> None:
        """Initialize the LAN server.

        Args:
            lan_key: Hex-encoded LAN key from Ayla cloud.
            lan_key_id: Key ID for key exchange validation.
            machine_ip: IP address of the De'Longhi machine.
            port: Port to listen on (default 10280).
        """
        self._lan_key = lan_key
        self._lan_key_id = lan_key_id
        self._machine_ip = machine_ip
        self._port = port
        self._server_ip: str | None = None

        # HTTP server
        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

        # Session keys (set after key exchange)
        self._app_sign_key: bytes | None = None
        self._app_crypto_key: bytes | None = None
        self._app_iv: bytes | None = None
        self._dev_crypto_key: bytes | None = None
        self._dev_iv: bytes | None = None
        self._session_active = False

        # Command queue — machine polls GET /local_lan/commands.json
        self._command_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._cmd_id = 0

        # Property cache — machine pushes via POST /local_lan/property/datapoint.json
        self._properties: dict[str, Any] = {}
        self._property_callbacks: list[Callable[[str, Any], None]] = []

        # Background tasks
        self._registration_task: asyncio.Task[None] | None = None
        self._rekey_task: asyncio.Task[None] | None = None
        self._running = False

    @property
    def is_connected(self) -> bool:
        """Return True if a LAN session is active (keys exchanged)."""
        return self._session_active

    async def start(self, hass: Any = None) -> None:
        """Start the LAN server and begin registration loop.

        Args:
            hass: HomeAssistant instance (unused for now, reserved for future).
        """
        if self._running:
            _LOGGER.warning("LAN server already running")
            return

        # Detect our local IP on the same network as the machine
        try:
            self._server_ip = await asyncio.get_event_loop().run_in_executor(
                None, _detect_local_ip, self._machine_ip
            )
        except OSError as err:
            _LOGGER.error("Cannot detect local IP to reach %s: %s", self._machine_ip, err)
            return

        _LOGGER.info(
            "Starting Ayla LAN server on %s:%d (machine: %s)",
            self._server_ip, self._port, self._machine_ip,
        )

        # Build aiohttp app with routes
        self._app = web.Application()
        self._app.router.add_post("/local_lan/key_exchange.json", self.handle_key_exchange)
        self._app.router.add_get("/local_lan/commands.json", self.handle_commands)
        self._app.router.add_post(
            "/local_lan/property/datapoint.json", self.handle_datapoint
        )

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()

        try:
            self._site = web.TCPSite(self._runner, "0.0.0.0", self._port)
            await self._site.start()
        except OSError as err:
            _LOGGER.error("Failed to bind LAN server on port %d: %s", self._port, err)
            await self._runner.cleanup()
            return

        self._running = True

        # Start background loops
        self._registration_task = asyncio.create_task(self._registration_loop())
        self._rekey_task = asyncio.create_task(self._rekey_loop())

        _LOGGER.info("Ayla LAN server started on port %d", self._port)

    async def stop(self) -> None:
        """Stop the LAN server and all background tasks."""
        self._running = False
        self._session_active = False

        if self._registration_task and not self._registration_task.done():
            self._registration_task.cancel()
            try:
                await self._registration_task
            except asyncio.CancelledError:
                pass

        if self._rekey_task and not self._rekey_task.done():
            self._rekey_task.cancel()
            try:
                await self._rekey_task
            except asyncio.CancelledError:
                pass

        if self._site:
            await self._site.stop()
        if self._runner:
            await self._runner.cleanup()

        self._app = None
        self._runner = None
        self._site = None

        _LOGGER.info("Ayla LAN server stopped")

    # ── Registration ──────────────────────────────────────────────────

    async def register(self) -> bool:
        """Send PUT /local_reg.json to the machine to register our server.

        Returns True if registration was accepted.
        """
        if not self._server_ip:
            _LOGGER.error("Server IP not determined, cannot register")
            return False

        payload = {
            "local_reg": {
                "ip": self._server_ip,
                "notify": 1,
                "port": self._port,
                "uri": "/local_lan",
            }
        }

        url = f"http://{self._machine_ip}/local_reg.json"

        try:
            async with ClientSession() as session:
                async with session.put(
                    url, json=payload, timeout=5
                ) as resp:
                    if resp.status == 200:
                        _LOGGER.debug("LAN registration accepted by %s", self._machine_ip)
                        return True
                    text = await resp.text()
                    _LOGGER.warning(
                        "LAN registration rejected: HTTP %d — %s", resp.status, text
                    )
                    return False
        except Exception as err:
            _LOGGER.debug("LAN registration failed: %s", err)
            return False

    async def _registration_loop(self) -> None:
        """Periodically re-register to keep the LAN connection alive."""
        while self._running:
            try:
                await self.register()
            except Exception:
                _LOGGER.debug("Registration nudge failed", exc_info=True)
            await asyncio.sleep(REGISTRATION_INTERVAL)

    async def _rekey_loop(self) -> None:
        """Periodically invalidate session to force re-key exchange."""
        while self._running:
            await asyncio.sleep(REKEY_INTERVAL)
            if self._session_active:
                _LOGGER.debug("Forcing re-key exchange (periodic)")
                self._session_active = False

    # ── Key Exchange ──────────────────────────────────────────────────

    async def handle_key_exchange(self, request: web.Request) -> web.Response:
        """Handle POST /local_lan/key_exchange.json from the machine.

        The machine sends its random_1 and time_1. We generate random_2 and
        time_2, then derive all 5 session keys.
        """
        try:
            body = await request.json()
        except Exception:
            _LOGGER.warning("Key exchange: invalid JSON body")
            return web.Response(status=400, text="Invalid JSON")

        key_exchange = body.get("key_exchange", {})
        random_1_b64 = key_exchange.get("random_1")
        time_1 = key_exchange.get("time_1")
        key_id = key_exchange.get("key_id")

        if not random_1_b64 or time_1 is None or key_id is None:
            _LOGGER.warning("Key exchange: missing fields: %s", body)
            return web.Response(status=400, text="Missing fields")

        if key_id != self._lan_key_id:
            _LOGGER.warning(
                "Key exchange: key_id mismatch (got %s, expected %s)",
                key_id, self._lan_key_id,
            )
            return web.Response(status=403, text="Key ID mismatch")

        try:
            random_1 = base64.b64decode(random_1_b64)
        except Exception:
            _LOGGER.warning("Key exchange: invalid base64 random_1")
            return web.Response(status=400, text="Invalid random_1")

        # Generate our random and timestamp
        random_2 = secrets.token_bytes(16)
        time_2 = int(time.time())

        # Convert timestamps to 4-byte big-endian
        time_1_bytes = struct.pack(">I", time_1)
        time_2_bytes = struct.pack(">I", time_2)

        # Build concatenation buffers for key derivation
        concat_app = random_1 + random_2 + time_1_bytes + time_2_bytes
        concat_dev = random_2 + random_1 + time_2_bytes + time_1_bytes

        # Derive all 5 session keys
        self._app_sign_key = _derive_key(self._lan_key, concat_app, 0x30)
        self._app_crypto_key = _derive_key(self._lan_key, concat_app, 0x31)[:16]
        self._app_iv = _derive_key(self._lan_key, concat_app, 0x32)[:16]
        self._dev_crypto_key = _derive_key(self._lan_key, concat_dev, 0x31)[:16]
        self._dev_iv = _derive_key(self._lan_key, concat_dev, 0x32)[:16]

        self._session_active = True

        _LOGGER.info("LAN key exchange complete — session active")

        # Respond with our random_2 and time_2
        response_payload = {
            "key_exchange": {
                "random_2": base64.b64encode(random_2).decode(),
                "time_2": time_2,
            }
        }

        return web.json_response(response_payload)

    # ── Crypto ────────────────────────────────────────────────────────

    def _encrypt(self, plaintext: bytes) -> tuple[bytes, bytes]:
        """AES-128-CBC encrypt and HMAC-SHA256 sign.

        Returns (ciphertext, hmac_digest).
        IV rotation: after encryption, the new IV becomes the last 16 bytes
        of the ciphertext.
        """
        if not self._app_crypto_key or not self._app_iv or not self._app_sign_key:
            raise RuntimeError("No active LAN session — keys not derived")

        padded = _pad_zero(plaintext)

        cipher = Cipher(algorithms.AES(self._app_crypto_key), modes.CBC(self._app_iv))
        encryptor = cipher.encryptor()
        ciphertext = encryptor.update(padded) + encryptor.finalize()

        # Rotate IV — last 16 bytes of ciphertext
        self._app_iv = ciphertext[-16:]

        # Sign the plaintext (not the ciphertext)
        sig = hmac.new(self._app_sign_key, plaintext, hashlib.sha256).digest()

        return ciphertext, sig

    def _decrypt(self, ciphertext: bytes) -> bytes:
        """AES-128-CBC decrypt with IV rotation.

        After decryption, the new inbound IV becomes the last 16 bytes
        of the ciphertext.
        """
        if not self._dev_crypto_key or not self._dev_iv:
            raise RuntimeError("No active LAN session — keys not derived")

        cipher = Cipher(algorithms.AES(self._dev_crypto_key), modes.CBC(self._dev_iv))
        decryptor = cipher.decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()

        # Rotate IV — last 16 bytes of ciphertext
        self._dev_iv = ciphertext[-16:]

        return _unpad_zero(padded)

    def _build_encrypted_message(self, plaintext: bytes) -> dict[str, str]:
        """Encrypt plaintext and return the wire format dict."""
        ciphertext, sig = self._encrypt(plaintext)
        return {
            "enc": base64.b64encode(ciphertext).decode(),
            "sign": base64.b64encode(sig).decode(),
        }

    def _decrypt_message(self, message: dict[str, Any]) -> bytes:
        """Decrypt an incoming encrypted message dict."""
        ciphertext = base64.b64decode(message["enc"])
        return self._decrypt(ciphertext)

    # ── Command Queue ─────────────────────────────────────────────────

    async def handle_commands(self, request: web.Request) -> web.Response:
        """Handle GET /local_lan/commands.json from the machine.

        The machine polls this endpoint to pick up queued commands.
        Returns an encrypted heartbeat if no commands are pending,
        or encrypted command payloads if commands are queued.
        """
        if not self._session_active:
            return web.Response(status=412, text="No active session")

        self._cmd_id += 1

        # Drain all pending commands (non-blocking)
        commands: list[dict[str, Any]] = []
        while not self._command_queue.empty():
            try:
                cmd = self._command_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            commands.append(cmd)

        if commands:
            # Wrap commands in the expected wire format
            payload = {
                "seq_no": self._cmd_id,
                "data": {
                    "properties": [
                        {
                            "property": {
                                "base_type": "string",
                                "name": cmd["cmd"]["data"]["name"],
                                "value": cmd["cmd"]["data"]["value"],
                            }
                        }
                        for cmd in commands
                    ]
                },
            }
        else:
            # Heartbeat — empty data keeps the session alive
            payload = {"seq_no": self._cmd_id, "data": {}}

        plaintext = json.dumps(payload).encode()
        encrypted = self._build_encrypted_message(plaintext)
        return web.json_response(encrypted)

    async def send_command(self, property_name: str, value: str) -> None:
        """Queue a property set command for the machine to pick up.

        Args:
            property_name: Ayla property name (e.g. "0x83_brew_cmd").
            value: Value to set (string representation).
        """
        if not self._session_active:
            _LOGGER.warning("Cannot send LAN command — no active session")
            return

        cmd = {
            "cmd": {
                "data": {
                    "name": property_name,
                    "value": value,
                },
            }
        }

        await self._command_queue.put(cmd)
        _LOGGER.debug("Queued LAN command: %s = %s...", property_name, value[:50] if len(value) > 50 else value)

    # ── Property Updates ──────────────────────────────────────────────

    async def handle_datapoint(self, request: web.Request) -> web.Response:
        """Handle POST /local_lan/property/datapoint.json from the machine.

        The machine pushes encrypted property updates here.
        """
        if not self._session_active:
            _LOGGER.debug("Datapoint received but no active session — ignoring")
            return web.Response(status=412, text="No active session")

        try:
            body = await request.json()
        except Exception:
            _LOGGER.warning("Datapoint: invalid JSON body")
            return web.Response(status=400, text="Invalid JSON")

        try:
            plaintext = self._decrypt_message(body)
            data = json.loads(plaintext)
        except Exception as err:
            _LOGGER.warning("Datapoint: decryption/parse failed: %s", err)
            return web.Response(status=400, text="Decrypt failed")

        # Extract property name and value — try multiple formats
        dp = data.get("datapoint") or data.get("data") or data
        name = dp.get("name")
        value = dp.get("value")

        if name:
            self._properties[name] = value
            _LOGGER.debug("LAN property update: %s = %s", name, value)

            # Notify callbacks
            for callback in self._property_callbacks:
                try:
                    callback(name, value)
                except Exception:
                    _LOGGER.debug("Property callback error", exc_info=True)

        return web.json_response({"status": "ok"})

    def get_latest_property(self, name: str) -> Any:
        """Get the latest value of a property pushed by the machine.

        Args:
            name: Ayla property name.

        Returns:
            The latest value, or None if not yet received.
        """
        return self._properties.get(name)

    def get_all_properties(self) -> dict[str, Any]:
        """Get all cached property values."""
        return dict(self._properties)

    def on_property_update(self, callback: Callable[[str, Any], None]) -> None:
        """Register a callback for property updates.

        The callback receives (property_name, value) on each update
        from the machine.

        Args:
            callback: Function called with (name, value) on each update.
        """
        self._property_callbacks.append(callback)

    def remove_property_callback(self, callback: Callable[[str, Any], None]) -> None:
        """Remove a previously registered property update callback."""
        try:
            self._property_callbacks.remove(callback)
        except ValueError:
            pass
