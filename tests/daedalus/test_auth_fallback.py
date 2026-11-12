"""Issue #18 fallbacks: Gigya format retry + LAN AUTH case retry."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

from custom_components.delonghi_daedalus.api import (
    DaedalusApi,
    DaedalusAuthError,
    DaedalusConnectionError,
)
from custom_components.delonghi_daedalus.lan_protocol import LanProtocolError

HOST = "fd12:b0af::18"  # unique ULA, no collision with other test files


# ------------------------------------------------------- Gigya format retry --


async def _serve(handlers: dict[str, callable]) -> tuple[web.AppRunner, str]:
    app = web.Application()
    for path, handler in handlers.items():
        app.router.add_post(path, handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    return runner, f"http://127.0.0.1:{port}"


def test_login_falls_back_to_legacy_on_non_credential_error() -> None:
    """App-format rejected by a screen-set error (not a credential refusal)
    must transparently retry the legacy body — the pre-#18 path."""

    async def _run() -> None:
        seen: list[str] = []

        async def login(request: web.Request) -> web.Response:
            body = await request.post()
            if "include" in body:  # app-aligned attempt
                seen.append("app")
                return web.json_response({"errorCode": 403013, "errorMessage": "screen-set rejected"})
            seen.append("legacy")
            return web.json_response({"errorCode": 0, "sessionInfo": {"sessionToken": "st", "sessionSecret": "ss"}})

        async def get_jwt(request: web.Request) -> web.Response:
            return web.json_response({"errorCode": 0, "id_token": "jwt-abc"})

        runner, base = await _serve({"/accounts.login": login, "/accounts.getJWT": get_jwt})
        try:
            api = DaedalusApi(gigya_base_url=base)
            token, secret, jwt = await api.login_and_get_jwt(email="u", password="p")
            assert seen == ["app", "legacy"]
            assert (token, secret, jwt) == ("st", "ss", "jwt-abc")
            await api.close()
        finally:
            await runner.cleanup()

    asyncio.run(_run())


def test_login_does_NOT_retry_legacy_on_wrong_password() -> None:
    """A credential refusal (403001) must NOT be replayed in the legacy
    format: double-firing a wrong password burns Gigya rate limits."""

    async def _run() -> None:
        seen: list[str] = []

        async def login(request: web.Request) -> web.Response:
            seen.append("attempt")
            return web.json_response({"errorCode": 403001, "errorMessage": "Wrong loginID/password"})

        runner, base = await _serve({"/accounts.login": login})
        try:
            api = DaedalusApi(gigya_base_url=base)
            with pytest.raises(DaedalusAuthError):
                await api.login_and_get_jwt(email="u", password="bad")
            assert seen == ["attempt"]  # exactly one login attempt
            await api.close()
        finally:
            await runner.cleanup()

    asyncio.run(_run())


def test_login_does_NOT_retry_on_lockout_codes() -> None:
    """Review #18 F1: 403023 (invalid loginID/password) and 403025 (account
    locked) are credential refusals too — replaying the legacy body would
    double every failed login and deepen a lockout."""

    async def _run(code: int) -> None:
        seen: list[str] = []

        async def login(request: web.Request) -> web.Response:
            seen.append("attempt")
            return web.json_response({"errorCode": code, "errorMessage": "refused"})

        runner, base = await _serve({"/accounts.login": login})
        try:
            api = DaedalusApi(gigya_base_url=base)
            with pytest.raises(DaedalusAuthError):
                await api.login_and_get_jwt(email="u", password="bad")
            assert seen == ["attempt"], f"code {code} must not replay the password"
            await api.close()
        finally:
            await runner.cleanup()

    for code in (403023, 403025, 403026, 403002):
        asyncio.run(_run(code))


def test_http_4xx_with_errorcode_short_circuits() -> None:
    """Review #18 F2: a proxy/screen-set answering HTTP 403 with the Gigya
    JSON body must still surface the errorCode — degrading it to a generic
    connection error would let the legacy fallback replay the password."""

    async def _run() -> None:
        seen: list[str] = []

        async def login(request: web.Request) -> web.Response:
            seen.append("attempt")
            return web.json_response({"errorCode": 403001, "errorMessage": "Wrong loginID/password"}, status=403)

        runner, base = await _serve({"/accounts.login": login})
        try:
            api = DaedalusApi(gigya_base_url=base)
            with pytest.raises(DaedalusAuthError):
                await api.login_and_get_jwt(email="u", password="bad")
            assert seen == ["attempt"]  # parsed as credential refusal → no replay
            await api.close()
        finally:
            await runner.cleanup()

    asyncio.run(_run())


def test_http_5xx_without_errorcode_is_connection_error() -> None:
    """A 4xx/5xx with no parsable Gigya body stays transient (connection
    error), so a flaky proxy does not look like a bad password."""

    async def _run() -> None:
        async def login(request: web.Request) -> web.Response:
            return web.Response(status=502, text="bad gateway")

        runner, base = await _serve({"/accounts.login": login})
        try:
            api = DaedalusApi(gigya_base_url=base)
            with pytest.raises(DaedalusConnectionError):
                await api.login_and_get_jwt(email="u", password="p")
            await api.close()
        finally:
            await runner.cleanup()

    asyncio.run(_run())


def test_getjwt_falls_back_to_legacy_when_app_form_rejected() -> None:
    async def _run() -> None:
        seen: list[str] = []

        async def login(request: web.Request) -> web.Response:
            return web.json_response({"errorCode": 0, "sessionInfo": {"sessionToken": "st", "sessionSecret": "ss"}})

        async def get_jwt(request: web.Request) -> web.Response:
            if "secret" in request.query:
                seen.append("app")
                return web.json_response({"errorCode": 403013, "errorMessage": "nope"})
            seen.append("legacy")
            return web.json_response({"errorCode": 0, "id_token": "jwt-abc"})

        runner, base = await _serve({"/accounts.login": login, "/accounts.getJWT": get_jwt})
        try:
            api = DaedalusApi(gigya_base_url=base)
            await api.login_and_get_jwt(email="u", password="p")
            assert seen == ["app", "legacy"]
            await api.close()
        finally:
            await runner.cleanup()

    asyncio.run(_run())


# --------------------------------------------------------- LAN AUTH retry --


def _ws_accepting() -> MagicMock:
    import aiohttp

    ws = MagicMock()
    ws.send_str = AsyncMock()
    ws.receive = AsyncMock(
        return_value=MagicMock(type=aiohttp.WSMsgType.TEXT, data=json.dumps({"Response": "OK", "ConnectionId": 9}))
    )
    ws.close = AsyncMock()
    return ws


def test_auth_rejection_retries_lowercase_and_remembers() -> None:
    """#18 firmware: uppercase AUTH → UNAUTHORIZED, lowercase auth → OK."""
    import aiohttp

    first = MagicMock()
    first.send_str = AsyncMock()
    first.receive = AsyncMock(
        return_value=MagicMock(
            type=aiohttp.WSMsgType.TEXT,
            data=json.dumps({"Response": "UNAUTHORIZED", "Reason": "unknown message"}),
        )
    )
    first.close = AsyncMock()
    second = _ws_accepting()

    async def _run() -> None:
        session = MagicMock()
        session.ws_connect = AsyncMock(side_effect=[first, second])
        api = DaedalusApi(session=session)
        conn = await api.connect_lan(host=HOST, serial_number="SN1", jwt="jwt")
        assert conn.auth_message == "auth"
        assert conn.connection_id == 9
        first.close.assert_awaited_once()  # rejected socket must not leak
        frame = json.loads(second.send_str.await_args.args[0])
        assert frame["Message"] == "auth"
        await api.close()

    asyncio.run(_run())


def test_both_auth_spellings_rejected_escalates() -> None:
    import aiohttp

    def rejecting() -> MagicMock:
        ws = MagicMock()
        ws.send_str = AsyncMock()
        ws.receive = AsyncMock(
            return_value=MagicMock(
                type=aiohttp.WSMsgType.TEXT, data=json.dumps({"Response": "UNAUTHORIZED", "Reason": "bad token"})
            )
        )
        ws.close = AsyncMock()
        return ws

    first, second = rejecting(), rejecting()

    async def _run() -> None:
        session = MagicMock()
        session.ws_connect = AsyncMock(side_effect=[first, second])
        api = DaedalusApi(session=session)
        with pytest.raises(DaedalusAuthError, match="both"):
            await api.connect_lan(host=HOST, serial_number="SN1", jwt="jwt")
        first.close.assert_awaited_once()
        second.close.assert_awaited_once()
        await api.close()

    asyncio.run(_run())


def test_configured_lowercase_sends_lowercase_first() -> None:
    """Entry already knows the machine wants lowercase → no AUTH attempt."""
    first = _ws_accepting()

    async def _run() -> None:
        session = MagicMock()
        session.ws_connect = AsyncMock(side_effect=[first])
        api = DaedalusApi(session=session)
        conn = await api.connect_lan(host=HOST, serial_number="SN1", jwt="jwt", auth_message="auth")
        frame = json.loads(first.send_str.await_args.args[0])
        assert frame["Message"] == "auth"
        assert conn.auth_message == "auth"
        await api.close()

    asyncio.run(_run())


def test_lan_protocol_error_subclass_ordering() -> None:
    """LanAuthRejectedError must stay a LanProtocolError subclass — the
    handler ordering in _open_authed_ws depends on it."""
    from custom_components.delonghi_daedalus.lan_protocol import LanAuthRejectedError

    assert issubclass(LanAuthRejectedError, LanProtocolError)
