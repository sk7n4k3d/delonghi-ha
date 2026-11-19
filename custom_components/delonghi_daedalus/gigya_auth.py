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

import base64
import hashlib
import json
from typing import Any

GIGYA_BASE_URL = "https://accounts.eu1.gigya.com"
GIGYA_API_KEY_PROD = "4_mXSplGaqrFT0H88TAjqJuA"
GIGYA_JWT_TTL_SECONDS = 90 * 24 * 60 * 60  # 7_776_000s, Daedalus default

# My Coffee Lounge 1.5.0 sends the login exactly like this (issue #18,
# mimasek71 capture): apiKey travels as a QUERY parameter, the body carries
# `include` and a 90-day sessionExpiration. The previous hand-built variant
# (apiKey in the body, sessionExpiration=-1, no include) is rejected by the
# Gigya screen-set some EU accounts sit behind — it is kept only as an
# automatic fallback for accounts that predate the app flow.
MCL_APP_SESSION_EXPIRATION_SECONDS = 7_776_000  # 90 days, verbatim from the app
MCL_APP_LOGIN_INCLUDE = "id_token,profile,data,preferences"

# Definite account and credential refusals from the SAP CDC response catalog.
# In particular, retrying 403042 (invalid login ID or password) would submit
# the same credentials twice.
_CREDENTIAL_ERROR_CODES = frozenset({403001, 403002, 403005, 403023, 403025, 403026, 403042})
_SESSION_ERROR_CODES = frozenset({403012, 403035})


class GigyaAuthError(RuntimeError):
    """Raised when Gigya returns a non-zero errorCode or malformed payload."""

    def __init__(self, message: str, *, error_code: int = 0) -> None:
        super().__init__(message)
        self.error_code = error_code


def jwt_expiry(token: str) -> int | None:
    """Read the expiry hint for refresh scheduling; services verify the token."""
    parts = token.split(".")
    if len(parts) != 3 or len(parts[1]) > 16_384:
        return None
    payload = parts[1]
    try:
        claims = json.loads(base64.b64decode(payload + "=" * (-len(payload) % 4), altchars=b"-_", validate=True))
    except (ValueError, RecursionError):
        return None
    expiry = claims.get("exp") if isinstance(claims, dict) else None
    return expiry if type(expiry) is int else None


def build_login_params(
    *,
    loginID: str,
    password: str,
    api_key: str = GIGYA_API_KEY_PROD,
) -> dict[str, str]:
    """Construct POST body for `accounts.login` (app-aligned format).

    `apiKey` is NOT part of the body in this format — see `build_login_query`.
    `sessionExpiration=7776000` (90 days) matches My Coffee Lounge 1.5.0;
    the old `-1` variant lives on in `build_login_params_legacy` as the
    automatic fallback.
    """
    return {
        "loginID": loginID,
        "password": password,
        "targetEnv": "mobile",
        "sessionExpiration": str(MCL_APP_SESSION_EXPIRATION_SECONDS),
        "include": MCL_APP_LOGIN_INCLUDE,
    }


def build_login_query(*, api_key: str = GIGYA_API_KEY_PROD) -> dict[str, str]:
    """Query-string parameters for the app-aligned `accounts.login`."""
    return {"apiKey": api_key}


def build_login_params_legacy(
    *,
    loginID: str,
    password: str,
    api_key: str = GIGYA_API_KEY_PROD,
) -> dict[str, str]:
    """Construct POST body for the pre-#18 format (apiKey in the body).

    Kept as the fallback path: accounts created before the current MCL
    screen-set still accept it, and some pools reject the query-string form.
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
    check_gigya_error(payload)
    session = payload.get("sessionInfo")
    if not isinstance(session, dict):
        raise GigyaAuthError("Gigya login succeeded but sessionInfo is missing")
    token = session.get("sessionToken")
    secret = session.get("sessionSecret")
    if not isinstance(token, str) or not token or not isinstance(secret, str) or not secret:
        raise GigyaAuthError("Gigya login succeeded but session credentials are empty")
    return token, secret


def build_jwt_request_params(
    *,
    session_token: str,
    api_key: str = GIGYA_API_KEY_PROD,
    ttl_seconds: int = GIGYA_JWT_TTL_SECONDS,
) -> dict[str, str]:
    """Construct POST body for the legacy `accounts.getJWT` (oauth_token body).

    Used when no sessionSecret was captured; the app-aligned variant is
    `build_jwt_app_request`.
    """
    return {
        "apiKey": api_key,
        "oauth_token": session_token,
        "expiration": str(ttl_seconds),
    }


def build_jwt_app_request(
    *,
    session_token: str,
    session_secret: str,
    api_key: str = GIGYA_API_KEY_PROD,
    ttl_seconds: int = GIGYA_JWT_TTL_SECONDS,
) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """Build the app-aligned `accounts.getJWT` call (MCL 1.5.0, mimasek71).

    Returns `(query, body, headers)`:
      - query:   apiKey + the sessionSecret as `secret`
      - body:    expiration only
      - headers: `Authorization: Bearer <sessionToken>`

    The sessionSecret is a bearer credential: never log it, never echo it in
    an exception message (the api layer scrubs defensively regardless).
    """
    return (
        {"apiKey": api_key, "secret": session_secret},
        {"expiration": str(ttl_seconds)},
        {"Authorization": f"Bearer {session_token}"},
    )


def parse_jwt_response(payload: dict[str, Any]) -> str:
    """Extract the JWT string from a getJWT response."""
    check_gigya_error(payload)
    token = payload.get("id_token")
    if not isinstance(token, str) or not token:
        raise GigyaAuthError("Gigya getJWT response contained no id_token")
    return token


def check_gigya_error(payload: dict[str, Any]) -> None:
    """Raise for a Gigya application error regardless of HTTP status."""
    error_code = payload.get("errorCode", 0)
    if error_code:
        # Audit #18 (A4): a malformed errorCode ("403001.5", "INVALID") must
        # degrade to an unknown code (fallback-eligible), never a raw
        # ValueError traceback out of the config flow. A truthy "0" string
        # means success and must NOT raise.
        try:
            code = int(error_code)
        except (TypeError, ValueError, OverflowError):
            code = -1
        if not code:
            return
        # errorMessage is server-controlled free text (can echo the password);
        # sanitize_text scrubs known shapes, the hard cap bounds the blast
        # radius of an unknown one (audit #18, A1).
        message = str(payload.get("errorMessage") or payload.get("errorDetails") or "unknown")[:120]
        raise GigyaAuthError(f"Gigya error {code}: {message}", error_code=code)


def is_credential_error(exc: GigyaAuthError) -> bool:
    """True when the Gigya errorCode proves the credentials were refused.

    Only these codes justify short-circuiting (no legacy-format retry, no
    next-pool probe): replaying them would hammer the same wrong password.
    Audit #18 (A5): normalize defensively — a str/float code must never
    silently downgrade a proven refusal into a replayable "unknown".
    """
    try:
        code = int(exc.error_code)
    except (TypeError, ValueError, OverflowError):
        return False
    return code in _CREDENTIAL_ERROR_CODES


def is_session_error(exc: GigyaAuthError) -> bool:
    """True when Gigya rejects the credentials sent to accounts.getJWT."""
    return is_credential_error(exc) or exc.error_code in _SESSION_ERROR_CODES


def is_transient_error(exc: GigyaAuthError) -> bool:
    """Identify rate limits and service errors encoded in Gigya's payload."""
    code = exc.error_code
    return code in {408, 429, 6005} or str(code).startswith("5")


def apikey_fingerprint(api_key: str) -> str:
    """Return a non-secret fingerprint identifying an apiKey.

    Format: ``len:<N> sha1[:8]=<hex>``. Safe to log because:

    - apiKey is a public OAuth-client-id-equivalent (extracted verbatim from
      the My Coffee Lounge APK manifest, ships in every install of the app).
    - The fingerprint adds nothing beyond what a hash of the public string
      would, but makes truncation / typos / wrong-pool selection visible
      from a remote bug report without asking the user to paste the key.

    Used for the H-daedalus-4 diagnostics surface — when Gigya rejects an
    apiKey with errorCode 400093, the fingerprint disambiguates between
    "the source const is wrong" (length differs from known pools) and
    "the user's install has a corrupted copy".
    """
    digest = hashlib.sha1(api_key.encode("ascii"), usedforsecurity=False).hexdigest()[:8]
    return f"len:{len(api_key)} sha1[:8]={digest}"
