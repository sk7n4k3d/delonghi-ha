"""LAN WebSocket resource hygiene (audit 2026-11-05, INFO→fixed).

`ws.receive(timeout=…)` *raises* TimeoutError — it is neither an
aiohttp.ClientError nor a LanProtocolError, so it used to escape every
handler of connect_lan: the socket was never closed and a machine that
accepts TCP but never answers the AUTH frame leaked one open WS per 30 s
coordinator cycle. Same trap in DaedalusLanConnection.receive().
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from custom_components.delonghi_daedalus.api import (
    DaedalusApi,
    DaedalusConnectionError,
    DaedalusLanConnection,
)
from custom_components.delonghi_daedalus.lan_protocol import LanProtocolError

# Unique ULA: cannot collide with hosts registered by other test files.
HOST6 = "fd12:b0af::7"


def _lan_api_with_ws(ws: MagicMock) -> DaedalusApi:
    session = MagicMock()
    session.ws_connect = AsyncMock(return_value=ws)
    return DaedalusApi(session=session)


def _auth_timeout_ws(side_effect: BaseException) -> MagicMock:
    ws = MagicMock()
    ws.send_str = AsyncMock()
    ws.receive = AsyncMock(side_effect=side_effect)
    ws.close = AsyncMock()
    return ws


@pytest.mark.parametrize("exc", [TimeoutError(), OSError(104, "Connection reset by peer")])
def test_connect_lan_closes_ws_and_raises_connection_error(exc: BaseException) -> None:
    async def _run() -> None:
        ws = _auth_timeout_ws(exc)
        api = _lan_api_with_ws(ws)
        with pytest.raises(DaedalusConnectionError) as caught:
            await api.connect_lan(host=HOST6, serial_number="SN1", jwt="jwt")
        ws.close.assert_awaited_once()
        assert caught.value.__cause__ is exc

    asyncio.run(_run())


def test_malformed_reply_text_is_host_scrubbed_at_birth() -> None:
    """Machine-controlled frame text (the LanProtocolError message) is scrubbed
    where the Daedalus*Error is built — the coordinator's sanitizer is only
    the second gate, and the raw traceback path has none."""

    async def _run() -> None:
        ws = MagicMock()
        ws.send_str = AsyncMock()
        ws.close = AsyncMock()
        garbage = MagicMock()
        garbage.type = aiohttp.WSMsgType.TEXT
        garbage.data = "not json"
        ws.receive = AsyncMock(return_value=garbage)
        api = _lan_api_with_ws(ws)
        with (
            patch(
                "custom_components.delonghi_daedalus.api.parse_message",
                side_effect=LanProtocolError(f"LAN frame is not valid JSON: {HOST6} said nope"),
            ),
            pytest.raises(DaedalusConnectionError) as caught,
        ):
            await api.connect_lan(host=HOST6, serial_number="SN1", jwt="jwt")
        assert HOST6 not in str(caught.value)
        assert "LAN AUTH got a malformed reply" in str(caught.value)

    asyncio.run(_run())


def test_ws_connect_failure_message_scrubs_host() -> None:
    async def _run() -> None:
        session = MagicMock()
        connect_exc = aiohttp.ClientConnectorError(MagicMock(), OSError(113, "No route to host"))
        session.ws_connect = AsyncMock(side_effect=connect_exc)
        api = DaedalusApi(session=session)
        with pytest.raises(DaedalusConnectionError) as caught:
            await api.connect_lan(host=HOST6, serial_number="SN1", jwt="jwt")
        text = str(caught.value)
        # Scrubbed from {url} and from the copied exception text; the WS URL
        # keeps its wss://**REDACTED** shape for triage.
        assert HOST6 not in text
        assert "LAN WS connect to wss://**REDACTED**" in text

    asyncio.run(_run())


def test_lan_connection_receive_timeout_raises_connection_error() -> None:
    async def _run() -> None:
        ws = MagicMock()
        ws.receive = AsyncMock(side_effect=TimeoutError())
        conn = DaedalusLanConnection(ws=ws, connection_id=7)
        with pytest.raises(DaedalusConnectionError, match="LAN receive failed"):
            await conn.receive(timeout=1)
        # handle lifecycle belongs to the coordinator (closed-check): a slow
        # machine must not silently kill the authenticated WS.
        ws.close.assert_not_called()

    asyncio.run(_run())


def test_register_lan_host_happens_on_connect_attempt() -> None:
    from custom_components.delonghi_daedalus import redact

    async def _run() -> None:
        ws = _auth_timeout_ws(TimeoutError())
        api = _lan_api_with_ws(ws)
        with pytest.raises(DaedalusConnectionError):
            await api.connect_lan(host=HOST6, serial_number="SN1", jwt="jwt")
        assert HOST6 in redact._KNOWN_HOST_FORMS

    asyncio.run(_run())
