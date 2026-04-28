"""Pure-function tests for the LAN WebSocket protocol (Daedalus stack).

Handshake reverse-engineered from `com.delonghigroup.appliance_kit` → app
connects to `wss://<ip>/ws/lan2lan` (TLS self-signed, trust-all) and sends:

    {"Message":"AUTH","SerialNo":"<SN>","AuthToken":"<JWT Gigya>"}

Device answers:

    {"Response":"OK","ConnectionId":42}

Then command-response pairs are matched through a caller-generated
`RequestId`:

    app    -> {"Message":"BREW","ConnectionId":42,"RequestId":"abc",...}
    device -> {"RequestId":"abc","Response":{...}}
"""

from __future__ import annotations

import json

import pytest

from custom_components.delonghi_daedalus.lan_protocol import (
    LanProtocolError,
    build_auth_frame,
    build_command_frame,
    build_lan_ws_url,
    generate_request_id,
    parse_auth_response,
    parse_message,
    validate_lan_host,
)


def test_build_lan_ws_url() -> None:
    assert build_lan_ws_url("192.168.1.42") == "wss://192.168.1.42/ws/lan2lan"
    # IPv6 host gets bracketed.
    assert build_lan_ws_url("fe80::1") == "wss://[fe80::1]/ws/lan2lan"


def test_build_auth_frame_shape() -> None:
    frame = build_auth_frame(serial_number="SN1234", jwt="eyJ...")
    decoded = json.loads(frame)
    assert decoded == {
        "Message": "AUTH",
        "SerialNo": "SN1234",
        "AuthToken": "eyJ...",
    }


def test_parse_auth_response_returns_connection_id() -> None:
    decoded = parse_auth_response({"Response": "OK", "ConnectionId": 42})
    assert decoded == 42


def test_parse_auth_response_rejects_non_ok() -> None:
    with pytest.raises(LanProtocolError):
        parse_auth_response({"Response": "ERROR", "Reason": "bad token"})


def test_parse_auth_response_rejects_missing_connection_id() -> None:
    with pytest.raises(LanProtocolError):
        parse_auth_response({"Response": "OK"})


def test_build_command_frame_includes_request_id_and_connection_id() -> None:
    frame = build_command_frame(
        message="BREW",
        connection_id=42,
        request_id="req-abc",
        params={"Recipe": "espresso"},
    )
    decoded = json.loads(frame)
    assert decoded["Message"] == "BREW"
    assert decoded["ConnectionId"] == 42
    assert decoded["RequestId"] == "req-abc"
    assert decoded["Recipe"] == "espresso"


def test_build_command_frame_rejects_reserved_keys_in_params() -> None:
    # Caller must not override Message/ConnectionId/RequestId via params.
    with pytest.raises(LanProtocolError):
        build_command_frame(
            message="BREW",
            connection_id=1,
            request_id="r",
            params={"Message": "tamper"},
        )


def test_generate_request_id_is_unique_and_urlsafe() -> None:
    seen = {generate_request_id() for _ in range(500)}
    assert len(seen) == 500
    for req in seen:
        assert req.isascii()
        assert "/" not in req and "+" not in req
        assert len(req) >= 16


def test_parse_message_accepts_json_string_or_bytes() -> None:
    payload = '{"Message":"HELLO","Value":1}'
    assert parse_message(payload) == {"Message": "HELLO", "Value": 1}
    assert parse_message(payload.encode("utf-8")) == {"Message": "HELLO", "Value": 1}


def test_parse_message_raises_on_garbage() -> None:
    with pytest.raises(LanProtocolError):
        parse_message("not-json")
    with pytest.raises(LanProtocolError):
        parse_message('["arrays","are","not","valid","top-level"]')


# ---------------------------------------------------------------------------
# validate_lan_host — gates the trust-all TLS context to LAN-only targets.
# Preserves the security invariant that the JWT in the AUTH frame can never be
# exposed to an off-path attacker because of an accidental public host.
# ---------------------------------------------------------------------------


class TestValidateLanHost:
    def test_accepts_rfc1918_v4(self) -> None:
        for host in ("192.168.1.42", "10.0.0.5", "172.16.0.1"):
            assert validate_lan_host(host) == host

    def test_accepts_loopback(self) -> None:
        assert validate_lan_host("127.0.0.1") == "127.0.0.1"
        assert validate_lan_host("::1") == "::1"

    def test_accepts_link_local(self) -> None:
        assert validate_lan_host("169.254.10.20") == "169.254.10.20"
        assert validate_lan_host("fe80::1") == "fe80::1"

    def test_accepts_unique_local_v6(self) -> None:
        # ULA fc00::/7 — fd00::/8 is the slice in practical use.
        assert validate_lan_host("fd12:3456:789a::1") == "fd12:3456:789a::1"

    def test_rejects_public_v4(self) -> None:
        with pytest.raises(LanProtocolError, match="not in a private"):
            validate_lan_host("8.8.8.8")
        with pytest.raises(LanProtocolError, match="not in a private"):
            validate_lan_host("1.1.1.1")

    def test_rejects_public_v6(self) -> None:
        with pytest.raises(LanProtocolError, match="not in a private"):
            validate_lan_host("2606:4700:4700::1111")

    def test_rejects_hostname(self) -> None:
        # No DNS lookup — hostnames can resolve to anything, and self-signed
        # TLS leaves no room to detect a hostile resolver swap.
        with pytest.raises(LanProtocolError, match="numeric IP address"):
            validate_lan_host("delonghi.example.com")
        with pytest.raises(LanProtocolError, match="numeric IP address"):
            validate_lan_host("coffee.local")
        with pytest.raises(LanProtocolError, match="numeric IP address"):
            validate_lan_host("localhost")

    def test_rejects_garbage(self) -> None:
        with pytest.raises(LanProtocolError):
            validate_lan_host("not-an-ip")
        with pytest.raises(LanProtocolError):
            validate_lan_host("")
