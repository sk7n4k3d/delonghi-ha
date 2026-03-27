"""De'Longhi Coffee API — Gigya + Ayla Networks cloud integration."""

from __future__ import annotations

import base64
import json
import logging
import struct
import time
from typing import Any

import requests

from .const import (
    APP_SIGNATURE,
    GIGYA_API_KEY,
    GIGYA_URL,
    REGIONS,
    REQUEST_TIMEOUT,
    RETRY_COUNT,
    RETRY_DELAY,
)

_LOGGER = logging.getLogger(__name__)


class DeLonghiAuthError(Exception):
    """Authentication error."""


class DeLonghiApiError(Exception):
    """API communication error."""


def _retry(func):  # noqa: ANN001, ANN202
    """Simple retry decorator with backoff (3 attempts, 2s delay)."""
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
                last_err = err
                if attempt < RETRY_COUNT:
                    _LOGGER.debug(
                        "Attempt %d/%d failed for %s: %s — retrying in %ds",
                        attempt, RETRY_COUNT, func.__name__, err, RETRY_DELAY,
                    )
                    time.sleep(RETRY_DELAY)
        raise DeLonghiApiError(
            f"{func.__name__} failed after {RETRY_COUNT} attempts: {last_err}"
        ) from last_err
    return wrapper


class DeLonghiApi:
    """API client for De'Longhi coffee machines via Ayla Networks."""

    def __init__(self, email: str, password: str, region: str = "EU") -> None:
        self._email = email
        self._password = password
        self._session = requests.Session()
        self._ayla_token: str | None = None
        self._ayla_refresh: str | None = None
        self._token_expires: float = 0
        self._devices: list[dict[str, Any]] = []
        self._device_name: str | None = None
        self._sw_version: str | None = None

        # Gigya always uses EU1 (confirmed from app manifest)
        self._gigya_url: str = GIGYA_URL

        # Region-specific Ayla endpoints AND credentials
        region_cfg = REGIONS.get(region, REGIONS["EU"])
        self._ayla_app_id: str = region_cfg["ayla_app_id"]
        self._ayla_app_secret: str = region_cfg["ayla_app_secret"]
        self._ayla_user: str = region_cfg["ayla_user"]
        self._ayla_ads: str = region_cfg["ayla_ads"]

    @property
    def device_name(self) -> str | None:
        """Return device product name from last get_devices call."""
        return self._device_name

    @property
    def sw_version(self) -> str | None:
        """Return device software version from last get_devices call."""
        return self._sw_version

    def authenticate(self) -> bool:
        """Full auth flow: Gigya login -> JWT -> Ayla token_sign_in."""
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
                raise DeLonghiAuthError(
                    f"Gigya login failed: {gigya_data.get('errorMessage', 'Unknown')}"
                )

            id_token: str | None = gigya_data.get("id_token")
            if not id_token:
                # Try from sessionInfo
                id_token = gigya_data.get("sessionInfo", {}).get("sessionToken")

            if not id_token:
                _LOGGER.error("No id_token in Gigya response for %s", self._email)
                raise DeLonghiAuthError("No id_token in Gigya response")

            # Step 2: Get long-lived JWT
            jwt_resp = self._session.post(
                f"{self._gigya_url}/accounts.getJWT",
                data={
                    "oauth_token": gigya_data["sessionInfo"]["sessionToken"],
                    "secret": gigya_data["sessionInfo"]["sessionSecret"],
                    "apiKey": GIGYA_API_KEY,
                    "fields": "data.favoriteStoreId",
                    "expiration": "7776000",
                    "httpStatusCodes": "true",
                },
                timeout=REQUEST_TIMEOUT,
            )
            jwt_data = jwt_resp.json()
            jwt_token: str = jwt_data.get("id_token", id_token)

            # Step 3: Ayla token_sign_in
            ayla_resp = self._session.post(
                f"{self._ayla_user}/api/v1/token_sign_in",
                data={
                    "app_id": self._ayla_app_id,
                    "app_secret": self._ayla_app_secret,
                    "token": jwt_token,
                },
                timeout=REQUEST_TIMEOUT,
            )

            if ayla_resp.status_code != 200:
                _LOGGER.error(
                    "Ayla auth failed: %s %s", ayla_resp.status_code, ayla_resp.text
                )
                raise DeLonghiAuthError(
                    f"Ayla auth failed: {ayla_resp.status_code} {ayla_resp.text}"
                )

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
        """Refresh token if expired."""
        if time.time() >= self._token_expires - 300:
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

        return self._devices

    @_retry
    def get_properties(self, dsn: str) -> dict[str, Any]:
        """Get all properties for a device."""
        resp = self._session.get(
            f"{self._ayla_ads}/apiv1/dsns/{dsn}/properties.json",
            headers=self._headers(),
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return {p["property"]["name"]: p["property"] for p in resp.json()}

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
                dsn, result["lan_enabled"], result["lan_ip"], result["status"],
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
                        dsn, result["lanip_key_id"],
                    )
                except (requests.RequestException, DeLonghiApiError) as err2:
                    _LOGGER.debug("Failed alt LAN key for %s: %s", dsn, err2)

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
        """
        headers = self._headers()

        # Try newer format first (app_data_request + app_id in packet)
        # Then legacy (data_request + no app_id)
        attempts = [
            ("app_data_request", True),   # newer: with app_id
            ("data_request", False),      # legacy: without app_id
        ]

        for prop_name, include_app_id in attempts:
            b64 = self._build_packet(ecam_bytes, include_app_id=include_app_id)
            resp = self._session.post(
                f"{self._ayla_ads}/apiv1/dsns/{dsn}/properties/{prop_name}/datapoints.json",
                json={"datapoint": {"value": b64}},
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code == 201:
                _LOGGER.info("Command sent via %s: %s", prop_name, ecam_bytes.hex())
                return True
            if resp.status_code != 404:
                _LOGGER.error("Command failed on %s: %s %s", prop_name, resp.status_code, resp.text)
                return False

        _LOGGER.error("Command failed: no valid request property found")
        return False

    @_retry
    def ping_connected(self, dsn: str) -> bool:
        """Send app_device_connected ping to force machine to push data updates.

        Tries with app_id (newer models) then without (legacy models).
        """
        ts = struct.pack(">I", int(time.time()))
        headers = self._headers()

        # Try with app signature first (newer), then without (legacy)
        for b64 in (
            base64.b64encode(ts + APP_SIGNATURE).decode(),
            base64.b64encode(ts).decode(),
        ):
            resp = self._session.post(
                f"{self._ayla_ads}/apiv1/dsns/{dsn}/properties/app_device_connected/datapoints.json",
                json={"datapoint": {"value": b64}},
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code == 201:
                return True
            if resp.status_code != 404:
                return False
        return False

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
            prop = self.get_property(dsn, "app_device_status")
            result["status"] = prop.get("value", "UNKNOWN")
        except (requests.RequestException, DeLonghiApiError, KeyError, ValueError) as err:
            _LOGGER.debug("Failed to get device status: %s", err)

        try:
            # Try newer property first, fall back to older
            try:
                monitor = self.get_property(dsn, "d302_monitor_machine")
            except (requests.RequestException, DeLonghiApiError):
                monitor = self.get_property(dsn, "d302_monitor")
            monitor_val = monitor.get("value")
            if monitor_val:
                raw = base64.b64decode(monitor_val)
                result["monitor_raw"] = raw.hex()
                result.update(self._parse_monitor_v2(raw))
        except (requests.RequestException, DeLonghiApiError, KeyError, ValueError) as err:
            _LOGGER.debug("Monitor parse error: %s", err)

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
        alarm_word = (
            (raw[7] & 0xFF)
            | ((raw[8] & 0xFF) << 8)
            | ((raw[12] & 0xFF) << 16)
            | ((raw[13] & 0xFF) << 24)
        )

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
        return result

    def get_counters(self, dsn: str) -> dict[str, Any]:
        """Get counters (fetches properties)."""
        return self.parse_counters(self.get_properties(dsn))

    def parse_counters(self, props: dict[str, Any]) -> dict[str, Any]:
        """Get beverage counters, maintenance stats, and JSON sub-counters."""

        # Dump raw counter properties for debugging model differences
        for name in sorted(props):
            if name.startswith(("d70", "d71", "d72", "d73", "d74", "d55", "d51", "d58")):
                val = props[name].get("value")
                if val is not None:
                    _LOGGER.debug("Counter property %s = %s", name, str(val)[:200])

        counters: dict[str, Any] = {}

        # Simple integer counters
        # Some properties differ between models:
        #   Eletta Explore: d701_tot_bev_b (total beverages)
        #   PrimaDonna Soul: d700_tot_bev_b (black), d701_tot_bev_bw (black+white), d703_tot_bev_w (water)
        counter_map: dict[str, str] = {
            "d700_tot_bev_b": "total_black_beverages",
            "d701_tot_bev_b": "total_beverages",
            "d701_tot_bev_bw": "total_beverages",
            "d703_tot_bev_w": "total_water_beverages",
            "d704_tot_bev_espressi": "total_espressos",
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
            "d730_tot_id27_brew_over_ice": "brew_over_ice",
            "d551_cnt_coffee_fondi": "grounds_count",
            "d552_cnt_calc_tot": "descale_count",
            "d553_water_tot_qty": "total_water_ml",
            "d510_ground_cnt_percentage": "grounds_percentage",
            "d513_percentage_usage_fltr": "filter_percentage",
            "d554_cnt_filter_tot": "filter_replacements",
            "d555_water_filter_qty": "water_through_filter_ml",
            "d550_water_calc_qty": "water_since_descale_ml",
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

        # JSON counters — parse sub-fields
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

        # Descale progress — calculate % from service parameters
        if "d580_service_parameters" in props:
            val = props["d580_service_parameters"].get("value")
            if val and isinstance(val, str) and val.startswith("{"):
                try:
                    svc = json.loads(val)
                    calc_qty = int(svc.get("last_4_water_calc_qty", 0))
                    threshold = int(svc.get("last_4_calc_threshold", 1))
                    descale_status = int(svc.get("descale_status", 0))
                    if threshold > 0:
                        counters["descale_progress"] = min(
                            round(calc_qty / threshold * 100), 100
                        )
                    counters["descale_status"] = descale_status
                except (json.JSONDecodeError, ValueError, TypeError):
                    pass

        # Computed total — sum all beverage categories
        # d700 (black) + d701 (black+white) + d702 (other) + d703 (water)
        total_parts = ["total_black_beverages", "total_beverages", "total_water_beverages"]
        computed_total = 0
        has_parts = False
        for key in total_parts:
            val = counters.get(key)
            if isinstance(val, int):
                computed_total += val
                has_parts = True
        other = counters.get("other_tot_bev_other") or counters.get("other_other")
        if isinstance(other, int):
            computed_total += other
            has_parts = True
        # d702 as direct integer (not JSON sub-key)
        if "d702_tot_bev_other" in props:
            raw_val = props["d702_tot_bev_other"].get("value")
            if raw_val is not None:
                try:
                    other_direct = int(raw_val)
                    if not isinstance(other, int):
                        computed_total += other_direct
                        has_parts = True
                except (ValueError, TypeError):
                    pass
        if has_parts:
            counters["computed_total"] = computed_total

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
            profile: User profile number (1-4, default 2).
        """
        props = self.get_properties(dsn)

        # Try the selected profile first, then fall back to any profile
        recipe_prop: dict[str, Any] | None = None
        for name, prop in props.items():
            if f"_rec_{profile}_{beverage_key}" in name and prop.get("value"):
                recipe_prop = prop
                break

        if not recipe_prop:
            for name, prop in props.items():
                if f"_rec_{beverage_key}" in name and prop.get("value"):
                    val = prop["value"]
                    if isinstance(val, str) and not val.startswith("{"):
                        recipe_prop = prop
                        break

        if not recipe_prop:
            _LOGGER.error("Recipe not found for %s", beverage_key)
            return False

        try:
            recipe_data = base64.b64decode(recipe_prop["value"])
        except (ValueError, base64.binascii.Error) as err:
            _LOGGER.error("Cannot decode recipe for %s: %s", beverage_key, err)
            return False

        if len(recipe_data) < 8:
            _LOGGER.error("Recipe %s too short: %d bytes", beverage_key, len(recipe_data))
            return False

        # Pre-brew checks: alarms and accessory
        self._pre_brew_check(dsn, recipe_data, beverage_key)

        is_iced = beverage_key.startswith(("i_", "mi_", "over_ice"))
        is_cold_brew = "_cb_" in beverage_key
        brew_cmd = self._recipe_to_brew_command(
            recipe_data, is_iced=is_iced, is_cold_brew=is_cold_brew, profile=profile
        )
        _LOGGER.info("Brewing %s: %s", beverage_key, brew_cmd.hex())
        return self.send_command(dsn, brew_cmd)

    # Beverage name → ID mapping for custom brew
    _BEVERAGE_IDS: dict[str, int] = {
        "espresso": 1, "coffee": 2, "long_coffee": 3, "doppio": 5,
        "americano": 6, "cappuccino": 7, "latte_macchiato": 8,
        "caffe_latte": 9, "flat_white": 10, "espresso_macchiato": 11,
        "hot_milk": 12, "hot_water": 16, "tea": 22, "cortado": 24,
    }

    # Accessory required per beverage type (2=Latte Crema Hot)
    _BEVERAGE_ACCESSORY: dict[str, int] = {
        "cappuccino": 2, "latte_macchiato": 2, "caffe_latte": 2,
        "flat_white": 2, "espresso_macchiato": 2, "hot_milk": 2,
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
    ) -> bool:
        """Brew a custom beverage with specific parameters."""
        bev_id = self._BEVERAGE_IDS.get(beverage)
        if bev_id is None:
            raise DeLonghiApiError(f"Unknown beverage: {beverage}")

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

        # profile_save = (1 << 2) | 2 = 6
        total = 6 + len(brew_params) + 1 + 2
        body = (
            bytes([0x0D, total - 1, 0x83, 0xF0, bev_id, 0x03])
            + bytes(brew_params)
            + bytes([6])
        )
        brew_cmd = body + self._crc16(body)

        # Pre-brew check (build a fake recipe for accessory check)
        fake_recipe = bytes([0xD0, 0, 0xA6, 0xF0, 1, bev_id, 28, acc, 0, 0])
        self._pre_brew_check(dsn, fake_recipe, beverage)

        _LOGGER.info("Brewing custom %s: %s", beverage, brew_cmd.hex())
        return self.send_command(dsn, brew_cmd)

    def _pre_brew_check(self, dsn: str, recipe: bytes, beverage_key: str) -> None:
        """Check machine state before brewing. Raises DeLonghiApiError on problems."""
        try:
            status = self.get_status(dsn)
        except (DeLonghiApiError, DeLonghiAuthError):
            return  # Can't check, proceed anyway

        # Check machine state
        machine_state = status.get("machine_state", "Unknown")
        if machine_state == "Off":
            raise DeLonghiApiError(
                f"Cannot brew {beverage_key}: machine is off. Power on first."
            )
        if machine_state == "Brewing":
            raise DeLonghiApiError(
                f"Cannot brew {beverage_key}: machine is already brewing."
            )
        if machine_state in ("Descaling", "Rinsing", "Going to sleep", "Turning On", "Heating"):
            raise DeLonghiApiError(
                f"Cannot brew {beverage_key}: machine is busy ({machine_state})."
            )

        # Check blocking alarms
        alarms = status.get("alarms", [])
        alarm_names = [a["name"] for a in alarms]

        blocking = {
            "Water Tank Empty",
            "Grounds Container Full",
            "Coffee Beans Empty",
            "Coffee Beans Empty 2",
            "Drip Tray Missing",
            "Tank In Position",
            "Hydraulic Problem",
            "Heater Probe Failure",
            "Infuser Motor Failure",
            "Steamer Probe Failure",
            "Bean Hopper Absent",
        }
        active_blocking = [a for a in alarm_names if a in blocking]
        if active_blocking:
            raise DeLonghiApiError(
                f"Cannot brew {beverage_key}: {', '.join(active_blocking)}"
            )

        # Check accessory requirement
        required_acc = self._get_recipe_accessory(recipe)
        if required_acc and required_acc > 1:
            try:
                monitor_prop = self.get_property(dsn, "d302_monitor_machine")
                monitor_val = monitor_prop.get("value")
                if monitor_val:
                    raw = base64.b64decode(monitor_val)
                    current_acc = raw[4] if len(raw) > 4 else 0
                    # Milk drinks need acc >= 2 (Latte Crema Hot/Cool)
                    # acc 0 = nothing, 1 = hot water spout
                    if current_acc < 2:
                        acc_names = {0: "nothing", 1: "hot water spout"}
                        raise DeLonghiApiError(
                            f"Cannot brew {beverage_key}: milk module required "
                            f"(current: {acc_names.get(current_acc, current_acc)})"
                        )
            except (KeyError, ValueError):
                pass

    @staticmethod
    def _get_recipe_accessory(recipe: bytes) -> int | None:
        """Extract ACCESSORIO(28) param from recipe if present."""
        raw = recipe[6:-2]
        big = {1, 9, 15}
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
        - End with profile_save = (1 << 2) | profile
        """
        if recipe[0] == 0x0D:
            return recipe

        bev_id = recipe[5]
        raw = recipe[6:-2]

        exclude: set[int] = {25}  # VISIBLE always excluded
        if is_iced or is_cold_brew:
            exclude.update({1, 9, 15})  # Exclude quantities

        brew_params = bytearray()
        i = 0
        while i < len(raw):
            pid = raw[i]
            if pid in cls._BIG_PARAMS and i + 2 < len(raw):
                if pid not in exclude:
                    brew_params += raw[i:i + 3]
                i += 3
            elif i + 1 < len(raw):
                if pid not in exclude:
                    brew_params += raw[i:i + 2]
                i += 2
            else:
                break

        if is_iced:
            brew_params += bytearray([31, 0])
        elif is_cold_brew:
            brew_params += bytearray([31, 3, 38, intensity])

        brew_params += bytearray([39, 1])  # RINSE=1

        total = 6 + len(brew_params) + 1 + 2
        body = (
            bytes([0x0D, total - 1, 0x83, 0xF0, bev_id, 0x03])
            + bytes(brew_params)
            + bytes([(1 << 2) | profile])  # profile_save
        )
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
                            name = data[offset:offset + 20].decode(
                                "utf-16-be", errors="replace"
                            ).replace("\x00", "")
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
                        name = data[:20].decode(
                            "utf-16-be", errors="replace"
                        ).replace("\x00", "")
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
        """Parse bean systems from pre-fetched properties."""
        beans: list[dict[str, Any]] = []

        for i in range(7):
            prop_name = f"d{250 + i}_beansystem_{i}"
            if prop_name in props:
                val = props[prop_name].get("value")
                if val:
                    try:
                        raw = base64.b64decode(val)
                        data = raw[5:-2]
                        text = data.decode("utf-16-be", errors="replace")
                        parts = [p for p in text.split("\x00") if p.strip()]
                        local_name = parts[0] if parts else f"Bean {i}"
                        english_name = parts[1] if len(parts) > 1 else local_name
                        beans.append({
                            "id": i,
                            "name": local_name,
                            "english_name": english_name,
                        })
                    except (ValueError, UnicodeDecodeError):
                        beans.append({"id": i, "name": f"Bean {i}", "english_name": f"Bean {i}"})

        return beans

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

        # Decode custom recipe names (d053_custom_name_13, d054_custom_name_46)
        for cprop in ("d053_custom_name_13", "d054_custom_name_46"):
            if cprop in props:
                val = props[cprop].get("value")
                if val:
                    try:
                        raw = base64.b64decode(val)
                        data = raw[6:-2] if len(raw) > 8 else raw
                        _LOGGER.debug("Custom names %s raw: %s", cprop, raw.hex())
                        # Same format as profiles: 22-byte stride (20 name + 2 meta)
                        for i in range(3):
                            offset = i * 22
                            if offset + 20 <= len(data):
                                name = data[offset:offset + 20].decode(
                                    "utf-16-be", errors="replace"
                                ).replace("\x00", "")
                                if name:
                                    _LOGGER.debug("Custom recipe %d: '%s'", i + (1 if "13" in cprop else 4), name)
                    except (ValueError, UnicodeDecodeError):
                        pass

        beverages: set[str] = set()
        for name in props:
            if "_rec_2_" in name:
                bev = name.split("_rec_2_", 1)[-1]
                if bev and not bev.isdigit():
                    beverages.add(bev)

        if not beverages:
            for name in props:
                if "_rec_1_" in name:
                    bev = name.split("_rec_1_", 1)[-1]
                    if bev and not bev.isdigit():
                        beverages.add(bev)

        if not beverages:
            for name in props:
                # Generic fallback: match any d{NNN}_rec_{N}_{beverage} pattern
                parts = name.split("_rec_", 1)
                if len(parts) == 2 and parts[0].startswith("d"):
                    rest = parts[1]
                    idx = rest.find("_")
                    if idx > 0:
                        bev = rest[idx + 1:]
                        if bev and not bev.isdigit():
                            beverages.add(bev)

        return sorted(beverages)
