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
