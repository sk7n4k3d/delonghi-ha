"""Async client for the De'Longhi Daedalus cloud + LAN path.

Only implements what we need for initial Home Assistant integration:
    - Gigya login + JWT retrieval (cloud, one-shot at setup / on refresh)
    - LAN WebSocket AUTH handshake (recurring, per-coordinator-cycle)

Brewing commands are intentionally out of scope until we capture the exact
`Message` names from runtime (MITM or Dart AOT reverse).
"""

from __future__ import annotations

import logging
import ssl
from typing import Any

import aiohttp

from .const import GIGYA_API_KEY_PROD, GIGYA_BASE_URL
from .gigya_auth import (
    GigyaAuthError,
    apikey_fingerprint,
    build_jwt_request_params,
    build_login_params,
    parse_jwt_response,
    parse_login_response,
)
from .lan_protocol import (
    LanProtocolError,
    build_auth_frame,
    build_command_frame,
    build_lan_ws_url,
    generate_request_id,
    parse_auth_response,
    parse_message,
    validate_lan_host,
)

_LOGGER = logging.getLogger(__name__)

_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)


def _augment_400093(message: str, api_key: str) -> str:
    """Append apiKey fingerprint when Gigya reports 400093 Invalid ApiKey.

    H-daedalus-4 (audit 2026-05-08): the live ticket on Issue #18 had no
    way to disambiguate "config corrupted at the install side" from
    "Gigya transient blip" — the only log line was the bare error message.
    The fingerprint is non-secret (apiKey is a public OAuth-client-id
    extracted from the APK manifest) but tells us at a glance whether
    the user's install ships a truncated key vs. the canonical 24/66/66.
    """
    if "400093" not in message:
        return message
    return f"{message} ({apikey_fingerprint(api_key)})"


class DaedalusError(RuntimeError):
    """Base error for the Daedalus client."""


class DaedalusAuthError(DaedalusError):
    """Credentials refused or JWT unobtainable."""


class DaedalusConnectionError(DaedalusError):
    """Transport / network-level failure against Gigya or the LAN WS."""


class DaedalusApi:
    """Thin, stateless-ish async client.

    One instance owns an aiohttp ClientSession for the Gigya REST calls.
    LAN WebSocket sessions are created per-connect (they're long-lived
    and handled by the coordinator).
    """

    def __init__(
        self,
        *,
        gigya_base_url: str = GIGYA_BASE_URL,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._gigya_base_url = gigya_base_url.rstrip("/")
        self._session = session
        self._owns_session = session is None

    async def close(self) -> None:
        if self._session is not None and self._owns_session:
            await self._session.close()
            self._session = None

    async def login_and_get_jwt(
        self,
        *,
        email: str,
        password: str,
        api_key: str = GIGYA_API_KEY_PROD,
    ) -> tuple[str, str]:
        """Perform Gigya login + getJWT in sequence.

        `api_key` must match the Gigya Pool the account was created under
        (EU / EU_US / CH — see const.GIGYA_API_KEYS). Using the wrong key
        yields errorCode 400093 ("Invalid parameter value: apiKey"), which
        the UI translates as 'invalid credentials' and is misleading.
        Returns (sessionToken, jwt). The sessionToken is what we'll pass
        to getJWT later to rotate the JWT without asking the user again.
        """
        session_token, _session_secret = await self._gigya_login(email, password, api_key)
        jwt = await self._gigya_get_jwt(session_token, api_key)
        return session_token, jwt

    async def refresh_jwt(self, *, session_token: str, api_key: str = GIGYA_API_KEY_PROD) -> str:
        """Get a fresh JWT from an existing session token."""
        return await self._gigya_get_jwt(session_token, api_key)

    async def _gigya_login(self, email: str, password: str, api_key: str) -> tuple[str, str]:
        payload = await self._post_gigya(
            "/accounts.login",
            build_login_params(loginID=email, password=password, api_key=api_key),
        )
        try:
            return parse_login_response(payload)
        except GigyaAuthError as exc:
            raise DaedalusAuthError(_augment_400093(str(exc), api_key)) from exc

    async def _gigya_get_jwt(self, session_token: str, api_key: str) -> str:
        payload = await self._post_gigya(
            "/accounts.getJWT",
            build_jwt_request_params(session_token=session_token, api_key=api_key),
        )
        try:
            return parse_jwt_response(payload)
        except GigyaAuthError as exc:
            raise DaedalusAuthError(_augment_400093(str(exc), api_key)) from exc

    async def _post_gigya(self, path: str, data: dict[str, str]) -> dict[str, Any]:
        session = self._get_session()
        url = f"{self._gigya_base_url}{path}"
        try:
            async with session.post(url, data=data, timeout=_REQUEST_TIMEOUT) as resp:
                resp.raise_for_status()
                return await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise DaedalusConnectionError(f"Gigya request to {path} failed: {exc}") from exc

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self._session

    # ------------------------------------------------------------------ LAN --
    async def connect_lan(self, *, host: str, serial_number: str, jwt: str) -> DaedalusLanConnection:
        """Open a LAN WS, perform AUTH, return a live connection handle.

        Refuses to connect to a non-private / hostname target — see
        `lan_protocol.validate_lan_host` for the rationale.
        """
        try:
            validate_lan_host(host)
        except LanProtocolError as exc:
            raise DaedalusConnectionError(str(exc)) from exc
        url = build_lan_ws_url(host)
        ssl_ctx = _build_trust_all_ssl_context()
        session = self._get_session()
        try:
            ws = await session.ws_connect(url, ssl=ssl_ctx, heartbeat=20)
        except aiohttp.ClientError as exc:
            raise DaedalusConnectionError(f"LAN WS connect to {url} failed: {exc}") from exc

        # H-daedalus-2 (audit 2026-05-08): split network failures from auth
        # failures during the AUTH handshake. The earlier code wrapped
        # *both* aiohttp.ClientError (transport-level: TLS handshake aborted,
        # TCP reset, read timeout) AND LanProtocolError (the device
        # explicitly rejected our AuthToken) as DaedalusAuthError. The
        # coordinator then interpreted any DaedalusAuthError as "JWT
        # expired, rotate it via Gigya getJWT" — turning a flaky Wi-Fi
        # link into an unbounded Gigya refresh storm. Now: transport
        # errors map to DaedalusConnectionError (transient, no Gigya
        # call), and only an explicit device-side rejection raises
        # DaedalusAuthError.
        try:
            await ws.send_str(build_auth_frame(serial_number=serial_number, jwt=jwt))
            raw = await ws.receive(timeout=10)
            if raw.type not in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
                await ws.close()
                raise DaedalusConnectionError(f"LAN AUTH: unexpected WS frame type {raw.type!r}")
            response = parse_message(raw.data)
            connection_id = parse_auth_response(response)
        except aiohttp.ClientError as exc:
            await ws.close()
            raise DaedalusConnectionError(f"LAN AUTH transport failed: {exc}") from exc
        except LanProtocolError as exc:
            # The device parsed our frame and rejected it (or sent a
            # malformed reply). This is the only path that genuinely
            # signals an auth problem — let the coordinator reauth.
            await ws.close()
            raise DaedalusAuthError(f"LAN AUTH handshake rejected: {exc}") from exc

        return DaedalusLanConnection(ws=ws, connection_id=connection_id)


class DaedalusLanConnection:
    """Live LAN WebSocket with an authenticated ConnectionId."""

    def __init__(self, *, ws: aiohttp.ClientWebSocketResponse, connection_id: int) -> None:
        self._ws = ws
        self.connection_id = connection_id

    @property
    def closed(self) -> bool:
        return self._ws.closed

    async def close(self) -> None:
        await self._ws.close()

    async def send_command(
        self,
        *,
        message: str,
        params: dict[str, Any] | None = None,
    ) -> str:
        """Send a command and return its generated RequestId.

        H-daedalus-1 (audit 2026-05-08): ``build_command_frame`` enforces
        a reserved-key guard against caller-supplied params overriding
        ``Message`` / ``ConnectionId`` / ``RequestId`` and raises
        ``LanProtocolError`` when violated. This wrapper used to catch
        only ``aiohttp.ClientError`` so a malformed-param call from a
        future service handler (e.g. ``delonghi_daedalus.send_raw``)
        bubbled an opaque error AND lost the request_id mapping. We now
        distinguish wire-format violations (``DaedalusError`` — caller
        bug) from transport failures (``DaedalusConnectionError``).
        """
        request_id = generate_request_id()
        try:
            frame = build_command_frame(
                message=message,
                connection_id=self.connection_id,
                request_id=request_id,
                params=params,
            )
        except LanProtocolError as exc:
            raise DaedalusError(f"LAN command frame build rejected: {exc}") from exc
        try:
            await self._ws.send_str(frame)
        except aiohttp.ClientError as exc:
            raise DaedalusConnectionError(f"LAN send failed: {exc}") from exc
        return request_id

    async def receive(self, *, timeout: float = 10.0) -> dict[str, Any]:
        """Read the next JSON frame from the machine."""
        try:
            raw = await self._ws.receive(timeout=timeout)
        except aiohttp.ClientError as exc:
            raise DaedalusConnectionError(f"LAN receive failed: {exc}") from exc
        if raw.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING):
            raise DaedalusConnectionError("LAN WS closed by machine")
        if raw.type not in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
            raise DaedalusConnectionError(f"unexpected WS frame type {raw.type!r}")
        return parse_message(raw.data)


def _build_trust_all_ssl_context() -> ssl.SSLContext:
    """Build a trust-all TLS context for the self-signed machine cert.

    The De'Longhi Daedalus firmware presents a self-signed certificate on
    port 443 and the official app's WebSocket client is configured with a
    trust-all X509TrustManager + ALLOW_ALL hostname verifier. We mirror
    that — there is no CA to pin against. Authentication happens in-band
    via the JWT passed in the AUTH frame.
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx
