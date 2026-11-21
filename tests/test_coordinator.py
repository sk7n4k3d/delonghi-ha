"""Test coordinator.py — DataUpdateCoordinator for De'Longhi Coffee.

Covers __init__, _detect_contentstack_pattern, _load_contentstack and all
the branches of _async_update_data (light poll, keepalive, full refresh,
monitor staleness, alarm suppression, pre-seeding of inverted bits,
diagnostic mode, error paths).
"""

import asyncio
from time import monotonic
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import requests

from custom_components.delonghi_coffee import coordinator as coord_mod  # noqa: E402
from custom_components.delonghi_coffee.api import (  # noqa: E402
    DeLonghiApi,
    DeLonghiApiError,
    DeLonghiAuthError,
    DeLonghiUnsupportedError,
)
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

    def _create_task(coro, *_args, **_kwargs):
        return asyncio.ensure_future(coro)

    hass.async_add_executor_job = _run_executor
    # Issue #84: the brew path arms a background verify task. Schedule it for
    # real so its coroutine is awaited (and cancelled when the loop closes)
    # instead of leaking an "never awaited" warning.
    hass.async_create_background_task = _create_task
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
    # Issue #46 added a 2s pause before the single retry of a failed fetch.
    # Collapse it so the error-path tests stay instant; the delay itself is
    # covered by TestTransientRetry.test_retry_pause_is_bounded.
    coord._transient_retry_delay = 0.0
    return coord, hass, api


def _make_lan_server(alive: bool = True):
    """LAN server double: session present, liveness scripted."""
    server = MagicMock()
    server.session = object()
    server.is_session_alive = MagicMock(return_value=alive)
    server.enqueue_command = AsyncMock()
    return server


def _lan_monitor_coord(frame_b64: str, alive: bool = True):
    """Coordinator whose machine pushes ``frame_b64`` on the LAN socket."""
    coord, _, api = _make_coord()
    now = monotonic()
    coord._last_full_refresh = now
    coord._last_keepalive = now
    # Use the real parser on the mock so LAN frames decode exactly as in prod.
    api.parse_monitor_value = DeLonghiApi.parse_monitor_value
    coord._lan_server = _make_lan_server(alive)
    coord._lan_properties = {"d302_monitor": frame_b64}
    return coord, api


# ───────────────────────────── __init__ ─────────────────────────────


class TestInit:
    def test_config_entry_forwarded_to_base(self):
        """Audit 2026-11-05 (L4): the coffee reauth escalation ("audit C1")
        rides on self.config_entry — it must come from the explicit kwarg,
        not HA's current_entry ContextVar."""
        entry = MagicMock()
        coord = DeLonghiCoordinator(_make_hass(), _make_api(), "DSN-1", config_entry=entry)
        assert coord.init_kwargs.get("config_entry") is entry

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
        assert coord._auto_profile is None
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

    # ── Issue #10: PrimaDonna Soul "millcore" firmware support ─────────
    #
    # @dalodzik beta.9 log shows:
    #   ContentStack: cannot determine ECAM model
    #   (oem=DL-millcore, serial=0BuhDwDNMjE3MDU1WloyNTA3MDEzMDEzNAD9pg==)
    #
    # Decoded the serial decodes to a binary envelope with payload
    # "217055ZZ25070130134" — SKU 217055 = PrimaDonna Soul, ECAM family
    # ECAM61075. The detector must:
    #   (a) decode the binary serial and look up the SKU,
    #   (b) recognise DL-millcore as a PrimaDonna Soul OEM family,
    # so that the user no longer sees "cannot determine ECAM model".

    def test_serial_binary_envelope_pd_soul_sku_217055(self):
        """Real serial from @dalodzik (issue #10) → SKU lookup → ECAM61075."""
        coord, _, api = _make_coord(api=_make_api())
        api._oem_model = "DL-millcore"
        api.model_info = {}
        coord._last_all_props = {"d270_serialnumber": {"value": "0BuhDwDNMjE3MDU1WloyNTA3MDEzMDEzNAD9pg=="}}
        assert coord._detect_contentstack_pattern() == "ECAM61075"

    def test_oem_map_millcore_pd_soul(self):
        """DL-millcore (PrimaDonna Soul) without serial → ECAM61075 family."""
        coord, _, api = _make_coord(api=_make_api())
        api._oem_model = "DL-millcore"
        api.model_info = {}
        coord._last_all_props = {}
        assert coord._detect_contentstack_pattern() == "ECAM61075"


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
        # No explicit choice: the monitor byte is only the AUTOMATIC default, it
        # never becomes an explicit selection (issue #78 / PR #79 review).
        assert coord.selected_profile is None
        assert coord._auto_profile == 2
        assert coord.resolve_profile() == 2
        assert result["monitor_stale"] is False
        # Full-refresh cached dicts must be default empty
        assert result["counters"] == {}
        assert result["beverages"] == []
        # No fetch_properties/ping in the light-poll path
        api.get_properties.assert_not_called()
        api.ping_connected.assert_not_called()
        api.request_monitor.assert_not_called()

    def test_data_exposes_monitor_raw_and_ts(self):
        """Issue #47: the power-on confirmation reads the frame and its embedded
        clock straight off ``coordinator.data`` — both must be plumbed through."""
        coord, api = self._setup_light_poll()
        api.get_status.return_value = {
            "machine_state": "Off",
            "profile": 0,
            "monitor_raw": "d012750f010100000007000001000000003ae46ad0aae1",
            "monitor_ts": 1792060129,
            "status": "RUN",
            "alarms": [],
            "alarm_word": None,
        }
        result = _run(coord._async_update_data())
        assert result["monitor_raw"] == "d012750f010100000007000001000000003ae46ad0aae1"
        assert result["monitor_ts"] == 1792060129

    def test_data_monitor_keys_are_none_without_a_monitor(self):
        """The keys always exist so the switch never has to guess."""
        coord, _ = self._setup_light_poll()
        result = _run(coord._async_update_data())
        assert result["monitor_raw"] is None
        assert result["monitor_ts"] is None

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

    def test_user_profile_choice_not_stomped_by_monitor(self):
        """F-COORD-01: a user-selected profile must survive subsequent polls
        even when the monitor reports a different (>0) active profile."""
        coord, api = self._setup_light_poll()
        coord.selected_profile = 3  # explicit user click via the select entity
        api.get_status.return_value = {
            "machine_state": "Ready",
            "profile": 1,  # monitor lags / reports last-brewed profile
            "monitor_raw": "xyz",
            "status": "RUN",
        }
        result = _run(coord._async_update_data())
        assert coord.selected_profile == 3  # user choice preserved
        # the raw monitor value is still exposed in the data dict for fallbacks
        assert result["profile"] == 1


class TestAsyncUpdateDataKeepalive:
    """Keepalive branch — full refresh NOT needed."""

    def _setup_keepalive_needed(self):
        coord, _, api = _make_coord()
        now = monotonic()
        coord._last_full_refresh = now  # full NOT needed
        coord._last_keepalive = -MQTT_KEEPALIVE_INTERVAL - 1  # keepalive overdue (CI-safe)
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
        coord._last_full_refresh = -FULL_REFRESH_INTERVAL - 1
        coord._last_keepalive = -MQTT_KEEPALIVE_INTERVAL - 1
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
        coord._last_full_refresh = -FULL_REFRESH_INTERVAL - 1  # force full refresh (CI-safe: monotonic() may be small)
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
        # Issue #90: the masking streak rides the published payload, not just
        # the coordinator attribute — the sensor reads it from data.
        assert "baseline_masking" in result
        assert result["counters"] == {"total_coffees": 12}
        assert result["beverages"] == ["espresso"]
        assert result["active_profile"] == 1
        assert result["lan_config"] == {"ip": "10.0.0.1"}

    @pytest.mark.parametrize("failure", ["lan_config", "result_build"])
    def test_failed_full_refresh_does_not_commit_counter_observation(self, failure):
        from homeassistant.helpers.update_coordinator import UpdateFailed

        coord, _, api = _make_coord()
        coord._contentstack_loaded = True
        _run(coord.local_baseline.async_set_many({"espresso": 30}))
        due = -FULL_REFRESH_INTERVAL - 1
        coord._last_full_refresh = due
        coord._cached_counters = {"espresso": 20}
        api.parse_counters.return_value = {"espresso": 21}
        tracker = api.rate_tracker
        if failure == "lan_config":
            api.get_lan_config.side_effect = AttributeError("invalid response")
        else:
            api.rate_tracker = None

        for _ in range(3):
            with pytest.raises(UpdateFailed, match="Unexpected error"):
                _run(coord._async_update_data())
            assert coord._last_full_refresh == due
            assert coord._cached_counters == {"espresso": 20}
            assert coord._baseline_mask_streak == {}

        api.get_lan_config.side_effect = None
        api.rate_tracker = tracker
        result = _run(coord._async_update_data())
        assert result["counters"] == {"espresso": 21}
        assert result["baseline_masking"] == {"espresso": 1}
        assert coord._last_full_refresh > due
        assert _run(coord._async_update_data())["baseline_masking"] == {"espresso": 1}

    @pytest.mark.parametrize("clear_then_readd", [False, True])
    def test_in_flight_full_refresh_does_not_age_an_edited_baseline(self, clear_then_readd):
        async def exercise():
            coord, _, api = _make_coord()
            due = -FULL_REFRESH_INTERVAL - 1
            coord._last_full_refresh = due
            coord._contentstack_loaded = True
            await coord.local_baseline.async_set_many({"espresso": 30, "tea": 15})
            api.parse_counters.return_value = {"espresso": 20, "tea": 1}
            coord.data = await coord._async_update_data()
            assert coord.data["baseline_masking"] == {"espresso": 1, "tea": 1}

            # Pause the next refresh after parsing its cloud observation.
            coord._last_full_refresh = due
            coord._contentstack_loaded = False
            sampled = asyncio.Event()
            finish = asyncio.Event()

            async def pause():
                sampled.set()
                await finish.wait()

            coord._load_contentstack = pause
            refreshing = asyncio.create_task(coord._async_update_data())
            await sampled.wait()
            try:
                if clear_then_readd:
                    await coord.local_baseline.async_clear(["espresso"])
                    await coord.local_baseline.async_set_many({"espresso": 30})
                else:
                    await coord.local_baseline.async_set_many({"espresso": 31})
                assert coord.data["baseline_masking"] == {"tea": 1}
            finally:
                finish.set()
                coord.data = await refreshing
            assert coord.data["baseline_masking"] == {"tea": 2}

            coord._last_full_refresh = due
            coord.data = await coord._async_update_data()
            assert coord.data["baseline_masking"] == {"espresso": 1, "tea": 3}

        _run(exercise())

    def test_full_refresh_wake_ping_unsupported_falls_back(self):
        """Full refresh: when ping returns False, request_monitor is called."""
        coord, _, api = _make_coord()
        api.ping_connected.return_value = False
        coord._last_full_refresh = -FULL_REFRESH_INTERVAL - 1
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
        coord._last_full_refresh = -FULL_REFRESH_INTERVAL - 1
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
        coord._last_full_refresh = -FULL_REFRESH_INTERVAL - 1
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
        coord._last_full_refresh = -FULL_REFRESH_INTERVAL - 1
        with (
            patch.object(coord_mod, "fetch_drink_catalog", return_value={}),
            patch.object(coord_mod, "fetch_bean_adapt", return_value={}),
            patch.object(coord_mod, "fetch_coffee_beans", return_value=[]),
        ):
            _run(coord._async_update_data())
            assert api.get_lan_config.call_count == 1
            # Force another full refresh
            coord._last_full_refresh = -FULL_REFRESH_INTERVAL - 1
            _run(coord._async_update_data())
        assert api.get_lan_config.call_count == 1  # still only once

    def test_contentstack_loaded_only_once(self):
        """Second full refresh must not re-trigger _load_contentstack."""
        coord, _, _ = _make_coord()
        coord._last_full_refresh = -FULL_REFRESH_INTERVAL - 1
        with (
            patch.object(coord_mod, "fetch_drink_catalog", return_value={}) as m_dc,
            patch.object(coord_mod, "fetch_bean_adapt", return_value={}),
            patch.object(coord_mod, "fetch_coffee_beans", return_value=[]),
        ):
            _run(coord._async_update_data())
            assert m_dc.call_count == 1
            coord._last_full_refresh = -FULL_REFRESH_INTERVAL - 1
            _run(coord._async_update_data())
        assert m_dc.call_count == 1  # still only once

    def test_beverages_not_reparsed_when_already_populated(self):
        coord, _, api = _make_coord()
        coord._last_full_refresh = -FULL_REFRESH_INTERVAL - 1
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

    def test_stale_monitor_with_cloud_sync_does_not_force_off(self):
        """Cloud session in SYNC = MQTT keep-alive dead. The coordinator must NOT
        rebrand machine_state="Ready" → "Off" in that branch — there is *no
        evidence either way* about the physical machine. Pretending to know
        misleads the switch entity, which then logs a fake "monitor confirmed
        Off state" right after a turn_off and the user thinks the command went
        through (it didn't, the cloud accepted but never relayed).

        Live trace 2026-05-08 on Bastien's machine: cloud=SYNC, monitor stale
        961 min, monitor raw `d012750f0101000400070000010000000097d169fb9d12`
        with `machine_state=Ready`. Coordinator was overriding to "Off" and
        the user was told it's off — physically it was still on.

        Off-rebrand is only safe when cloud=RUN (live MQTT, idle = plausible).
        """
        coord, _, api = _make_coord()
        coord._last_full_refresh = monotonic()
        coord._last_keepalive = monotonic()
        coord._last_monitor_raw = "same"
        coord._monitor_last_changed = monotonic() - 9999
        api.get_status.return_value = {
            "machine_state": "Ready",
            "profile": 1,
            "monitor_raw": "same",
            "status": "SYNC",  # ← MQTT zombie, cloud doesn't have fresh state
            "alarms": [{"name": "Descale Needed"}],
            "alarm_word": 0x10004,
        }
        result = _run(coord._async_update_data())
        # state preserved — the stale monitor still says Ready, cloud can't
        # disprove it. Do not invent an "Off" claim.
        assert result["machine_state"] == "Ready"
        assert result["monitor_stale"] is True
        # Alarms still suppressed — they're stale data, can't be trusted —
        # but that's the existing alarm suppression branch (already covered).
        assert result["alarms"] == []

    def test_stale_monitor_with_cloud_run_still_forces_off(self):
        """Regression guard for the existing heuristic — cloud=RUN + stale =
        idle machine, "Off" rebrand stays safe. Mirrors the earlier
        test_monitor_timeout_forces_state_off_and_suppresses_alarms but is
        kept here as the explicit positive case for the cloud_status gate."""
        coord, _, api = _make_coord()
        coord._last_full_refresh = monotonic()
        coord._last_keepalive = monotonic()
        coord._last_monitor_raw = "same"
        coord._monitor_last_changed = monotonic() - 9999
        api.get_status.return_value = {
            "machine_state": "Ready",
            "profile": 1,
            "monitor_raw": "same",
            "status": "RUN",  # ← live cloud session, monitor idle = machine off
            "alarms": [],
            "alarm_word": 0,
        }
        result = _run(coord._async_update_data())
        assert result["machine_state"] == "Off"
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
        # Audit C1: auth errors now surface as ConfigEntryAuthFailed so HA
        # opens the reauth flow instead of looping on UpdateFailed.
        from homeassistant.exceptions import ConfigEntryAuthFailed

        coord, _, api = _make_coord()
        api.get_status.side_effect = DeLonghiAuthError("bad")
        with pytest.raises(ConfigEntryAuthFailed, match="Authentication error"):
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


class TestFastPollWindow:
    """v1.6.0-beta.14: brew button presses must trigger a temporary 5s
    polling interval so transient machine_state values (Brewing,
    Frothing milk, Rinsing) are observable. Without this the default
    60s polling misses every brew cycle entirely."""

    def test_default_interval_is_normal(self):
        coord, _, _ = _make_coord()
        # The cached "normal" interval is the 60s baseline. update_interval
        # itself lives on DataUpdateCoordinator and may be stubbed in tests.
        assert coord._normal_update_interval.total_seconds() == 60
        assert coord._fast_poll_until is None

    def test_request_fast_poll_shrinks_interval(self):
        coord, _, _ = _make_coord()
        coord.request_fast_poll(duration_s=90.0, interval_s=5.0)
        assert coord.update_interval.total_seconds() == 5
        assert coord._fast_poll_until is not None

    def test_request_fast_poll_extends_existing_window(self):
        coord, _, _ = _make_coord()
        coord.request_fast_poll(duration_s=30.0, interval_s=5.0)
        first_deadline = coord._fast_poll_until
        coord.request_fast_poll(duration_s=120.0, interval_s=5.0)
        second_deadline = coord._fast_poll_until
        assert second_deadline > first_deadline
        # Interval still 5s
        assert coord.update_interval.total_seconds() == 5

    def test_request_fast_poll_rejects_zero_or_negative(self):
        coord, _, _ = _make_coord()
        coord.request_fast_poll(duration_s=0, interval_s=5)
        assert coord._fast_poll_until is None
        coord.request_fast_poll(duration_s=10, interval_s=0)
        assert coord._fast_poll_until is None

    def test_window_expiry_reverts_interval(self):
        coord, _, api = _make_coord()
        # Arm a tiny window then force-expire it
        coord.request_fast_poll(duration_s=1.0, interval_s=5.0)
        # Stub the expiry by pushing the deadline into the past
        coord._fast_poll_until = monotonic() - 1.0

        # Run one update cycle — the cleanup branch must restore 60s
        from custom_components.delonghi_coffee.const import SCAN_INTERVAL_SECONDS

        _run(coord._async_update_data())
        assert coord.update_interval.total_seconds() == SCAN_INTERVAL_SECONDS
        assert coord._fast_poll_until is None


# ───────────────── issue #46: transient cloud errors ────────────────


class TestTransientRetry:
    """One hiccup must not turn every entity unavailable for a poll cycle.

    Before the fix a single ``requests`` ConnectionError propagated straight
    out of ``_async_update_data`` as UpdateFailed (~1.1x/day in the reporter's
    logs). The coordinator now retries the failed fetch once after a short
    pause and only gives up when that second attempt fails too.

    The retry lives on the full-refresh ``get_properties`` fetch — the only
    cloud call in the cycle that can still raise. ``api.get_status`` swallows
    transport errors internally (its ``@_retry``-wrapped ``get_properties``
    call ends as a swallowed ``DeLonghiApiError``) and returns
    ``status=UNKNOWN``, so wrapping it would be dead code (audit #46).
    """

    def _full_refresh(self, coord):
        """Force the next poll down the full-refresh path."""
        coord._last_full_refresh = -coord_mod.FULL_REFRESH_INTERVAL - 1
        coord._last_keepalive = monotonic()

    def test_retry_pause_is_bounded(self):
        assert 0 < coord_mod.TRANSIENT_RETRY_DELAY_SECONDS <= 5

    def test_status_is_fetched_directly(self):
        """get_status is called bare — no retry wrap (it never raises in prod)."""
        coord, _, api = _make_coord()
        coord._last_full_refresh = monotonic()
        coord._last_keepalive = monotonic()
        api.get_status.side_effect = DeLonghiApiError("Connection aborted")
        # The swallowed-error contract means prod get_status returns UNKNOWN
        # instead of raising; if a mock does raise, it maps straight to
        # UpdateFailed with exactly one call (no retry).
        from homeassistant.helpers.update_coordinator import UpdateFailed

        with pytest.raises(UpdateFailed, match="Error fetching data"):
            _run(coord._async_update_data())
        assert api.get_status.call_count == 1

    def test_requests_error_is_retried(self):
        """The literal shape from the issue log: requests.ConnectionError."""
        coord, _, api = _make_coord()
        self._full_refresh(coord)
        api.get_properties.side_effect = [
            requests.ConnectionError("HTTPSConnectionPool host=adla-ayla-com"),
            {"d270_serialnumber": {"value": "ECAM61075MBXYZ"}},
        ]
        _run(coord._async_update_data())
        assert api.get_properties.call_count == 2
        assert coord._last_full_refresh > 0

    def test_auth_error_is_not_retried(self):
        """Auth semantics are unchanged: straight to ConfigEntryAuthFailed.

        DeLonghiAuthError is the one error api.get_status can still raise in
        production (it is not a DeLonghiApiError subclass, so get_status's
        swallow does not catch it) — and it must never be retried.
        """
        from homeassistant.exceptions import ConfigEntryAuthFailed

        coord, _, api = _make_coord()
        api.get_status.side_effect = DeLonghiAuthError("expired")
        with pytest.raises(ConfigEntryAuthFailed):
            _run(coord._async_update_data())
        assert api.get_status.call_count == 1

    def test_unsupported_error_is_not_retried(self):
        """A definitive 404 (DeLonghiUnsupportedError) gains nothing from a retry."""
        from homeassistant.helpers.update_coordinator import UpdateFailed

        coord, _, api = _make_coord()
        self._full_refresh(coord)
        api.get_properties.side_effect = DeLonghiUnsupportedError("no such property")
        with pytest.raises(UpdateFailed):
            _run(coord._async_update_data())
        assert api.get_properties.call_count == 1

    def test_keepalive_ping_timeout_is_not_fatal(self):
        """Issue #73: a raw Ayla ReadTimeout from the keepalive ping must not raise
        UpdateFailed. The status was already read this cycle; the ping is best-effort.
        This is the literal shape from the reporter's log (last frame
        'ping_connected: trying …' then 'Unexpected error during coordinator refresh')."""
        coord, _, api = _make_coord()
        coord._last_full_refresh = monotonic()  # fresh → not a full refresh
        coord._last_keepalive = -coord_mod.MQTT_KEEPALIVE_INTERVAL - 1  # keepalive due
        api.get_status.return_value = {
            "machine_state": "Ready",
            "profile": 1,
            "monitor_raw": "abc",
            "status": "RUN",
            "alarms": [],
            "alarm_word": None,
        }
        api.ping_connected.side_effect = requests.ReadTimeout("HTTPSConnectionPool read timeout")
        result = _run(coord._async_update_data())  # must NOT raise
        assert result["machine_state"] == "Ready"  # status preserved despite ping timeout
        assert coord._keepalive_failures == 1  # counted as a keepalive miss, not fatal

    def test_full_refresh_wake_ping_timeout_still_fetches_properties(self):
        """On the full-refresh wake path a ping timeout must not abort the cycle:
        the all-properties GET still runs and the refresh succeeds."""
        coord, _, api = _make_coord()
        self._full_refresh(coord)
        api.ping_connected.side_effect = requests.ReadTimeout("HTTPSConnectionPool read timeout")
        api.get_properties.return_value = {"d270_serialnumber": {"value": "ECAM61075MBXYZ"}}
        result = _run(coord._async_update_data())  # must NOT raise
        assert api.get_properties.call_count >= 1  # properties fetch still happened
        assert result is not None

    def test_full_refresh_wake_timeout_does_not_reset_keepalive_freshness(self):
        """Issue #73 review (numanx11): a full refresh whose wake ping times out must
        NOT mark the heartbeat fresh. Otherwise the next poll skips an overdue
        keepalive. The successful properties GET still publishes, but the missed wake
        is counted and `_last_keepalive` is left where it was."""
        coord, _, api = _make_coord()
        self._full_refresh(coord)
        coord._keepalive_failures = 2
        ka_before = coord._last_keepalive
        api.ping_connected.side_effect = requests.ReadTimeout("HTTPSConnectionPool read timeout")
        api.get_properties.return_value = {"d270_serialnumber": {"value": "ECAM61075MBXYZ"}}
        _run(coord._async_update_data())
        assert coord._keepalive_failures == 3  # missed wake counted, not cleared by the GET
        assert coord._last_keepalive == ka_before  # heartbeat NOT advanced on a failed wake

    def test_full_refresh_wake_success_clears_keepalive(self):
        """Companion: when the wake ping succeeds, the full refresh does count as a
        keepalive — misses cleared and freshness advanced."""
        coord, _, api = _make_coord()
        self._full_refresh(coord)
        coord._keepalive_failures = 2
        api.ping_connected.return_value = True
        api.get_properties.return_value = {"d270_serialnumber": {"value": "ECAM61075MBXYZ"}}
        _run(coord._async_update_data())
        assert coord._keepalive_failures == 0
        assert coord._last_keepalive > 0

    def test_properties_recovers_on_retry(self):
        """Full-refresh get_properties gets the same single retry."""
        coord, _, api = _make_coord()
        coord._last_full_refresh = -coord_mod.FULL_REFRESH_INTERVAL - 1
        coord._last_keepalive = monotonic()
        api.get_properties.side_effect = [DeLonghiApiError("Connection reset"), {}]
        _run(coord._async_update_data())
        assert api.get_properties.call_count == 2
        assert coord._last_full_refresh > 0

    def test_properties_failure_after_retry_raises(self):
        from homeassistant.helpers.update_coordinator import UpdateFailed

        coord, _, api = _make_coord()
        coord._last_full_refresh = -coord_mod.FULL_REFRESH_INTERVAL - 1
        coord._last_keepalive = monotonic()
        api.get_properties.side_effect = DeLonghiApiError("still down")
        with pytest.raises(UpdateFailed):
            _run(coord._async_update_data())
        assert api.get_properties.call_count == 2


# ────────────────── issue #50: LAN monitor overlay ──────────────────

LAN_READY = "0Ayk8AECAAAABwAAAAA="  # d00ca4f001020000000700000000 → Ready, profile 1
LAN_OFF = "0Ayk8AIAAAAAAAAAAAA="  # d00ca4f002000000000000000000 → Off
LAN_BREWING = "0Ayk8AECAAAAAwUeAAA="  # state 3, action 5, progress 30
LAN_CLEANING = "0BJ1DwEBAAAABwAAAQAAAAA65GrQquE="  # Ready + Cleaning Needed


class TestLanMonitorOverlay:
    """The cloud monitor on DL-millcore only refreshes while the Coffee Link
    app is open, so the machine's own LAN push must win when that session is
    alive — otherwise _pre_brew_check and every sensor read a frozen state."""

    def _cloud_stale(self, api, raw="ffffffffffff"):
        api.get_status.return_value = {
            "status": "RUN",
            "machine_state": "Off",
            "profile": 0,
            "monitor_raw": raw,
            "alarms": [],
            "alarm_word": 0,
        }

    def test_no_lan_server_keeps_cloud_state(self):
        coord, _, api = _make_coord()
        coord._last_full_refresh = monotonic()
        coord._last_keepalive = monotonic()
        self._cloud_stale(api)
        result = _run(coord._async_update_data())
        assert result["machine_state"] == "Off"
        assert result["monitor_source"] == "cloud"

    def test_stale_session_keeps_cloud_state(self):
        coord, _ = _lan_monitor_coord(LAN_READY, alive=False)
        self._cloud_stale(coord.api)
        result = _run(coord._async_update_data())
        assert result["machine_state"] == "Off"
        assert result["monitor_source"] == "cloud"
        coord._lan_server.is_session_alive.assert_called_once_with(coord_mod.LAN_SESSION_STALE_SECONDS)

    def test_lan_monitor_overrides_frozen_cloud(self):
        """The #50 symptom: power-on via HA, cloud still says Off, LAN says Ready."""
        coord, _ = _lan_monitor_coord(LAN_READY)
        self._cloud_stale(coord.api)
        result = _run(coord._async_update_data())
        assert result["machine_state"] == "Ready"
        assert result["monitor_source"] == "lan"
        assert result["profile"] == 1

    def test_lan_action_and_progress_reach_the_data_dict(self):
        coord, _ = _lan_monitor_coord(LAN_BREWING)
        self._cloud_stale(coord.api)
        result = _run(coord._async_update_data())
        assert result["machine_state"] == "Brewing"
        assert result["monitor_action"] == 5
        assert result["monitor_progress"] == 30

    def test_identical_lan_value_changes_nothing(self):
        """Healthy-cloud models keep byte-identical behaviour."""
        coord, _, api = _make_coord()
        coord._last_full_refresh = monotonic()
        coord._last_keepalive = monotonic()
        api.get_status.return_value = {
            "status": "RUN",
            "machine_state": "Ready",
            "profile": 1,
            "monitor_raw": bytes.fromhex("d00ca4f001020000000700000000").hex(),
            "alarms": [],
            "alarm_word": 0,
        }
        coord._lan_server = _make_lan_server(True)
        coord._lan_properties = {"d302_monitor": LAN_READY}
        coord.api.parse_monitor_value = DeLonghiApi.parse_monitor_value
        result = _run(coord._async_update_data())
        assert result["machine_state"] == "Ready"
        assert result["profile"] == 1
        assert result["alarms"] == []

    def test_undecodable_lan_frame_falls_back_to_cloud(self):
        coord, _ = _lan_monitor_coord("not-a-monitor")
        self._cloud_stale(coord.api)
        result = _run(coord._async_update_data())  # must not raise
        assert result["machine_state"] == "Off"
        assert result["monitor_source"] == "cloud"

    def test_parser_blowup_never_breaks_the_refresh(self):
        coord, api = _lan_monitor_coord(LAN_READY)
        self._cloud_stale(api)
        api.parse_monitor_value = MagicMock(side_effect=RuntimeError("boom"))
        result = _run(coord._async_update_data())
        assert result["machine_state"] == "Off"
        assert result["monitor_source"] == "cloud"

    def test_lan_alarms_survive_without_cloud_status(self):
        """PD Soul has no app_device_status → cloud=UNKNOWN, which normally
        suppresses alarms. A LAN-pushed alarm is fresh evidence, keep it."""
        coord, api = _lan_monitor_coord(LAN_CLEANING)
        api.get_status.return_value = {
            "status": "UNKNOWN",
            "machine_state": "Off",
            "profile": 0,
            "monitor_raw": "ffffffffffff",
            "alarms": [],
            "alarm_word": None,
        }
        result = _run(coord._async_update_data())
        assert [a["name"] for a in result["alarms"]] == ["Cleaning Needed"]

        # Same machine without the LAN session: suppression still applies.
        coord2, _, api2 = _make_coord()
        coord2._last_full_refresh = monotonic()
        coord2._last_keepalive = monotonic()
        api2.get_status.return_value = {
            "status": "UNKNOWN",
            "machine_state": "Ready",
            "profile": 1,
            "monitor_raw": "aabb",
            "alarms": [{"bit": 16, "name": "Cleaning Needed"}],
            "alarm_word": 1 << 16,
        }
        result2 = _run(coord2._async_update_data())
        assert result2["alarms"] == []

    def test_live_lan_session_prevents_the_assume_off_override(self):
        """An idle-but-connected machine keeps pushing the same Ready frame.

        "Unchanged for 45 min" must not be read as "Off" while the LAN
        session proves the machine is awake.
        """
        coord, api = _lan_monitor_coord(LAN_READY)
        self._cloud_stale(api)
        # The LAN frame equals the tracked raw → the staleness clock is not reset
        coord._last_monitor_raw = bytes.fromhex("d00ca4f001020000000700000000").hex()
        coord._monitor_last_changed = monotonic() - (coord._monitor_stale_timeout + 10)
        result = _run(coord._async_update_data())
        assert result["monitor_stale"] is True
        assert result["machine_state"] == "Ready"

    def test_stale_monitor_still_assumes_off_without_lan(self):
        coord, _, api = _make_coord()
        coord._last_full_refresh = monotonic()
        coord._last_keepalive = monotonic()
        api.get_status.return_value = {
            "status": "RUN",
            "machine_state": "Ready",
            "profile": 1,
            "monitor_raw": "aabb",
            "alarms": [],
            "alarm_word": 0,
        }
        coord._last_monitor_raw = "aabb"  # unchanged → does not reset the clock
        coord._monitor_last_changed = monotonic() - (coord._monitor_stale_timeout + 10)
        result = _run(coord._async_update_data())
        assert result["machine_state"] == "Off"

    def test_lan_alarms_survive_monitor_timeout(self):
        """#50: the timed_out suppression rule must not bite LAN frames either.

        An idle machine keeps pushing the same frame, so the staleness clock
        can pass the timeout while the LAN evidence is perfectly fresh.
        """
        coord, api = _lan_monitor_coord(LAN_CLEANING)
        api.get_status.return_value = {
            "status": "RUN",
            "machine_state": "Off",
            "profile": 0,
            "monitor_raw": "ffffffffffff",
            "alarms": [],
            "alarm_word": 0,
        }
        # LAN frame equals the tracked raw → the staleness clock is not reset
        coord._last_monitor_raw = "d012750f010100000007000001000000003ae46ad0aae1"
        coord._monitor_last_changed = monotonic() - (coord._monitor_stale_timeout + 10)
        result = _run(coord._async_update_data())
        assert result["monitor_stale"] is True
        assert [a["name"] for a in result["alarms"]] == ["Cleaning Needed"]

    def test_overlay_sticks_across_a_stale_session_gap(self):
        """#50 flicker: a push gap past LAN_SESSION_STALE_SECONDS must not
        flip Ready→Off while the cloud datapoint is frozen."""
        coord, api = _lan_monitor_coord(LAN_READY)
        self._cloud_stale(api)
        first = _run(coord._async_update_data())
        assert first["machine_state"] == "Ready"
        assert first["monitor_source"] == "lan"

        # Machine stops pushing: session goes stale, cloud still frozen Off.
        coord._lan_server.is_session_alive = MagicMock(return_value=False)
        second = _run(coord._async_update_data())
        assert second["machine_state"] == "Ready"
        assert second["monitor_source"] == "lan"

    def test_sticky_overlay_dropped_when_cloud_moves(self):
        """The cloud moving again is fresh evidence — it wins over the sticky LAN."""
        coord, api = _lan_monitor_coord(LAN_READY)
        self._cloud_stale(api)
        _run(coord._async_update_data())  # overlay applied over the frozen cloud raw
        coord._lan_server.is_session_alive = MagicMock(return_value=False)
        api.get_status.return_value = {
            "status": "RUN",
            "machine_state": "Off",
            "profile": 0,
            "monitor_raw": "aabbccddeeff",
            "alarms": [],
            "alarm_word": 0,
        }
        result = _run(coord._async_update_data())
        assert result["machine_state"] == "Off"
        assert result["monitor_source"] == "cloud"

    def test_sticky_overlay_superseded_by_newer_push(self):
        """A newer LAN frame replaces the sticky one (Ready → Brewing)."""
        coord, api = _lan_monitor_coord(LAN_READY)
        self._cloud_stale(api)
        _run(coord._async_update_data())
        coord._lan_server.is_session_alive = MagicMock(return_value=False)
        assert _run(coord._async_update_data())["machine_state"] == "Ready"  # sticky
        # Machine wakes up and pushes a Brewing frame — session alive again.
        coord._lan_server.is_session_alive = MagicMock(return_value=True)
        coord._lan_properties = {"d302_monitor": LAN_BREWING}
        result = _run(coord._async_update_data())
        assert result["machine_state"] == "Brewing"
        assert result["monitor_action"] == 5

    def test_push_becomes_the_sticky_overlay(self):
        """A socket push protects later stale-session polls even when no poll
        has observed the cloud raw yet."""
        coord, api = _lan_monitor_coord(LAN_READY)
        coord.data = {"machine_state": "Off", "monitor_source": "cloud"}
        coord.async_set_updated_data = MagicMock()
        _run(coord._on_lan_property({"property": {"name": "d302_monitor", "value": LAN_READY}}))
        # Session goes stale right after the push; the next poll sees the
        # frozen cloud again — the pushed frame must win.
        coord._lan_server.is_session_alive = MagicMock(return_value=False)
        self._cloud_stale(api)
        result = _run(coord._async_update_data())
        assert result["machine_state"] == "Ready"
        assert result["monitor_source"] == "lan"

    def test_push_carries_monitor_ts_and_raw(self):
        """Issue #47: the switch reads the frame and its embedded clock off the
        pushed data dict — a LAN frame is the freshest witness of both."""
        coord, api = _lan_monitor_coord(LAN_CLEANING)
        api.parse_monitor_value = DeLonghiApi.parse_monitor_value
        coord.data = {"machine_state": "Off", "monitor_source": "cloud", "monitor_ts": None, "monitor_raw": "cloud"}
        coord.async_set_updated_data = MagicMock()

        _run(coord._on_lan_property({"property": {"name": "d302_monitor", "value": LAN_CLEANING}}))

        pushed = coord.async_set_updated_data.call_args.args[0]
        assert pushed["monitor_source"] == "lan"
        assert pushed["monitor_raw"] == "d012750f010100000007000001000000003ae46ad0aae1"
        assert pushed["monitor_ts"] == 1792060129  # 2026-10-15, capture date of that frame

    def test_push_without_embedded_clock_clears_monitor_ts(self):
        """An unstamped LAN frame must not leave a stale cloud clock behind."""
        coord, api = _lan_monitor_coord(LAN_READY)
        api.parse_monitor_value = DeLonghiApi.parse_monitor_value
        coord.data = {
            "machine_state": "Off",
            "monitor_source": "cloud",
            "monitor_ts": 1792060129,
            "monitor_raw": "cloud",
        }
        coord.async_set_updated_data = MagicMock()

        _run(coord._on_lan_property({"property": {"name": "d302_monitor", "value": LAN_READY}}))

        pushed = coord.async_set_updated_data.call_args.args[0]
        assert pushed["monitor_ts"] is None
        assert pushed["monitor_raw"] == "d00ca4f001020000000700000000"

    def test_push_applies_state_immediately(self):
        """The socket push must update entities without waiting for the poll."""
        coord, api = _lan_monitor_coord(LAN_READY)
        api.parse_monitor_value = DeLonghiApi.parse_monitor_value
        coord.data = {"machine_state": "Off", "monitor_source": "cloud"}
        coord.async_set_updated_data = MagicMock()

        _run(coord._on_lan_property({"property": {"name": "d302_monitor", "value": LAN_READY}}))

        pushed = coord.async_set_updated_data.call_args.args[0]
        assert pushed["machine_state"] == "Ready"
        assert pushed["monitor_source"] == "lan"
        assert pushed["monitor_stale"] is False
        # staleness bookkeeping follows the LAN frame
        assert coord._monitor_stale_count == 0

    def test_push_without_monitor_leaves_state_alone(self):
        coord, _ = _lan_monitor_coord(LAN_READY)
        coord.data = {"machine_state": "Off"}
        coord.async_set_updated_data = MagicMock()
        _run(coord._on_lan_property({"property": {"name": "d701_tot_bev_b", "value": 42}}))
        pushed = coord.async_set_updated_data.call_args.args[0]
        assert pushed["machine_state"] == "Off"
        assert pushed["lan_properties"]["d701_tot_bev_b"] == 42

    def test_async_get_status_overlays_lan(self):
        coord, api = _lan_monitor_coord(LAN_READY)
        self._cloud_stale(api)
        status = _run(coord.async_get_status())
        assert status["machine_state"] == "Ready"
        assert status["monitor_source"] == "lan"

    def test_async_get_status_swallows_cloud_errors(self):
        """A cloud wobble must not block a brew — state falls back to Unknown."""
        coord, _, api = _make_coord()
        api.get_status.side_effect = DeLonghiApiError("down")
        status = _run(coord.async_get_status())
        assert status == {}

    def test_async_get_status_uses_lan_when_cloud_is_down(self):
        coord, api = _lan_monitor_coord(LAN_READY)
        api.get_status.side_effect = DeLonghiApiError("down")
        status = _run(coord.async_get_status())
        assert status["machine_state"] == "Ready"


class TestBrewDelivery:
    """Issue #50: a DL-millcore machine ignores the cloud data_request brew."""

    def _brew_coord(self, lan_ok: bool):
        coord, _, api = _make_coord()
        api.build_brew_command = MagicMock(return_value=b"\x0d\x0f\x83\xf0")
        api.send_command = MagicMock(return_value=True)
        coord.send_command_lan = AsyncMock(return_value=lan_ok)
        return coord, api

    def test_lan_brew_skips_cloud(self):
        coord, api = self._brew_coord(True)
        coord._lan_server = _make_lan_server(True)
        coord.api.parse_monitor_value = DeLonghiApi.parse_monitor_value
        coord._lan_properties = {"d302_monitor": LAN_READY}
        assert _run(coord.async_brew("espresso", 2)) is True
        coord.send_command_lan.assert_awaited_once_with(b"\x0d\x0f\x83\xf0")
        api.send_command.assert_not_called()

    def test_cloud_fallback_when_lan_unavailable(self):
        coord, api = self._brew_coord(False)
        assert _run(coord.async_brew("espresso", 2)) is True
        coord.send_command_lan.assert_awaited_once()
        api.send_command.assert_called_once_with("DSN-1", b"\x0d\x0f\x83\xf0")

    def test_pre_brew_check_sees_the_lan_state(self):
        """The builder must receive the LAN-corrected status, not the stale cloud."""
        coord, api = self._brew_coord(True)
        coord._lan_server = _make_lan_server(True)
        coord.api.parse_monitor_value = DeLonghiApi.parse_monitor_value
        coord._lan_properties = {"d302_monitor": LAN_READY}
        coord.api.get_status.return_value = {"status": "RUN", "machine_state": "Off", "alarms": []}
        _run(coord.async_brew("espresso", 2))
        passed_status = api.build_brew_command.call_args.args[3]
        assert passed_status["machine_state"] == "Ready"

    def test_send_brew_command_lan_true_short_circuits(self):
        coord, api = self._brew_coord(True)
        assert _run(coord.async_send_brew_command(b"\x01")) is True
        api.send_command.assert_not_called()


class TestBrewAck:
    """Issue #84: a brew swallowed by the machine must be resent exactly once.

    The cloud answers 201 on acceptance, but after idle the machine sometimes
    never executes the first frame (#47 pattern on the brew path). The raw
    monitor is the witness — any byte movement proves delivery.
    """

    def _ack_coord(self, monitor_raw="AQID"):
        coord, _, api = _make_coord()
        api.build_brew_command = MagicMock(return_value=b"\x0d\x0f\x83\xf0")
        api.send_command = MagicMock(return_value=True)
        coord.send_command_lan = AsyncMock(return_value=False)
        coord.data = {"monitor_raw": monitor_raw}
        # Collapse the 15 s / 3 s waits so the verifier runs in milliseconds.
        coord._brew_ack_timeout = 0.05
        coord._brew_ack_poll = 0.01
        return coord, api

    def test_static_monitor_resends_once(self):
        coord, api = self._ack_coord()
        _run(coord._async_verify_brew_ack(b"\x0d\x0f\x83\xf0"))
        api.send_command.assert_called_once_with("DSN-1", b"\x0d\x0f\x83\xf0")

    def test_moving_monitor_no_resend(self):
        coord, api = self._ack_coord()
        coord._brew_ack_timeout = 0.1

        async def main():
            async def move():
                await asyncio.sleep(0.03)
                coord.data["monitor_raw"] = "BQI"

            task = asyncio.ensure_future(move())
            await coord._async_verify_brew_ack(b"\x0d\x0f\x83\xf0")
            await task

        _run(main())
        api.send_command.assert_not_called()

    def test_no_baseline_no_resend(self):
        """Without a witness, never risk a double brew on an unverifiable send."""
        coord, api = self._ack_coord(monitor_raw=None)
        _run(coord._async_verify_brew_ack(b"\x0d\x0f\x83\xf0"))
        api.send_command.assert_not_called()

    def test_resend_follows_lan_first_path(self):
        coord, api = self._ack_coord()
        coord.send_command_lan = AsyncMock(return_value=True)
        _run(coord._async_verify_brew_ack(b"\x0d\x0f\x83\xf0"))
        coord.send_command_lan.assert_awaited_once_with(b"\x0d\x0f\x83\xf0")
        api.send_command.assert_not_called()

    def test_resend_error_is_swallowed(self):
        coord, api = self._ack_coord()
        api.send_command = MagicMock(side_effect=DeLonghiApiError("boom"))
        _run(coord._async_verify_brew_ack(b"\x0d\x0f\x83\xf0"))  # must not raise

    def test_async_brew_arms_verifier(self):
        coord, api = self._ack_coord()
        coord._schedule_brew_ack = MagicMock()
        assert _run(coord.async_brew("espresso", 2)) is True
        coord._schedule_brew_ack.assert_called_once_with(b"\x0d\x0f\x83\xf0")

    def test_failed_send_does_not_arm_verifier(self):
        coord, api = self._ack_coord()
        api.send_command = MagicMock(return_value=False)
        coord._schedule_brew_ack = MagicMock()
        assert _run(coord.async_brew("espresso", 2)) is False
        coord._schedule_brew_ack.assert_not_called()

    def test_new_brew_cancels_pending_verifier(self):
        coord, api = self._ack_coord()
        coord._brew_ack_timeout = 30.0
        coord._brew_ack_poll = 1.0

        async def main():
            coord._schedule_brew_ack(b"\x01")
            first = coord._brew_ack_task
            await asyncio.sleep(0.01)  # verifier now parked in its poll sleep
            coord._schedule_brew_ack(b"\x02")
            second = coord._brew_ack_task
            await asyncio.sleep(0.01)
            assert first.cancelled()
            second.cancel()

        _run(main())
        api.send_command.assert_not_called()


# Sentinel: the per-poll status omits ``active_profile`` entirely (firmwares
# without d286, or a read that came back UNKNOWN).
_NO_ACTIVE = object()


class TestProfileTracking:
    """Issue #78 / PR #79 review: explicit choice vs automatic observation.

    ``selected_profile`` is the user's EXPLICIT choice — restored, persisted and
    never written by a poll. ``_auto_profile`` is the AUTOMATIC default derived
    from what the machine reports (fresh cloud ``active_profile`` / monitor
    byte) and is only consulted while no explicit choice exists.
    """

    def _settle(self, coord):
        """Skip full-refresh and keepalive side effects for a light cycle."""
        coord._last_full_refresh = monotonic()
        coord._last_keepalive = monotonic()

    def _status(self, api, *, active=_NO_ACTIVE, monitor=0, status="RUN"):
        """Script the per-poll status: fresh cloud ``active_profile`` and the
        monitor byte (``profile``)."""
        payload = {
            "machine_state": "Off",
            "profile": monitor,
            "monitor_raw": None,
            "status": status,
            "alarms": [],
            "alarm_word": None,
        }
        if active is not _NO_ACTIVE:
            payload["active_profile"] = active
        api.get_status = MagicMock(return_value=payload)

    # ── the four acceptance cases of the PR #79 review ──────────────────

    def test_cas1_conflicting_signals_are_not_resolved(self):
        """No saved choice, monitor 1 vs cloud 3: a conflict is NOT resolved."""
        coord, _, api = _make_coord()
        self._status(api, active=3, monitor=1)
        self._settle(coord)
        asyncio.run(coord._async_update_data())
        assert coord.selected_profile is None  # never seeded from the monitor
        assert coord._auto_profile is None  # picking one of two conflicts is #78
        assert coord.resolve_profile() is None

    def test_cas2_nothing_usable_is_unresolved_and_never_fabricates_1(self):
        """No saved choice and no usable signal → unresolved, and the published
        data keeps the absence instead of inventing slot 1."""
        coord, _, api = _make_coord()
        self._status(api, monitor=0)  # no fresh cloud value, monitor byte 0
        self._settle(coord)
        result = asyncio.run(coord._async_update_data())
        assert coord._auto_profile is None
        assert coord.resolve_profile() is None
        assert result["active_profile"] is None  # absent, not 1

    def test_cas3_saved_choice_survives_cloud_moving_3_then_4(self):
        """A saved explicit 2 stays 2 while the cloud moves 3 → 4."""
        coord, _, api = _make_coord()
        coord.selected_profile = 2  # restored from the config entry
        self._settle(coord)
        self._status(api, active=3, monitor=0)
        asyncio.run(coord._async_update_data())
        assert coord.selected_profile == 2
        assert coord.resolve_profile() == 2
        self._status(api, active=4, monitor=0)
        asyncio.run(coord._async_update_data())
        assert coord.selected_profile == 2  # a cloud move never overwrites it
        assert coord.resolve_profile() == 2
        assert coord._auto_profile is None  # automatic default not computed
        assert coord._last_d286 is None  # explicit mode does not observe at all

    def test_cas4_read_gap_rebaselines_without_hijacking_auto(self):
        """fresh 2 → a poll with no fresh value → fresh 3: the gap re-poses the
        baseline, and the 3 is adopted only on agreement, never as a hijack."""
        coord, _, api = _make_coord()
        self._settle(coord)
        self._status(api, active=2, monitor=0)
        asyncio.run(coord._async_update_data())
        assert coord._auto_profile == 2
        assert coord._last_d286 == 2
        # read gap: this poll carries no fresh cloud value at all
        self._status(api, monitor=0, status="UNKNOWN")
        asyncio.run(coord._async_update_data())
        assert coord._last_d286 is None  # baseline dropped, not compared
        assert coord._auto_profile is None  # nothing usable this poll
        # first sighting after the gap: baseline again, and a conflicting pair
        # stays unresolved instead of "following" the 3
        self._status(api, active=3, monitor=4)
        asyncio.run(coord._async_update_data())
        assert coord._last_d286 == 3
        assert coord._auto_profile is None
        assert coord.resolve_profile() is None
        # the same value confirmed by the monitor resolves normally
        self._status(api, active=3, monitor=3)
        asyncio.run(coord._async_update_data())
        assert coord._auto_profile == 3
        assert coord.resolve_profile() == 3

    def test_conflict_vetoes_follow_on_every_poll(self):
        """numanx11 #79 blocker: a conflict vetoes automatic following on EVERY
        poll, not just at startup. (3,3)->3, (4,3)->ask, (4,3)->ask, (4,4)->4."""
        coord, _, api = _make_coord()
        self._settle(coord)
        self._status(api, active=3, monitor=3)
        asyncio.run(coord._async_update_data())
        assert coord.resolve_profile() == 3
        # cloud moves to 4 while the monitor still says 3: contradictory pair,
        # must NOT follow the cloud to 4 — ask instead
        self._status(api, active=4, monitor=3)
        asyncio.run(coord._async_update_data())
        assert coord.resolve_profile() is None
        assert coord._last_d286 == 4  # baseline kept current during the conflict
        # identical contradictory reading: still unresolved
        self._status(api, active=4, monitor=3)
        asyncio.run(coord._async_update_data())
        assert coord.resolve_profile() is None
        # signals agree on 4: resolves to 4
        self._status(api, active=4, monitor=4)
        asyncio.run(coord._async_update_data())
        assert coord.resolve_profile() == 4

    def test_failed_full_refresh_does_not_advance_auto_target(self):
        """numanx11 #79 r3: a poll whose full-refresh later fails must NOT
        advance the automatic target — resolve() stays on the last good value
        that was actually published."""
        from homeassistant.helpers.update_coordinator import UpdateFailed

        coord, _, api = _make_coord()
        self._status(api, active=2, monitor=2)
        self._settle(coord)
        asyncio.run(coord._async_update_data())
        assert coord.resolve_profile() == 2
        # next poll: cloud+monitor move to 4, but the full-refresh fetch fails
        self._status(api, active=4, monitor=4)
        api.get_properties.side_effect = DeLonghiApiError("boom")
        # CI-safe: 0 only forces a full refresh once monotonic() > interval;
        # a fresh CI container has monotonic() < interval and skips the path.
        coord._last_full_refresh = -FULL_REFRESH_INTERVAL - 1
        with pytest.raises(UpdateFailed):
            asyncio.run(coord._async_update_data())
        assert coord.resolve_profile() == 2  # not advanced to the failed poll's 4

    def test_lan_monitor_push_updates_auto_target(self):
        """numanx11 #79 r3: a LAN frame publishing a monitor slot must move the
        automatic target so resolve() agrees with the published observation."""
        coord, _, api = _make_coord()
        coord.async_set_updated_data = MagicMock()
        coord._lan_server = _make_lan_server(True)
        self._status(api, monitor=2)  # monitor-only poll -> auto 2
        self._settle(coord)
        coord.data = asyncio.run(coord._async_update_data())
        assert coord.resolve_profile() == 2
        # a LAN frame pushes monitor 4 while the cloud profile is absent
        with patch.object(coord, "_lan_monitor_status", return_value={"profile": 4, "machine_state": "Ready"}):
            asyncio.run(coord._on_lan_property({"property": {"name": "d302_monitor", "value": "x"}}))
        assert coord.resolve_profile() == 4  # auto target followed the LAN monitor

    def test_out_of_range_cloud_value_is_not_a_slot(self):
        """Validation: a slot must be a whole number in 1..5 — 7 is neither a
        default nor an observation."""
        coord, _, api = _make_coord()
        self._status(api, active=7, monitor=0)
        self._settle(coord)
        asyncio.run(coord._async_update_data())
        assert coord._auto_profile is None
        assert coord._last_d286 is None
        assert coord.resolve_profile() is None

    # ── explicit vs automatic behaviour ─────────────────────────────────

    def test_mark_profile_selection_stamps_explicit_choice(self):
        coord, _, _ = _make_coord()
        coord.mark_profile_selection(4)
        assert coord.selected_profile == 4  # explicit, not an observation
        assert coord.resolve_profile() == 4

    def test_first_or_repeated_cloud_value_keeps_restored_choice(self):
        """Issue #78: a frozen cloud profile (first sighting or repeated) must
        NOT touch the restored/explicit HA choice. Replaces the old F-COORD-01
        'follow on first differing read' assertion, which was wrong for the
        Eletta where d286 is stuck."""
        coord, _, api = _make_coord()
        coord.selected_profile = 2
        self._status(api, active=3, monitor=1)  # fresh cloud advertises 3
        self._settle(coord)
        asyncio.run(coord._async_update_data())
        assert coord.selected_profile == 2
        asyncio.run(coord._async_update_data())  # repeated 3
        assert coord.selected_profile == 2
        assert coord.resolve_profile() == 2

    def test_genuine_cloud_change_follows_the_auto_default(self):
        """NEW MODEL: in automatic mode a real in-session change (baseline 2 → 3)
        moves ``_auto_profile`` — it is never promoted to an explicit choice."""
        coord, _, api = _make_coord()
        coord._auto_profile = 2
        coord._last_d286 = 2  # baseline already established at 2
        self._status(api, active=3, monitor=0)  # monitor silent, cloud moved
        self._settle(coord)
        asyncio.run(coord._async_update_data())
        assert coord._auto_profile == 3
        assert coord._last_d286 == 3
        assert coord.selected_profile is None
        assert coord.resolve_profile() == 3

    def test_own_command_echo_cannot_override_explicit_choice(self):
        """No grace window any more: the explicit choice is simply never
        overwritten, so the lagging echo of our own command is harmless."""
        coord, _, api = _make_coord()
        coord.mark_profile_selection(3)
        self._status(api, active=2, monitor=2)  # stale echo of our own command
        self._settle(coord)
        asyncio.run(coord._async_update_data())
        assert coord.selected_profile == 3
        assert coord.resolve_profile() == 3

    def test_missing_cloud_active_never_stomps(self):
        coord, _, _ = _make_coord()
        coord.selected_profile = 2
        coord._cached_profiles = {"profiles": {}}  # no "active" key at all
        self._settle(coord)
        asyncio.run(coord._async_update_data())
        assert coord.selected_profile == 2

    def test_stale_cached_value_is_not_a_transition(self):
        """coderabbit #79: when the per-poll status omits active_profile, an
        older full-refresh cached value must not become the default."""
        coord, _, _ = _make_coord()  # default status: no active_profile, profile 0
        coord._auto_profile = 2
        coord._last_d286 = 2
        coord._cached_profiles = {"active": 1, "profiles": {}}  # stale cache, differs
        self._settle(coord)
        asyncio.run(coord._async_update_data())
        assert coord.selected_profile is None  # nothing seeded from the cache
        assert coord._auto_profile is None  # the stale cache is not a default
        assert coord._last_d286 is None  # no fresh value: baseline re-posed

    def test_failed_refresh_clears_baseline(self):
        """coderabbit #79: a failed refresh must drop the baseline so the next
        successful read re-establishes it as a first sighting, not a transition."""
        from homeassistant.helpers.update_coordinator import UpdateFailed

        coord, _, api = _make_coord()
        coord._auto_profile = 2
        coord._last_d286 = 2
        api.get_status.side_effect = DeLonghiApiError("timeout")
        with pytest.raises(UpdateFailed):
            asyncio.run(coord._async_update_data())
        assert coord._last_d286 is None  # cleared on failure

    def test_recovery_read_rebaselines_without_hijack(self):
        """After an in-session outage, the first fresh read differing from the
        pre-outage baseline sets the baseline only — it is not a transition, and
        it never becomes an explicit choice."""
        from homeassistant.helpers.update_coordinator import UpdateFailed

        coord, _, api = _make_coord()
        coord._auto_profile = 2
        coord._last_d286 = 2
        api.get_status.side_effect = DeLonghiApiError("timeout")
        with pytest.raises(UpdateFailed):
            asyncio.run(coord._async_update_data())
        # recover: cloud now advertises 3 (differs from pre-outage 2), monitor
        # silent — the value is adopted as the automatic default by agreement
        self._status(api, active=3, monitor=0)
        self._settle(coord)
        asyncio.run(coord._async_update_data())
        assert coord._last_d286 == 3  # post-outage sighting: baseline only
        assert coord._auto_profile == 3
        assert coord.selected_profile is None  # never promoted to an explicit choice

    def test_published_active_profile_keeps_a_real_cloud_value(self):
        """A slot the machine really reports stays resolved."""
        coord, _, api = _make_coord()
        self._status(api, active=4, monitor=0)
        self._settle(coord)
        result = asyncio.run(coord._async_update_data())
        assert result["active_profile"] == 4

    def test_published_active_profile_falls_back_to_cache_not_to_1(self):
        """Without a fresh value the full-refresh cache is published — and when
        even that knows nothing, the absence is preserved (see CAS2)."""
        coord, _, _ = _make_coord()
        coord._cached_profiles = {"active": 5, "profiles": {}}
        self._settle(coord)
        result = asyncio.run(coord._async_update_data())  # fresh absent
        assert result["active_profile"] == 5

    def test_saved_out_of_range_choice_is_ignored(self):
        """A corrupt config-entry value is not an explicit choice."""
        entry = MagicMock()
        entry.data = {coord_mod.CONF_SELECTED_PROFILE: 7}
        coord = DeLonghiCoordinator(_make_hass(), _make_api(), "DSN-1", config_entry=entry)
        assert coord.selected_profile is None
        assert coord._auto_profile is None
        assert coord.resolve_profile() is None

    def test_mark_profile_selection_persists_to_entry(self):
        """Issue #78: an explicit selection is written to the config entry so it
        survives a restart without re-sending a selection command."""
        coord, _, _ = _make_coord()
        entry = MagicMock()
        entry.data = {}
        coord.config_entry = entry
        coord.hass.config_entries.async_update_entry = MagicMock()
        coord.mark_profile_selection(4)
        coord.hass.config_entries.async_update_entry.assert_called_once()
        new_data = coord.hass.config_entries.async_update_entry.call_args[1]["data"]
        assert new_data[coord_mod.CONF_SELECTED_PROFILE] == 4

    def test_persist_is_noop_when_unchanged(self):
        """Re-selecting the same profile must not rewrite the entry (no churn)."""
        coord, _, _ = _make_coord()
        entry = MagicMock()
        entry.data = {coord_mod.CONF_SELECTED_PROFILE: 4}
        coord.config_entry = entry
        coord.hass.config_entries.async_update_entry = MagicMock()
        coord.mark_profile_selection(4)
        coord.hass.config_entries.async_update_entry.assert_not_called()


class TestResolveProfile:
    """Issue #78: resolve_profile() is the single source of truth for the target
    slot. Precedence: EXPLICIT choice (persisted) > AUTOMATIC default (observed).
    Only valid slots (a whole number in 1..5) are ever returned; ``None`` when
    neither is known, so callers must ask instead of defaulting to slot 1."""

    def _coord(self, selected=None, auto=None):
        coord, _, _ = _make_coord()
        coord.selected_profile = selected
        coord._auto_profile = auto
        return coord

    def test_explicit_choice_wins(self):
        assert self._coord(selected=2, auto=3).resolve_profile() == 2

    def test_falls_back_to_auto_default(self):
        assert self._coord(selected=None, auto=3).resolve_profile() == 3

    def test_returns_none_when_nothing_known(self):
        # The conflict-unresolved case: nothing selected, no automatic default.
        assert self._coord().resolve_profile() is None

    def test_invalid_explicit_falls_through_to_auto(self):
        # 7 is not a slot, so it is not a choice either — never returned.
        assert self._coord(selected=7, auto=4).resolve_profile() == 4

    def test_never_returns_an_out_of_range_auto_default(self):
        assert self._coord(selected=None, auto=7).resolve_profile() is None
        assert self._coord(selected=None, auto=0).resolve_profile() is None
        assert self._coord(selected=None, auto=-2).resolve_profile() is None

    def test_non_int_values_are_not_slots(self):
        assert self._coord(selected="3", auto=None).resolve_profile() is None
        assert self._coord(selected=None, auto="4").resolve_profile() is None

    def test_zero_explicit_is_not_a_choice(self):
        assert self._coord(selected=0, auto=None).resolve_profile() is None
