"""De'Longhi Coffee API — Gigya + Ayla Networks cloud integration."""

from __future__ import annotations

import base64
import contextlib
import functools
import json
import logging
import re
import struct
import threading
import time
from typing import Any

import requests

from .const import (
    APP_SIGNATURE,
    BEAN_NAME_MAX_BYTES,
    GIGYA_API_KEY,
    GIGYA_URL,
    MODEL_NAMES,
    OEM_TO_APP_MODEL,
    OPCODE_READ_BEAN_SYSTEM,
    OPCODE_SELECT_BEAN_SYSTEM,
    OPCODE_WRITE_BEAN_SYSTEM,
    REGIONS,
    REQUEST_TIMEOUT,
    RETRY_COUNT,
    RETRY_DELAY,
    TRANSCODE_TABLE_URL,
)
from .logger import ApiTimer, RateLimitTracker, sanitize

_LOGGER = logging.getLogger(__name__)


def _decode_utf16(data: bytes) -> str:
    """Decode UTF-16 text, auto-detecting endianness from null byte positions.

    De'Longhi stores names in UTF-16 but the endianness varies between
    properties (profile names vs custom recipe names). This detects
    whether null bytes sit at even positions (BE) or odd positions (LE).
    """
    if len(data) < 2:
        return ""
    # Sample the first 20 bytes at most. Trim to an even length so the
    # even/odd split is symmetric and safe even when ``data`` has an odd
    # size (would otherwise let the BE counter peek past the LE counter).
    sample_len = min(len(data), 20)
    sample_len -= sample_len % 2
    nulls_even = sum(1 for i in range(0, sample_len, 2) if data[i] == 0)
    nulls_odd = sum(1 for i in range(1, sample_len, 2) if data[i] == 0)
    encoding = "utf-16-be" if nulls_even >= nulls_odd else "utf-16-le"
    return data.decode(encoding, errors="replace").replace("\x00", "").strip()


class DeLonghiAuthError(Exception):
    """Authentication error."""


class DeLonghiApiError(Exception):
    """API communication error."""


def _retry(func):  # noqa: ANN001, ANN202
    """Retry decorator with exponential backoff (3 attempts, 2/4/8s delays).

    - 404: no retry (property doesn't exist)
    - 401: re-authenticate then retry
    - 429: longer backoff (rate limited by Ayla)
    - Other errors: exponential backoff
    """

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        last_err: Exception | None = None
        for attempt in range(1, RETRY_COUNT + 1):
            try:
                return func(*args, **kwargs)
            except DeLonghiAuthError:
                raise
            except (requests.RequestException, DeLonghiApiError) as err:
                # Don't retry 404s — property doesn't exist, retrying won't help
                if isinstance(err, requests.HTTPError) and err.response is not None and err.response.status_code == 404:
                    raise
                # Re-authenticate on 401 (token revoked server-side).
                # Guard against recursion: if we are already re-authenticating
                # and still hit 401, propagate instead of re-entering the
                # auth path (which would just fail the same way).
                if isinstance(err, requests.HTTPError) and err.response is not None and err.response.status_code == 401:
                    self_obj = args[0]
                    if getattr(self_obj, "_reauthenticating", False):
                        _LOGGER.error("Got 401 during re-authentication, aborting retry loop")
                        raise DeLonghiAuthError("re-authentication itself returned 401") from err
                    _LOGGER.warning("Got 401, re-authenticating")
                    self_obj._reauthenticating = True
                    try:
                        with contextlib.suppress(DeLonghiAuthError, DeLonghiApiError):
                            self_obj.authenticate()
                    finally:
                        self_obj._reauthenticating = False
                # Rate limited — longer backoff
                if isinstance(err, requests.HTTPError) and err.response is not None and err.response.status_code == 429:
                    delay = RETRY_DELAY * (2**attempt)  # 4, 8, 16s for 429
                    _LOGGER.warning(
                        "Rate limited (429) on %s — backing off %ds (attempt %d/%d)",
                        func.__name__,
                        delay,
                        attempt,
                        RETRY_COUNT,
                    )
                    time.sleep(delay)
                    last_err = err
                    continue
                last_err = err
                if attempt < RETRY_COUNT:
                    delay = RETRY_DELAY * (2 ** (attempt - 1))  # 2, 4s exponential
                    _LOGGER.debug(
                        "Attempt %d/%d failed for %s: %s — retrying in %ds",
                        attempt,
                        RETRY_COUNT,
                        func.__name__,
                        err,
                        delay,
                    )
                    time.sleep(delay)
        raise DeLonghiApiError(f"{func.__name__} failed after {RETRY_COUNT} attempts: {last_err}") from last_err

    return wrapper


class DeLonghiApi:
    """API client for De'Longhi coffee machines via Ayla Networks."""

    def __init__(self, email: str, password: str, region: str = "EU", oem_model: str = "") -> None:
        self._email = email
        self._password = password
        self._session = requests.Session()
        self._ayla_token: str | None = None
        self._ayla_refresh: str | None = None
        self._token_expires: float = 0
        # Serialize concurrent token refresh from executor threads (coordinator
        # poll + button press + select change can all race past the expiry
        # check and trigger duplicate refresh_token POSTs).
        self._token_lock = threading.Lock()
        self._devices: list[dict[str, Any]] = []
        self._device_name: str | None = None
        self._sw_version: str | None = None
        self._oem_model: str = oem_model
        self._custom_recipe_names: dict[int, str] = {}

        # Gigya always uses EU1 (confirmed from app manifest)
        self._gigya_url: str = GIGYA_URL

        # Region-specific Ayla endpoints AND credentials
        region_cfg = REGIONS.get(region, REGIONS["EU"])
        self._ayla_app_id: str = region_cfg["ayla_app_id"]
        self._ayla_app_secret: str = region_cfg["ayla_app_secret"]
        self._ayla_user: str = region_cfg["ayla_user"]
        self._ayla_ads: str = region_cfg["ayla_ads"]

        # Cache which command property works for this model
        # For PrimaDonna (DL-pd-*): data_request without app_id
        # For Eletta/Striker (DL-striker-*): app_data_request with app_id
        self._cmd_property: str | None = None
        if oem_model.startswith("DL-pd-"):
            self._cmd_property = "data_request"
        elif oem_model.startswith("DL-striker-"):
            self._cmd_property = "app_data_request"

        # Cache whether ping_connected is supported (None = unknown, False = not supported)
        self._ping_supported: bool | None = None

        # Guard flag used by the _retry decorator to prevent recursive
        # re-authentication when the auth endpoint itself returns 401.
        self._reauthenticating: bool = False

        # Rate limiting tracker
        self._rate_tracker = RateLimitTracker()

        # TranscodeTable cache (fetched from De'Longhi backend)
        # Credit: TranscodeTable approach from FrozenGalaxy/PyDeLonghiAPI
        self._transcode_table: list[dict] | None = None
        self._model_info: dict[str, Any] | None = None

    @property
    def rate_tracker(self) -> RateLimitTracker:
        """Return the API rate limit tracker."""
        return self._rate_tracker

    @property
    def model_info(self) -> dict[str, Any] | None:
        """Return cached model info from TranscodeTable matching."""
        return self._model_info

    @property
    def device_name(self) -> str | None:
        """Return device product name from last get_devices call."""
        return self._device_name

    @property
    def sw_version(self) -> str | None:
        """Return device software version from last get_devices call."""
        return self._sw_version

    # ── Model identification (TranscodeTable) ─────────────────────────
    # Credit: TranscodeTable approach from FrozenGalaxy/PyDeLonghiAPI

    @staticmethod
    def parse_serial_number(value: str | None) -> dict[str, str] | None:
        """Parse d270_serialnumber to extract SKU-matching digits.

        The serial format is typically: ECAM{model}{suffix}{production_info}
        e.g. "ECAM45065S12345" or "ECAM61075MB12345"
        We extract the numeric portion for TranscodeTable matching.
        """
        if not value:
            return None
        # Extract all digit sequences from the serial
        digits = re.findall(r"\d+", value)
        all_digits = "".join(digits)
        return {"raw": value, "digits": all_digits}

    @staticmethod
    def match_transcode_table(
        table: list[dict],
        sku_digits: str | None = None,
        oem_model: str | None = None,
    ) -> dict[str, Any] | None:
        """Match a machine in the TranscodeTable by SKU digits or OEM model.

        Priority: SKU digits match > OEM model mapping.
        Returns the first matching machine entry with all capability fields.
        """
        # Try SKU digits match first (most precise)
        if sku_digits and len(sku_digits) >= 6:
            suffix = sku_digits[:6]  # First 6 digits from serial
            for machine in table:
                pc = machine.get("product_code", "")
                if pc.endswith(suffix) and machine.get("appModelId") != "default":
                    return machine

        # Fall back to OEM model → appModelId mapping
        if oem_model:
            target_app_id = OEM_TO_APP_MODEL.get(oem_model)
            if target_app_id:
                for machine in table:
                    if machine.get("appModelId") == target_app_id:
                        return machine

        return None

    def fetch_transcode_table(self) -> None:
        """Fetch and cache the TranscodeTable from De'Longhi backend.

        Gracefully degrades if the fetch fails (returns None, uses fallbacks).
        """
        if self._transcode_table is not None:
            return  # Already cached

        try:
            resp = self._session.post(
                TRANSCODE_TABLE_URL,
                json={"locale": "en_US", "currentVersion": "1.0"},
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code == 200:
                data = resp.json()
                self._transcode_table = data.get("machines", [])
                _LOGGER.info("TranscodeTable loaded: %d machines", len(self._transcode_table))
            else:
                _LOGGER.warning("TranscodeTable fetch failed: HTTP %d", resp.status_code)
        except requests.RequestException as err:
            _LOGGER.warning("TranscodeTable fetch network error: %s", err)
        except ValueError as err:
            # JSON decode failure — backend returned non-JSON.
            _LOGGER.warning("TranscodeTable fetch: invalid JSON payload (%s)", err)

    def identify_model(self, props: dict[str, Any]) -> dict[str, Any]:
        """Identify the machine model from properties and TranscodeTable.

        Returns a dict with model info:
        - name: Friendly display name
        - appModelId: TranscodeTable application model ID
        - nProfiles, nStandardRecipes, connectionType, protocolVersion (if available)
        """
        # Return cached result
        if self._model_info is not None:
            return self._model_info

        # Parse serial number for SKU matching
        serial_prop = props.get("d270_serialnumber", {})
        serial_val = serial_prop.get("value") if isinstance(serial_prop, dict) else None
        serial_info = self.parse_serial_number(serial_val)
        sku_digits = serial_info["digits"] if serial_info else None

        # Try TranscodeTable match
        if self._transcode_table:
            match = self.match_transcode_table(
                self._transcode_table,
                sku_digits=sku_digits,
                oem_model=self._oem_model,
            )
            if match:
                self._model_info = match
                _LOGGER.info(
                    "Model identified: %s (%s) via TranscodeTable",
                    match.get("name"),
                    match.get("appModelId"),
                )
                return self._model_info

        # Fallback to static MODEL_NAMES
        friendly = MODEL_NAMES.get(self._oem_model, self._oem_model)
        self._model_info = {
            "name": friendly,
            "appModelId": OEM_TO_APP_MODEL.get(self._oem_model, self._oem_model),
            "source": "fallback",
        }
        _LOGGER.info("Model identified (fallback): %s", friendly)
        return self._model_info

    def authenticate(self) -> bool:
        """Full auth flow: Gigya login -> JWT -> Ayla token_sign_in."""
        _LOGGER.debug("Authenticating %s (Ayla ADS: %s)", self._email, self._ayla_ads)
        try:
            # Step 1: Gigya login
            gigya_resp = self._session.post(
                f"{self._gigya_url}/accounts.login",
                data={
                    "loginID": self._email,
                    "password": self._password,
                    "apiKey": GIGYA_API_KEY,
                    "targetEnv": "mobile",
                    "include": "id_token,profile,data,preferences",
                    "sessionExpiration": "7776000",
                    "httpStatusCodes": "true",
                },
                timeout=REQUEST_TIMEOUT,
            )
            gigya_data = gigya_resp.json()

            if gigya_resp.status_code != 200 or gigya_data.get("errorCode", 0) != 0:
                _LOGGER.error(
                    "Gigya login failed for %s: %s",
                    self._email,
                    gigya_data.get("errorMessage", "Unknown error"),
                )
                raise DeLonghiAuthError(f"Gigya login failed: {gigya_data.get('errorMessage', 'Unknown')}")

            id_token: str | None = gigya_data.get("id_token")
            if not id_token:
                # Try from sessionInfo
                id_token = gigya_data.get("sessionInfo", {}).get("sessionToken")

            if not id_token:
                _LOGGER.error("No id_token in Gigya response for %s", self._email)
                raise DeLonghiAuthError("No id_token in Gigya response")

            # Step 2: Get long-lived JWT (requires sessionInfo)
            session_info = gigya_data.get("sessionInfo", {})
            session_token = session_info.get("sessionToken")
            session_secret = session_info.get("sessionSecret")

            jwt_token: str = id_token  # default to id_token from step 1
            if session_token and session_secret:
                jwt_resp = self._session.post(
                    f"{self._gigya_url}/accounts.getJWT",
                    data={
                        "oauth_token": session_token,
                        "secret": session_secret,
                        "apiKey": GIGYA_API_KEY,
                        "fields": "data.favoriteStoreId",
                        "expiration": "7776000",
                        "httpStatusCodes": "true",
                    },
                    timeout=REQUEST_TIMEOUT,
                )
                jwt_data = jwt_resp.json()
                jwt_token = jwt_data.get("id_token", id_token)
            else:
                _LOGGER.debug("No sessionInfo, using id_token directly")

            # Step 3: Ayla token_sign_in (must be JSON + provider to get push-capable token)
            ayla_resp = self._session.post(
                f"{self._ayla_user}/api/v1/token_sign_in",
                json={
                    "app_id": self._ayla_app_id,
                    "app_secret": self._ayla_app_secret,
                    "provider": "gigya_eu1_field",
                    "token": jwt_token,
                },
                timeout=REQUEST_TIMEOUT,
            )

            if ayla_resp.status_code != 200:
                _LOGGER.error("Ayla auth failed: %s %s", ayla_resp.status_code, sanitize(ayla_resp.text[:200]))
                raise DeLonghiAuthError(f"Ayla auth failed: {ayla_resp.status_code}")

            ayla_data: dict[str, Any] = ayla_resp.json()
            self._ayla_token = ayla_data["access_token"]
            self._ayla_refresh = ayla_data.get("refresh_token")
            self._token_expires = time.time() + ayla_data.get("expires_in", 86400)

            _LOGGER.info("De'Longhi auth successful")
            return True

        except DeLonghiAuthError:
            raise
        except KeyError as err:
            _LOGGER.error("Missing expected key in auth response: %s", err)
            raise DeLonghiAuthError(f"Malformed auth response: {err}") from err
        except ValueError as err:
            _LOGGER.error("Invalid JSON in auth response: %s", err)
            raise DeLonghiApiError(f"Invalid response: {err}") from err
        except requests.RequestException as err:
            _LOGGER.error("Network error during auth: %s", err)
            raise DeLonghiApiError(f"Network error: {err}") from err

    def _ensure_token(self) -> None:
        """Refresh token if expired (thread-safe via double-checked lock)."""
        # Fast path: token still valid, no lock needed
        if time.time() < self._token_expires - 300:
            return
        with self._token_lock:
            # Re-check after acquiring lock (another thread may have refreshed)
            if time.time() < self._token_expires - 300:
                return
            if self._ayla_refresh:
                try:
                    resp = self._session.post(
                        f"{self._ayla_user}/users/refresh_token.json",
                        json={"user": {"refresh_token": self._ayla_refresh}},
                        timeout=REQUEST_TIMEOUT,
                    )
                    if resp.status_code == 200:
                        data: dict[str, Any] = resp.json()
                        self._ayla_token = data["access_token"]
                        self._ayla_refresh = data.get("refresh_token", self._ayla_refresh)
                        self._token_expires = time.time() + data.get("expires_in", 86400)
                        return
                except (requests.RequestException, KeyError, ValueError) as err:
                    _LOGGER.debug("Token refresh failed, re-authenticating: %s", err)
            self.authenticate()

    def _headers(self) -> dict[str, str]:
        self._ensure_token()
        return {
            "Authorization": f"auth_token {self._ayla_token}",
            "Content-Type": "application/json",
            "x-ayla-source": "Mobile",
        }

    @_retry
    def get_devices(self) -> list[dict[str, Any]]:
        """Get all De'Longhi devices."""
        resp = self._session.get(
            f"{self._ayla_ads}/apiv1/devices.json",
            headers=self._headers(),
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        self._devices = [d["device"] for d in resp.json()]

        # Store device info from first device for device_info
        if self._devices:
            dev = self._devices[0]
            self._device_name = dev.get("product_name")
            self._sw_version = dev.get("sw_version")
            # Backfill oem_model from Ayla metadata if the config entry
            # didn't carry it (older installs predate model detection).
            # Without this, ContentStack + cmd_property routing both fail
            # silently on PrimaDonna Soul / etc.
            if not self._oem_model:
                ayla_oem = dev.get("oem_model") or dev.get("model")
                if ayla_oem:
                    self._oem_model = ayla_oem
                    _LOGGER.info(
                        "Backfilled oem_model from Ayla metadata: %s",
                        ayla_oem,
                    )
                    # Re-apply the cmd_property routing now that we know
                    # the model — mirrors the __init__ branch.
                    if ayla_oem.startswith("DL-pd-"):
                        self._cmd_property = "data_request"
                    elif ayla_oem.startswith("DL-striker-"):
                        self._cmd_property = "app_data_request"

        return self._devices

    @_retry
    def get_properties(self, dsn: str, names: list[str] | None = None) -> dict[str, Any]:
        """Get properties for a device. If names is provided, limits response."""
        with ApiTimer("get_properties", self._rate_tracker):
            params = [("names[]", n) for n in names] if names else None
            resp = self._session.get(
                f"{self._ayla_ads}/apiv1/dsns/{dsn}/properties.json",
                headers=self._headers(),
                params=params,
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            result = {p["property"]["name"]: p["property"] for p in resp.json()}
            _LOGGER.debug(
                "get_properties(names=%s): %d properties (%s)",
                names,
                len(result),
                ", ".join(sorted(result.keys())[:20]) + ("..." if len(result) > 20 else ""),
            )
            return result

    @_retry
    def get_property(self, dsn: str, name: str) -> dict[str, Any]:
        """Get a single property."""
        resp = self._session.get(
            f"{self._ayla_ads}/apiv1/dsns/{dsn}/properties/{name}.json",
            headers=self._headers(),
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json().get("property", {})

    def get_lan_config(self, dsn: str) -> dict[str, Any]:
        """Get LAN connection config (lan_key, lan_enabled, IP).

        Returns dict with:
          - lan_enabled: bool
          - lanip_key: str (AES key for local LAN crypto)
          - lanip_key_id: int
          - lan_ip: str (machine's local IP)
          - status: str (Online/Offline)
        """
        result: dict[str, Any] = {
            "lan_enabled": False,
            "lanip_key": None,
            "lanip_key_id": None,
            "lan_ip": None,
            "status": None,
        }

        # Get device info for lan_enabled and IP
        try:
            resp = self._session.get(
                f"{self._ayla_ads}/apiv1/dsns/{dsn}.json",
                headers=self._headers(),
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            device = resp.json().get("device", {})
            result["lan_enabled"] = bool(device.get("lan_enabled", False))
            result["lan_ip"] = device.get("lan_ip")
            result["status"] = device.get("connection_status")
            _LOGGER.debug(
                "LAN config for %s: enabled=%s, ip=%s, status=%s",
                dsn,
                result["lan_enabled"],
                result["lan_ip"],
                result["status"],
            )
        except (requests.RequestException, DeLonghiApiError) as err:
            _LOGGER.debug("Failed to get device info for %s: %s", dsn, err)

        # Get LAN key from connection_config endpoint
        if result["lan_enabled"]:
            try:
                resp = self._session.get(
                    f"{self._ayla_ads}/apiv1/devices/{dsn}/lan.json",
                    headers=self._headers(),
                    timeout=REQUEST_TIMEOUT,
                )
                resp.raise_for_status()
                lanip = resp.json().get("lanip", {})
                result["lanip_key"] = lanip.get("lanip_key")
                result["lanip_key_id"] = lanip.get("lanip_key_id")
                _LOGGER.debug(
                    "LAN key for %s: key_id=%s, key=%s",
                    dsn,
                    result["lanip_key_id"],
                    "***" if result["lanip_key"] else "None",
                )
            except (requests.RequestException, DeLonghiApiError) as err:
                _LOGGER.debug("Failed to get LAN key for %s: %s", dsn, err)
                # Try alternative endpoint
                try:
                    resp = self._session.get(
                        f"{self._ayla_ads}/apiv1/devices/{dsn}/connection_config.json",
                        headers=self._headers(),
                        timeout=REQUEST_TIMEOUT,
                    )
                    resp.raise_for_status()
                    cfg = resp.json()
                    result["lanip_key"] = cfg.get("local_key")
                    result["lanip_key_id"] = cfg.get("local_key_id")
                    _LOGGER.debug(
                        "LAN key (alt) for %s: key_id=%s",
                        dsn,
                        result["lanip_key_id"],
                    )
                except (requests.RequestException, DeLonghiApiError) as err2:
                    _LOGGER.debug("Failed alt LAN key for %s: %s", dsn, err2)

        _LOGGER.info(
            "LAN config result: enabled=%s, ip=%s, key=%s, status=%s",
            result["lan_enabled"],
            result["lan_ip"],
            "present" if result["lanip_key"] else "MISSING",
            result["status"],
        )
        return result

    def _build_packet(self, ecam_bytes: bytes, include_app_id: bool = True) -> str:
        """Build WiFi packet -> Base64.

        Newer models (Eletta Explore): ECAM + timestamp + app_id
        Older models (PrimaDonna Soul): ECAM + timestamp only
        """
        ts = struct.pack(">I", int(time.time()))
        if include_app_id:
            full = ecam_bytes + ts + APP_SIGNATURE
        else:
            full = ecam_bytes + ts
        return base64.b64encode(full).decode()

    @_retry
    def send_command(self, dsn: str, ecam_bytes: bytes) -> bool:
        """Send an ECAM command to the machine.

        Newer models (Eletta Explore): app_data_request, packet with app_id
        Older models (PrimaDonna Soul): data_request, packet without app_id

        For PrimaDonna Soul, we try BOTH packet formats (with/without app_id)
        because different firmware versions may require different formats even
        on the same property endpoint.

        The command property is determined by model (DL-pd-* vs DL-striker-*).
        If model is unknown, auto-detects by trying both — but only caches
        when one returns 404 (proving the other is correct). A 201 from Ayla
        only means "datapoint accepted by cloud", NOT "machine received it".
        """
        self._rate_tracker.record()
        headers = self._headers()

        # Build attempt list: (property_name, include_app_id)
        # For PrimaDonna models, try both formats on data_request to maximize
        # chances of the cloud actually forwarding to the machine.
        if self._cmd_property == "data_request":
            attempts = [
                ("data_request", True),  # try with app_id first (newer firmware)
                ("data_request", False),  # legacy: without app_id
            ]
        elif self._cmd_property == "app_data_request":
            attempts = [
                ("app_data_request", True),  # Eletta: with app_id
            ]
        else:
            # Unknown model: try all combinations
            attempts = [
                ("app_data_request", True),
                ("data_request", True),
                ("data_request", False),
            ]

        for prop_name, include_app_id in attempts:
            _LOGGER.debug(
                "send_command: trying %s (app_id=%s, cached=%s) cmd=%s",
                prop_name,
                include_app_id,
                self._cmd_property,
                ecam_bytes.hex(),
            )
            b64 = self._build_packet(ecam_bytes, include_app_id=include_app_id)
            resp = self._session.post(
                f"{self._ayla_ads}/apiv1/dsns/{dsn}/properties/{prop_name}/datapoints.json",
                json={"datapoint": {"value": b64}},
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code == 201:
                _LOGGER.info("Command sent via %s (app_id=%s): %s", prop_name, include_app_id, ecam_bytes.hex())
                return True
            if resp.status_code == 404:
                _LOGGER.debug("send_command: %s returned 404, trying next", prop_name)
                # 404 proves this property doesn't exist — cache the other one
                if prop_name == "app_data_request" and not self._cmd_property:
                    self._cmd_property = "data_request"
                    _LOGGER.info("Detected command property: data_request (from 404 on app_data_request)")
                continue
            # Non-404 error → retryable failure
            _LOGGER.error(
                "send_command: %s returned HTTP %d: %s", prop_name, resp.status_code, sanitize(resp.text[:200])
            )
            resp.raise_for_status()

        raise DeLonghiApiError("No valid command property found (all endpoints returned 404)")

    def ping_connected(self, dsn: str) -> bool:
        """Send app_device_connected ping to force machine to push data updates.

        Tries with app_id (newer models) then without (legacy models).
        Skips entirely if previous attempts showed the property doesn't exist.
        """
        if self._ping_supported is False:
            _LOGGER.debug("ping_connected: skipped (not supported on this model)")
            return False

        ts = struct.pack(">I", int(time.time()))
        headers = self._headers()

        formats = ["with_app_id", "without_app_id"]
        for i, b64 in enumerate(
            (
                base64.b64encode(ts + APP_SIGNATURE).decode(),
                base64.b64encode(ts).decode(),
            )
        ):
            _LOGGER.debug("ping_connected: trying %s format", formats[i])
            resp = self._session.post(
                f"{self._ayla_ads}/apiv1/dsns/{dsn}/properties/app_device_connected/datapoints.json",
                json={"datapoint": {"value": b64}},
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code == 201:
                self._ping_supported = True
                _LOGGER.debug("ping_connected: success (%s)", formats[i])
                return True
            if resp.status_code == 404:
                _LOGGER.debug("ping_connected: %s returned 404, trying next", formats[i])
                continue
            raise DeLonghiApiError(f"Ping failed: HTTP {resp.status_code}")

        # Both formats returned 404 — property doesn't exist on this model
        self._ping_supported = False
        _LOGGER.info("ping_connected: not supported on this model, disabling future pings")
        return False

    # Pre-built monitor command: 0x84 with params 0F 03 02 + CRC
    _MONITOR_CMD = bytes.fromhex("0d07840f03025640")

    def request_monitor(self, dsn: str) -> bool:
        """Send monitor command (0x84) to force machine to push fresh data."""
        return self.send_command(dsn, self._MONITOR_CMD)

    def brew(self, dsn: str, recipe_ecam: bytes) -> bool:
        """Send a brew command."""
        return self.send_command(dsn, recipe_ecam)

    def get_status(self, dsn: str) -> dict[str, Any]:
        """Get machine status from monitor property (MonitorDataV2 format)."""
        result: dict[str, Any] = {
            "status": "UNKNOWN",
            "machine_state": "Unknown",
            "alarms": [],
            "profile": 0,
        }

        try:
            props = self.get_properties(dsn, names=["app_device_status", "d302_monitor_machine", "d302_monitor"])
            result["status"] = props.get("app_device_status", {}).get("value", "UNKNOWN")

            has_d302 = "d302_monitor_machine" in props
            has_d302_legacy = "d302_monitor" in props
            monitor = props.get("d302_monitor_machine", props.get("d302_monitor", {}))
            monitor_val = monitor.get("value")
            _LOGGER.debug(
                "get_status: cloud=%s, d302_monitor_machine=%s, d302_monitor=%s, monitor_val=%s",
                result["status"],
                has_d302,
                has_d302_legacy,
                "present" if monitor_val else "absent",
            )
            if monitor_val:
                raw = base64.b64decode(monitor_val)
                result["monitor_raw"] = raw.hex()
                result.update(self._parse_monitor_v2(raw))
                _LOGGER.debug(
                    "get_status: machine_state=%s, alarms=%s, profile=%d",
                    result.get("machine_state", "?"),
                    [a["name"] for a in result.get("alarms", [])],
                    result.get("profile", 0),
                )
        except (requests.RequestException, DeLonghiApiError, KeyError, ValueError) as err:
            _LOGGER.debug("Status/monitor fetch error: %s", err)

        return result

    @staticmethod
    def _parse_monitor_v2(raw: bytes) -> dict[str, Any]:
        """Parse MonitorDataV2 binary data.

        Byte layout (type 2, Striker/Eletta Explore):
        [0-3]  Header (0xD0, len, cmd, flags)
        [4]    Active profile
        [5-6]  Accessory/switch bits
        [7]    Alarm byte 0 (bits 0-7)
        [8]    Alarm byte 1 (bits 8-15)
        [9]    Machine state (0=off, 6=heating, 7=ready, 3=brewing)
        [10]   Sub-state
        [11]   Extra data
        [12]   Alarm byte 2 (bits 16-23)
        [13]   Alarm byte 3 (bits 24-31)
        [14+]  Additional data + timestamp
        """
        from .const import ALARMS, MACHINE_STATES

        result: dict[str, Any] = {"alarms": [], "machine_state": "Unknown", "profile": 0}

        if len(raw) < 14:
            return result

        _LOGGER.debug("Monitor raw: %s", raw.hex())
        result["profile"] = raw[4]

        # Machine state
        state_val = raw[9]
        result["machine_state"] = MACHINE_STATES.get(state_val, f"Unknown ({state_val})")

        # 32-bit alarm word from 4 bytes
        alarm_word = (raw[7] & 0xFF) | ((raw[8] & 0xFF) << 8) | ((raw[12] & 0xFF) << 16) | ((raw[13] & 0xFF) << 24)

        active_alarms: list[dict[str, Any]] = []
        for bit, meta in ALARMS.items():
            if alarm_word & (1 << bit):
                active_alarms.append({"bit": bit, **meta})

        if active_alarms:
            _LOGGER.debug(
                "Monitor alarms: word=0x%08X, active=[%s]",
                alarm_word,
                ", ".join(f"bit{a['bit']}:{a['name']}" for a in active_alarms),
            )

        result["alarms"] = active_alarms
        result["alarm_word"] = alarm_word
        return result

    def get_counters(self, dsn: str) -> dict[str, Any]:
        """Get counters (fetches properties)."""
        return self.parse_counters(self.get_properties(dsn))

    def parse_counters(self, props: dict[str, Any]) -> dict[str, Any]:
        """Get beverage counters, maintenance stats, and JSON sub-counters.

        Models differ in two ways:

        1. **Total beverage property**: Eletta Explore exposes `d701_tot_bev_b`
           (the total). PrimaDonna Soul splits it into `d700_tot_bev_b` (black),
           `d701_tot_bev_bw` (black+white) and `d703_tot_bev_w` (water).

        2. **d702 / d733-d740 shape**: on Eletta these are *JSON aggregates*
           with sub-keys like `tot_custom_b_bw`. On PrimaDonna Soul they are
           *individual integer counters* with completely different semantics
           (d733=taste_espressi, d734=taste_coffee, etc.) and the custom
           beverage total lives in its own property `d741_tot_custom_b_bw`.

           We detect the shape at runtime: strings that start with `{` are
           parsed as JSON, everything else is treated as a direct integer
           named after the raw property key.
        """

        # Dump raw counter properties for debugging model differences
        for name in sorted(props):
            if name.startswith(("d70", "d71", "d72", "d73", "d74", "d55", "d51", "d58")):
                val = props[name].get("value")
                if val is not None:
                    _LOGGER.debug("Counter property %s = %s", name, str(val)[:200])

        counters: dict[str, Any] = {}

        # --- Simple integer counters ---
        counter_map: dict[str, str] = {
            # Totals — Eletta has d701_tot_bev_b; PrimaDonna has d700 / d701_bw / d703
            "d700_tot_bev_b": "total_black_beverages",
            "d701_tot_bev_b": "total_beverages",
            "d701_tot_bev_bw": "total_bw_beverages",
            "d703_tot_bev_w": "total_water_beverages",
            "d704_tot_bev_espressi": "total_espressos",
            # Individual drinks
            "d705_tot_id1_espr": "espresso",
            "d706_tot_id2_coffee": "coffee",
            "d707_tot_id3_long": "long_coffee",
            "d708_tot_id5_doppio_p": "doppio",
            "d709_id6_americano": "americano",
            "d710_tot_id7_capp": "cappuccino",
            "d711_id8_lattmacc": "latte_macchiato",
            "d712_id9_cafflatt": "caffe_latte",
            "d713_id10_flatwhite": "flat_white",
            "d714_id11_esprmacc": "espresso_macchiato",
            "d715_id12_hotmilk": "hot_milk",
            "d716_id13_cappdoppio_p": "cappuccino_doppio",
            "d717_id15_caprev": "cappuccino_mix",
            "d718_id16_hotwater": "hot_water",
            "d719_id22_tea": "tea",
            "d720_tot_id23_coffee_pot": "coffee_pot",
            # PrimaDonna Soul extras (d721-d748 observed in #3 jostrasser log)
            "d727_id24_cortado": "cortado",
            "d728_id25_long_black": "long_black",
            "d729_id26_travel_mug": "travel_mug",
            "d730_tot_id27_brew_over_ice": "brew_over_ice",
            "d731_pregr_coff_cnt": "preground_brews",
            "d741_tot_custom_b_bw": "usage_tot_custom_b_bw",
            "d742_tot_bev_no_original": "beverages_modified",
            "d747_bev_abort_cnt": "beverages_aborted",
            "d748_bev_2x_cnt": "beverages_doubled",
            # Maintenance
            "d551_cnt_coffee_fondi": "grounds_count",
            "d552_cnt_calc_tot": "descale_count",
            "d553_water_tot_qty": "total_water_ml",
            "d510_ground_cnt_percentage": "grounds_percentage",
            "d513_percentage_usage_fltr": "filter_percentage",
            "d554_cnt_filter_tot": "filter_replacements",
            "d555_water_filter_qty": "water_through_filter_ml",
            # d550_water_calc_qty is a weighted internal value (water × hardness)
            # for descale calculation, NOT actual mL — excluded in favor of descale_progress
            "d557_milk_cln_cnt": "milk_clean_count",
            "d558_bev_cnt_desc_on": "beverages_since_descale",
        }
        for prop_name, friendly in counter_map.items():
            if prop_name in props:
                val = props[prop_name].get("value")
                if val is not None:
                    try:
                        counters[friendly] = int(val)
                    except (ValueError, TypeError):
                        counters[friendly] = val

        # --- JSON aggregates (Eletta only) ---
        # On PrimaDonna Soul these same d7xx properties are individual integer
        # counters with different names (e.g. d733_taste_espressi vs
        # d733_tot_bev_counters). We check the shape at runtime — only parse as
        # JSON if the value actually is one.
        json_props: dict[str, str] = {
            "d702_tot_bev_other": "other",
            "d733_tot_bev_counters": "mug",
            "d734_tot_bev_usage": "usage",
            "d735_iced_bev": "iced",
            "d736_mug_bev": "mug_bev",
            "d737_mug_iced_bev": "mug_iced",
            "d738_cold_brew_bev": "cold_brew",
            "d739_taste_bev": "taste",
            "d740_water_qty_bev": "water_qty",
        }
        for prop_name, prefix in json_props.items():
            if prop_name in props:
                val = props[prop_name].get("value")
                if val and isinstance(val, str) and val.startswith("{"):
                    try:
                        data = json.loads(val)
                        for key, v in data.items():
                            try:
                                counters[f"{prefix}_{key}"] = int(v)
                            except (ValueError, TypeError):
                                counters[f"{prefix}_{key}"] = v
                    except json.JSONDecodeError:
                        pass

        # --- d702 fallback: direct integer on PrimaDonna ---
        # The Eletta d702_tot_bev_other is JSON; PrimaDonna stores it as an
        # integer ("other" category count). Expose as other_tot_bev_other so
        # the existing "Coffee Other Beverages" sensor lights up.
        if "other_tot_bev_other" not in counters and "d702_tot_bev_other" in props:
            raw = props["d702_tot_bev_other"].get("value")
            if raw is not None:
                with contextlib.suppress(ValueError, TypeError):
                    counters["other_tot_bev_other"] = int(raw)

        # --- Descale progress (Eletta only — reads d580_service_parameters) ---
        # PrimaDonna Soul has no d580. Users can watch `beverages_since_descale`
        # (d558_bev_cnt_desc_on) as a proxy — it's already exposed.
        if "d580_service_parameters" in props:
            val = props["d580_service_parameters"].get("value")
            if val and isinstance(val, str) and val.startswith("{"):
                try:
                    svc = json.loads(val)
                    calc_qty = int(svc.get("last_4_water_calc_qty", 0))
                    threshold = int(svc.get("last_4_calc_threshold", 1))
                    descale_status = int(svc.get("descale_status", 0))
                    if threshold > 0:
                        counters["descale_progress"] = min(round(calc_qty / threshold * 100), 100)
                    counters["descale_status"] = descale_status
                except (json.JSONDecodeError, ValueError, TypeError):
                    pass

        # --- Bean system usage counters (d721-d726 / id200-id205) ---
        # PrimaDonna Soul tracks how often each bean profile has been used.
        bean_map = {
            "d721_id200_bs_1": 1,
            "d722_id201_bs_2": 2,
            "d723_id202_bs_3": 3,
            "d724_id203_bs_4": 4,
            "d725_id204_bs_5": 5,
            "d726_id205_bs_6": 6,
        }
        for prop_name, bs_id in bean_map.items():
            if prop_name in props:
                val = props[prop_name].get("value")
                if val is not None:
                    with contextlib.suppress(ValueError, TypeError):
                        counters[f"bean_system_{bs_id}_uses"] = int(val)

        # --- Computed total — sum all beverage categories ---
        # Real user data (issue #3) proves d700 and d701_bw are SEPARATE on PrimaDonna:
        #   jostrasser: d700=4827(black), d701_bw=34(milk), d702=916(other), d703=3(water)
        #   lodzen:     d700=52(black),   d701_bw=15(milk), d702=0(other),   d703=8(water)
        # Eletta: d701_tot_bev_b is THE total (no d700 property exists).
        if "total_beverages" in counters:
            # Eletta: d701 is the total; water is separate.
            total_parts = ["total_beverages", "total_water_beverages"]
        else:
            # PrimaDonna: d700(black) + d701_bw(with milk) + d703(water).
            total_parts = ["total_black_beverages", "total_bw_beverages", "total_water_beverages"]
        computed_total = 0
        has_parts = False
        for key in total_parts:
            val = counters.get(key)
            if isinstance(val, int):
                computed_total += val
                has_parts = True
        # Include "other" category if the sensor has a numeric value.
        other = counters.get("other_tot_bev_other") or counters.get("other_other")
        if isinstance(other, int):
            computed_total += other
            has_parts = True
        if has_parts:
            counters["computed_total"] = computed_total
            # On PrimaDonna there's no d701_tot_bev_b, so "Total Beverages"
            # would be permanently blank. Alias computed_total into it so the
            # existing sensor shows a meaningful value everywhere.
            counters.setdefault("total_beverages", computed_total)

        return counters

    def brew_beverage(self, dsn: str, beverage_key: str, profile: int = 2) -> bool:
        """Brew a beverage by converting stored recipe to brew command.

        Universal conversion verified against 9 MITM captures:
        - Normal drinks: all recipe params except VISIBLE(25)
        - Iced drinks: exclude COFFEE/MILK/HOT_WATER quantities
        - Cold brew: exclude quantities, add ICED=3 + INTENSITY
        - Always append RINSE(39)=1 and profile_save byte

        Args:
            dsn: Device serial number.
            beverage_key: Beverage identifier (e.g. "espresso").
            profile: User profile number (1-5, default 2).
        """
        _LOGGER.debug("brew_beverage: fetching all properties for %s", dsn)
        props = self.get_properties(dsn)

        # Log all recipe properties for diagnostic
        rec_props = [n for n in props if "_rec_" in n]
        _LOGGER.debug(
            "brew_beverage: %d total properties, %d recipe props: %s",
            len(props),
            len(rec_props),
            rec_props[:15],
        )

        # Try the selected profile first, then fall back to any profile.
        # Two naming conventions:
        #   Eletta Explore:  d302_rec_{profile}_{key}   → match "_rec_{profile}_{key}"
        #   PrimaDonna Soul: d{num}_{profile}_rec_{key}  → match "_{profile}_rec_{key}"
        recipe_prop: dict[str, Any] | None = None
        targets = [
            f"_rec_{profile}_{beverage_key}",  # Eletta: _rec_2_espresso
            f"_{profile}_rec_{beverage_key}",  # PrimaDonna: _2_rec_espresso
        ]
        for name, prop in props.items():
            if prop.get("value") and any(t in name for t in targets):
                val = prop["value"]
                if isinstance(val, str) and not val.startswith("{"):
                    _LOGGER.debug("brew_beverage: profile %d match: %s", profile, name)
                    recipe_prop = prop
                    break

        if not recipe_prop:
            _LOGGER.debug("brew_beverage: no profile %d match, trying fallback", profile)
            # Fallback: try other profiles, then default (no profile)
            for name, prop in props.items():
                if not prop.get("value") or not isinstance(prop["value"], str) or prop["value"].startswith("{"):
                    continue
                if name.endswith(f"_rec_{beverage_key}"):
                    _LOGGER.debug("brew_beverage: fallback match (default/other profile): %s", name)
                    recipe_prop = prop
                    break
                for p in range(1, 6):  # Profiles 1-5
                    if p == profile:
                        continue
                    if f"_rec_{p}_{beverage_key}" in name or f"_{p}_rec_{beverage_key}" in name:
                        _LOGGER.debug("brew_beverage: fallback match (profile %d): %s", p, name)
                        recipe_prop = prop
                        break
                if recipe_prop:
                    break

        if not recipe_prop:
            _LOGGER.error(
                "brew_beverage: recipe NOT FOUND for '%s' (profile %d). Available recipe props: %s",
                beverage_key,
                profile,
                rec_props,
            )
            raise DeLonghiApiError(f"Recipe not found for {beverage_key} (tried profile {profile} + fallback)")

        try:
            recipe_data = base64.b64decode(recipe_prop["value"])
        except (ValueError, base64.binascii.Error) as err:
            raise DeLonghiApiError(f"Cannot decode recipe for {beverage_key}: {err}") from err

        _LOGGER.debug(
            "brew_beverage: recipe for %s = %d bytes: %s",
            beverage_key,
            len(recipe_data),
            recipe_data.hex(),
        )

        if len(recipe_data) < 8:
            raise DeLonghiApiError(f"Recipe {beverage_key} too short: {len(recipe_data)} bytes")

        # Pre-brew checks: alarms and accessory
        self._pre_brew_check(dsn, recipe_data, beverage_key)

        # Ping machine to ensure push channel is active (app does this before every brew)
        try:
            self.ping_connected(dsn)
        except (DeLonghiApiError, DeLonghiAuthError):
            _LOGGER.debug("brew_beverage: pre-brew ping failed, sending command anyway")

        is_iced = beverage_key.startswith(("i_", "mi_", "over_ice"))
        is_cold_brew = "_cb_" in beverage_key
        brew_cmd = self._recipe_to_brew_command(
            recipe_data, is_iced=is_iced, is_cold_brew=is_cold_brew, profile=profile
        )
        _LOGGER.info(
            "Brewing %s (profile=%d, iced=%s, cold=%s): %s",
            beverage_key,
            profile,
            is_iced,
            is_cold_brew,
            brew_cmd.hex(),
        )
        return self.send_command(dsn, brew_cmd)

    # Beverage name → ID mapping for custom brew
    _BEVERAGE_IDS: dict[str, int] = {
        "espresso": 1,
        "coffee": 2,
        "long_coffee": 3,
        "doppio": 5,
        "americano": 6,
        "cappuccino": 7,
        "latte_macchiato": 8,
        "caffe_latte": 9,
        "flat_white": 10,
        "espresso_macchiato": 11,
        "hot_milk": 12,
        "hot_water": 16,
        "tea": 22,
        "cortado": 24,
    }

    # Accessory required per beverage type (2=Latte Crema Hot)
    _BEVERAGE_ACCESSORY: dict[str, int] = {
        "cappuccino": 2,
        "latte_macchiato": 2,
        "caffe_latte": 2,
        "flat_white": 2,
        "espresso_macchiato": 2,
        "hot_milk": 2,
        "cortado": 2,
    }

    def brew_custom(
        self,
        dsn: str,
        beverage: str,
        coffee_qty: int | None = None,
        milk_qty: int | None = None,
        water_qty: int | None = None,
        taste: int = 3,
        milk_froth: int = 2,
        temperature: int = 1,
        profile: int = 1,
    ) -> bool:
        """Brew a custom beverage with specific parameters."""
        bev_id = self._BEVERAGE_IDS.get(beverage)
        if bev_id is None:
            raise DeLonghiApiError(f"Unknown beverage: {beverage}")
        if not 1 <= profile <= 5:
            raise DeLonghiApiError(f"Profile must be 1-5, got {profile}")

        # Build param pairs
        brew_params = bytearray()

        # Accessory
        acc = self._BEVERAGE_ACCESSORY.get(beverage, 0)
        if acc:
            brew_params += bytearray([28, acc])  # ACCESSORIO
            brew_params += bytearray([11, milk_froth])  # MILK_FROTH

        # DUExPER for espresso-type
        if beverage in ("espresso", "doppio"):
            brew_params += bytearray([8, 1 if beverage == "doppio" else 0])

        # Coffee quantity (16-bit)
        if coffee_qty is not None:
            brew_params += bytearray([1, (coffee_qty >> 8) & 0xFF, coffee_qty & 0xFF])

        # Hot water quantity (16-bit)
        if water_qty is not None:
            brew_params += bytearray([15, (water_qty >> 8) & 0xFF, water_qty & 0xFF])

        # IDX_LEN
        brew_params += bytearray([27, 1])

        # Taste
        brew_params += bytearray([2, taste])

        # Milk quantity (16-bit)
        if milk_qty is not None:
            brew_params += bytearray([9, (milk_qty >> 8) & 0xFF, milk_qty & 0xFF])

        # Tea temperature
        if beverage == "tea":
            brew_params += bytearray([13, temperature])

        # RINSE
        brew_params += bytearray([39, 1])

        total = 6 + len(brew_params) + 1 + 2
        body = bytes([0x0D, total - 1, 0x83, 0xF0, bev_id, 0x03]) + bytes(brew_params) + bytes([(profile << 2) | 2])
        brew_cmd = body + self._crc16(body)

        # Pre-brew check (build a fake recipe for accessory check)
        fake_recipe = bytes([0xD0, 0, 0xA6, 0xF0, 1, bev_id, 28, acc, 0, 0])
        self._pre_brew_check(dsn, fake_recipe, beverage)

        # Ping machine to ensure push channel is active
        try:
            self.ping_connected(dsn)
        except (DeLonghiApiError, DeLonghiAuthError):
            _LOGGER.debug("Pre-brew ping failed, sending command anyway")

        _LOGGER.info("Brewing custom %s: %s", beverage, brew_cmd.hex())
        return self.send_command(dsn, brew_cmd)

    @_retry
    def cancel_brew(self, dsn: str) -> bool:
        """Cancel the current brew/operation. Send ECAM 0x8F Cancel Command."""
        # len = total(5) - 1 = 4: [0x0D][0x04][0x8F] + CRC(2)
        cancel_body = bytes([0x0D, 0x04, 0x8F])
        cancel_cmd = cancel_body + self._crc16(cancel_body)
        _LOGGER.info("Sending CANCEL command to %s", dsn)
        return self.send_command(dsn, cancel_cmd)

    @_retry
    def sync_recipes(self, dsn: str, profile: int = 1) -> bool:
        """Force machine to synchronize and upload recipes to the cloud.

        Sends ECAM 0xA9 (READ_RECIPES) for a specific profile.
        """
        # len = total(6) - 1 = 5: [0x0D][0x05][0xA9][profile] + CRC(2)
        sync_body = bytes([0x0D, 0x05, 0xA9, profile])
        sync_cmd = sync_body + self._crc16(sync_body)
        _LOGGER.info("Requesting recipes sync for profile %d on %s", profile, dsn)
        return self.send_command(dsn, sync_cmd)

    # ── Bean Adapt (issue #7) ─────────────────────────────────────────────
    # Opcodes shared by @MattG-K: 0xB9 select, 0xBA read, 0xBB write. Full
    # framing mirrors every other ECAM command already in use:
    #   [0x0D][len][opcode][payload][CRC-16/SPI-FUJITSU]
    # where ``len = total_size - 1``.

    @staticmethod
    def _build_bean_select_body(slot: int) -> bytes:
        """Build Select Bean System (0xB9) body — single slot byte payload."""
        if not 1 <= slot <= 7:
            raise DeLonghiApiError(f"Bean slot must be 1-7, got {slot}")
        # total = 1(header) + 1(len) + 1(opcode) + 1(slot) + 2(crc) = 6
        return bytes([0x0D, 0x05, OPCODE_SELECT_BEAN_SYSTEM, slot])

    @staticmethod
    def _build_bean_read_body(slot: int) -> bytes:
        """Build Read Bean System (0xBA) body — single slot byte payload."""
        if not 1 <= slot <= 7:
            raise DeLonghiApiError(f"Bean slot must be 1-7, got {slot}")
        # total = 6, len = 5 (same layout as select)
        return bytes([0x0D, 0x05, OPCODE_READ_BEAN_SYSTEM, slot])

    @staticmethod
    def _encode_bean_name(name: str) -> bytes:
        """Encode a bean profile name as 40 bytes UTF-16-BE, null padded.

        The Coffee Link app accepts up to 20 UTF-16 code units before
        running out of room in the fixed 40-byte name field. We raise on
        anything longer so the caller gets a clean error instead of a
        silently-truncated profile.
        """
        encoded = name.encode("utf-16-be")
        if len(encoded) > BEAN_NAME_MAX_BYTES:
            raise DeLonghiApiError(f"Bean name too long: {len(encoded)} bytes (max {BEAN_NAME_MAX_BYTES} UTF-16-BE)")
        return encoded.ljust(BEAN_NAME_MAX_BYTES, b"\x00")

    @classmethod
    def _build_bean_write_body(
        cls,
        slot: int,
        name: str,
        temperature: int,
        intensity: int,
        grinder: int,
        flag1: int = 0,
        flag2: int = 1,
    ) -> bytes:
        """Build Write Bean System (0xBB) body.

        Payload layout (46 bytes, matches MattG-K's capture in issue #7):

            [slot: 1B][name: 40B UTF-16-BE null padded][tail: 5B]

        Tail bytes ``[temperature, intensity, grinder, flag1, flag2]`` use
        the raw magic values the Coffee Link app sends on the wire — the
        semantic mapping (e.g. high=0x0A, strong=0x02, grinder level → 0x04
        for level 5) may differ per model and is what the HA user has to
        tell us on issue #7 for the next revision.
        """
        if not 1 <= slot <= 7:
            raise DeLonghiApiError(f"Bean slot must be 1-7, got {slot}")
        if not 0 <= temperature <= 0xFF:
            raise DeLonghiApiError(f"temperature must be 0-255, got {temperature}")
        if not 0 <= intensity <= 0xFF:
            raise DeLonghiApiError(f"intensity must be 0-255, got {intensity}")
        if not 0 <= grinder <= 0xFF:
            raise DeLonghiApiError(f"grinder must be 0-255, got {grinder}")
        if flag1 not in (0, 1):
            raise DeLonghiApiError(f"flag1 must be 0 or 1, got {flag1}")
        if flag2 not in (0, 1):
            raise DeLonghiApiError(f"flag2 must be 0 or 1, got {flag2}")

        name_bytes = cls._encode_bean_name(name)
        payload = bytes([slot]) + name_bytes + bytes([temperature, intensity, grinder, flag1, flag2])
        # total = 1(0x0D) + 1(len) + 1(opcode) + 46(payload) + 2(crc) = 51
        return bytes([0x0D, len(payload) + 4, OPCODE_WRITE_BEAN_SYSTEM]) + payload

    @_retry
    def select_bean_system(self, dsn: str, slot: int) -> bool:
        """Activate a bean profile on the machine (ECAM 0xB9)."""
        body = self._build_bean_select_body(slot)
        cmd = body + self._crc16(body)
        _LOGGER.info("Selecting bean system slot %d on %s", slot, dsn)
        return self.send_command(dsn, cmd)

    @_retry
    def read_bean_system(self, dsn: str, slot: int) -> bool:
        """Ask the machine to publish a bean profile (ECAM 0xBA).

        The machine answers through the normal property push channel
        (``d250-d256_beansystem_N``), so the caller typically follows up
        with ``get_bean_systems`` on the next refresh cycle.
        """
        body = self._build_bean_read_body(slot)
        cmd = body + self._crc16(body)
        _LOGGER.info("Reading bean system slot %d on %s", slot, dsn)
        return self.send_command(dsn, cmd)

    @_retry
    def write_bean_system(
        self,
        dsn: str,
        slot: int,
        name: str,
        temperature: int,
        intensity: int,
        grinder: int,
        flag1: int = 0,
        flag2: int = 1,
    ) -> bool:
        """Write a Bean Adapt profile (ECAM 0xBB)."""
        body = self._build_bean_write_body(slot, name, temperature, intensity, grinder, flag1, flag2)
        cmd = body + self._crc16(body)
        _LOGGER.info(
            "Writing bean system slot %d name=%r temperature=%d intensity=%d grinder=%d on %s: %s",
            slot,
            name,
            temperature,
            intensity,
            grinder,
            dsn,
            cmd.hex(),
        )
        return self.send_command(dsn, cmd)

    def _pre_brew_check(self, dsn: str, recipe: bytes, beverage_key: str) -> None:
        """Check machine state before brewing. Raises DeLonghiApiError on problems."""
        _LOGGER.debug("_pre_brew_check: checking machine state for %s", beverage_key)
        try:
            status = self.get_status(dsn)
        except (DeLonghiApiError, DeLonghiAuthError) as err:
            _LOGGER.debug("_pre_brew_check: status fetch failed (%s), skipping checks", err)
            return

        # Check machine state
        machine_state = status.get("machine_state", "Unknown")
        if machine_state == "Off":
            raise DeLonghiApiError(f"Cannot brew {beverage_key}: machine is off. Power on first.")
        if machine_state == "Brewing":
            raise DeLonghiApiError(f"Cannot brew {beverage_key}: machine is already brewing.")
        if machine_state in ("Descaling", "Rinsing", "Going to sleep", "Turning On", "Heating"):
            raise DeLonghiApiError(f"Cannot brew {beverage_key}: machine is busy ({machine_state}).")

        # Check blocking alarms
        alarms = status.get("alarms", [])
        alarm_names = [a["name"] for a in alarms]

        blocking = {
            "Water Tank Empty",
            "Grounds Container Full",
            "Coffee Beans Empty",
            "Coffee Beans Empty 2",
            "Drip Tray Missing",
            "Water Tank Missing",
            "Grid Missing",
            "Hydraulic Problem",
            "Heater Probe Failure",
            "Infuser Motor Failure",
            "Steamer Probe Failure",
            "Bean Hopper Absent",
        }
        active_blocking = [a for a in alarm_names if a in blocking]
        if active_blocking:
            raise DeLonghiApiError(f"Cannot brew {beverage_key}: {', '.join(active_blocking)}")

        # Check accessory requirement using monitor data already in status
        required_acc = self._get_recipe_accessory(recipe)
        if required_acc and required_acc > 1:
            monitor_raw = status.get("monitor_raw")
            if monitor_raw:
                try:
                    raw = bytes.fromhex(monitor_raw)
                    current_acc = raw[5] if len(raw) > 5 else 0
                    if current_acc < 2:
                        acc_names = {0: "nothing", 1: "hot water spout"}
                        raise DeLonghiApiError(
                            f"Cannot brew {beverage_key}: milk module required "
                            f"(current: {acc_names.get(current_acc, current_acc)})"
                        )
                except (ValueError, IndexError):
                    pass

        _LOGGER.debug(
            "_pre_brew_check: OK — state=%s, alarms=%s, accessory_required=%s",
            machine_state,
            alarm_names or "none",
            required_acc,
        )

    @staticmethod
    def _get_recipe_accessory(recipe: bytes) -> int | None:
        """Extract ACCESSORIO(28) param from recipe if present."""
        raw = recipe[6:-2]
        big = DeLonghiApi._BIG_PARAMS
        i = 0
        while i < len(raw):
            pid = raw[i]
            if pid == 28 and i + 1 < len(raw):  # ACCESSORIO
                return raw[i + 1]
            if pid in big and i + 2 < len(raw):
                i += 3
            elif i + 1 < len(raw):
                i += 2
            else:
                break
        return None

    @staticmethod
    def _crc16(data: bytes) -> bytes:
        """CRC-16/SPI-FUJITSU checksum."""
        crc = 0x1D0F
        for byte in data:
            crc ^= byte << 8
            for _ in range(8):
                if crc & 0x8000:
                    crc = (crc << 1) ^ 0x1021
                else:
                    crc = crc << 1
        crc &= 0xFFFF
        return crc.to_bytes(2, byteorder="big")

    # 16-bit parameter IDs (quantities in mL)
    _BIG_PARAMS: set[int] = {1, 9, 15}  # COFFEE, MILK, HOT_WATER

    @classmethod
    def _recipe_to_brew_command(
        cls,
        recipe: bytes,
        is_iced: bool = False,
        is_cold_brew: bool = False,
        intensity: int = 1,
        profile: int = 2,
    ) -> bytes:
        """Convert stored recipe (0xD0/0xA6) to brew command (0x0D/0x83).

        Recipe params are [id][value] pairs (2 bytes) or [id][hi][lo] (3 bytes
        for 16-bit params: COFFEE=1, MILK=9, HOT_WATER=15).

        Rules verified against 9 MITM captures (espresso, hot_water, tea,
        iced_americano, iced_cappuccino, cold_brew_original, cold_brew_intense,
        cold_brew_to_mix, americano_froid):
        - Exclude VISIBLE(25) — recipe-only
        - For iced/cold_brew: also exclude COFFEE(1), MILK(9), HOT_WATER(15)
        - For iced: append ICED(31)=0
        - For cold_brew: append ICED(31)=3 + INTENSITY(38)=value
        - Always append RINSE(39)=1
        - End with profile_save = (profile << 2) | 2
        """
        if recipe[0] == 0x0D:
            return recipe

        bev_id = recipe[5]
        raw = recipe[6:-2]

        exclude: set[int] = {25, 27}  # VISIBLE + IDX_LEN always excluded
        if is_iced or is_cold_brew:
            exclude.update({1, 9, 15})  # Exclude quantities

        brew_params = bytearray()
        i = 0
        while i < len(raw):
            pid = raw[i]
            if pid in cls._BIG_PARAMS and i + 2 < len(raw):
                if pid not in exclude:
                    brew_params += raw[i : i + 3]
                i += 3
            elif i + 1 < len(raw):
                if pid not in exclude:
                    brew_params += raw[i : i + 2]
                i += 2
            else:
                break

        brew_params += bytearray([27, 1])  # IDX_LEN=1 (always, matches app behavior)

        if is_iced:
            brew_params += bytearray([31, 0])
        elif is_cold_brew:
            brew_params += bytearray([31, 3, 38, intensity])

        brew_params += bytearray([39, 1])  # RINSE=1

        total = 6 + len(brew_params) + 1 + 2
        body = bytes([0x0D, total - 1, 0x83, 0xF0, bev_id, 0x03]) + bytes(brew_params) + bytes([(profile << 2) | 2])
        return body + cls._crc16(body)

    def get_profiles(self, dsn: str) -> dict[str, Any]:
        """Get profiles (fetches properties)."""  # noqa: D401
        return self.parse_profiles(self.get_properties(dsn))

    def parse_profiles(self, props: dict[str, Any]) -> dict[str, Any]:
        """Parse user profiles from pre-fetched properties."""
        profiles: dict[int, dict[str, Any]] = {}
        active = 1

        colors = {0: "green", 1: "red", 2: "blue", 3: "orange"}
        figures = {0: "woman", 1: "man", 2: "kid"}

        # d051_profile_name1_3: profiles 1-3
        if "d051_profile_name1_3" in props:
            val = props["d051_profile_name1_3"].get("value")
            if val:
                try:
                    raw = base64.b64decode(val)
                    _LOGGER.debug("Profile raw d051: %s (%d bytes)", raw.hex(), len(raw))
                    data = raw[6:-2]
                    _LOGGER.debug("Profile data (after header): %s (%d bytes)", data.hex(), len(data))
                    # Each profile: 20 bytes name (UTF-16-BE) + 2 bytes metadata
                    stride = 22
                    for i in range(3):
                        offset = i * stride
                        if offset + 20 <= len(data):
                            name = _decode_utf16(data[offset : offset + 20])
                            icon = data[offset + 20] if offset + 20 < len(data) else 0
                            profiles[i + 1] = {
                                "name": name,
                                "color": colors.get(icon // 3, "unknown"),
                                "figure": figures.get(icon % 3, "unknown"),
                                "icon_id": icon,
                            }
                except (ValueError, UnicodeDecodeError):
                    pass

        # d052_profile_name4: profile 4
        if "d052_profile_name4" in props:
            val = props["d052_profile_name4"].get("value")
            if val:
                try:
                    raw = base64.b64decode(val)
                    data = raw[6:-2]
                    if len(data) >= 20:
                        name = _decode_utf16(data[:20])
                        icon = data[20] if len(data) > 20 else 0
                        profiles[4] = {
                            "name": name,
                            "color": colors.get(icon // 3, "unknown"),
                            "figure": figures.get(icon % 3, "unknown"),
                            "icon_id": icon,
                        }
                except (ValueError, UnicodeDecodeError):
                    pass

        # Active profile from d286_mach_sett_profile
        if "d286_mach_sett_profile" in props:
            val = props["d286_mach_sett_profile"].get("value")
            if val:
                try:
                    raw = base64.b64decode(val)
                    active = raw[4] if len(raw) > 4 else 1
                except (ValueError, IndexError):
                    pass

        return {"active": active, "profiles": profiles}

    def get_bean_systems(self, dsn: str) -> list[dict[str, Any]]:
        """Get beans (fetches properties)."""  # noqa: D401
        return self.parse_bean_systems(self.get_properties(dsn))

    def parse_bean_systems(self, props: dict[str, Any]) -> list[dict[str, Any]]:
        """Parse bean systems from pre-fetched properties.

        Each ``d250-d256_beansystem_N`` property wraps an ECAM bean profile.
        After the 5-byte ECAM header and before the trailing 2-byte CRC, the
        payload contains two 20-byte UTF-16-BE strings (local + English name)
        followed by a handful of Bean Adapt parameter bytes. We surface those
        trailing bytes as ``raw_params_hex`` so users with a configured bean
        profile can share the exact layout for issue #7 without us inventing
        a decoder.
        """
        beans: list[dict[str, Any]] = []

        for i in range(7):
            prop_name = f"d{250 + i}_beansystem_{i}"
            if prop_name not in props:
                continue
            val = props[prop_name].get("value")
            if not val:
                continue
            try:
                raw = base64.b64decode(val)
                data = raw[5:-2]
                text = _decode_utf16(data)
                parts = [p for p in text.split("\x00") if p.strip()]
                local_name = parts[0] if parts else f"Bean {i}"
                english_name = parts[1] if len(parts) > 1 else local_name
                # The first 40 bytes of ``data`` are the two UTF-16-BE names
                # (20 bytes each). Everything after that belongs to the Bean
                # Adapt parameter block — temperature, intensity, grinder,
                # etc. — but the exact layout differs per model and the
                # write format MattG-K captured in issue #7 cannot be
                # assumed to match 1:1. Expose it verbatim for now.
                raw_params_hex = data[40:].hex() if len(data) > 40 else ""
                beans.append(
                    {
                        "id": i,
                        "name": local_name,
                        "english_name": english_name,
                        "raw_bytes": len(raw),
                        "raw_params_hex": raw_params_hex,
                    }
                )
            except (ValueError, UnicodeDecodeError):
                beans.append(
                    {
                        "id": i,
                        "name": f"Bean {i}",
                        "english_name": f"Bean {i}",
                        "raw_bytes": 0,
                        "raw_params_hex": "",
                    }
                )

        return beans

    def parse_bean_system_par(self, props: dict[str, Any]) -> dict[str, Any]:
        """Return raw Bean Adapt parameter block (``d260_beansystem_par``).

        This property is documented in MattG-K's issue #7 capture as the
        global parameter table that the machine uses for grinder/flow/temp
        calibration. We expose the raw bytes verbatim until a diff across
        before/after Bean Adapt runs gives us a confirmed field layout.
        """
        prop = props.get("d260_beansystem_par")
        if not isinstance(prop, dict):
            return {}
        val = prop.get("value")
        if not val:
            return {}
        try:
            raw = base64.b64decode(val)
        except (ValueError, TypeError, base64.binascii.Error):
            return {"raw_hex": "", "raw_bytes": 0, "error": "decode_failed"}
        return {"raw_hex": raw.hex(), "raw_bytes": len(raw)}

    def get_available_beverages(self, dsn: str) -> list[str]:
        """Get beverages (fetches properties)."""  # noqa: D401
        return self.parse_available_beverages(self.get_properties(dsn))

    def parse_available_beverages(self, props: dict[str, Any]) -> list[str]:
        """Parse available beverages from pre-fetched properties.

        Tries profile 2 first (user defaults), falls back to profile 1,
        then any _rec_N_ pattern. Some models only have profile 1.
        """
        # Log all recipe/custom properties for debugging
        rec_props = sorted(n for n in props if "_rec_" in n or "custom" in n.lower())
        if rec_props:
            _LOGGER.debug("Recipe properties (%d): %s", len(rec_props), ", ".join(rec_props))

        # Parse custom recipe names (d053_custom_name_13, d054_custom_name_46)
        custom_names: dict[int, str] = {}
        for cprop, start_idx in (("d053_custom_name_13", 1), ("d054_custom_name_46", 4)):
            if cprop in props:
                val = props[cprop].get("value")
                if val:
                    try:
                        raw = base64.b64decode(val)
                        data = raw[6:-2] if len(raw) > 8 else raw
                        for i in range(3):
                            offset = i * 22
                            if offset + 20 <= len(data):
                                name = _decode_utf16(data[offset : offset + 20])
                                if name:
                                    slot = start_idx + i
                                    custom_names[slot] = name
                                    _LOGGER.debug("Custom recipe %d: '%s'", slot, name)
                    except (ValueError, UnicodeDecodeError):
                        pass

        beverages: set[str] = set()

        # Add custom recipes (d240-d245 for Eletta, d028-d033 for PrimaDonna Soul)
        for slot in range(1, 7):
            for prop_name in (f"d{239 + slot}_rec_custom_{slot}", f"d{27 + slot:03d}_rec_custom_{slot}"):
                if prop_name in props:
                    val = props[prop_name].get("value")
                    if val and isinstance(val, str) and not val.startswith("{"):
                        bev_key = f"custom_{slot}"
                        beverages.add(bev_key)
                        _LOGGER.debug("Custom beverage discovered: %s (from %s)", bev_key, prop_name)
                        break

        # Extract beverage keys from recipe properties.
        # Two naming conventions exist:
        #   Eletta Explore:    d302_rec_{profile}_{key}  → split gives [prefix, "{profile}_{key}"]
        #   PrimaDonna Soul:   d{num}_{profile}_rec_{key} → split gives ["{num}_{profile}", "{key}"]
        #   PrimaDonna (default): d{num}_rec_{key}        → split gives ["{num}", "{key}"]
        for name in props:
            if "_rec_" not in name or "custom" in name or "priority" in name or "recipe_custom_name" in name:
                continue
            parts = name.split("_rec_", 1)
            if len(parts) != 2 or not parts[0].startswith("d"):
                continue
            rest = parts[1]  # e.g. "2_espresso" or "espresso" or "custom_1"

            # Eletta format: rest = "{profile}_{key}" where profile is a single digit
            if len(rest) > 2 and rest[0].isdigit() and rest[1] == "_":
                bev = rest[2:]
            else:
                # PrimaDonna format: rest is just the key (profile is before _rec_)
                bev = rest

            if bev and not bev.isdigit() and not bev.startswith("custom_"):
                beverages.add(bev)

        _LOGGER.debug("parse_available_beverages: found %d beverages: %s", len(beverages), sorted(beverages))

        # Store custom names for button labels
        self._custom_recipe_names = custom_names

        return sorted(beverages)

    def get_custom_recipe_names(self) -> dict[str, str]:
        """Return custom recipe names keyed by beverage key."""
        return {f"custom_{slot}": name for slot, name in getattr(self, "_custom_recipe_names", {}).items()}
