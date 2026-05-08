"""Tests for the async DaedalusApi client.

Only the Gigya HTTP round-trip is network-tested here. The LAN WebSocket
path is exercised in integration tests (coordinator) because aiohttp's WS
server mock is heavier than this scope justifies.
"""

from __future__ import annotations

import asyncio

import aiohttp
import pytest
from aiohttp import web

from custom_components.delonghi_daedalus.api import (
    DaedalusApi,
    DaedalusAuthError,
    DaedalusConnectionError,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _start_gigya(handlers: dict[str, callable]) -> tuple[web.AppRunner, str]:
    """Spin a localhost aiohttp server that stubs Gigya endpoints."""
    app = web.Application()
    for path, handler in handlers.items():
        app.router.add_post(path, handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    return runner, f"http://127.0.0.1:{port}"


def test_login_roundtrip_returns_jwt() -> None:
    async def _run() -> None:
        async def login(request: web.Request) -> web.Response:
            body = await request.post()
            assert body["loginID"] == "user@example.com"
            assert body["password"] == "hunter2"
            assert body["apiKey"] == "4_mXSplGaqrFT0H88TAjqJuA"
            return web.json_response(
                {
                    "errorCode": 0,
                    "sessionInfo": {"sessionToken": "st", "sessionSecret": "ss"},
                    "UID": "uid-1",
                }
            )

        async def get_jwt(request: web.Request) -> web.Response:
            body = await request.post()
            assert body["oauth_token"] == "st"
            return web.json_response({"errorCode": 0, "id_token": "jwt-abc"})

        runner, base = await _start_gigya({"/accounts.login": login, "/accounts.getJWT": get_jwt})
        try:
            api = DaedalusApi(gigya_base_url=base)
            session_token, jwt = await api.login_and_get_jwt(email="user@example.com", password="hunter2")
            assert session_token == "st"
            assert jwt == "jwt-abc"
        finally:
            await api.close()
            await runner.cleanup()

    asyncio.run(_run())


def test_login_raises_auth_error_on_gigya_error_code() -> None:
    async def _run() -> None:
        async def login(request: web.Request) -> web.Response:
            return web.json_response({"errorCode": 403042, "errorMessage": "Invalid credentials"})

        runner, base = await _start_gigya({"/accounts.login": login})
        try:
            api = DaedalusApi(gigya_base_url=base)
            with pytest.raises(DaedalusAuthError):
                await api.login_and_get_jwt(email="u", password="p")
        finally:
            await api.close()
            await runner.cleanup()

    asyncio.run(_run())


def test_login_wraps_transport_errors() -> None:
    async def _run() -> None:
        api = DaedalusApi(gigya_base_url="http://127.0.0.1:1")  # closed port
        try:
            with pytest.raises(DaedalusConnectionError):
                await api.login_and_get_jwt(email="u", password="p")
        finally:
            await api.close()

    asyncio.run(_run())


def test_login_uses_custom_api_key_when_pool_is_eu_us() -> None:
    """EU_US accounts (Gigya SDK v3) need the alternate apiKey from the manifest."""

    async def _run() -> None:
        captured: dict[str, str] = {}

        async def login(request: web.Request) -> web.Response:
            body = await request.post()
            captured["apiKey"] = str(body["apiKey"])
            return web.json_response(
                {
                    "errorCode": 0,
                    "sessionInfo": {"sessionToken": "st", "sessionSecret": "ss"},
                    "UID": "uid-1",
                }
            )

        async def get_jwt(request: web.Request) -> web.Response:
            return web.json_response({"errorCode": 0, "id_token": "jwt-abc"})

        runner, base = await _start_gigya({"/accounts.login": login, "/accounts.getJWT": get_jwt})
        try:
            api = DaedalusApi(gigya_base_url=base)
            eu_us_key = "3_e5qn7USZK-QtsIso1wCelqUKAK_IVEsYshRIssQ-X-k55haiZXmKWDHDRul2e5Y2"
            await api.login_and_get_jwt(email="u", password="p", api_key=eu_us_key)
            assert captured["apiKey"] == eu_us_key
        finally:
            await api.close()
            await runner.cleanup()

    asyncio.run(_run())


# -- H-daedalus-1 + H-daedalus-2 (audit 2026-05-08) --------------------------


def test_400093_message_includes_apikey_fingerprint() -> None:
    """When Gigya rejects an apiKey with errorCode 400093, the raised
    ``DaedalusAuthError`` must include the apiKey fingerprint (non-secret,
    public OAuth-client-id-equivalent) so a remote bug reporter can
    confirm whether their installed const matches the source-of-truth.
    Live ticket: Issue #18 (stivxgamer, Eletta Ultra)."""

    async def _run() -> None:
        async def login(request: web.Request) -> web.Response:
            return web.json_response({"errorCode": 400093, "errorMessage": "Invalid ApiKey parameter"})

        runner, base = await _start_gigya({"/accounts.login": login})
        try:
            api = DaedalusApi(gigya_base_url=base)
            with pytest.raises(DaedalusAuthError) as excinfo:
                # Pass a known-truncated apiKey so the test asserts the
                # exact length the helper surfaces.
                await api.login_and_get_jwt(email="u", password="p", api_key="4_truncated")
        finally:
            await api.close()
            await runner.cleanup()

        msg = str(excinfo.value)
        assert "400093" in msg
        assert "len:11" in msg
        assert "sha1[:8]=" in msg

    asyncio.run(_run())


def test_send_command_raises_daedalus_error_on_reserved_param_key() -> None:
    """H-daedalus-1: build_command_frame guards against caller-supplied
    params overriding Message/ConnectionId/RequestId. send_command must
    surface that as DaedalusError (caller bug, not transport failure),
    so a future service handler can distinguish wire-format violations
    from network blips.
    """
    from unittest.mock import AsyncMock, MagicMock

    from custom_components.delonghi_daedalus.api import DaedalusError, DaedalusLanConnection

    async def _run() -> None:
        ws = MagicMock()
        ws.send_str = AsyncMock()
        conn = DaedalusLanConnection(ws=ws, connection_id=1)
        with pytest.raises(DaedalusError, match="LAN command frame build rejected"):
            # ``Message`` is a reserved key — caller cannot override it.
            await conn.send_command(message="Brew", params={"Message": "Hijack"})
        # And the malformed call must NOT have hit the WebSocket.
        ws.send_str.assert_not_called()

    asyncio.run(_run())


def test_connect_lan_transport_error_does_not_raise_auth_error() -> None:
    """H-daedalus-2: a network-level failure during the AUTH handshake
    (TLS abort, peer reset, read timeout) must surface as
    ``DaedalusConnectionError`` — NOT ``DaedalusAuthError`` — so the
    coordinator does not interpret it as "JWT stale, refresh via Gigya".
    Earlier behaviour turned a flaky Wi-Fi link into an unbounded Gigya
    refresh storm.
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    from custom_components.delonghi_daedalus.api import DaedalusConnectionError

    async def _run() -> None:
        api = DaedalusApi()
        try:
            ws = MagicMock()
            ws.send_str = AsyncMock(side_effect=aiohttp.ClientConnectionError("peer reset"))
            ws.close = AsyncMock()

            session = MagicMock()
            session.ws_connect = AsyncMock(return_value=ws)
            with patch.object(api, "_get_session", return_value=session):
                with pytest.raises(DaedalusConnectionError, match="transport failed"):
                    await api.connect_lan(host="192.168.1.42", serial_number="SN1", jwt="eyJ.eyJ.x")
                ws.close.assert_awaited()
        finally:
            await api.close()

    asyncio.run(_run())
