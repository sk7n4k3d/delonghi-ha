"""Sensor and binary-sensor entity tests.

Focus on the ``available`` semantics introduced for issue #3: sensors
whose underlying counter is absent from the coordinator data must report
``unavailable`` (not ``unknown``) and alarm binary sensors must stay
``unavailable`` until the machine actually reports a supported alarm
word for the relevant bit.
"""

import asyncio
from unittest.mock import MagicMock

from custom_components.delonghi_coffee import binary_sensor as binary_sensor_mod
from custom_components.delonghi_coffee import sensor as sensor_mod
from custom_components.delonghi_coffee.binary_sensor import DeLonghiAlarmSensor
from custom_components.delonghi_coffee.const import ALARMS
from custom_components.delonghi_coffee.sensor import (
    COUNTER_SENSORS,
    DeLonghiBeanSensor,
    DeLonghiCounterSensor,
    DeLonghiProfileSensor,
    DeLonghiStatusSensor,
)


def _make_counter_sensor(coord, key: str) -> DeLonghiCounterSensor:
    meta = COUNTER_SENSORS[key]
    return DeLonghiCounterSensor(coord, "DSN123", "Eletta", "Machine", None, key, meta)


def _make_alarm_sensor(coord, bit: int, inverted: bool = False) -> DeLonghiAlarmSensor:
    meta = {"icon": "mdi:alert", "inverted": inverted}
    return DeLonghiAlarmSensor(coord, "DSN123", "Model", "Machine", None, bit, meta)


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
        assert attrs["api_calls_hour"] == 0
        assert attrs["api_total_calls"] == 0
        # No lan_config block → no lan_enabled key
        assert "lan_enabled" not in attrs

    def test_extra_state_attributes_full(self):
        s = self._make({
            "status": "ONLINE",
            "profile": 2,
            "alarms": [{"name": "Water tank empty"}, {"name": "Grounds full"}],
            "api_rate": 12,
            "api_total_calls": 4567,
            "lan_config": {"lan_enabled": True, "lan_ip": "10.0.0.42"},
            "drink_catalog": {"espresso": {}, "americano": {}, "cappuccino": {}},
        })
        attrs = s.extra_state_attributes
        assert attrs["cloud_status"] == "ONLINE"
        assert attrs["profile"] == 2
        assert attrs["active_alarms"] == ["Water tank empty", "Grounds full"]
        assert attrs["alarm_count"] == 2
        assert attrs["api_calls_hour"] == 12
        assert attrs["api_total_calls"] == 4567
        assert attrs["lan_enabled"] is True
        assert attrs["lan_ip"] == "10.0.0.42"
        assert attrs["contentstack_drinks"] == 3


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

    def test_native_value_uses_monitor_profile_first(self):
        s = self._make({
            "profile": 2,
            "active_profile": 3,
            "profiles": {2: {"name": "Sasha"}, 3: {"name": "Anna"}},
        })
        # profile (monitor) wins over active_profile (cloud)
        assert s.native_value == "Sasha"

    def test_native_value_falls_back_to_cloud_when_monitor_zero(self):
        s = self._make({
            "profile": 0,
            "active_profile": 3,
            "profiles": {3: {"name": "Anna"}},
        })
        assert s.native_value == "Anna"

    def test_native_value_default_label_when_unnamed(self):
        s = self._make({"profile": 4, "profiles": {4: {}}})
        assert s.native_value == "Profile 4"

    def test_native_value_default_label_when_unknown_profile(self):
        s = self._make({"profile": 5, "profiles": {}})
        assert s.native_value == "Profile 5"

    def test_extra_state_attributes_lists_all_profiles(self):
        s = self._make({
            "active_profile": 2,
            "profiles": {
                1: {"name": "A", "color": "red", "figure": "1"},
                2: {"name": "B", "color": "blue", "figure": "2"},
            },
        })
        attrs = s.extra_state_attributes
        assert attrs["active_profile_id"] == 2
        assert attrs["profile_1_name"] == "A"
        assert attrs["profile_1_color"] == "red"
        assert attrs["profile_1_figure"] == "1"
        assert attrs["profile_2_name"] == "B"


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
        s = self._make({
            "beans": [
                {"id": 1, "name": "Arabica", "english_name": "Arabica", "raw_params_hex": "deadbeef"},
                {"id": 2, "name": "Robusta", "english_name": "Robusta"},  # no raw_hex
            ],
            "coffee_beans_count": 12,
        })
        attrs = s.extra_state_attributes
        assert attrs["bean_1_name"] == "Arabica"
        assert attrs["bean_1_english"] == "Arabica"
        assert attrs["bean_1_raw_params_hex"] == "deadbeef"
        assert attrs["bean_2_name"] == "Robusta"
        assert "bean_2_raw_params_hex" not in attrs
        assert attrs["coffee_beans_catalog_count"] == 12

    def test_extra_state_attributes_bean_system_par(self):
        s = self._make({
            "beans": [],
            "bean_system_par": {"raw_hex": "cafebabe", "raw_bytes": 4},
        })
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
        s = self._make({
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
                    {"roast_level": "Light", "stoichio_ratio": "1:2", "machine_roasting_level": 3, "temperature": 92},
                    {"roast_level": "Dark", "stoichio_ratio": "1:3", "machine_roasting_level": 7, "temperature": 96},
                ],
            },
        })
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

        # 3 named sensors + every counter
        assert len(added) == 3 + len(COUNTER_SENSORS)
        kinds = {type(e).__name__ for e in added}
        assert {"DeLonghiStatusSensor", "DeLonghiProfileSensor", "DeLonghiBeanSensor"} <= kinds
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
