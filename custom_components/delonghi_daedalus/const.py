"""Constants for the De'Longhi Daedalus integration.

All values here are extracted verbatim from the public APK manifest / code
of `com.delonghigroup.daedalus` — API keys are public-by-design (Gigya /
AWS IoT custom authorizer names), equivalent to OAuth client IDs.
"""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "delonghi_daedalus"
MANUFACTURER: Final = "De'Longhi"

# --- Gigya (SAP CDC) ---------------------------------------------------------
GIGYA_BASE_URL: Final = "https://accounts.eu1.gigya.com"
GIGYA_API_KEY_PROD: Final = "4_mXSplGaqrFT0H88TAjqJuA"

# --- AWS API Gateway (REST — devices list, pairing, OTA jobs) ----------------
AWS_REST_BASE_URL_PROD: Final = "https://bm5vp76k69.execute-api.eu-central-1.amazonaws.com/dlg-prod/"

# --- AWS IoT Core (MQTT 5 over WSS:443) --------------------------------------
# Custom Lambda token authorizer; password = JWT Gigya.
AWS_IOT_BROKER_PROD: Final = "a2612mo23mfrw1-ats.iot.eu-central-1.amazonaws.com"
AWS_IOT_AUTHORIZER_PROD: Final = "dlg-prod-token-authorizer"

# --- LAN fallback WebSocket --------------------------------------------------
LAN_WS_PATH: Final = "/ws/lan2lan"
LAN_WS_PORT: Final = 443  # TLS self-signed, trust-all on the app side

# --- Coordinator ------------------------------------------------------------
CONF_EMAIL: Final = "email"
CONF_PASSWORD: Final = "password"  # noqa: S105 — config_entry key, not a secret
CONF_HOST: Final = "host"
CONF_SERIAL_NUMBER: Final = "serial_number"
CONF_MACHINE_NAME: Final = "machine_name"
CONF_JWT: Final = "jwt"  # noqa: S105
CONF_SESSION_TOKEN: Final = "session_token"  # noqa: S105
CONF_SESSION_SECRET: Final = "session_secret"  # noqa: S105

DEFAULT_UPDATE_INTERVAL_SECONDS: Final = 30

# JWT refresh when remaining TTL drops below this (80% of 90d ≈ 72d).
JWT_REFRESH_THRESHOLD_SECONDS: Final = 18 * 24 * 60 * 60
