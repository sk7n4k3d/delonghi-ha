"""Sensor and binary-sensor entity tests.

Focus on the ``available`` semantics introduced for issue #3: sensors
whose underlying counter is absent from the coordinator data must report
``unavailable`` (not ``unknown``) and alarm binary sensors must stay
``unavailable`` until the machine actually reports a supported alarm
word for the relevant bit.
"""

import asyncio
from binascii import crc_hqx
from unittest.mock import MagicMock

import pytest
from homeassistant.components.sensor import SensorStateClass
from homeassistant.helpers.entity import EntityCategory

from custom_components.delonghi_coffee import binary_sensor as binary_sensor_mod
from custom_components.delonghi_coffee import sensor as sensor_mod
from custom_components.delonghi_coffee.api import DeLonghiApi
from custom_components.delonghi_coffee.binary_sensor import DeLonghiAlarmSensor
from custom_components.delonghi_coffee.const import ALARMS, DOMAIN
from custom_components.delonghi_coffee.sensor import (
    COUNTER_SENSORS,
    DeLonghiApiUsageSensor,
    DeLonghiBeanSensor,
    DeLonghiBlockingAlarmsSensor,
    DeLonghiCounterSensor,
    DeLonghiLanDiagnosticSensor,
    DeLonghiProfileSensor,
    DeLonghiStatusSensor,
)


def _make_counter_sensor(coord, key: str) -> DeLonghiCounterSensor:
    meta = COUNTER_SENSORS[key]
    return DeLonghiCounterSensor(coord, "DSN123", "Eletta", "Machine", None, key, meta)


def _make_alarm_sensor(coord, bit: int, inverted: bool = False, model: str = "Model") -> DeLonghiAlarmSensor:
    meta = {"icon": "mdi:alert", "inverted": inverted}
    return DeLonghiAlarmSensor(coord, "DSN123", model, "Machine", None, bit, meta)


class TestCounterSensorAvailable:
    """Issue #3: missing counter sources must render as ``unavailable``."""

    def setup_method(self) -> None:
        self.coord = MagicMock()
        self.coord.data = {
            "counters": {
                "espresso": 5,
                "grounds_count": 0,  # present but zero — must stay available
                "total_beverages": 10,
            }
        }

    def test_present_nonzero_counter_is_available(self):
        sensor = _make_counter_sensor(self.coord, "espresso")
        assert sensor.available is True
        assert sensor.native_value == 5

    def test_present_zero_counter_is_available(self):
        """A counter that reports 0 is meaningful and must stay visible."""
        sensor = _make_counter_sensor(self.coord, "grounds_count")
        assert sensor.available is True
        assert sensor.native_value == 0

    def test_absent_counter_is_unavailable(self):
        """PrimaDonna Soul does not expose ``descale_progress`` — #3."""
        sensor = _make_counter_sensor(self.coord, "descale_progress")
        assert sensor.available is False
        assert sensor.native_value is None

    def test_empty_counters_dict_every_sensor_unavailable(self):
        """Cold start with no counters yet must not leak ``unknown`` states."""
        self.coord.data = {"counters": {}}
        for key in COUNTER_SENSORS:
            sensor = _make_counter_sensor(self.coord, key)
            assert sensor.available is False, f"{key} should be unavailable"
            assert sensor.native_value is None

    def test_missing_counters_key_is_unavailable(self):
        """Coordinator data without a ``counters`` key must degrade safely."""
        self.coord.data = {}
        sensor = _make_counter_sensor(self.coord, "espresso")
        assert sensor.available is False
        assert sensor.native_value is None


class TestAlarmBinarySensorAvailable:
    """Issue #3: alarm bits that never fire on a firmware stay unavailable."""

    def test_alarm_word_missing_is_unavailable(self):
        coord = MagicMock()
        coord.data = {"alarm_word": None}
        coord.seen_alarm_bits = set()
        sensor = _make_alarm_sensor(coord, bit=0)
        assert sensor.available is False

    def test_normal_bit_set_reports_on(self):
        coord = MagicMock()
        coord.data = {"alarm_word": 1 << 5}
        coord.seen_alarm_bits = set()
        sensor = _make_alarm_sensor(coord, bit=5)
        assert sensor.available is True
        assert sensor.is_on is True

    def test_normal_bit_unset_reports_off(self):
        coord = MagicMock()
        coord.data = {"alarm_word": 0}
        coord.seen_alarm_bits = set()
        sensor = _make_alarm_sensor(coord, bit=3)
        assert sensor.available is True
        assert sensor.is_on is False

    def test_inverted_bit_never_seen_is_unavailable(self):
        """Tank/grid bits on firmwares that don't drive them stay hidden."""
        coord = MagicMock()
        coord.data = {"alarm_word": 0}
        coord.seen_alarm_bits = set()
        sensor = _make_alarm_sensor(coord, bit=13, inverted=True)
        assert sensor.available is False

    def test_inverted_bit_first_seen_becomes_available(self):
        """First time the bit is driven, sensor learns support."""
        coord = MagicMock()
        coord.data = {"alarm_word": 1 << 13}
        coord.seen_alarm_bits = set()
        sensor = _make_alarm_sensor(coord, bit=13, inverted=True)
        assert sensor.available is True
        assert 13 in coord.seen_alarm_bits
        # Bit set → inverted component PRESENT → not a problem.
        assert sensor.is_on is False

    def test_inverted_bit_seen_then_unset_reports_problem(self):
        coord = MagicMock()
        coord.data = {"alarm_word": 0}
        coord.seen_alarm_bits = {13}
        sensor = _make_alarm_sensor(coord, bit=13, inverted=True)
        assert sensor.available is True
        # Bit unset after being seen → component missing → PROBLEM.
        assert sensor.is_on is True


class TestCounterSensorScaling:
    def test_scaled_value_rounded_one_decimal(self):
        coord = MagicMock()
        # total_water_ml has scale 0.001 (mL → L) and precision 1
        coord.data = {"counters": {"total_water_ml": 12345}}
        sensor = _make_counter_sensor(coord, "total_water_ml")
        assert sensor.native_value == 12.3

    def test_scaled_zero_returns_zero(self):
        coord = MagicMock()
        coord.data = {"counters": {"total_water_ml": 0}}
        sensor = _make_counter_sensor(coord, "total_water_ml")
        # 0 is falsy but not None, so it must NOT short-circuit to None
        assert sensor.native_value == 0


class TestStatusSensor:
    def _make(self, data=None):
        coord = MagicMock()
        coord.data = data or {}
        return DeLonghiStatusSensor(coord, "DSN-X", "Eletta", "Coffee", "1.0")

    def test_init_attributes(self):
        s = self._make()
        assert s._attr_unique_id == "DSN-X_status"
        assert s._attr_translation_key == "machine_status"
        assert s._attr_icon == "mdi:coffee-maker"
        assert s._attr_has_entity_name is True

    def test_native_value_default_unknown(self):
        s = self._make({})
        assert s.native_value == "Unknown"

    def test_native_value_returns_machine_state(self):
        s = self._make({"machine_state": "Brewing"})
        assert s.native_value == "Brewing"

    def test_extra_state_attributes_minimal(self):
        s = self._make({})
        attrs = s.extra_state_attributes
        assert attrs["cloud_status"] == "UNKNOWN"
        assert attrs["profile"] == 0
        assert attrs["active_alarms"] == []
        assert attrs["alarm_count"] == 0
        # No lan_config block → no lan_enabled key
        assert "lan_enabled" not in attrs

    def test_lan_diagnostic_is_disabled_and_separates_monitor_from_activity(self):
        coord = MagicMock()
        coord.last_lan_monitor_utc = "2026-11-26T01:00:00+00:00"
        coord._lan_server.last_activity_utc = "2026-11-26T01:05:00+00:00"
        sensor = DeLonghiLanDiagnosticSensor(coord, "DSN-X", "Eletta", "Coffee", None)
        assert sensor._attr_entity_registry_enabled_default is False
        assert sensor._attr_unique_id == "DSN-X_lan_monitor_diagnostic"
        assert sensor.native_value == "2026-11-26T01:00:00+00:00"
        assert sensor.extra_state_attributes["last_lan_activity"] == "2026-11-26T01:05:00+00:00"
        coord._lan_server = None
        assert sensor.extra_state_attributes["last_lan_activity"] is None

    def test_extra_state_attributes_expose_no_api_counters(self):
        """Issue #45: per-poll counters must not live on the status sensor.

        ``api_calls_hour`` / ``api_total_calls`` moved on nearly every 60 s
        refresh, so HA fired a state_changed (and the recorder wrote a row)
        every minute with the machine Off. The counters now belong to
        ``DeLonghiApiUsageSensor``.
        """
        s = self._make({"api_rate": 12, "api_total_calls": 4567})
        attrs = s.extra_state_attributes
        assert "api_calls_hour" not in attrs
        assert "api_total_calls" not in attrs
        assert "calls_last_hour" not in attrs

    def test_extra_state_attributes_full(self):
        s = self._make(
            {
                "status": "ONLINE",
                "profile": 2,
                "alarms": [{"name": "Water tank empty"}, {"name": "Grounds full"}],
                "api_rate": 12,
                "api_total_calls": 4567,
                "lan_config": {"lan_enabled": True, "lan_ip": "10.0.0.42"},
                "drink_catalog": {"espresso": {}, "americano": {}, "cappuccino": {}},
            }
        )
        attrs = s.extra_state_attributes
        assert attrs["cloud_status"] == "ONLINE"
        assert attrs["profile"] == 2
        assert attrs["active_alarms"] == ["Water tank empty", "Grounds full"]
        assert attrs["alarm_count"] == 2
        assert attrs["lan_enabled"] is True
        assert attrs["lan_ip"] == "10.0.0.42"
        assert attrs["contentstack_drinks"] == 3
        # Counters present in the coordinator data stay out (#45).
        assert "api_calls_hour" not in attrs
        assert "api_total_calls" not in attrs

    def test_attributes_stable_across_polls(self):
        """Issue #45: an idle machine must produce byte-identical attributes.

        Simulates two refreshes where only the API counters moved (which is
        what happens every 60 s while the machine sits Off): if the status
        sensor's attributes are equal, HA skips the state write entirely and
        the recorder stores nothing.
        """
        coord = MagicMock()
        coord.data = {
            "machine_state": "Off",
            "status": "RUN",
            "profile": 7,
            "alarms": [],
            "api_rate": 48,
            "api_total_calls": 48,
        }
        s = self._make(coord.data)
        first = dict(s.extra_state_attributes)
        coord.data.update({"api_rate": 49, "api_total_calls": 49})
        assert s.extra_state_attributes == first


class TestElettaBrewDisplay:
    """Issue #83: map confirmed espresso phases on the status sensor only."""

    NOW = 0x6B00E156
    IDLE = "d012750f0101000800070000010000000070af6b00df2c"
    # Real capture from issue #89: the grinding step that opens an espresso
    # cycle on the Eletta Explore, state byte still 7 (Ready).
    GRIND = "d012750f01010008000704000100000000b1696b01c065"

    @pytest.fixture(autouse=True)
    def freeze_clock(self, monkeypatch):
        monkeypatch.setattr(sensor_mod, "time", lambda: self.NOW)

    def _monitor(self, state, action, progress, timestamp=NOW):
        """Build a checksummed frame for observations without raw capture."""
        body = bytes.fromhex(self.IDLE)[:-6]
        body = body[:9] + bytes((state, action, progress)) + body[12:]
        return (body + crc_hqx(body, 0x1D0F).to_bytes(2, "big") + timestamp.to_bytes(4, "big")).hex()

    def _data(self, frame):
        parsed = DeLonghiApi.parse_monitor_hex(frame)
        return {
            "machine_state": parsed["machine_state"],
            "monitor_source": "cloud",
            "monitor_stale": False,
            "monitor_raw": frame,
            "monitor_ts": parsed["monitor_ts"],
            "monitor_action": parsed["action"],
            "monitor_progress": parsed["progress"],
        }

    def _sensor(self, data, oem_model="DL-striker-cb"):
        hass = MagicMock()
        entry = MagicMock(entry_id="eletta", data={"model": oem_model} if oem_model is not None else {})
        coordinator = MagicMock(data=data)
        hass.data = {
            DOMAIN: {
                entry.entry_id: {
                    "coordinator": coordinator,
                    "dsn": "test",
                    "model": "Eletta Explore",
                    "device_name": "Coffee",
                }
            }
        }
        add_entities = MagicMock()
        asyncio.run(sensor_mod.async_setup_entry(hass, entry, add_entities))
        return next(entity for entity in add_entities.call_args.args[0] if isinstance(entity, DeLonghiStatusSensor))

    def test_replay_original_espresso_frames(self):
        frames = [
            (self.IDLE, "Ready", 0, 0),
            ("d012750f0107000800070700010000000062d56b00e14b", "Brewing", 7, 0),
            ("d012750f0107000800070a050100000000cbde6b00e14f", "Brewing", 10, 5),
            ("d012750f0107000800070b4c0100000000514d6b00e156", "Brewing", 11, 76),
        ]
        sensor = self._sensor({})
        for frame, expected_state, action, progress in frames:
            sensor.coordinator.data = data = self._data(frame)
            original = data.copy()
            assert sensor.native_value == expected_state
            assert sensor.extra_state_attributes["monitor_action"] == action
            assert sensor.extra_state_attributes["monitor_progress"] == progress
            assert data == original

    def test_espresso_completion_and_quiet_rinse_observation(self):
        sensor = self._sensor({})
        for action, progress in ((4, 0), (6, 0), (10, 5), (11, 45), (13, 100), (14, 100), (0, 0), (0, 0)):
            sensor.coordinator.data = self._data(self._monitor(7, action, progress))
            assert sensor.native_value == ("Brewing" if action else "Ready")

    def test_grinding_frame_from_issue_89_reports_brewing(self, monkeypatch):
        """The cycle opens with action 4; the clock is frozen at the capture."""
        monkeypatch.setattr(sensor_mod, "time", lambda: 0x6B01C065)
        sensor = self._sensor(self._data(self.GRIND))
        assert sensor.native_value == "Brewing"

    def test_grinding_frame_outside_the_freshness_window_leaves_ready(self):
        data = self._data(self.GRIND)
        assert self._sensor(data).native_value == "Ready"

    def test_hot_water_keeps_the_explicit_state_in_shared_action_phase(self):
        sensor = self._sensor({})
        for state, action, progress, expected_state in (
            (11, 3, 8, "Dispensing Hot Water"),
            (11, 3, 85, "Dispensing Hot Water"),
            (11, 6, 100, "Dispensing Hot Water"),
            (7, 0, 0, "Ready"),
        ):
            sensor.coordinator.data = self._data(self._monitor(state, action, progress))
            assert sensor.native_value == expected_state

    @pytest.mark.parametrize("state", [0, 3, 5, 8, 10, 12, 29])
    def test_other_explicit_states_are_preserved(self, state):
        data = self._data(self._monitor(state, 6, 20))
        assert self._sensor(data).native_value == data["machine_state"]

    @pytest.mark.parametrize("oem_model", [None, "", "DL-striker-other", "DL-striker-best", "DL-millcore"])
    def test_other_or_unknown_oem_ignores_espresso_signature(self, oem_model):
        data = self._data(self._monitor(7, 10, 5))
        assert self._sensor(data, oem_model).native_value == "Ready"

    @pytest.mark.parametrize(("action", "progress"), [(3, 8), (12, 100), (10, 101)])
    def test_unconfirmed_or_invalid_telemetry_leaves_ready(self, action, progress):
        data = self._data(self._monitor(7, action, progress))
        assert self._sensor(data).native_value == "Ready"

    @pytest.mark.parametrize(
        "changes",
        [
            {"monitor_action": None},
            {"monitor_action": "10"},
            {"monitor_action": 10.0},
            {"monitor_progress": None},
            {"monitor_progress": True},
            {"monitor_progress": 15},
            {"monitor_raw": None},
            {"monitor_raw": "invalid"},
            {"monitor_source": "lan"},
            {"monitor_source": None},
            {"monitor_stale": True},
            {"monitor_stale": None},
            {"monitor_ts": None},
            {"monitor_ts": NOW - 1},
        ],
    )
    def test_incomplete_or_inconsistent_evidence_leaves_ready(self, changes):
        data = self._data(self._monitor(7, 10, 5)) | changes
        assert self._sensor(data).native_value == "Ready"

    @pytest.mark.parametrize("delta", [-100, 100])
    def test_old_or_future_frames_leave_ready(self, delta):
        data = self._data(self._monitor(7, 10, 5, self.NOW + delta))
        assert self._sensor(data).native_value == "Ready"

    def test_invalid_crc_leaves_ready(self):
        frame = self._monitor(7, 10, 5)
        damaged_frame = frame[:-12] + "0000" + frame[-8:]
        assert self._sensor(self._data(damaged_frame)).native_value == "Ready"


class TestApiUsageSensor:
    """Issue #45: the API counters live on their own diagnostic entity."""

    def _make(self, data=None):
        coord = MagicMock()
        coord.data = data or {}
        return DeLonghiApiUsageSensor(coord, "DSN-X", "Eletta", "Coffee", "1.0")

    def test_init_attributes(self):
        s = self._make()
        assert s._attr_unique_id == "DSN-X_api_usage"
        assert s._attr_name == "API usage"
        assert s._attr_entity_category == EntityCategory.DIAGNOSTIC
        # Opt-in: no recorder rows for users who never look at API traffic.
        assert s._attr_entity_registry_enabled_default is False
        assert s._attr_native_unit_of_measurement == "calls"
        assert s._attr_state_class == SensorStateClass.TOTAL_INCREASING

    def test_native_value_defaults_to_zero(self):
        assert self._make({}).native_value == 0

    def test_native_value_is_total_calls(self):
        s = self._make({"api_total_calls": 4567, "api_rate": 12})
        assert s.native_value == 4567

    def test_rate_is_an_attribute(self):
        s = self._make({"api_total_calls": 4567, "api_rate": 12})
        assert s.extra_state_attributes == {"calls_last_hour": 12}

    def test_rate_defaults_to_zero(self):
        assert self._make({}).extra_state_attributes == {"calls_last_hour": 0}


class TestProfileSensor:
    def _make(self, data=None):
        coord = MagicMock()
        coord.data = data or {}
        return DeLonghiProfileSensor(coord, "DSN-Y", "ECAM", "Soul", None)

    def test_init_attributes(self):
        s = self._make()
        assert s._attr_unique_id == "DSN-Y_active_profile"
        assert s._attr_translation_key == "active_profile"
        assert s._attr_icon == "mdi:account"

    def test_native_value_uses_cloud_active_profile_first(self):
        """Cloud `active_profile` is authoritative; monitor `profile` can lag.

        Regression: the sensor previously preferred the monitor byte and
        produced "Sasha" on Bastien's machine while the user-selected profile
        on the machine UI was actually #3 ("fuck claud"), because the monitor
        kept reporting the last *brewed* profile."""
        s = self._make(
            {
                "profile": 2,  # monitor stale (last brew)
                "active_profile": 3,  # cloud current selection
                "profiles": {2: {"name": "Sasha"}, 3: {"name": "Anna"}},
            }
        )
        # Cloud wins — monitor byte is ignored when cloud has a real value.
        assert s.native_value == "Anna"

    def test_native_value_falls_back_to_monitor_when_cloud_missing(self):
        """If the cloud property hasn't refreshed yet (cold start), the
        monitor byte is the next-best signal."""
        s = self._make(
            {
                "profile": 2,
                "active_profile": 0,  # cloud not yet populated
                "profiles": {2: {"name": "Sasha"}},
            }
        )
        assert s.native_value == "Sasha"

    def test_native_value_falls_back_to_cloud_when_monitor_zero(self):
        s = self._make(
            {
                "profile": 0,
                "active_profile": 3,
                "profiles": {3: {"name": "Anna"}},
            }
        )
        assert s.native_value == "Anna"

    def test_native_value_default_label_when_unnamed(self):
        # profiles dict has the entry but no name → labeled fallback
        s = self._make({"active_profile": 4, "profiles": {4: {}}})
        assert s.native_value == "Profile 4"

    def test_native_value_default_label_when_unknown_profile(self):
        s = self._make({"profile": 5, "profiles": {}})
        assert s.native_value == "Profile 5"

    def test_extra_state_attributes_lists_all_profiles(self):
        s = self._make(
            {
                "active_profile": 2,
                "profile": 1,  # monitor surfaced for support diagnostics
                "profiles": {
                    1: {"name": "A", "color": "red", "figure": "1"},
                    2: {"name": "B", "color": "blue", "figure": "2"},
                },
            }
        )
        attrs = s.extra_state_attributes
        assert attrs["active_profile_id"] == 2
        # Monitor byte is also exposed so users / support can detect drift
        assert attrs["monitor_profile_id"] == 1
        assert attrs["profile_1_name"] == "A"
        assert attrs["profile_1_color"] == "red"
        assert attrs["profile_1_figure"] == "1"
        assert attrs["profile_2_name"] == "B"

    def test_state_and_attributes_use_same_source(self):
        """Regression: state and attributes were diverging — state read
        monitor byte, attributes read cloud, leading to UI showing one
        profile while attributes claimed another."""
        s = self._make(
            {
                "profile": 1,  # monitor stale
                "active_profile": 3,  # cloud authoritative
                "profiles": {1: {"name": "Old"}, 3: {"name": "Current"}},
            }
        )
        state = s.native_value
        attrs = s.extra_state_attributes
        # Both surfaces must agree on the same profile (id 3 = "Current")
        assert state == "Current"
        assert attrs["active_profile_id"] == 3
        assert attrs["profile_3_name"] == "Current"
        # And the monitor drift is surfaced separately for diagnostics
        assert attrs["monitor_profile_id"] == 1


class TestBeanSensor:
    def _make(self, data=None):
        coord = MagicMock()
        coord.data = data or {}
        return DeLonghiBeanSensor(coord, "DSN-Z", "PrimaDonna", "Soul", "2.0")

    def test_init_attributes(self):
        s = self._make()
        assert s._attr_unique_id == "DSN-Z_bean_system"
        assert s._attr_translation_key == "bean_system"
        assert s._attr_icon == "mdi:seed"

    def test_native_value_counts_beans(self):
        s = self._make({"beans": [{"id": 1}, {"id": 2}, {"id": 3}]})
        assert s.native_value == 3

    def test_native_value_empty(self):
        s = self._make({})
        assert s.native_value == 0

    def test_extra_state_attributes_minimal(self):
        s = self._make({"beans": []})
        attrs = s.extra_state_attributes
        assert attrs["coffee_beans_catalog_count"] == 0

    def test_extra_state_attributes_with_beans_and_raw_hex(self):
        s = self._make(
            {
                "beans": [
                    {"id": 1, "name": "Arabica", "english_name": "Arabica", "raw_params_hex": "deadbeef"},
                    {"id": 2, "name": "Robusta", "english_name": "Robusta"},  # no raw_hex
                ],
                "coffee_beans_count": 12,
            }
        )
        attrs = s.extra_state_attributes
        assert attrs["bean_1_name"] == "Arabica"
        assert attrs["bean_1_english"] == "Arabica"
        assert attrs["bean_1_raw_params_hex"] == "deadbeef"
        assert attrs["bean_2_name"] == "Robusta"
        assert "bean_2_raw_params_hex" not in attrs
        assert attrs["coffee_beans_catalog_count"] == 12

    def test_extra_state_attributes_bean_system_par(self):
        s = self._make(
            {
                "beans": [],
                "bean_system_par": {"raw_hex": "cafebabe", "raw_bytes": 4},
            }
        )
        attrs = s.extra_state_attributes
        assert attrs["bean_system_par_raw_hex"] == "cafebabe"
        assert attrs["bean_system_par_bytes"] == 4

    def test_extra_state_attributes_bean_system_par_skipped_when_no_hex(self):
        s = self._make({"beans": [], "bean_system_par": {"raw_hex": ""}})
        attrs = s.extra_state_attributes
        assert "bean_system_par_raw_hex" not in attrs

    def test_extra_state_attributes_bean_system_par_skipped_when_not_dict(self):
        s = self._make({"beans": [], "bean_system_par": "not-a-dict"})
        attrs = s.extra_state_attributes
        assert "bean_system_par_raw_hex" not in attrs

    def test_extra_state_attributes_full_bean_adapt(self):
        s = self._make(
            {
                "beans": [],
                "bean_adapt": {
                    "bean_types": ["Arabica", "Robusta"],
                    "roasting_levels": ["Light", "Medium", "Dark"],
                    "taste_feedback": ["Bitter", "Balanced"],
                    "grinder_min": 1,
                    "grinder_max": 8,
                    "grinder_step": 1,
                    "flow_min": 5,
                    "flow_max": 25,
                    "flow_delta": 5,
                    "preinfusion_water_min": 5,
                    "preinfusion_water_max": 30,
                    "bean_table": [
                        {"bean_type": "Light Roast", "powder_quantity": "8g"},
                        {"bean_type": "Dark Roast", "powder_quantity": "10g"},
                    ],
                    "roasting_table": [
                        {
                            "roast_level": "Light",
                            "stoichio_ratio": "1:2",
                            "machine_roasting_level": 3,
                            "temperature": 92,
                        },
                        {
                            "roast_level": "Dark",
                            "stoichio_ratio": "1:3",
                            "machine_roasting_level": 7,
                            "temperature": 96,
                        },
                    ],
                },
            }
        )
        attrs = s.extra_state_attributes
        assert attrs["bean_types"] == ["Arabica", "Robusta"]
        assert attrs["roasting_levels"] == ["Light", "Medium", "Dark"]
        assert attrs["grinder_range"] == "1-8 step=1"
        assert attrs["flow_range"] == "5-25 delta=5"
        assert attrs["preinfusion_water"] == "5-30 mL"
        assert attrs["powder_qty_light_roast"] == "8g"
        assert attrs["powder_qty_dark_roast"] == "10g"
        assert attrs["roast_light_stoichio"] == "1:2"
        assert attrs["roast_light_machine_level"] == 3
        assert attrs["roast_light_temperature"] == 92
        assert attrs["roast_dark_temperature"] == 96


class TestSetupEntry:
    def test_creates_status_profile_bean_and_all_counter_sensors(self):
        hass = MagicMock()
        entry = MagicMock()
        entry.entry_id = "eid"
        coord = MagicMock()
        hass.data = {
            "delonghi_coffee": {
                entry.entry_id: {
                    "coordinator": coord,
                    "dsn": "DSN-S",
                    "model": "ECAM",
                    "device_name": "Test",
                    "sw_version": "1.0",
                }
            }
        }
        added: list = []
        async_add = MagicMock(side_effect=lambda ents: added.extend(ents))
        asyncio.run(sensor_mod.async_setup_entry(hass, entry, async_add))

        # 6 named sensors (Status, Profile, Bean, BlockingAlarms, ApiUsage, LAN diagnostic)
        # + every counter
        assert len(added) == 6 + len(COUNTER_SENSORS)
        kinds = {type(e).__name__ for e in added}
        assert {
            "DeLonghiStatusSensor",
            "DeLonghiProfileSensor",
            "DeLonghiBeanSensor",
            "DeLonghiBlockingAlarmsSensor",
            "DeLonghiApiUsageSensor",
            "DeLonghiLanDiagnosticSensor",
        } <= kinds
        assert sum(1 for e in added if isinstance(e, DeLonghiCounterSensor)) == len(COUNTER_SENSORS)

    def test_setup_entry_handles_missing_sw_version(self):
        hass = MagicMock()
        entry = MagicMock()
        entry.entry_id = "eid"
        hass.data = {
            "delonghi_coffee": {
                entry.entry_id: {
                    "coordinator": MagicMock(),
                    "dsn": "DSN-S",
                    "model": "ECAM",
                    "device_name": "Test",
                    # sw_version absent
                }
            }
        }
        added: list = []
        async_add = MagicMock(side_effect=lambda ents: added.extend(ents))
        asyncio.run(sensor_mod.async_setup_entry(hass, entry, async_add))
        assert added  # smoke


class TestDeviceInfo:
    def test_includes_sw_version_when_present(self):
        info = sensor_mod._device_info("DSN-A", "ECAM", "Coffee", "v3.1")
        assert info == {
            "identifiers": {("delonghi_coffee", "DSN-A")},
            "name": "Coffee",
            "manufacturer": "De'Longhi",
            "model": "ECAM",
            "sw_version": "v3.1",
        }

    def test_omits_sw_version_when_none(self):
        info = sensor_mod._device_info("DSN-A", "ECAM", "Coffee", None)
        assert "sw_version" not in info


class TestBinarySensorSetup:
    def test_setup_creates_one_sensor_per_known_alarm(self):
        hass = MagicMock()
        entry = MagicMock()
        entry.entry_id = "eid"
        coord = MagicMock()
        hass.data = {
            "delonghi_coffee": {
                entry.entry_id: {
                    "coordinator": coord,
                    "dsn": "DSN-B",
                    "model": "ECAM",
                    "device_name": "Coffee",
                    "sw_version": "1.0",
                }
            }
        }
        added: list = []
        async_add = MagicMock(side_effect=lambda ents: added.extend(ents))
        asyncio.run(binary_sensor_mod.async_setup_entry(hass, entry, async_add))
        assert len(added) == len(ALARMS)
        assert all(isinstance(e, DeLonghiAlarmSensor) for e in added)


class TestAlarmSensorIsOn:
    def test_is_on_returns_none_when_alarm_word_missing(self):
        coord = MagicMock()
        coord.data = {"alarm_word": None}
        coord.seen_alarm_bits = set()
        sensor = _make_alarm_sensor(coord, bit=0)
        assert sensor.is_on is None


class TestBlockingAlarmsSensor:
    """Aggregator that surfaces *blocking* alarms — the ones that prevent
    the machine from completing Turning On → Ready."""

    def _sensor(self, alarms_data):
        coord = MagicMock()
        coord.data = {"alarms": alarms_data}
        return DeLonghiBlockingAlarmsSensor(coord, "DSN-B", "Eletta Explore", "Coffee", "1.0")

    def test_zero_when_no_alarms(self):
        s = self._sensor([])
        assert s.native_value == 0
        attrs = s.extra_state_attributes
        assert attrs["blocking_alarms"] == []
        assert attrs["blocking_bits"] == []
        assert attrs["is_blocking_power_on"] is False
        assert attrs["all_active_alarms"] == []

    def test_zero_when_only_non_blocking_alarms(self):
        # Descale Needed (bit 2) + Cleaning Needed (bit 16) — both informative
        s = self._sensor(
            [
                {"bit": 2, "name": "Descale Needed"},
                {"bit": 16, "name": "Cleaning Needed"},
            ]
        )
        assert s.native_value == 0
        attrs = s.extra_state_attributes
        assert attrs["blocking_alarms"] == []
        assert attrs["is_blocking_power_on"] is False
        # All alarms still surface for context
        assert attrs["all_active_alarms"] == ["Descale Needed", "Cleaning Needed"]

    def test_counts_blocking_alarms(self):
        s = self._sensor(
            [
                {"bit": 0, "name": "Water Tank Empty"},
                {"bit": 2, "name": "Descale Needed"},  # not blocking
                {"bit": 12, "name": "Hydraulic Problem"},
            ]
        )
        assert s.native_value == 2
        attrs = s.extra_state_attributes
        assert attrs["blocking_alarms"] == ["Water Tank Empty", "Hydraulic Problem"]
        assert attrs["blocking_bits"] == [0, 12]
        assert attrs["is_blocking_power_on"] is True
        assert "Descale Needed" in attrs["all_active_alarms"]

    def test_handles_missing_alarms_key(self):
        coord = MagicMock()
        coord.data = {}  # no 'alarms' key at all
        s = DeLonghiBlockingAlarmsSensor(coord, "DSN-B", "Eletta Explore", "Coffee", "1.0")
        assert s.native_value == 0
        assert s.extra_state_attributes["blocking_alarms"] == []

    def test_handles_alarm_without_name_falls_back_to_bit(self):
        s = self._sensor(
            [
                {"bit": 0},  # no name field — must not crash
                {"bit": 99, "name": ""},  # empty name — same fallback
            ]
        )
        attrs = s.extra_state_attributes
        # Bit 0 (Water Tank Empty) is blocking, label fallback "bit0"
        assert "bit0" in attrs["blocking_alarms"]
        # Bit 99 unknown alarm, surfaces in all_active with "bit99" label
        assert "bit99" in attrs["all_active_alarms"]


class TestCounterSensorBaselineWithStringCloudValue:
    """Audit 2026-11-05: cloud counter kept as a raw string by parse_counters
    (int() failure) crashed the baseline comparison paths with TypeError.
    LocalBaselineStore.merge guarded it; native_value's scaling and
    extra_state_attributes' source computation did not.
    """

    def _store_with_baseline(self, key: str, value: int):
        from custom_components.delonghi_coffee.local_baseline import LocalBaselineStore

        store = LocalBaselineStore(MagicMock(), "DSN123")
        asyncio.run(store.async_set_many({key: value}))
        return store

    def _coord_with(self, counters, store):
        coord = MagicMock()
        coord.data = {"counters": counters}
        coord.local_baseline = store
        return coord

    def test_scaled_string_cloud_value_yields_unknown(self):
        store = self._store_with_baseline("total_water_ml", 100)
        coord = self._coord_with({"total_water_ml": "not-a-number"}, store)
        sensor = _make_counter_sensor(coord, "total_water_ml")
        # merge() returns the string (incompatible types → cloud wins). The
        # old fix returned it verbatim — but with a state_class set, HA's
        # SensorEntity.state then raises ValueError on EVERY state write
        # (audit 2026-11-05). Numeric-or-None is the only HA-valid contract.
        assert sensor.native_value is None

    def test_string_counter_warns_once(self, caplog):
        import logging as _logging

        store = self._store_with_baseline("total_water_ml", 100)
        coord = self._coord_with({"total_water_ml": "not-a-number"}, store)
        sensor = _make_counter_sensor(coord, "total_water_ml")
        with caplog.at_level(_logging.WARNING, logger="custom_components.delonghi_coffee.sensor"):
            for _ in range(3):
                assert sensor.native_value is None
        warns = [r for r in caplog.records if "non-numeric" in r.getMessage()]
        assert len(warns) == 1, "warning must fire once per entity, not per state write"

    def test_attributes_source_baseline_on_incompatible_types(self):
        store = self._store_with_baseline("total_water_ml", 100)
        coord = self._coord_with({"total_water_ml": "not-a-number"}, store)
        sensor = _make_counter_sensor(coord, "total_water_ml")
        # used to raise TypeError on ``str < int``
        attrs = sensor.extra_state_attributes
        assert attrs["source"] == "baseline"

    def test_numeric_cloud_still_wins_and_scales(self):
        """Happy path unchanged: numeric cloud > baseline → cloud + scaled."""
        store = self._store_with_baseline("total_water_ml", 100)
        coord = self._coord_with({"total_water_ml": 500}, store)
        sensor = _make_counter_sensor(coord, "total_water_ml")
        assert sensor.native_value == 0.5
        assert sensor.extra_state_attributes["source"] == "cloud"

    def test_scaled_string_without_baseline_yields_unknown(self):
        """No baseline at all: string → unknown (was a crash, then a string
        HA would reject — both fixed by the numeric guard)."""
        coord = MagicMock()
        coord.data = {"counters": {"total_water_ml": "n/a"}}
        coord.local_baseline = None
        sensor = _make_counter_sensor(coord, "total_water_ml")
        assert sensor.native_value is None

    def test_missing_counter_yields_unknown_without_warning(self, caplog):
        """A missing key (val None) is the ordinary ``unknown`` case — it must
        not be treated as a non-numeric anomaly."""
        import logging as _logging

        coord = MagicMock()
        coord.data = {"counters": {}}
        coord.local_baseline = None
        sensor = _make_counter_sensor(coord, "total_water_ml")
        with caplog.at_level(_logging.WARNING, logger="custom_components.delonghi_coffee.sensor"):
            assert sensor.native_value is None
        assert not [r for r in caplog.records if "non-numeric" in r.getMessage()]


class TestAlarmQuietBits:
    """Issue #108: striker bit 16 is a transient rinse flag, not "Cleaning Needed".

    The entity must be registered disabled by default on Eletta Explore only;
    every other model/bit keeps the enabled default, and the state logic is
    untouched (an enabled entity still reports the raw bit).
    """

    def _coord(self, alarm_word: int):
        coord = MagicMock()
        coord.data = {"alarm_word": alarm_word}
        coord.seen_alarm_bits = set()
        coord.available = True
        return coord

    def test_eletta_explore_bit16_disabled_by_default(self):
        sensor = _make_alarm_sensor(self._coord(0), bit=16, model="Eletta Explore")
        assert getattr(sensor, "_attr_entity_registry_enabled_default", True) is False

    def test_eletta_explore_other_bits_stay_enabled(self):
        sensor = _make_alarm_sensor(self._coord(0), bit=2, model="Eletta Explore")
        assert getattr(sensor, "_attr_entity_registry_enabled_default", True) is not False

    def test_other_models_bit16_stays_enabled(self):
        sensor = _make_alarm_sensor(self._coord(0), bit=16, model="PrimaDonna Soul")
        assert getattr(sensor, "_attr_entity_registry_enabled_default", True) is not False

    def test_state_logic_unchanged_when_enabled(self):
        """A user who re-enables the entity still sees the raw bit."""
        sensor = _make_alarm_sensor(self._coord(1 << 16), bit=16, model="Eletta Explore")
        assert sensor.is_on is True
