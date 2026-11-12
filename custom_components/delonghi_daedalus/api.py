"""Async client for the De'Longhi Daedalus cloud + LAN path.

Only implements what we need for initial Home Assistant integration:
    - Gigya login + JWT retrieval (cloud, one-shot at setup / on refresh)
    - LAN WebSocket AUTH handshake (recurring, per-coordinator-cycle)

Brewing commands are intentionally out of scope until we capture the exact
`Message` names from runtime (MITM or Dart AOT reverse).
"""

from __future__ import annotations

import contextlib
import ssl
from typing import Any

import aiohttp

from .const import GIGYA_API_KEY_PROD, GIGYA_BASE_URL
from .gigya_auth import (
    GigyaAuthError,
    apikey_fingerprint,
    build_jwt_app_request,
    build_jwt_request_params,
    build_login_params,
    build_login_params_legacy,
    build_login_query,
    is_credential_error,
    parse_jwt_response,
    parse_login_response,
)
from .lan_protocol import (
    LanAuthRejectedError,
    LanProtocolError,
    build_auth_frame,
    build_command_frame,
    build_lan_ws_url,
    generate_request_id,
    parse_auth_response,
    parse_message,
    validate_lan_host,
)
from .redact import get_logger, register_lan_host, sanitize_text

_LOGGER = get_logger(__name__)

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
    ) -> tuple[str, str, str]:
        """Perform Gigya login + getJWT in sequence.

        `api_key` must match the Gigya Pool the account was created under
        (EU / EU_US / CH — see const.GIGYA_API_KEYS). Using the wrong key
        yields errorCode 400093 ("Invalid parameter value: apiKey"), which
        the UI translates as 'invalid credentials' and is misleading.
        Returns (sessionToken, sessionSecret, jwt). The sessionToken (plus
        its secret) is what we'll pass to getJWT later to rotate the JWT
        without asking the user again.
        """
        session_token, session_secret = await self._gigya_login(email, password, api_key)
        jwt = await self._gigya_get_jwt(session_token, api_key, session_secret=session_secret)
        return session_token, session_secret, jwt

    async def refresh_jwt(
        self,
        *,
        session_token: str,
        api_key: str = GIGYA_API_KEY_PROD,
        session_secret: str | None = None,
    ) -> str:
        """Get a fresh JWT from an existing session token.

        With a sessionSecret (entries created after #18) the app-aligned
        Bearer+secret call is used; without one (older entries) the legacy
        oauth_token body still works.
        """
        return await self._gigya_get_jwt(session_token, api_key, session_secret=session_secret)

    async def _gigya_login(self, email: str, password: str, api_key: str) -> tuple[str, str]:
        # App-aligned attempt first (issue #18): some EU screen-sets reject
        # the old hand-built body outright. A credential refusal short-circuits
        # — retrying the same password in another format would only burn
        # Gigya rate-limit budget and risk a lockout.
        try:
            payload = await self._post_gigya(
                "/accounts.login",
                build_login_params(loginID=email, password=password, api_key=api_key),
                query=build_login_query(api_key=api_key),
            )
            return parse_login_response(payload)
        except GigyaAuthError as exc:
            if is_credential_error(exc):
                raise DaedalusAuthError(_augment_400093(sanitize_text(str(exc)), api_key)) from exc
            _LOGGER.debug("Daedalus: app-aligned login rejected (%s), retrying legacy format", sanitize_text(str(exc)))
        except aiohttp.ClientError as exc:  # pragma: no cover — transport kept uniform
            raise DaedalusConnectionError(sanitize_text(f"Gigya request to /accounts.login failed: {exc}")) from exc
        try:
            payload = await self._post_gigya(
                "/accounts.login",
                build_login_params_legacy(loginID=email, password=password, api_key=api_key),
            )
            return parse_login_response(payload)
        except GigyaAuthError as exc:
            # Gigya error text is server-controlled and can echo the loginID
            # (audit 2026-11-05): scrub before it propagates into logs,
            # error strings and last_exception.
            raise DaedalusAuthError(_augment_400093(sanitize_text(str(exc)), api_key)) from exc

    async def _gigya_get_jwt(
        self,
        session_token: str,
        api_key: str,
        *,
        session_secret: str | None = None,
    ) -> str:
        if session_secret:
            try:
                query, body, headers = build_jwt_app_request(
                    session_token=session_token, session_secret=session_secret, api_key=api_key
                )
                payload = await self._post_gigya("/accounts.getJWT", body, query=query, headers=headers)
                return parse_jwt_response(payload)
            except GigyaAuthError as exc:
                if is_credential_error(exc):
                    raise DaedalusAuthError(_augment_400093(sanitize_text(str(exc)), api_key)) from exc
                _LOGGER.debug(
                    "Daedalus: app-aligned getJWT rejected (%s), retrying legacy format", sanitize_text(str(exc))
                )
        try:
            payload = await self._post_gigya(
                "/accounts.getJWT",
                build_jwt_request_params(session_token=session_token, api_key=api_key),
            )
            return parse_jwt_response(payload)
        except GigyaAuthError as exc:
            raise DaedalusAuthError(_augment_400093(sanitize_text(str(exc)), api_key)) from exc

    async def _post_gigya(
        self,
        path: str,
        data: dict[str, str],
        *,
        query: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        session = self._get_session()
        url = f"{self._gigya_base_url}{path}"
        try:
            async with session.post(url, data=data, params=query, headers=headers, timeout=_REQUEST_TIMEOUT) as resp:
                if resp.status >= 400:
                    # Gigya normally answers 200 + errorCode, but a proxy or
                    # a stricter screen-set can answer 4xx/5xx with the same
                    # JSON body. Parse it anyway: the errorCode is what lets
                    # is_credential_error() short-circuit — degrading a 403
                    # refusal into a generic connection error would let the
                    # legacy fallback replay the password (review #18, F2).
                    body: Any = None
                    with contextlib.suppress(Exception):
                        body = await resp.json(content_type=None)
                    if isinstance(body, dict) and body.get("errorCode"):
                        raise GigyaAuthError(
                            f"Gigya error {body['errorCode']} (HTTP {resp.status}): "
                            f"{body.get('errorMessage') or body.get('errorDetails') or 'unknown'}",
                            error_code=int(body["errorCode"]),
                        )
                    raise DaedalusConnectionError(sanitize_text(f"Gigya request to {path} failed: HTTP {resp.status}"))
                return await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise DaedalusConnectionError(sanitize_text(f"Gigya request to {path} failed: {exc}")) from exc

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self._session

    @property
    def http_session(self) -> aiohttp.ClientSession:
        """Shared aiohttp session (used by the optional cloud telemetry path)."""
        return self._get_session()

    # ------------------------------------------------------------------ LAN --
    async def connect_lan(
        self,
        *,
        host: str,
        serial_number: str,
        jwt: str,
        auth_message: str = "AUTH",
    ) -> DaedalusLanConnection:
        """Open a LAN WS, perform AUTH, return a live connection handle.

        Refuses to connect to a non-private / hostname target — see
        `lan_protocol.validate_lan_host` for the rationale.

        Issue #18 (mimasek71): some Eletta Ultra firmwares answer
        `UNAUTHORIZED` to the `AUTH` frame and only accept the lowercase
        `auth` Message. A rejection therefore triggers exactly one
        reconnect with the lowercase spelling before escalating — a
        proven rejection either way, so no extra credential attempts are
        burned on a wrong JWT.
        """
        # Remember the host so the logger filter and every downstream
        # sanitize_text scrub it even without the value in hand: aiohttp's
        # own exception texts (chained via `from exc`, rendered in tracebacks
        # and last_exception) repeat it — audit 2026-11-05 (F1/F2).
        register_lan_host(host)
        try:
            validate_lan_host(host)
        except LanProtocolError as exc:
            # The error echoes the host repr; scrub it at birth so no public
            # channel (flow WARNING, UpdateFailed, diagnostics) ever carries
            # it, IPv6 forms included.
            raise DaedalusConnectionError(sanitize_text(str(exc), host=host)) from exc
        try:
            return await self._open_authed_ws(
                host=host, serial_number=serial_number, jwt=jwt, auth_message=auth_message
            )
        except LanAuthRejectedError as first_exc:
            if auth_message == "auth":
                raise DaedalusAuthError(
                    sanitize_text(f"LAN AUTH handshake rejected: {first_exc}", host=host, serial=serial_number)
                ) from first_exc
            _LOGGER.info(
                "Daedalus: LAN AUTH frame rejected (%s), retrying once with lowercase 'auth' (#18)",
                auth_message,
            )
            try:
                return await self._open_authed_ws(host=host, serial_number=serial_number, jwt=jwt, auth_message="auth")
            except LanAuthRejectedError as second_exc:
                raise DaedalusAuthError(
                    sanitize_text(
                        f"LAN AUTH handshake rejected with both {auth_message!r} and 'auth': {second_exc}",
                        host=host,
                        serial=serial_number,
                    )
                ) from second_exc

    async def _open_authed_ws(
        self,
        *,
        host: str,
        serial_number: str,
        jwt: str,
        auth_message: str,
    ) -> DaedalusLanConnection:
        """One WS connect + AUTH attempt.

        Raises `LanAuthRejectedError` (raw, caller decides escalation vs
        lowercase retry) only on a parsed ``Response != OK``; every other
        failure is a `DaedalusConnectionError`. The WS is closed on every
        failure path — no socket leak per cycle (audit 2026-11-05).
        """
        url = build_lan_ws_url(host)
        ssl_ctx = _build_trust_all_ssl_context()
        session = self._get_session()
        try:
            ws = await session.ws_connect(url, ssl=ssl_ctx, heartbeat=20)
        except aiohttp.ClientError as exc:
            # Both `{url}` and the aiohttp text carry the host — scrub here so
            # the message is clean in EVERY channel, including the raw
            # traceback HA prints for unexpected errors (no sanitizer runs
            # at that point).
            raise DaedalusConnectionError(sanitize_text(f"LAN WS connect to {url} failed: {exc}", host=host)) from exc

        # H-daedalus-2 (audit 2026-05-08): split network failures from auth
        # failures during the AUTH handshake. Transport errors map to
        # DaedalusConnectionError (transient, no Gigya call), and only an
        # explicit device-side rejection (a parsed frame with Response != OK,
        # audit 2026-11-05) raises LanAuthRejectedError; malformed replies are
        # transient too.
        try:
            await ws.send_str(build_auth_frame(serial_number=serial_number, jwt=jwt, message=auth_message))
            raw = await ws.receive(timeout=10)
            if raw.type not in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
                await ws.close()
                raise DaedalusConnectionError(
                    sanitize_text(f"LAN AUTH: unexpected WS frame type {raw.type!r}", host=host)
                )
            response = parse_message(raw.data)
            connection_id = parse_auth_response(response)
        except (aiohttp.ClientError, TimeoutError, OSError) as exc:
            # TimeoutError (ws.receive timing out on a machine that accepts
            # TCP but never answers the AUTH frame) and bare OSError (reset,
            # TLS abort) used to escape ALL the handlers below: the ws was
            # never closed and one socket leaked per 30 s cycle (audit
            # 2026-11-05). TimeoutError ⊂ OSError since 3.11; named
            # explicitly for clarity and older-runtime safety.
            await ws.close()
            raise DaedalusConnectionError(sanitize_text(f"LAN AUTH transport failed: {exc}", host=host)) from exc
        except LanAuthRejectedError:
            # Subclass of LanProtocolError: must be caught FIRST so the
            # rejection propagates raw to connect_lan (which owns the
            # lowercase retry / escalation) instead of being downgraded to
            # a transient connection error below.
            await ws.close()
            raise
        except LanProtocolError as exc:
            # Garbled / incomplete reply (non-JSON, missing ConnectionId…):
            # NOT proof of an auth problem — a rebooting machine or any
            # service on port 443 of a mistyped IP lands here. Treat as
            # transient so the coordinator retries next cycle instead of
            # rotating JWTs needlessly or opening a reauth flow the user
            # cannot act on (audit 2026-11-05).
            await ws.close()
            raise DaedalusConnectionError(sanitize_text(f"LAN AUTH got a malformed reply: {exc}", host=host)) from exc

        return DaedalusLanConnection(ws=ws, connection_id=connection_id, auth_message=auth_message)


class DaedalusLanConnection:
    """Live LAN WebSocket with an authenticated ConnectionId."""

    def __init__(
        self,
        *,
        ws: aiohttp.ClientWebSocketResponse,
        connection_id: int,
        auth_message: str = "AUTH",
    ) -> None:
        self._ws = ws
        self.connection_id = connection_id
        # Which Message spelling the machine actually accepted ("AUTH" or
        # "auth", #18) — the coordinator persists it to skip the retry on
        # the next reconnect.
        self.auth_message = auth_message

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
        except (aiohttp.ClientError, TimeoutError, OSError) as exc:
            # TimeoutError is raised (not returned) by aiohttp's receive on
            # timeout — same trap as connect_lan (audit 2026-11-05): a bare
            # TimeoutError would escape every DaedalusError handler upstream.
            # The handle itself stays open: a slow machine is transient, the
            # coordinator's closed-check owns the lifecycle.
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
