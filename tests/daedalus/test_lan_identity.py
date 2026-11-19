"""Exercise certificate pinning and redirect rejection with a local TLS peer."""

from __future__ import annotations

import asyncio
import ipaddress
import ssl
from datetime import UTC, datetime, timedelta

import pytest
from aiohttp import web
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from yarl import URL

from custom_components.delonghi_daedalus import api


@pytest.fixture
def certificate(tmp_path):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test Daedalus")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False)
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    return context, cert.fingerprint(hashes.SHA256()).hex()


async def _serve(handler, context):
    app = web.Application()
    app.router.add_get("/ws/lan2lan", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=context)
    await site.start()
    return runner, runner.addresses[0][1]


def test_preflight_is_credential_free_and_connect_checks_pin_before_http(certificate, monkeypatch):
    context, expected_pin = certificate

    async def run():
        requests = []

        async def handler(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            requests.append(await ws.receive_json())
            await ws.send_json({"Response": "OK", "ConnectionId": 7, "SerialNo": "SN1"})
            await ws.receive()
            return ws

        runner, port = await _serve(handler, context)
        monkeypatch.setattr(api, "validate_lan_host", lambda host: host)
        monkeypatch.setattr(api, "LAN_WS_PORT", port)
        monkeypatch.setattr(api, "build_lan_ws_url", lambda host: f"wss://{host}:{port}/ws/lan2lan")
        client = api.DaedalusApi()
        try:
            assert await client.probe_lan_fingerprint(host="127.0.0.1") == expected_pin
            assert not requests
            kwargs = {"host": "127.0.0.1", "serial_number": "SN1", "jwt": "test-jwt"}
            with pytest.raises(api.DaedalusLanIdentityError, match="Reconfigure"):
                await client.connect_lan(**kwargs)
            with pytest.raises(api.DaedalusLanIdentityError, match="certificate changed"):
                await client.connect_lan(**kwargs, fingerprint="00" * 32)
            assert not requests
            connection = await client.connect_lan(**kwargs, fingerprint=expected_pin)
            await connection.close()
            assert requests == [{"Message": "AUTH", "SerialNo": "SN1", "AuthToken": "test-jwt"}]
        finally:
            await client.close()
            await runner.cleanup()

    asyncio.run(run())


@pytest.mark.parametrize("location", ["https://203.0.113.1/ws/lan2lan", "https://169.254.169.254/ws/lan2lan"])
def test_verified_endpoint_cannot_redirect_lan_connection(certificate, monkeypatch, location):
    context, expected_pin = certificate

    async def run():
        # Supply an HTTPS redirect from a peer with the approved cert. The
        # trace must reject it immediately, before another outbound attempt.
        seen = []

        async def handler(request):
            seen.append(request.url)
            raise web.HTTPFound(location=location)

        runner, port = await _serve(handler, context)
        monkeypatch.setattr(api, "validate_lan_host", lambda host: host)
        monkeypatch.setattr(
            api, "build_lan_ws_url", lambda host: str(URL.build(scheme="wss", host=host, port=port, path="/ws/lan2lan"))
        )
        client = api.DaedalusApi()
        try:
            with pytest.raises(api.DaedalusConnectionError, match="Unexpected redirect"):
                await client.connect_lan(
                    host="127.0.0.1", serial_number="SN1", jwt="test-jwt", fingerprint=expected_pin
                )
            assert len(seen) == 1
            assert seen[0].host == "127.0.0.1"
        finally:
            await client.close()
            await runner.cleanup()

    asyncio.run(run())
