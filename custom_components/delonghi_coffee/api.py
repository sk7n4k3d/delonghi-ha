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
    AYLA_APP_ID,
    AYLA_APP_SECRET,
    APP_SIGNATURE,
    CAPTURED_BREW_ESPRESSO,
    GIGYA_API_KEY,
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

        # Region-specific endpoints
        region_cfg = REGIONS.get(region, REGIONS["EU"])
        self._gigya_url: str = region_cfg["gigya_url"]
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
                    "app_id": AYLA_APP_ID,
                    "app_secret": AYLA_APP_SECRET,
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

    def _build_packet(self, ecam_bytes: bytes) -> str:
        """Build WiFi packet: ECAM + timestamp + app signature -> Base64."""
        ts = struct.pack(">I", int(time.time()))
        full = ecam_bytes + ts + APP_SIGNATURE
        return base64.b64encode(full).decode()

    @_retry
    def send_command(self, dsn: str, ecam_bytes: bytes) -> bool:
        """Send an ECAM command to the machine."""
        b64 = self._build_packet(ecam_bytes)
        resp = self._session.post(
            f"{self._ayla_ads}/apiv1/dsns/{dsn}/properties/app_data_request/datapoints.json",
            json={"datapoint": {"value": b64}},
            headers=self._headers(),
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code == 201:
            _LOGGER.info("Command sent: %s", ecam_bytes.hex())
            return True
        _LOGGER.error("Command failed: %s %s", resp.status_code, resp.text)
        return False

    @_retry
    def ping_connected(self, dsn: str) -> bool:
        """Send app_device_connected ping to force machine to push data updates."""
        ts = struct.pack(">I", int(time.time()))
        b64 = base64.b64encode(ts + APP_SIGNATURE).decode()
        resp = self._session.post(
            f"{self._ayla_ads}/apiv1/dsns/{dsn}/properties/app_device_connected/datapoints.json",
            json={"datapoint": {"value": b64}},
            headers=self._headers(),
            timeout=REQUEST_TIMEOUT,
        )
        return resp.status_code == 201

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
            monitor = self.get_property(dsn, "d302_monitor_machine")
            monitor_val = monitor.get("value")
            if monitor_val:
                raw = base64.b64decode(monitor_val)
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

        result["alarms"] = active_alarms
        return result

    def get_counters(self, dsn: str) -> dict[str, Any]:
        """Get beverage counters, maintenance stats, and JSON sub-counters."""
        props = self.get_properties(dsn)
        counters: dict[str, Any] = {}

        # Simple integer counters
        counter_map: dict[str, str] = {
            "d701_tot_bev_b": "total_beverages",
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

        return counters

    def brew_beverage(self, dsn: str, beverage_key: str) -> bool:
        """Brew a beverage by converting stored recipe to brew command.

        Recipe format (0xD0/0xA6) → Brew format (0x0D/0x83):
        1. Take bev_id from recipe[5]
        2. Take params from recipe[6:-2]
        3. Remove recipe marker bytes (0x19 0x01)
        4. Append brew suffix (0x27 0x01 0x06)
        5. Build: [0x0D][len][0x83][0xF0][bev_id][0x03][params][CRC-16]
        """
        props = self.get_properties(dsn)

        recipe_prop: dict[str, Any] | None = None
        for name, prop in props.items():
            if f"_rec_2_{beverage_key}" in name and prop.get("value"):
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

        brew_cmd = self._recipe_to_brew_command(recipe_data)
        _LOGGER.info("Brewing %s: %s", beverage_key, brew_cmd.hex())
        return self.send_command(dsn, brew_cmd)

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

    @classmethod
    def _recipe_to_brew_command(cls, recipe: bytes) -> bytes:
        """Convert stored recipe (0xD0/0xA6) to brew command (0x0D/0x83).

        Recipe: [0xD0][len][0xA6][flags][profile][bev_id][params][0x19 0x01 marker][more_params][CRC]
        Brew:   [0x0D][len][0x83][0xF0][bev_id][0x03][params_without_marker][0x27 0x01 0x06][CRC]
        """
        if recipe[0] == 0x0D:
            return recipe

        bev_id = recipe[5]
        params = bytearray(recipe[6:-2])

        # Remove recipe marker bytes (0x19 0x01)
        for j in range(len(params) - 1):
            if params[j] == 0x19 and params[j + 1] == 0x01:
                params = params[:j] + params[j + 2:]
                break

        # Append brew suffix
        params += bytearray([0x27, 0x01, 0x06])

        # Build command: len = total_size - 1
        brew_body = bytes([0x0D, 0x00, 0x83, 0xF0, bev_id, 0x03]) + bytes(params)
        total = len(brew_body) + 2  # +CRC
        brew_body = bytes([0x0D, total - 1, 0x83, 0xF0, bev_id, 0x03]) + bytes(params)

        return brew_body + cls._crc16(brew_body)

    def get_available_beverages(self, dsn: str) -> list[str]:
        """Get list of available beverage keys from device properties."""
        props = self.get_properties(dsn)
        beverages: set[str] = set()
        for name in props:
            # Match d1XX_rec_2_BEVERAGE pattern (profile 2 = user defaults)
            if "_rec_2_" in name:
                bev = name.split("_rec_2_", 1)[-1]
                beverages.add(bev)
        return sorted(beverages)
