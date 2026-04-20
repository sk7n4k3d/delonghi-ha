"""Tests for the async DaedalusApi client.

Only the Gigya HTTP round-trip is network-tested here. The LAN WebSocket
path is exercised in integration tests (coordinator) because aiohttp's WS
server mock is heavier than this scope justifies.
"""

from __future__ import annotations

import asyncio

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
