"""Pure helpers for Gigya (SAP CDC) login + JWT flow used by My Coffee Lounge.

These functions are deliberately I/O free so they can be unit-tested without
network. The async HTTP calls live in `api.py`.

Identity flow (extracted from the Daedalus APK):
    POST /accounts.login     -> {sessionInfo: {sessionToken, sessionSecret}}
    POST /accounts.getJWT    -> {id_token: "<JWT, 90-day TTL>"}

The JWT is then passed verbatim as MQTT password (AWS IoT Core custom Lambda
authorizer) and inside the `{"Message":"AUTH","SerialNo":..,"AuthToken":..}`
frame on the LAN WebSocket (wss://<ip>/ws/lan2lan).
"""

from __future__ import annotations

from typing import Any

GIGYA_BASE_URL = "https://accounts.eu1.gigya.com"
GIGYA_API_KEY_PROD = "4_mXSplGaqrFT0H88TAjqJuA"
GIGYA_JWT_TTL_SECONDS = 90 * 24 * 60 * 60  # 7_776_000s, Daedalus default


class GigyaAuthError(RuntimeError):
    """Raised when Gigya returns a non-zero errorCode or malformed payload."""


def build_login_params(
    *,
    loginID: str,
    password: str,
    api_key: str = GIGYA_API_KEY_PROD,
) -> dict[str, str]:
    """Construct POST body for `accounts.login`.

    `sessionExpiration=-1` requests a long-lived session. Daedalus relies on
    getJWT() to rotate the short-lived auth token, so we want the underlying
    session to stay usable for months.
    """
    return {
        "loginID": loginID,
        "password": password,
        "apiKey": api_key,
        "targetEnv": "mobile",
        "sessionExpiration": "-1",
    }


def parse_login_response(payload: dict[str, Any]) -> tuple[str, str]:
    """Extract (sessionToken, sessionSecret) from a login response.

    Raises GigyaAuthError on non-zero errorCode or missing session block.
    """
    _check_gigya_error(payload)
    session = payload.get("sessionInfo")
    if not isinstance(session, dict):
        raise GigyaAuthError("Gigya login succeeded but sessionInfo is missing")
    token = session.get("sessionToken")
    secret = session.get("sessionSecret")
    if not token or not secret:
        raise GigyaAuthError("Gigya login succeeded but session credentials are empty")
    return token, secret


def build_jwt_request_params(
    *,
    session_token: str,
    api_key: str = GIGYA_API_KEY_PROD,
    ttl_seconds: int = GIGYA_JWT_TTL_SECONDS,
) -> dict[str, str]:
    """Construct POST body for `accounts.getJWT`."""
    return {
        "apiKey": api_key,
        "oauth_token": session_token,
        "expiration": str(ttl_seconds),
    }


def parse_jwt_response(payload: dict[str, Any]) -> str:
    """Extract the JWT string from a getJWT response."""
    _check_gigya_error(payload)
    token = payload.get("id_token")
    if not isinstance(token, str) or not token:
        raise GigyaAuthError("Gigya getJWT response contained no id_token")
    return token


def _check_gigya_error(payload: dict[str, Any]) -> None:
    error_code = payload.get("errorCode", 0)
    if error_code:
        message = payload.get("errorMessage") or payload.get("errorDetails") or "unknown"
        raise GigyaAuthError(f"Gigya error {error_code}: {message}")
