"""Test coordinator.py — DataUpdateCoordinator for De'Longhi Coffee.

Covers __init__, _detect_contentstack_pattern, _load_contentstack and all
the branches of _async_update_data (light poll, keepalive, full refresh,
monitor staleness, alarm suppression, pre-seeding of inverted bits,
diagnostic mode, error paths).
"""

import asyncio
from time import monotonic
from unittest.mock import MagicMock, patch

import pytest

from custom_components.delonghi_coffee import coordinator as coord_mod  # noqa: E402
from custom_components.delonghi_coffee.api import DeLonghiApiError, DeLonghiAuthError  # noqa: E402
from custom_components.delonghi_coffee.const import (  # noqa: E402
    FULL_REFRESH_INTERVAL,
    MQTT_KEEPALIVE_INTERVAL,
)
from custom_components.delonghi_coffee.coordinator import DeLonghiCoordinator  # noqa: E402


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_hass():
    """Return a hass stub whose async_add_executor_job runs the callable inline."""
    hass = MagicMock()

    async def _run_executor(func, *args, **kwargs):
        return func(*args, **kwargs)

    hass.async_add_executor_job = _run_executor
    return hass


def _make_api(**overrides):
    """Return a MagicMock api with sensible happy-path defaults.

    Each overridable attribute can be supplied via kwargs to customise a
    single branch without rebuilding the entire mock.
    """
    api = MagicMock()
    api.get_status = MagicMock(
        return_value={
            "machine_state": "Off",
            "profile": 0,
            "monitor_raw": None,
            "status": "RUN",
            "alarms": [],
            "alarm_word": None,
        }
    )
    api.parse_counters = MagicMock(return_value={"total_coffees": 12})
    api.parse_profiles = MagicMock(return_value={"active": 1, "profiles": {1: {}}})
    api.parse_bean_systems = MagicMock(return_value=[])
    api.parse_bean_system_par = MagicMock(return_value={})
    api.parse_available_beverages = MagicMock(return_value=["espresso"])
    api.get_custom_recipe_names = MagicMock(return_value={"custom_1": "Test"})
    api.get_properties = MagicMock(return_value={"d270_serialnumber": {"value": "ECAM61075MBXYZ"}})
    api.get_lan_config = MagicMock(return_value={"ip": "10.0.0.1"})
    api.ping_connected = MagicMock(return_value=True)
    api.request_monitor = MagicMock()
    api.rate_tracker = MagicMock(current_rate=0, total_calls=0)
    api._oem_model = "DL-striker-cb"
    api.model_info = {}
    for k, v in overrides.items():
        setattr(api, k, v)
    return api


def _make_coord(api=None, dsn="DSN-1"):
    hass = _make_hass()
    api = api or _make_api()
    coord = DeLonghiCoordinator(hass, api, dsn)
    # _GenericBase stub in conftest does not assign self.hass; production
    # HA's DataUpdateCoordinator would. Set it manually so async_add_executor_job works.
    coord.hass = hass
    return coord, hass, api


# ───────────────────────────── __init__ ─────────────────────────────


class TestInit:
    def test_defaults_populated(self):
        coord, _, api = _make_coord()
        assert coord.api is api
        assert coord.dsn == "DSN-1"
        assert coord.beverages == []
        assert coord._last_full_refresh == 0
        assert coord._last_keepalive == 0
        assert coord._cached_counters == {}
        assert coord._cached_profiles == {}
        assert coord._cached_beans == []
        assert coord._cached_bean_system_par == {}
        assert coord._lan_config is None
        assert coord.selected_profile is None
        assert coord.seen_alarm_bits == set()
        assert coord.custom_recipe_names == {}
        assert coord.drink_catalog == {}
        assert coord.bean_adapt is None
        assert coord.coffee_beans == []
        assert coord._contentstack_loaded is False
        assert coord._last_monitor_raw is None
        assert coord._monitor_stale_count == 0
        assert coord._monitor_stale_timeout == 2700
        assert coord.diagnostic_mode is False
        assert coord._last_diagnostic == {}
        assert coord._last_all_props == {}
        # _monitor_last_changed should be a monotonic() value — positive
        assert coord._monitor_last_changed > 0


# ────────────────────── _detect_contentstack_pattern ─────────────────


class TestDetectContentstackPattern:
    def test_serial_wins(self):
        coord, _, _ = _make_coord()
        coord._last_all_props = {"d270_serialnumber": {"value": "ECAM61075MBXXX"}}
        assert coord._detect_contentstack_pattern() == "ECAM61075"

    def test_serial_non_dict_skipped(self):
        """Non-dict serial_prop must not raise, falls through to model_info/oem."""
        coord, _, api = _make_coord()
        api._oem_model = "DL-striker-cb"
        coord._last_all_props = {"d270_serialnumber": "not a dict"}
        assert coord._detect_contentstack_pattern() == "ECAM63050"

    def test_serial_empty_value_falls_through(self):
        coord, _, api = _make_coord()
        api._oem_model = "DL-striker-best"
        coord._last_all_props = {"d270_serialnumber": {"value": ""}}
        assert coord._detect_contentstack_pattern() == "ECAM63075"

    def test_model_info_appmodelid(self):
        coord, _, api = _make_coord(api=_make_api())
        api._oem_model = "unknown-oem"
        api.model_info = {"appModelId": "ECAM61075-SOMETHING"}
        coord._last_all_props = {}
        assert coord._detect_contentstack_pattern() == "ECAM61075"

    def test_model_info_product_code(self):
        coord, _, api = _make_coord(api=_make_api())
        api._oem_model = "unknown-oem"
        api.model_info = {"product_code": "ECAM22110"}
        assert coord._detect_contentstack_pattern() == "ECAM22110"

    def test_model_info_name_with_punctuation(self):
        """'ECAM610.75' → 'ECAM61075' (dots stripped)."""
        coord, _, api = _make_coord(api=_make_api())
        api._oem_model = "unknown-oem"
        api.model_info = {"name": "ECAM610.75"}
        assert coord._detect_contentstack_pattern() == "ECAM61075"

    def test_model_info_non_string_value_skipped(self):
        """Non-string fields must not raise; falls through to OEM."""
        coord, _, api = _make_coord(api=_make_api())
        api._oem_model = "DL-striker-cb"
        api.model_info = {"appModelId": 12345}  # int, not a string
        assert coord._detect_contentstack_pattern() == "ECAM63050"

    def test_oem_map_striker_cb(self):
        coord, _, api = _make_coord(api=_make_api())
        api._oem_model = "DL-striker-cb"
        api.model_info = {}
        assert coord._detect_contentstack_pattern() == "ECAM63050"

    def test_oem_map_striker_best(self):
        coord, _, api = _make_coord(api=_make_api())
        api._oem_model = "DL-striker-best"
        api.model_info = {}
        assert coord._detect_contentstack_pattern() == "ECAM63075"

    def test_no_match_returns_empty(self):
        coord, _, api = _make_coord(api=_make_api())
        api._oem_model = "DL-something-unmapped"
        api.model_info = {}
        coord._last_all_props = {}
        assert coord._detect_contentstack_pattern() == ""

    def test_empty_oem_returns_empty(self):
        coord, _, api = _make_coord(api=_make_api())
        api._oem_model = ""
        api.model_info = {}
        coord._last_all_props = {}
        assert coord._detect_contentstack_pattern() == ""

    def test_none_oem_returns_empty(self):
        coord, _, api = _make_coord(api=_make_api())
        api._oem_model = None
        api.model_info = {}
        coord._last_all_props = {}
        assert coord._detect_contentstack_pattern() == ""


# ───────────────────────── _load_contentstack ────────────────────────


class TestLoadContentstack:
    def test_happy_path(self):
        coord, _, _ = _make_coord()
        coord._last_all_props = {"d270_serialnumber": {"value": "ECAM63050FOO"}}
        with (
            patch.object(coord_mod, "fetch_drink_catalog", return_value={1: {"name": "Espresso"}}) as m_dc,
            patch.object(coord_mod, "fetch_bean_adapt", return_value={"bean_types": []}) as m_ba,
            patch.object(coord_mod, "fetch_coffee_beans", return_value=[{"id": "x"}]) as m_cb,
        ):
            _run(coord._load_contentstack())
            assert coord.drink_catalog == {1: {"name": "Espresso"}}
            assert coord.bean_adapt == {"bean_types": []}
            assert coord.coffee_beans == [{"id": "x"}]
            assert coord._contentstack_loaded is True
            m_dc.assert_called_once()
            m_ba.assert_called_once()
            m_cb.assert_called_once()

    def test_unknown_model_skips(self):
        """No serial/model_info/oem → marks loaded and returns early."""
        coord, _, api = _make_coord()
        api._oem_model = ""
        api.model_info = {}
        coord._last_all_props = {}
        with (
            patch.object(coord_mod, "fetch_drink_catalog") as m_dc,
            patch.object(coord_mod, "fetch_bean_adapt") as m_ba,
            patch.object(coord_mod, "fetch_coffee_beans") as m_cb,
        ):
            _run(coord._load_contentstack())
            assert coord._contentstack_loaded is True
            m_dc.assert_not_called()
            m_ba.assert_not_called()
            m_cb.assert_not_called()

    def test_unknown_model_logs_with_missing_props(self):
        """Debug log path: _last_all_props empty AND oem empty."""
        coord, _, api = _make_coord()
        api._oem_model = None  # the `or "?"` fallback path
        coord._last_all_props = {}
        _run(coord._load_contentstack())
        assert coord._contentstack_loaded is True

    def test_exception_swallowed(self):
        """A fetcher blowing up must not flip _contentstack_loaded — retry next refresh."""
        coord, _, _ = _make_coord()
        coord._last_all_props = {"d270_serialnumber": {"value": "ECAM63050FOO"}}
        with (
            patch.object(coord_mod, "fetch_drink_catalog", side_effect=RuntimeError("boom")),
            patch.object(coord_mod, "fetch_bean_adapt"),
            patch.object(coord_mod, "fetch_coffee_beans"),
        ):
            _run(coord._load_contentstack())
            assert coord._contentstack_loaded is False

    def test_model_name_hint_via_model_info(self):
        """When model_info.name is 'ECAM610.75', second arg passed is 'ECAM61075'."""
        coord, _, api = _make_coord()
        coord._last_all_props = {"d270_serialnumber": {"value": "ECAM61075XXX"}}
        api.model_info = {"name": "ECAM610.75"}
        with (
            patch.object(coord_mod, "fetch_drink_catalog", return_value={}) as m_dc,
            patch.object(coord_mod, "fetch_bean_adapt", return_value=None),
            patch.object(coord_mod, "fetch_coffee_beans", return_value=[]),
        ):
            _run(coord._load_contentstack())
            # first positional = pattern (ECAM61075), second = model_name (ECAM61075)
            args, _ = m_dc.call_args
            assert args[0] == "ECAM61075"
            assert args[1] == "ECAM61075"

    def test_model_name_hint_non_string_name(self):
        """Non-string model_info.name must not raise."""
        coord, _, api = _make_coord()
        coord._last_all_props = {"d270_serialnumber": {"value": "ECAM61075XXX"}}
        api.model_info = {"name": 12345}  # not a string
        with (
            patch.object(coord_mod, "fetch_drink_catalog", return_value={}) as m_dc,
            patch.object(coord_mod, "fetch_bean_adapt", return_value=None),
            patch.object(coord_mod, "fetch_coffee_beans", return_value=[]),
        ):
            _run(coord._load_contentstack())
            args, _ = m_dc.call_args
            assert args[1] == ""  # model_name hint stays empty


# ───────────────────── _async_update_data happy paths ────────────────


class TestAsyncUpdateDataLight:
    """Light-poll only tests — no keepalive, no full refresh required."""

    def _setup_light_poll(self):
        """Helper: coord where both timers are fresh so neither kicks in."""
        coord, _, api = _make_coord()
        now = monotonic()
        coord._last_full_refresh = now  # fresh
        coord._last_keepalive = now  # fresh
        return coord, api

    def test_returns_expected_dict(self):
        coord, api = self._setup_light_poll()
        api.get_status.return_value = {
            "machine_state": "Ready",
            "profile": 2,
            "monitor_raw": "abc",
            "status": "RUN",
            "alarms": [],
            "alarm_word": None,
        }
        result = _run(coord._async_update_data())
        assert result["machine_state"] == "Ready"
        assert result["profile"] == 2
        assert result["status"] == "RUN"
        assert coord.selected_profile == 2
        assert result["monitor_stale"] is False
        # Full-refresh cached dicts must be default empty
        assert result["counters"] == {}
        assert result["beverages"] == []
        # No fetch_properties/ping in the light-poll path
        api.get_properties.assert_not_called()
        api.ping_connected.assert_not_called()
        api.request_monitor.assert_not_called()

    def test_selected_profile_not_overwritten_by_zero(self):
        coord, api = self._setup_light_poll()
        coord.selected_profile = 3  # already set
        api.get_status.return_value = {
            "machine_state": "Ready",
            "profile": 0,  # monitor reports no active profile
            "monitor_raw": "xyz",
            "status": "RUN",
        }
        _run(coord._async_update_data())
        assert coord.selected_profile == 3  # unchanged


class TestAsyncUpdateDataKeepalive:
    """Keepalive branch — full refresh NOT needed."""

    def _setup_keepalive_needed(self):
        coord, _, api = _make_coord()
        now = monotonic()
        coord._last_full_refresh = now  # full NOT needed
        coord._last_keepalive = 0  # keepalive overdue
        return coord, api, now

    def test_ping_connected_ok(self):
        coord, api, _ = self._setup_keepalive_needed()
        api.ping_connected.return_value = True
        _run(coord._async_update_data())
        api.ping_connected.assert_called_once_with("DSN-1")
        api.request_monitor.assert_not_called()
        assert coord._last_keepalive > 0

    def test_ping_unsupported_falls_back_to_request_monitor(self):
        coord, api, _ = self._setup_keepalive_needed()
        api.ping_connected.return_value = False
        _run(coord._async_update_data())
        api.ping_connected.assert_called_once()
        api.request_monitor.assert_called_once_with("DSN-1")
        assert coord._last_keepalive > 0

    def test_keepalive_api_error_swallowed(self):
        coord, api, _ = self._setup_keepalive_needed()
        api.ping_connected.side_effect = DeLonghiApiError("nope")
        before = coord._last_keepalive
        _run(coord._async_update_data())
        # keepalive timestamp must NOT advance on failure
        assert coord._last_keepalive == before

    def test_keepalive_auth_error_swallowed(self):
        coord, api, _ = self._setup_keepalive_needed()
        api.ping_connected.side_effect = DeLonghiAuthError("expired")
        before = coord._last_keepalive
        _run(coord._async_update_data())
        assert coord._last_keepalive == before

    def test_keepalive_skipped_when_full_refresh_due(self):
        """need_keepalive and need_full both True → keepalive branch skipped; full path runs."""
        coord, _, api = _make_coord()
        coord._last_full_refresh = 0
        coord._last_keepalive = 0
        _run(coord._async_update_data())
        # In full path, ping_connected is called ONCE by the full-refresh wake
        assert api.ping_connected.call_count == 1
        # full refresh finished → timestamps updated
        assert coord._last_full_refresh > 0
        assert coord._last_keepalive > 0


class TestAsyncUpdateDataFullRefresh:
    """Full-refresh branch — get_properties + parsers + LAN config + ContentStack."""

    def test_full_refresh_fetches_and_parses(self):
        coord, _, api = _make_coord()
        coord._last_full_refresh = 0  # force full refresh
        with (
            patch.object(coord_mod, "fetch_drink_catalog", return_value={1: {}}),
            patch.object(coord_mod, "fetch_bean_adapt", return_value={}),
            patch.object(coord_mod, "fetch_coffee_beans", return_value=[]),
        ):
            result = _run(coord._async_update_data())
        api.get_properties.assert_called_once_with("DSN-1")
        api.parse_counters.assert_called_once()
        api.parse_profiles.assert_called_once()
        api.parse_bean_systems.assert_called_once()
        api.parse_bean_system_par.assert_called_once()
        api.parse_available_beverages.assert_called_once()
        api.get_custom_recipe_names.assert_called_once()
        api.get_lan_config.assert_called_once_with("DSN-1")
        assert coord.beverages == ["espresso"]
        assert coord.custom_recipe_names == {"custom_1": "Test"}
        assert coord._lan_config == {"ip": "10.0.0.1"}
        assert coord._contentstack_loaded is True
        assert coord._last_full_refresh > 0
        assert coord._last_keepalive > 0
        assert result["counters"] == {"total_coffees": 12}
        assert result["beverages"] == ["espresso"]
        assert result["active_profile"] == 1
        assert result["lan_config"] == {"ip": "10.0.0.1"}

    def test_full_refresh_wake_ping_unsupported_falls_back(self):
        """Full refresh: when ping returns False, request_monitor is called."""
        coord, _, api = _make_coord()
        api.ping_connected.return_value = False
        coord._last_full_refresh = 0
        with (
            patch.object(coord_mod, "fetch_drink_catalog", return_value={}),
            patch.object(coord_mod, "fetch_bean_adapt", return_value={}),
            patch.object(coord_mod, "fetch_coffee_beans", return_value=[]),
        ):
            _run(coord._async_update_data())
        api.ping_connected.assert_called_once_with("DSN-1")
        api.request_monitor.assert_called_once_with("DSN-1")

    def test_full_refresh_wake_failure_swallowed(self):
        """Ping raising DeLonghiApiError during full-refresh wake does not abort refresh."""
        coord, _, api = _make_coord()
        api.ping_connected.side_effect = DeLonghiApiError("dead")
        coord._last_full_refresh = 0
        with (
            patch.object(coord_mod, "fetch_drink_catalog", return_value={}),
            patch.object(coord_mod, "fetch_bean_adapt", return_value={}),
            patch.object(coord_mod, "fetch_coffee_beans", return_value=[]),
        ):
            result = _run(coord._async_update_data())
        # Refresh continues despite wake failure
        api.get_properties.assert_called_once()
        assert result is not None

    def test_full_refresh_wake_auth_error_swallowed(self):
        coord, _, api = _make_coord()
        api.ping_connected.side_effect = DeLonghiAuthError("token")
        coord._last_full_refresh = 0
        with (
            patch.object(coord_mod, "fetch_drink_catalog", return_value={}),
            patch.object(coord_mod, "fetch_bean_adapt", return_value={}),
            patch.object(coord_mod, "fetch_coffee_beans", return_value=[]),
        ):
            _run(coord._async_update_data())
        api.get_properties.assert_called_once()

    def test_lan_config_fetched_only_once(self):
        """Second full refresh must NOT re-fetch LAN config."""
        coord, _, api = _make_coord()
        coord._last_full_refresh = 0
        with (
            patch.object(coord_mod, "fetch_drink_catalog", return_value={}),
            patch.object(coord_mod, "fetch_bean_adapt", return_value={}),
            patch.object(coord_mod, "fetch_coffee_beans", return_value=[]),
        ):
            _run(coord._async_update_data())
            assert api.get_lan_config.call_count == 1
            # Force another full refresh
            coord._last_full_refresh = 0
            _run(coord._async_update_data())
        assert api.get_lan_config.call_count == 1  # still only once

    def test_contentstack_loaded_only_once(self):
        """Second full refresh must not re-trigger _load_contentstack."""
        coord, _, _ = _make_coord()
        coord._last_full_refresh = 0
        with (
            patch.object(coord_mod, "fetch_drink_catalog", return_value={}) as m_dc,
            patch.object(coord_mod, "fetch_bean_adapt", return_value={}),
            patch.object(coord_mod, "fetch_coffee_beans", return_value=[]),
        ):
            _run(coord._async_update_data())
            assert m_dc.call_count == 1
            coord._last_full_refresh = 0
            _run(coord._async_update_data())
        assert m_dc.call_count == 1  # still only once

    def test_beverages_not_reparsed_when_already_populated(self):
        coord, _, api = _make_coord()
        coord._last_full_refresh = 0
        coord.beverages = ["already_here"]
        with (
            patch.object(coord_mod, "fetch_drink_catalog", return_value={}),
            patch.object(coord_mod, "fetch_bean_adapt", return_value={}),
            patch.object(coord_mod, "fetch_coffee_beans", return_value=[]),
        ):
            _run(coord._async_update_data())
        api.parse_available_beverages.assert_not_called()
        api.get_custom_recipe_names.assert_not_called()
        assert coord.beverages == ["already_here"]


# ─────────────────── monitor staleness / alarm suppression ───────────


class TestMonitorStaleness:
    def test_stale_count_increments_when_raw_unchanged(self):
        coord, _, api = _make_coord()
        coord._last_full_refresh = monotonic()  # skip full
        coord._last_keepalive = monotonic()  # skip keepalive
        api.get_status.return_value = {
            "machine_state": "Ready",
            "profile": 0,
            "monitor_raw": "SAME",
            "status": "RUN",
        }
        _run(coord._async_update_data())
        assert coord._monitor_stale_count == 0  # first call: _last_monitor_raw was None
        _run(coord._async_update_data())
        assert coord._monitor_stale_count == 1  # same raw, incremented
        _run(coord._async_update_data())
        assert coord._monitor_stale_count == 2

    def test_stale_count_resets_when_raw_changes(self):
        coord, _, api = _make_coord()
        coord._last_full_refresh = monotonic()
        coord._last_keepalive = monotonic()
        api.get_status.return_value = {
            "machine_state": "Ready",
            "profile": 0,
            "monitor_raw": "AAA",
            "status": "RUN",
        }
        _run(coord._async_update_data())
        _run(coord._async_update_data())  # stale_count -> 1
        api.get_status.return_value = {
            "machine_state": "Ready",
            "profile": 0,
            "monitor_raw": "BBB",  # changed
            "status": "RUN",
        }
        _run(coord._async_update_data())
        assert coord._monitor_stale_count == 0

    def test_monitor_timeout_forces_state_off_and_suppresses_alarms(self):
        coord, _, api = _make_coord()
        coord._last_full_refresh = monotonic()
        coord._last_keepalive = monotonic()
        # _last_monitor_raw must already match incoming monitor_raw so the
        # else-branch (which would reset _monitor_last_changed = monotonic())
        # is NOT taken; only then does our artificially-old timestamp stick.
        coord._last_monitor_raw = "same"
        coord._monitor_last_changed = monotonic() - 9999
        api.get_status.return_value = {
            "machine_state": "Ready",
            "profile": 0,
            "monitor_raw": "same",
            "status": "RUN",
            "alarms": [{"name": "Tank"}],
            "alarm_word": 0x1234,
        }
        result = _run(coord._async_update_data())
        assert result["machine_state"] == "Off"
        assert result["alarms"] == []
        assert result["alarm_word"] is None
        assert result["monitor_stale"] is True

    def test_no_cloud_suppresses_alarms(self):
        coord, _, api = _make_coord()
        coord._last_full_refresh = monotonic()
        coord._last_keepalive = monotonic()
        api.get_status.return_value = {
            "machine_state": "Ready",
            "profile": 0,
            "monitor_raw": "abc",
            "status": "UNKNOWN",  # no cloud
            "alarms": [{"name": "Tank"}],
            "alarm_word": 0x42,
        }
        result = _run(coord._async_update_data())
        assert result["alarms"] == []
        assert result["alarm_word"] is None
        # machine_state unchanged (monitor not yet timed out)
        assert result["machine_state"] == "Ready"

    def test_no_cloud_but_no_alarms_passes_through(self):
        """status=UNKNOWN + empty alarm list: suppress branch not taken."""
        coord, _, api = _make_coord()
        coord._last_full_refresh = monotonic()
        coord._last_keepalive = monotonic()
        api.get_status.return_value = {
            "machine_state": "Ready",
            "profile": 0,
            "monitor_raw": "abc",
            "status": "UNKNOWN",
            "alarms": [],
            "alarm_word": 0,
        }
        result = _run(coord._async_update_data())
        assert result["alarms"] == []
        assert result["alarm_word"] == 0

    def test_machine_state_unknown_not_overridden(self):
        coord, _, api = _make_coord()
        coord._last_full_refresh = monotonic()
        coord._last_keepalive = monotonic()
        coord._monitor_last_changed = monotonic() - 9999  # timed out
        api.get_status.return_value = {
            "machine_state": "Unknown",  # stays as-is
            "profile": 0,
            "monitor_raw": "abc",
            "status": "RUN",
        }
        result = _run(coord._async_update_data())
        assert result["machine_state"] == "Unknown"


# ─────────────────── pre-seed inverted alarm bits ────────────────────


class TestPreseedAlarmBits:
    def test_bit_13_seeded_when_ready(self):
        coord, _, api = _make_coord()
        coord._last_full_refresh = monotonic()
        coord._last_keepalive = monotonic()
        api.get_status.return_value = {
            "machine_state": "Ready",
            "profile": 0,
            "monitor_raw": "abc",
            "status": "RUN",
            "alarms": [],
            "alarm_word": (1 << 13),
        }
        _run(coord._async_update_data())
        assert 13 in coord.seen_alarm_bits
        assert 18 not in coord.seen_alarm_bits

    def test_both_bits_seeded_when_brewing(self):
        coord, _, api = _make_coord()
        coord._last_full_refresh = monotonic()
        coord._last_keepalive = monotonic()
        api.get_status.return_value = {
            "machine_state": "Brewing",
            "profile": 0,
            "monitor_raw": "abc",
            "status": "RUN",
            "alarm_word": (1 << 13) | (1 << 18),
        }
        _run(coord._async_update_data())
        assert coord.seen_alarm_bits == {13, 18}

    def test_heating_state_triggers_seed(self):
        coord, _, api = _make_coord()
        coord._last_full_refresh = monotonic()
        coord._last_keepalive = monotonic()
        api.get_status.return_value = {
            "machine_state": "Heating",
            "profile": 0,
            "monitor_raw": "abc",
            "status": "RUN",
            "alarm_word": (1 << 18),
        }
        _run(coord._async_update_data())
        assert 18 in coord.seen_alarm_bits

    def test_not_seeded_when_state_off(self):
        coord, _, api = _make_coord()
        coord._last_full_refresh = monotonic()
        coord._last_keepalive = monotonic()
        api.get_status.return_value = {
            "machine_state": "Off",
            "profile": 0,
            "monitor_raw": "abc",
            "status": "RUN",
            "alarm_word": (1 << 13) | (1 << 18),
        }
        _run(coord._async_update_data())
        assert coord.seen_alarm_bits == set()

    def test_not_seeded_when_alarm_word_none(self):
        coord, _, api = _make_coord()
        coord._last_full_refresh = monotonic()
        coord._last_keepalive = monotonic()
        api.get_status.return_value = {
            "machine_state": "Ready",
            "profile": 0,
            "monitor_raw": "abc",
            "status": "RUN",
            "alarm_word": None,
        }
        _run(coord._async_update_data())
        assert coord.seen_alarm_bits == set()


# ──────────────────────── diagnostic mode ─────────────────────────────


class TestDiagnosticMode:
    def test_diagnostic_dump_built_when_enabled(self):
        coord, _, api = _make_coord()
        coord._last_full_refresh = monotonic()
        coord._last_keepalive = monotonic()
        coord.diagnostic_mode = True
        coord._last_all_props = {"d270_serialnumber": {"value": "ECAM"}}
        with patch.object(coord_mod, "get_diagnostic_dump", return_value={"dump": "ok"}) as m:
            result = _run(coord._async_update_data())
        m.assert_called_once()
        assert coord._last_diagnostic == {"dump": "ok"}
        assert result["diagnostic"] == {"dump": "ok"}

    def test_diagnostic_dump_not_built_when_disabled(self):
        coord, _, api = _make_coord()
        coord._last_full_refresh = monotonic()
        coord._last_keepalive = monotonic()
        coord.diagnostic_mode = False
        coord._last_all_props = {"d270_serialnumber": {"value": "ECAM"}}
        with patch.object(coord_mod, "get_diagnostic_dump") as m:
            result = _run(coord._async_update_data())
        m.assert_not_called()
        assert result["diagnostic"] == {}

    def test_diagnostic_skipped_when_no_props_yet(self):
        """Diagnostic mode ON but never ran full refresh — _last_all_props empty."""
        coord, _, api = _make_coord()
        coord._last_full_refresh = monotonic()
        coord._last_keepalive = monotonic()
        coord.diagnostic_mode = True
        coord._last_all_props = {}
        with patch.object(coord_mod, "get_diagnostic_dump") as m:
            _run(coord._async_update_data())
        m.assert_not_called()


# ────────────────────────── error paths ──────────────────────────────


class TestErrorPaths:
    def test_auth_error_raises_update_failed(self):
        from homeassistant.helpers.update_coordinator import UpdateFailed

        coord, _, api = _make_coord()
        api.get_status.side_effect = DeLonghiAuthError("bad")
        with pytest.raises(UpdateFailed, match="Authentication error"):
            _run(coord._async_update_data())

    def test_api_error_raises_update_failed(self):
        from homeassistant.helpers.update_coordinator import UpdateFailed

        coord, _, api = _make_coord()
        api.get_status.side_effect = DeLonghiApiError("timeout")
        with pytest.raises(UpdateFailed, match="Error fetching data"):
            _run(coord._async_update_data())

    def test_generic_exception_raises_update_failed(self):
        from homeassistant.helpers.update_coordinator import UpdateFailed

        coord, _, api = _make_coord()
        api.get_status.side_effect = ValueError("weird")
        with pytest.raises(UpdateFailed, match="Unexpected error"):
            _run(coord._async_update_data())


# ─────────────────────── intervals sanity ────────────────────────────


class TestIntervalsConstants:
    """Guardrail: if someone flips these constants without updating tests,
    the assumption that need_full/need_keepalive can be toggled purely via
    setting `_last_full_refresh = 0` breaks."""

    def test_full_refresh_interval_reasonable(self):
        assert FULL_REFRESH_INTERVAL >= 60  # at least a minute

    def test_mqtt_keepalive_interval_reasonable(self):
        assert MQTT_KEEPALIVE_INTERVAL >= 60


@pytest.fixture(autouse=True)
def _ensure_event_loop():
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError("closed")
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    yield
