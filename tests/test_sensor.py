"""Sensor and binary-sensor entity tests.

Focus on the ``available`` semantics introduced for issue #3: sensors
whose underlying counter is absent from the coordinator data must report
``unavailable`` (not ``unknown``) and alarm binary sensors must stay
``unavailable`` until the machine actually reports a supported alarm
word for the relevant bit.
"""

from unittest.mock import MagicMock

from custom_components.delonghi_coffee.binary_sensor import DeLonghiAlarmSensor
from custom_components.delonghi_coffee.sensor import (
    COUNTER_SENSORS,
    DeLonghiCounterSensor,
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
