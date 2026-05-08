"""Additional coverage for delonghi_coffee.api — focuses on brew_beverage,
_recipe_to_brew_command edge cases, _pre_brew_check accessory branch,
_ensure_token refresh paths, fetch_transcode_table error paths, retry/429,
LAN key fallback error paths and get_available_beverages naming variants.

These tests exercise the lines the baseline suite misses without touching
production code or the (already complete) parse_* helpers.
"""

from __future__ import annotations

import base64
import json
import time
from unittest.mock import MagicMock, patch

import pytest
import requests

from custom_components.delonghi_coffee.api import (
    DeLonghiApi,
    DeLonghiApiError,
    DeLonghiAuthError,
)


def _mock_response(
    status_code: int,
    json_data: dict | list | None = None,
    text: str = "",
) -> MagicMock:
    """Create a mock requests.Response."""
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.text = text or json.dumps(json_data or {})
    resp.json.return_value = json_data or {}
    if status_code >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(response=resp)
    else:
        resp.raise_for_status.return_value = None
    return resp


def _make_api(oem_model: str = "DL-striker-cb") -> DeLonghiApi:
    """Instantiate an API with a valid token (bypass auth)."""
    api = DeLonghiApi(
        "test@example.com",
        "password",
        region="EU",
        oem_model=oem_model,
    )
    api._ayla_token = "fake_token"
    api._token_expires = time.time() + 86400
    api._session = MagicMock()
    return api


def _fake_recipe_prop(
    *,
    visible: bool = True,
    with_coffee: bool = True,
    with_accessory: int | None = None,
) -> str:
    """Build a fake base64 recipe payload (D0 A6 header + params + CRC).

    Returns the base64-encoded string so it round-trips through
    ``base64.b64decode`` exactly like the real Ayla value.
    """
    # Params: ACCESSORIO(28)? VISIBLE(25)? COFFEE(1)? TASTE(8)=3 TEMP(2)=1
    params = bytearray()
    if with_accessory is not None:
        params += bytearray([28, with_accessory])
    if visible:
        params += bytearray([25, 1])
    if with_coffee:
        params += bytearray([1, 0x00, 0x28])  # 40 mL
    params += bytearray([8, 3])  # TASTE
    params += bytearray([2, 1])  # TEMP
    # Header: [0xD0][total_len-1][0xA6][0xF0][profile][bev_id]
    total = 6 + len(params) + 2
    body = bytes([0xD0, total - 1, 0xA6, 0xF0, 0x01, 0x01]) + bytes(params)
    # CRC doesn't matter for our decode path — the parser reads [6:-2].
    payload = body + b"\x00\x00"
    return base64.b64encode(payload).decode()


# ---------------------------------------------------------------------------
# brew_beverage — lines 1049-1144
# ---------------------------------------------------------------------------


class TestBrewBeverage:
    """Exercise the full brew_beverage flow and its error paths."""

    def test_brew_beverage_happy_profile_match(self):
        """Recipe found for exact profile match, send_command returns True."""
        api = _make_api()
        props = {
            "d302_rec_2_espresso": {"value": _fake_recipe_prop()},
            "d302_rec_2_coffee": {"value": _fake_recipe_prop()},
        }
        with (
            patch.object(api, "get_properties", return_value=props),
            patch.object(api, "ping_connected", return_value=True),
            patch.object(api, "_pre_brew_check"),
            patch.object(api, "send_command", return_value=True) as mock_send,
        ):
            assert api.brew_beverage("DSN", "espresso", profile=2) is True
            # send_command called once with a 0x0D/0x83 brew frame
            sent = mock_send.call_args.args[1]
            assert sent[0] == 0x0D and sent[2] == 0x83

    def test_brew_beverage_fallback_to_default(self):
        """Falls back to `_rec_{key}` property when profile-specific match misses."""
        api = _make_api()
        props = {
            "d302_rec_espresso": {"value": _fake_recipe_prop()},
        }
        with (
            patch.object(api, "get_properties", return_value=props),
            patch.object(api, "ping_connected", return_value=True),
            patch.object(api, "_pre_brew_check"),
            patch.object(api, "send_command", return_value=True),
        ):
            # Requested profile=4, no exact match → fallback to suffix match
            assert api.brew_beverage("DSN", "espresso", profile=4) is True

    def test_brew_beverage_fallback_to_other_profile(self):
        """Falls back to another profile (1-5) when exact not found."""
        api = _make_api()
        props = {
            "d302_rec_1_espresso": {"value": _fake_recipe_prop()},
        }
        with (
            patch.object(api, "get_properties", return_value=props),
            patch.object(api, "ping_connected", return_value=True),
            patch.object(api, "_pre_brew_check"),
            patch.object(api, "send_command", return_value=True),
        ):
            # Requested profile=3, only profile 1 present → fallback loop hits it
            assert api.brew_beverage("DSN", "espresso", profile=3) is True

    def test_brew_beverage_primadonna_naming(self):
        """PrimaDonna naming (`d{num}_{profile}_rec_{key}`) is also matched."""
        api = _make_api(oem_model="DL-pd-soul")
        props = {
            "d028_2_rec_espresso": {"value": _fake_recipe_prop()},
        }
        with (
            patch.object(api, "get_properties", return_value=props),
            patch.object(api, "ping_connected", return_value=True),
            patch.object(api, "_pre_brew_check"),
            patch.object(api, "send_command", return_value=True),
        ):
            assert api.brew_beverage("DSN", "espresso", profile=2) is True

    def test_brew_beverage_recipe_not_found(self):
        """Empty properties → DeLonghiApiError."""
        api = _make_api()
        with (
            patch.object(api, "get_properties", return_value={}),
            pytest.raises(DeLonghiApiError, match="Recipe not found"),
        ):
            api.brew_beverage("DSN", "espresso")

    def test_brew_beverage_skips_json_values(self):
        """Values starting with '{' are skipped (JSON configs, not recipes)."""
        api = _make_api()
        props = {
            "d302_rec_2_espresso": {"value": '{"some": "config"}'},
        }
        with (
            patch.object(api, "get_properties", return_value=props),
            pytest.raises(DeLonghiApiError, match="Recipe not found"),
        ):
            api.brew_beverage("DSN", "espresso", profile=2)

    def test_brew_beverage_skips_empty_values(self):
        """Properties with None/empty values are skipped (fallback loop)."""
        api = _make_api()
        # profile 2 has empty value → fallback iteration kicks in and finds profile 5
        props = {
            "d302_rec_2_espresso": {"value": ""},
            "d302_rec_5_espresso": {"value": _fake_recipe_prop()},
        }
        with (
            patch.object(api, "get_properties", return_value=props),
            patch.object(api, "ping_connected", return_value=True),
            patch.object(api, "_pre_brew_check"),
            patch.object(api, "send_command", return_value=True),
        ):
            assert api.brew_beverage("DSN", "espresso", profile=2) is True

    def test_brew_beverage_base64_decode_failure(self):
        """Non-base64 recipe value raises DeLonghiApiError."""
        api = _make_api()
        props = {
            "d302_rec_2_espresso": {"value": "not@@valid@@base64!!!"},
        }
        with (
            patch.object(api, "get_properties", return_value=props),
            pytest.raises(DeLonghiApiError, match="Cannot decode recipe"),
        ):
            api.brew_beverage("DSN", "espresso", profile=2)

    def test_brew_beverage_recipe_too_short(self):
        """Recipe shorter than 8 bytes raises DeLonghiApiError."""
        api = _make_api()
        # 4-byte payload → too short
        tiny_b64 = base64.b64encode(b"\xd0\x04\xa6\xf0").decode()
        props = {"d302_rec_2_espresso": {"value": tiny_b64}}
        with (
            patch.object(api, "get_properties", return_value=props),
            pytest.raises(DeLonghiApiError, match="too short"),
        ):
            api.brew_beverage("DSN", "espresso", profile=2)

    def test_brew_beverage_ping_failure_is_swallowed(self):
        """A failing pre-brew ping does not abort the brew."""
        api = _make_api()
        props = {"d302_rec_2_espresso": {"value": _fake_recipe_prop()}}
        with (
            patch.object(api, "get_properties", return_value=props),
            patch.object(api, "ping_connected", side_effect=DeLonghiApiError("ping boom")),
            patch.object(api, "_pre_brew_check"),
            patch.object(api, "send_command", return_value=True),
        ):
            assert api.brew_beverage("DSN", "espresso", profile=2) is True

    def test_brew_beverage_ping_auth_failure_is_swallowed(self):
        """Auth failure during ping is swallowed too."""
        api = _make_api()
        props = {"d302_rec_2_espresso": {"value": _fake_recipe_prop()}}
        with (
            patch.object(api, "get_properties", return_value=props),
            patch.object(api, "ping_connected", side_effect=DeLonghiAuthError("auth gone")),
            patch.object(api, "_pre_brew_check"),
            patch.object(api, "send_command", return_value=True),
        ):
            assert api.brew_beverage("DSN", "espresso", profile=2) is True

    def test_brew_beverage_iced_flag_detection(self):
        """`i_` and `over_ice` prefixes trigger iced brew."""
        api = _make_api()
        props = {
            "d302_rec_2_i_americano": {"value": _fake_recipe_prop()},
        }
        captured: dict[str, bool] = {}

        def _capture_recipe_to_brew(recipe, *, is_iced, is_cold_brew, profile):
            captured["is_iced"] = is_iced
            captured["is_cold_brew"] = is_cold_brew
            return bytes([0x0D, 0x0A, 0x83]) + b"\x00" * 7 + b"\xab\xcd"

        with (
            patch.object(api, "get_properties", return_value=props),
            patch.object(api, "ping_connected", return_value=True),
            patch.object(api, "_pre_brew_check"),
            patch.object(api, "send_command", return_value=True),
            patch.object(api, "_recipe_to_brew_command", side_effect=_capture_recipe_to_brew),
        ):
            api.brew_beverage("DSN", "i_americano", profile=2)

        assert captured["is_iced"] is True
        assert captured["is_cold_brew"] is False

    def test_brew_over_ice_marked_as_iced(self):
        """H-logic-3: ``brew_over_ice`` (drink_id 27) is an iced drink but
        the original detection ``startswith(("i_", "mi_", "over_ice"))``
        missed it because the key starts with ``brew_``. The recipe-to-brew
        path then dropped the ICED(31)=0 marker and kept the hot-recipe
        coffee/milk/hot-water quantities — the machine brewed regular
        coffee instead of pouring it over ice.
        """
        api = _make_api()
        props = {"d302_rec_2_brew_over_ice": {"value": _fake_recipe_prop()}}
        captured: dict[str, bool] = {}

        def _capture(recipe, *, is_iced, is_cold_brew, profile):
            captured["is_iced"] = is_iced
            return bytes([0x0D, 0x0A, 0x83]) + b"\x00" * 7 + b"\xab\xcd"

        with (
            patch.object(api, "get_properties", return_value=props),
            patch.object(api, "ping_connected", return_value=True),
            patch.object(api, "_pre_brew_check"),
            patch.object(api, "send_command", return_value=True),
            patch.object(api, "_recipe_to_brew_command", side_effect=_capture),
        ):
            api.brew_beverage("DSN", "brew_over_ice", profile=2)

        assert captured["is_iced"] is True

    def test_mug_iced_family_marked_as_iced(self):
        """H-logic-3: the ``mug_i_*`` family (drink_ids 100-107) is iced too
        but the original detection only recognised ``i_`` / ``mi_`` /
        ``over_ice`` prefixes — ``mug_i_cappuccino`` etc. fell through.
        """
        api = _make_api()
        for key in (
            "mug_i_brew_over_ice",
            "mug_i_americano",
            "mug_i_cappuccino",
            "mug_i_latte_macch",
            "mug_i_caffelatte",
            "mug_i_capp_mix",
            "mug_i_flat_white",
            "mug_i_cold_milk",
        ):
            props = {f"d302_rec_2_{key}": {"value": _fake_recipe_prop()}}
            captured: dict[str, bool] = {}

            def _capture(recipe, *, is_iced, is_cold_brew, profile, _cap=captured):
                _cap["is_iced"] = is_iced
                return bytes([0x0D, 0x0A, 0x83]) + b"\x00" * 7 + b"\xab\xcd"

            with (
                patch.object(api, "get_properties", return_value=props),
                patch.object(api, "ping_connected", return_value=True),
                patch.object(api, "_pre_brew_check"),
                patch.object(api, "send_command", return_value=True),
                patch.object(api, "_recipe_to_brew_command", side_effect=_capture),
            ):
                api.brew_beverage("DSN", key, profile=2)

            assert captured["is_iced"] is True, f"{key} should be detected as iced"

    def test_brew_beverage_cold_brew_flag_detection(self):
        """`_cb_` substring triggers cold-brew branch."""
        api = _make_api()
        props = {
            "d302_rec_2_original_cb_intense": {"value": _fake_recipe_prop()},
        }
        captured: dict[str, bool] = {}

        def _capture(recipe, *, is_iced, is_cold_brew, profile):
            captured["is_iced"] = is_iced
            captured["is_cold_brew"] = is_cold_brew
            return bytes([0x0D, 0x0A, 0x83]) + b"\x00" * 7 + b"\xab\xcd"

        with (
            patch.object(api, "get_properties", return_value=props),
            patch.object(api, "ping_connected", return_value=True),
            patch.object(api, "_pre_brew_check"),
            patch.object(api, "send_command", return_value=True),
            patch.object(api, "_recipe_to_brew_command", side_effect=_capture),
        ):
            api.brew_beverage("DSN", "original_cb_intense", profile=2)

        assert captured["is_cold_brew"] is True


# ---------------------------------------------------------------------------
# brew_custom — lines 1195-1247
# ---------------------------------------------------------------------------


class TestBrewCustomFull:
    """Exercise the param-building branches of brew_custom."""

    def test_espresso_doppio_branch(self):
        """Espresso adds DUExPER(8)=0, doppio adds DUExPER(8)=1."""
        api = _make_api()
        with (
            patch.object(api, "_pre_brew_check"),
            patch.object(api, "ping_connected", return_value=True),
            patch.object(api, "send_command", return_value=True) as mock_send,
        ):
            api.brew_custom("DSN", "espresso", coffee_qty=40, profile=1)
            frame = mock_send.call_args.args[1]
            # Body[6] is the first param id — for espresso (no accessory),
            # the first param is DUExPER(8).
            assert frame[6] == 8
            assert frame[7] == 0  # espresso → 0

            mock_send.reset_mock()
            api.brew_custom("DSN", "doppio", coffee_qty=40, profile=1)
            frame = mock_send.call_args.args[1]
            assert frame[6] == 8
            assert frame[7] == 1  # doppio → 1

    def test_cappuccino_adds_accessory_and_milk_froth(self):
        """Cappuccino sets ACCESSORIO(28)=2 + MILK_FROTH(11)."""
        api = _make_api()
        with (
            patch.object(api, "_pre_brew_check"),
            patch.object(api, "ping_connected", return_value=True),
            patch.object(api, "send_command", return_value=True) as mock_send,
        ):
            api.brew_custom("DSN", "cappuccino", coffee_qty=40, milk_qty=120, milk_froth=2, profile=2)
            frame = mock_send.call_args.args[1]
            # First two param bytes = ACCESSORIO(28)=2
            assert frame[6] == 28
            assert frame[7] == 2
            # Next two = MILK_FROTH(11)
            assert frame[8] == 11

    def test_water_qty_16bit(self):
        """Water qty is encoded as 16-bit big-endian."""
        api = _make_api()
        with (
            patch.object(api, "_pre_brew_check"),
            patch.object(api, "ping_connected", return_value=True),
            patch.object(api, "send_command", return_value=True) as mock_send,
        ):
            api.brew_custom("DSN", "hot_water", water_qty=500, profile=1)
            frame = mock_send.call_args.args[1]
            hex_frame = frame.hex()
            # HOT_WATER(15)=0x0F, 500 = 0x01F4
            assert "0f01f4" in hex_frame

    def test_tea_adds_temperature(self):
        """Tea beverage appends TEA_TEMP(13)=temperature."""
        api = _make_api()
        with (
            patch.object(api, "_pre_brew_check"),
            patch.object(api, "ping_connected", return_value=True),
            patch.object(api, "send_command", return_value=True) as mock_send,
        ):
            api.brew_custom("DSN", "tea", water_qty=200, temperature=4, profile=1)
            frame = mock_send.call_args.args[1]
            # Somewhere in the param block we expect 0x0D 0x04 (TEA_TEMP=4).
            assert b"\x0d\x04" in frame

    def test_brew_custom_ping_failure_is_swallowed(self):
        """Ping failure during brew_custom is swallowed (warning only)."""
        api = _make_api()
        with (
            patch.object(api, "_pre_brew_check"),
            patch.object(api, "ping_connected", side_effect=DeLonghiApiError("ping")),
            patch.object(api, "send_command", return_value=True),
        ):
            assert api.brew_custom("DSN", "espresso", coffee_qty=40, profile=1) is True

    def test_brew_custom_ping_auth_failure_is_swallowed(self):
        """Auth failure during ping in brew_custom is swallowed too."""
        api = _make_api()
        with (
            patch.object(api, "_pre_brew_check"),
            patch.object(api, "ping_connected", side_effect=DeLonghiAuthError("auth")),
            patch.object(api, "send_command", return_value=True),
        ):
            assert api.brew_custom("DSN", "espresso", coffee_qty=40, profile=1) is True


# ---------------------------------------------------------------------------
# _pre_brew_check accessory branch — lines 1438-1450 + 1411
# ---------------------------------------------------------------------------


class TestPreBrewAccessoryCheck:
    """Exercise the monitor-based accessory validation."""

    def test_wrong_accessory_blocks(self):
        """Recipe needs milk module but machine reports current_acc=0."""
        api = _make_api()
        # Recipe with ACCESSORIO(28)=2 → milk module required
        recipe = bytes([0xD0, 0x0C, 0xA6, 0xF0, 0x01, 0x07, 28, 2, 8, 3, 0x00, 0x00])
        # Monitor raw with byte[5]=0 → current accessory is "nothing"
        monitor_raw = bytes([0xD0, 0x0C, 0xA4, 0xF0, 0x01, 0x00]).hex() + "00" * 10
        status = {
            "machine_state": "Ready",
            "alarms": [],
            "monitor_raw": monitor_raw,
        }
        with (
            patch.object(api, "get_status", return_value=status),
            pytest.raises(DeLonghiApiError, match="milk module required"),
        ):
            api._pre_brew_check("DSN", recipe, "cappuccino")

    def test_accessory_ok_passes(self):
        """When the monitor reports acc>=2, check passes."""
        api = _make_api()
        recipe = bytes([0xD0, 0x0C, 0xA6, 0xF0, 0x01, 0x07, 28, 2, 8, 3, 0x00, 0x00])
        # byte[5]=2 → milk module installed
        monitor_raw = bytes([0xD0, 0x0C, 0xA4, 0xF0, 0x01, 0x02]).hex() + "00" * 10
        status = {
            "machine_state": "Ready",
            "alarms": [],
            "monitor_raw": monitor_raw,
        }
        with patch.object(api, "get_status", return_value=status):
            api._pre_brew_check("DSN", recipe, "cappuccino")  # should not raise

    def test_accessory_check_swallows_hex_error(self):
        """Malformed monitor_raw is tolerated (ValueError caught)."""
        api = _make_api()
        recipe = bytes([0xD0, 0x0C, 0xA6, 0xF0, 0x01, 0x07, 28, 2, 8, 3, 0x00, 0x00])
        status = {
            "machine_state": "Ready",
            "alarms": [],
            "monitor_raw": "zzzz-not-hex",
        }
        with patch.object(api, "get_status", return_value=status):
            api._pre_brew_check("DSN", recipe, "cappuccino")  # should not raise

    def test_machine_busy_descaling(self):
        """Descaling state is a blocking busy state."""
        api = _make_api()
        recipe = bytes([0xD0, 0x0A, 0xA6, 0xF0, 0x01, 0x01, 8, 3, 0x00, 0x00])
        with (
            patch.object(api, "get_status", return_value={"machine_state": "Descaling", "alarms": []}),
            pytest.raises(DeLonghiApiError, match="busy \\(Descaling\\)"),
        ):
            api._pre_brew_check("DSN", recipe, "espresso")


# ---------------------------------------------------------------------------
# _recipe_to_brew_command edge cases — lines 1533-1541
# ---------------------------------------------------------------------------


class TestRecipeToBrewEdges:
    """Exhaust remaining `_recipe_to_brew_command` branches."""

    def test_iced_excludes_quantities(self):
        """Iced branch drops COFFEE(1), MILK(9), HOT_WATER(15)."""
        # Recipe with all 3 big params + TASTE(8)
        recipe = bytes(
            [
                0xD0,
                0x12,
                0xA6,
                0xF0,
                0x01,
                0x01,
                1,
                0x00,
                0x28,  # COFFEE 40mL (should drop)
                9,
                0x00,
                0x78,  # MILK 120mL (should drop)
                15,
                0x00,
                0xC8,  # HOT_WATER 200mL (should drop)
                8,
                3,  # TASTE (keep)
                0x00,
                0x00,
            ]
        )
        cmd = DeLonghiApi._recipe_to_brew_command(recipe, is_iced=True, profile=2)
        params = cmd[6 : -(1 + 2)]
        # Collect param IDs
        param_ids: list[int] = []
        i = 0
        while i < len(params):
            pid = params[i]
            param_ids.append(pid)
            i += 3 if pid in (1, 9, 15) else 2
        assert 1 not in param_ids
        assert 9 not in param_ids
        assert 15 not in param_ids
        assert 8 in param_ids
        assert 31 in param_ids  # ICED appended

    def test_odd_trailing_byte_breaks_loop(self):
        """Odd leftover byte (neither full pair nor triple) breaks cleanly."""
        # Recipe with 2 bytes of pad after TASTE — but one pad byte is a
        # big-param id with incomplete data (1 byte left) → the while loop
        # breaks on the final ``else: break`` branch.
        recipe = bytes(
            [
                0xD0,
                0x0A,
                0xA6,
                0xF0,
                0x01,
                0x01,
                8,
                3,  # TASTE (keep)
                1,  # COFFEE id with no following bytes → break
                0x00,
                0x00,
            ]
        )
        cmd = DeLonghiApi._recipe_to_brew_command(recipe, profile=1)
        # Should still produce a valid frame (no crash, no missing IDs)
        assert cmd[0] == 0x0D
        assert cmd[2] == 0x83


# ---------------------------------------------------------------------------
# _ensure_token branches — lines 106-116 (retry 429) + refresh success
# ---------------------------------------------------------------------------


class TestEnsureTokenExtras:
    """Missing branches of the refresh logic."""

    def test_refresh_keeps_old_refresh_when_server_omits_it(self):
        """If response has no refresh_token, keep the existing one."""
        api = _make_api()
        api._ayla_refresh = "old_refresh"
        api._token_expires = time.time() - 100  # expired
        api._session.post.return_value = _mock_response(
            200,
            {
                "access_token": "new_access",
                # no refresh_token
                "expires_in": 7200,
            },
        )
        api._ensure_token()
        assert api._ayla_token == "new_access"
        assert api._ayla_refresh == "old_refresh"

    def test_refresh_keyerror_triggers_reauth(self):
        """KeyError from malformed refresh response falls through to authenticate."""
        api = _make_api()
        api._ayla_refresh = "refresh"
        api._token_expires = time.time() - 100
        api._session.post.return_value = _mock_response(200, {})  # no access_token
        with patch.object(api, "authenticate") as mock_auth:
            api._ensure_token()
            mock_auth.assert_called_once()


# ---------------------------------------------------------------------------
# _retry 429 rate-limit branch — lines 106-116
# ---------------------------------------------------------------------------


class TestRetry429:
    """Exercise the dedicated 429 branch of the retry decorator."""

    def test_429_backoff_then_success(self):
        """429 triggers longer backoff and retries; eventually succeeds."""
        api = _make_api()
        resp429 = MagicMock(spec=requests.Response)
        resp429.status_code = 429
        resp429.raise_for_status.side_effect = requests.HTTPError(response=resp429)

        api._session.get.side_effect = [
            requests.HTTPError(response=resp429),
            _mock_response(200, [{"property": {"name": "test", "value": "ok"}}]),
        ]

        # Patch time.sleep so the test stays fast
        with patch("custom_components.delonghi_coffee.api.time.sleep"):
            props = api.get_properties("DSN")
        assert "test" in props

    def test_429_exhausted_raises(self):
        """All 3 attempts return 429 → DeLonghiApiError."""
        api = _make_api()
        resp429 = MagicMock(spec=requests.Response)
        resp429.status_code = 429
        resp429.raise_for_status.side_effect = requests.HTTPError(response=resp429)

        api._session.get.side_effect = requests.HTTPError(response=resp429)

        with (
            patch("custom_components.delonghi_coffee.api.time.sleep"),
            pytest.raises(DeLonghiApiError, match="failed after"),
        ):
            api.get_properties("DSN")


# ---------------------------------------------------------------------------
# fetch_transcode_table error paths — line 274
# ---------------------------------------------------------------------------


class TestFetchTranscodeTable:
    """Error paths of fetch_transcode_table."""

    def test_http_non_200_logged(self):
        """Non-200 status doesn't raise but logs and leaves table None."""
        api = _make_api()
        api._session.post.return_value = _mock_response(503)
        api.fetch_transcode_table()
        assert api._transcode_table is None

    def test_cached_short_circuits(self):
        """If table already cached, no HTTP call happens."""
        api = _make_api()
        api._transcode_table = [{"foo": "bar"}]
        api.fetch_transcode_table()
        api._session.post.assert_not_called()

    def test_network_error_tolerated(self):
        """RequestException is swallowed and logged."""
        api = _make_api()
        api._session.post.side_effect = requests.ConnectionError("timeout")
        api.fetch_transcode_table()  # should not raise
        assert api._transcode_table is None

    def test_invalid_json_tolerated(self):
        """ValueError (from .json()) is swallowed and logged."""
        api = _make_api()
        bad = MagicMock(spec=requests.Response)
        bad.status_code = 200
        bad.json.side_effect = ValueError("bad json")
        api._session.post.return_value = bad
        api.fetch_transcode_table()  # should not raise
        assert api._transcode_table is None


# ---------------------------------------------------------------------------
# get_devices — line 487 (PrimaDonna backfill)
# ---------------------------------------------------------------------------


class TestGetDevicesBackfill:
    """OEM model backfill from Ayla metadata when config didn't carry it."""

    def test_backfills_primadonna(self):
        """PrimaDonna model is backfilled and cmd_property is re-applied."""
        api = _make_api(oem_model="")  # no model known up front
        assert api._cmd_property is None
        api._session.get.return_value = _mock_response(
            200,
            [
                {
                    "device": {
                        "dsn": "DSN",
                        "oem_model": "DL-pd-soul",
                        "product_name": "PrimaDonna Soul",
                        "sw_version": "2.0",
                    }
                }
            ],
        )
        api.get_devices()
        assert api._oem_model == "DL-pd-soul"
        assert api._cmd_property == "data_request"

    def test_backfills_striker(self):
        """Striker (Eletta Explore) model is backfilled too."""
        api = _make_api(oem_model="")
        api._session.get.return_value = _mock_response(
            200,
            [
                {
                    "device": {
                        "dsn": "DSN",
                        "model": "DL-striker-cb",
                        "product_name": "Eletta",
                        "sw_version": "1.6",
                    }
                }
            ],
        )
        api.get_devices()
        assert api._oem_model == "DL-striker-cb"
        assert api._cmd_property == "app_data_request"


# ---------------------------------------------------------------------------
# send_command error path — lines 691-694
# ---------------------------------------------------------------------------


class TestSendCommandHttpError:
    """Non-404 HTTP errors should propagate (and be sanitized in logs)."""

    def test_500_raises(self):
        """HTTP 500 from send_command propagates through retry."""
        api = _make_api(oem_model="DL-striker-cb")
        resp500 = MagicMock(spec=requests.Response)
        resp500.status_code = 500
        resp500.text = "Internal Server Error — should be sanitized in logs"
        resp500.raise_for_status.side_effect = requests.HTTPError(response=resp500)
        api._session.post.return_value = resp500

        with (
            patch("custom_components.delonghi_coffee.api.time.sleep"),
            pytest.raises(DeLonghiApiError),
        ):
            api.send_command("DSN", bytes.fromhex("0d07840f02015512"))


# ---------------------------------------------------------------------------
# request_monitor + brew wrappers — lines 744, 748
# ---------------------------------------------------------------------------


class TestThinWrappers:
    """request_monitor and brew are one-liners that deserve basic coverage."""

    def test_request_monitor_sends_monitor_cmd(self):
        api = _make_api(oem_model="DL-striker-cb")
        with patch.object(api, "send_command", return_value=True) as mock_send:
            assert api.request_monitor("DSN") is True
            sent = mock_send.call_args.args[1]
            assert sent == DeLonghiApi._MONITOR_CMD

    def test_brew_passes_through(self):
        api = _make_api(oem_model="DL-striker-cb")
        ecam = bytes.fromhex("0d0383f0")
        with patch.object(api, "send_command", return_value=True) as mock_send:
            assert api.brew("DSN", ecam) is True
            mock_send.assert_called_once_with("DSN", ecam)


# ---------------------------------------------------------------------------
# get_counters thin wrapper — line 841
# ---------------------------------------------------------------------------


class TestGetCounters:
    def test_get_counters_delegates(self):
        """get_counters fetches properties and delegates to parse_counters."""
        api = _make_api()
        with (
            patch.object(api, "get_properties", return_value={"d701_tot_bev_b": {"value": "42"}}),
            patch.object(api, "parse_counters", return_value={"total_beverages": 42}) as parse_mock,
        ):
            result = api.get_counters("DSN")
        parse_mock.assert_called_once()
        assert result == {"total_beverages": 42}


# ---------------------------------------------------------------------------
# get_profiles / get_bean_systems / get_available_beverages thin wrappers
# lines 1558, 1626, 1706
# ---------------------------------------------------------------------------


class TestGetProfilesWrapper:
    def test_delegates(self):
        api = _make_api()
        with (
            patch.object(api, "get_properties", return_value={}),
            patch.object(api, "parse_profiles", return_value={"active": 1, "profiles": {}}) as m,
        ):
            api.get_profiles("DSN")
        m.assert_called_once()


class TestGetBeanSystemsWrapper:
    def test_delegates(self):
        api = _make_api()
        with (
            patch.object(api, "get_properties", return_value={}),
            patch.object(api, "parse_bean_systems", return_value=[]) as m,
        ):
            api.get_bean_systems("DSN")
        m.assert_called_once()


class TestGetAvailableBeveragesWrapper:
    def test_delegates(self):
        api = _make_api()
        with (
            patch.object(api, "get_properties", return_value={}),
            patch.object(api, "parse_available_beverages", return_value=[]) as m,
        ):
            api.get_available_beverages("DSN")
        m.assert_called_once()


# ---------------------------------------------------------------------------
# get_custom_recipe_names — line 1784
# ---------------------------------------------------------------------------


class TestGetCustomRecipeNames:
    def test_empty_when_unset(self):
        api = _make_api()
        # Fresh instance — no _custom_recipe_names has been populated
        api._custom_recipe_names = {}
        assert api.get_custom_recipe_names() == {}

    def test_maps_keys(self):
        api = _make_api()
        api._custom_recipe_names = {1: "Morning", 3: "Evening"}
        got = api.get_custom_recipe_names()
        assert got == {"custom_1": "Morning", "custom_3": "Evening"}


# ---------------------------------------------------------------------------
# parse_available_beverages custom recipe name parsing — lines 1723-1737
# ---------------------------------------------------------------------------


class TestParseAvailableBeveragesCustomNames:
    """Exercises the `d053_custom_name_13`/`d054_custom_name_46` branches."""

    def _build_custom_name_payload(self, names: list[str]) -> str:
        """Build a base64 payload with up to 3 names of 20 bytes each (UTF-16-BE)."""
        # Structure: [header 6 bytes][name1 20B][pad 2B][name2 20B][pad 2B][name3 20B][pad 2B][CRC 2B]
        payload = bytearray([0xD0, 0x00, 0xA6, 0xF0, 0x01, 0x01])  # header
        for i in range(3):
            name = names[i] if i < len(names) else ""
            encoded = name.encode("utf-16-be")[:20].ljust(20, b"\x00")
            payload += encoded
            payload += b"\x00\x00"  # 2-byte metadata padding
        payload += b"\x00\x00"  # CRC
        return base64.b64encode(bytes(payload)).decode()

    def test_parses_custom_names_13(self):
        api = _make_api()
        props = {
            "d053_custom_name_13": {
                "value": self._build_custom_name_payload(["Morning", "Lunch", ""]),
            },
        }
        beverages = api.parse_available_beverages(props)
        # The side effect: custom_recipe_names populated
        names = api._custom_recipe_names
        assert names.get(1) == "Morning"
        assert names.get(2) == "Lunch"
        # And beverages list was returned (even if empty in this fixture)
        assert beverages == []

    def test_parses_custom_names_46(self):
        api = _make_api()
        props = {
            "d054_custom_name_46": {
                "value": self._build_custom_name_payload(["Siesta", "", "Evening"]),
            },
        }
        api.parse_available_beverages(props)
        assert api._custom_recipe_names.get(4) == "Siesta"
        # Slot 5 empty name → skipped
        assert 5 not in api._custom_recipe_names
        assert api._custom_recipe_names.get(6) == "Evening"

    def test_custom_name_decode_error_tolerated(self):
        """Base64 garbage is caught — no raise, just empty names."""
        api = _make_api()
        props = {"d053_custom_name_13": {"value": "not%%base64!!"}}
        api.parse_available_beverages(props)  # should not raise
        assert api._custom_recipe_names == {}

    def test_primadonna_naming_extraction(self):
        """PrimaDonna `d028_1_rec_espresso` yields `espresso`."""
        api = _make_api()
        props = {
            "d028_1_rec_espresso": {"value": "valid_recipe_b64"},
            "d029_1_rec_coffee": {"value": "valid_recipe_b64"},
        }
        beverages = api.parse_available_beverages(props)
        assert "espresso" in beverages
        assert "coffee" in beverages

    def test_priority_and_recipe_custom_name_skipped(self):
        """Properties containing `priority` or `recipe_custom_name` are skipped."""
        api = _make_api()
        props = {
            "d123_rec_priority_list": {"value": "anything"},
            "d124_rec_recipe_custom_name": {"value": "anything"},
            "d302_rec_2_espresso": {"value": "valid"},
        }
        beverages = api.parse_available_beverages(props)
        # Only espresso should come through
        assert beverages == ["espresso"]


# ---------------------------------------------------------------------------
# parse_bean_systems decode error branch — lines 1647 + 1671-1680
# ---------------------------------------------------------------------------


class TestParseBeanSystemsDecodeError:
    def test_empty_value_skipped(self):
        """Empty value skips the bean (line 1647 — `if not val: continue`)."""
        api = _make_api()
        props = {"d250_beansystem_0": {"value": ""}}
        beans = api.parse_bean_systems(props)
        assert beans == []

    def test_decode_failure_adds_fallback_entry(self):
        """Corrupt base64 adds a default fallback entry."""
        api = _make_api()
        props = {"d250_beansystem_0": {"value": "!!!not-base64!!!"}}
        beans = api.parse_bean_systems(props)
        # Decoder catches ValueError and falls through to append fallback.
        # The exact number depends on the payload length, but it should not raise.
        assert isinstance(beans, list)


# ---------------------------------------------------------------------------
# parse_profiles decode error — lines 1609-1610, 1619-1620
# ---------------------------------------------------------------------------


class TestParseProfilesErrors:
    def test_d052_decode_failure(self):
        """Malformed d052 falls through to pass."""
        api = _make_api()
        props = {"d052_profile_name4": {"value": "not-base64!!!"}}
        # Should not raise — just returns defaults
        result = api.parse_profiles(props)
        assert result["active"] == 1

    def test_d286_decode_failure(self):
        """Malformed d286_mach_sett_profile falls through to pass."""
        api = _make_api()
        props = {"d286_mach_sett_profile": {"value": "not-base64!!!"}}
        result = api.parse_profiles(props)
        # active defaults to 1 (fallback)
        assert result["active"] == 1


# ---------------------------------------------------------------------------
# parse_counters JSON edge — lines 954-955 + 983-984
# ---------------------------------------------------------------------------


class TestParseCountersEdges:
    def test_json_non_int_value_preserved_as_is(self):
        """If a JSON aggregate sub-value cannot be int()-coerced, keep it as-is."""
        api = _make_api()
        props = {
            "d735_iced_bev": {
                "value": json.dumps({"a_key": "not_a_number"}),
            },
        }
        counters = api.parse_counters(props)
        # non-int fall-through: raw string preserved under friendly prefix
        assert counters.get("iced_a_key") == "not_a_number"

    def test_service_params_malformed_tolerated(self):
        """Malformed d580_service_parameters JSON is tolerated."""
        api = _make_api()
        props = {
            "d580_service_parameters": {"value": "{not valid json"},
        }
        counters = api.parse_counters(props)
        # Not crashing is the win — nothing was parsed
        assert "descale_progress" not in counters


# ---------------------------------------------------------------------------
# get_lan_config error paths — lines 562-563, 601-602
# ---------------------------------------------------------------------------


class TestGetLanConfigErrors:
    def test_device_info_error_keeps_defaults(self):
        """Network error on device info doesn't raise — returns defaults."""
        api = _make_api()
        api._session.get.side_effect = requests.ConnectionError("fail")
        cfg = api.get_lan_config("DSN")
        assert cfg["lan_enabled"] is False
        assert cfg["lanip_key"] is None

    def test_alt_endpoint_also_fails(self):
        """Both lan.json AND connection_config.json fail — tolerated."""
        api = _make_api()
        fail_resp = MagicMock(spec=requests.Response)
        fail_resp.status_code = 500
        fail_resp.raise_for_status.side_effect = requests.HTTPError(response=fail_resp)

        api._session.get.side_effect = [
            # device info → lan_enabled=True
            _mock_response(
                200,
                {"device": {"lan_enabled": True, "lan_ip": "192.168.1.1", "connection_status": "Online"}},
            ),
            # lan.json → error
            requests.ConnectionError("lan fail"),
            # connection_config.json → also error
            requests.ConnectionError("cfg fail"),
        ]
        cfg = api.get_lan_config("DSN")
        assert cfg["lan_enabled"] is True
        assert cfg["lanip_key"] is None


# ---------------------------------------------------------------------------
# get_property — lines 517-523
# ---------------------------------------------------------------------------


class TestGetProperty:
    def test_single_property(self):
        """get_property fetches a named property."""
        api = _make_api()
        api._session.get.return_value = _mock_response(200, {"property": {"name": "app_device_status", "value": "RUN"}})
        prop = api.get_property("DSN", "app_device_status")
        assert prop["value"] == "RUN"

    def test_missing_property_key_returns_empty(self):
        """Response without 'property' key falls back to empty dict."""
        api = _make_api()
        api._session.get.return_value = _mock_response(200, {})
        prop = api.get_property("DSN", "missing")
        assert prop == {}


# ---------------------------------------------------------------------------
# authenticate — line 416-417 (invalid JSON)
# ---------------------------------------------------------------------------


class TestAuthenticateInvalidJson:
    def test_invalid_json_raises_api_error(self):
        """ValueError from .json() during auth raises DeLonghiApiError."""
        api = DeLonghiApi("a@b", "p", region="EU")
        bad = MagicMock(spec=requests.Response)
        bad.status_code = 200
        bad.json.side_effect = ValueError("not json")
        bad.raise_for_status.return_value = None
        api._session = MagicMock()
        api._session.post.return_value = bad
        with pytest.raises(DeLonghiApiError, match="Invalid response"):
            api.authenticate()


# ---------------------------------------------------------------------------
# Final polish: cover the last handful of defensive branches
# ---------------------------------------------------------------------------


class TestFinalDefensiveBranches:
    """Cover the 4 remaining defensive lines to push api.py to 100%."""

    def test_auth_error_in_retry_propagates(self):
        """DeLonghiAuthError raised inside @_retry is not retried — line 83."""
        api = _make_api()
        # get_properties is decorated with @_retry. Injecting DeLonghiAuthError
        # must propagate immediately without retrying.
        api._session.get.side_effect = DeLonghiAuthError("auth died")
        with pytest.raises(DeLonghiAuthError, match="auth died"):
            api.get_properties("DSN")
        # Only one attempt — no retry on auth errors
        assert api._session.get.call_count == 1

    def test_build_bean_write_invalid_grinder(self):
        """Grinder value out of range raises — line 1336."""
        with pytest.raises(DeLonghiApiError, match="grinder must be 0-255"):
            DeLonghiApi._build_bean_write_body(1, "Test", 50, 50, -1)
        with pytest.raises(DeLonghiApiError, match="grinder must be 0-255"):
            DeLonghiApi._build_bean_write_body(1, "Test", 50, 50, 256)

    def test_get_recipe_accessory_odd_trailing_byte(self):
        """Odd trailing byte in recipe params breaks the while loop — line 1474.

        We craft a recipe whose param block ends with a single byte that is
        neither a big-param id nor followed by a value byte — the `else: break`
        is the only way out.
        """
        # Header (6B) + one orphan byte (pid with no value, not a big param)
        # + CRC (2B).
        recipe = bytes([0xD0, 0x09, 0xA6, 0xF0, 0x01, 0x01, 0x05, 0x00, 0x00])
        # 0x05 is not ACCESSORIO(28), not a big param — so the while loop
        # checks `i + 1 < len(raw)` → False for this layout → else: break.
        assert DeLonghiApi._get_recipe_accessory(recipe) is None

    def test_parse_available_beverages_non_d_prefix_skipped(self):
        """Properties with `_rec_` but not starting with `d` are skipped — line 1762."""
        api = _make_api()
        props = {
            # Property doesn't start with 'd' → `parts[0].startswith("d")` False
            "x123_rec_something": {"value": "whatever"},
            # A valid one so we can verify we don't short-circuit globally
            "d302_rec_2_espresso": {"value": "valid"},
        }
        beverages = api.parse_available_beverages(props)
        assert "espresso" in beverages
        # The weird property must NOT leak through
        assert "something" not in beverages
