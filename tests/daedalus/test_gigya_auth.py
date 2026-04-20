"""Pure-function tests for the Gigya auth layer (Daedalus stack)."""

from __future__ import annotations

import pytest

from custom_components.delonghi_daedalus.gigya_auth import (
    GIGYA_JWT_TTL_SECONDS,
    GigyaAuthError,
    build_jwt_request_params,
    build_login_params,
    parse_jwt_response,
    parse_login_response,
)


def test_build_login_params_includes_required_fields() -> None:
    params = build_login_params(
        loginID="user@example.com",
        password="hunter2",
        api_key="4_mXSplGaqrFT0H88TAjqJuA",
    )
    assert params["loginID"] == "user@example.com"
    assert params["password"] == "hunter2"
    assert params["apiKey"] == "4_mXSplGaqrFT0H88TAjqJuA"
    assert params["targetEnv"] == "mobile"
    # Cookie/session handling requires session expiration — set to long-lived
    # since Daedalus refreshes via getJWT, not via re-login.
    assert params["sessionExpiration"] == "-1"


def test_parse_login_response_extracts_session_credentials() -> None:
    payload = {
        "errorCode": 0,
        "sessionInfo": {
            "sessionToken": "st-abc",
            "sessionSecret": "ss-xyz",
        },
        "UID": "uid-1",
    }
    session_token, session_secret = parse_login_response(payload)
    assert session_token == "st-abc"
    assert session_secret == "ss-xyz"


def test_parse_login_response_raises_on_error_code() -> None:
    payload = {
        "errorCode": 403042,
        "errorMessage": "Invalid LoginID or password",
    }
    with pytest.raises(GigyaAuthError) as exc:
        parse_login_response(payload)
    assert "403042" in str(exc.value)
    assert "Invalid LoginID or password" in str(exc.value)


def test_parse_login_response_raises_when_session_missing() -> None:
    # Success code but no session block — treat as malformed, not silent.
    payload = {"errorCode": 0}
    with pytest.raises(GigyaAuthError):
        parse_login_response(payload)


def test_build_jwt_request_params_uses_90_day_ttl() -> None:
    params = build_jwt_request_params(
        session_token="st-abc",
        api_key="4_mXSplGaqrFT0H88TAjqJuA",
    )
    assert params["apiKey"] == "4_mXSplGaqrFT0H88TAjqJuA"
    assert params["oauth_token"] == "st-abc"
    assert int(params["expiration"]) == GIGYA_JWT_TTL_SECONDS
    assert GIGYA_JWT_TTL_SECONDS == 90 * 24 * 60 * 60


def test_parse_jwt_response_returns_token_string() -> None:
    payload = {"errorCode": 0, "id_token": "eyJhbGciOi…"}
    assert parse_jwt_response(payload) == "eyJhbGciOi…"


def test_parse_jwt_response_raises_on_error() -> None:
    payload = {"errorCode": 400006, "errorMessage": "Invalid parameter value"}
    with pytest.raises(GigyaAuthError):
        parse_jwt_response(payload)


def test_parse_jwt_response_raises_when_token_missing() -> None:
    payload = {"errorCode": 0}
    with pytest.raises(GigyaAuthError):
        parse_jwt_response(payload)
