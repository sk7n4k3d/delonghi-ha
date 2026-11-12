"""Pure-function tests for the Gigya auth layer (Daedalus stack)."""

from __future__ import annotations

import pytest

from custom_components.delonghi_daedalus.gigya_auth import (
    GIGYA_JWT_TTL_SECONDS,
    GigyaAuthError,
    _check_gigya_error,
    build_jwt_app_request,
    build_jwt_request_params,
    build_login_params,
    build_login_params_legacy,
    build_login_query,
    is_credential_error,
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
    # App-aligned format (#18): apiKey travels in the QUERY, not the body.
    assert "apiKey" not in params
    assert params["targetEnv"] == "mobile"
    # 90-day session, verbatim from My Coffee Lounge 1.5.0 — the app never
    # asks for a session that outlives the JWT it will rotate.
    assert params["sessionExpiration"] == "7776000"
    assert params["include"] == "id_token,profile,data,preferences"


def test_build_login_query_carries_api_key() -> None:
    assert build_login_query(api_key="k1") == {"apiKey": "k1"}


def test_build_jwt_app_request_shape() -> None:
    query, body, headers = build_jwt_app_request(session_token="st", session_secret="ss", api_key="k1", ttl_seconds=100)
    assert query == {"apiKey": "k1", "secret": "ss"}
    assert body == {"expiration": "100"}
    assert headers == {"Authorization": "Bearer st"}


def test_is_credential_error_only_on_refusal_codes() -> None:
    assert is_credential_error(GigyaAuthError("x", error_code=403001))
    assert is_credential_error(GigyaAuthError("x", error_code=403005))
    assert not is_credential_error(GigyaAuthError("x", error_code=403013))
    assert not is_credential_error(GigyaAuthError("x"))


def test_build_login_params_legacy_keeps_old_shape() -> None:
    params = build_login_params_legacy(
        loginID="u",
        password="p",
        api_key="k1",  # noqa: S106 — test fixture
    )
    assert params["apiKey"] == "k1"
    assert params["sessionExpiration"] == "-1"
    assert "include" not in params


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


# -- H-daedalus-4 (audit 2026-05-08) -----------------------------------------


def test_apikey_fingerprint_format() -> None:
    """``apikey_fingerprint`` returns ``len:N sha1[:8]=<hex>`` — a non-secret
    handle that survives log scrubbing and lets a remote bug reporter
    confirm whether their installed apiKey matches the source-of-truth.
    """
    from custom_components.delonghi_daedalus.gigya_auth import apikey_fingerprint

    fp = apikey_fingerprint("4_mXSplGaqrFT0H88TAjqJuA")
    assert fp.startswith("len:24 ")
    assert "sha1[:8]=" in fp
    # 8 hex chars after the prefix
    suffix = fp.split("sha1[:8]=", 1)[1]
    assert len(suffix) == 8
    assert all(c in "0123456789abcdef" for c in suffix)


def test_apikey_fingerprint_changes_on_truncation() -> None:
    """A truncated key gets a different fingerprint — proves the helper
    can flag the live H-daedalus-4 hypothesis (HACS mirror corruption)."""
    from custom_components.delonghi_daedalus.gigya_auth import apikey_fingerprint

    full = apikey_fingerprint("4_mXSplGaqrFT0H88TAjqJuA")
    truncated = apikey_fingerprint("4_mXSplGaqrFT0H88")
    assert full != truncated
    assert "len:24" in full
    assert "len:17" in truncated


def test_malformed_errorcode_degrades_without_traceback() -> None:
    """Audit #18 A4: non-numeric errorCode must not raise ValueError out of
    the parser; it becomes an unknown code (fallback-eligible)."""
    with pytest.raises(GigyaAuthError) as caught:
        _check_gigya_error({"errorCode": "INVALID", "errorMessage": "weird proxy"})
    assert caught.value.error_code == -1


def test_string_zero_errorcode_is_success() -> None:
    _check_gigya_error({"errorCode": "0"})  # must not raise


def test_is_credential_error_normalizes_types() -> None:
    """Audit #18 A5: a str/float refusal code must still short-circuit."""
    assert is_credential_error(GigyaAuthError("x", error_code="403001"))
    assert is_credential_error(GigyaAuthError("x", error_code=403005.0))
    assert not is_credential_error(GigyaAuthError("x", error_code="nope"))
