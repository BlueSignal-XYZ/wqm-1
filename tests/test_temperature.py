"""Tests for DS18B20 temperature sensor (sensors/temperature.py)."""

import sys
from unittest.mock import MagicMock


class TestDS18B20Init:
    def test_init_when_w1thermsensor_available(self):
        from sensors.temperature import DS18B20

        sensor = DS18B20()
        assert sensor._sensor is not None or isinstance(sensor._sensor, MagicMock)

    def test_init_when_no_sensor_found(self):
        w1_mod = sys.modules["w1thermsensor"]
        original = w1_mod.W1ThermSensor
        w1_mod.W1ThermSensor = MagicMock(side_effect=w1_mod.NoSensorFoundError)
        try:
            from importlib import reload

            import sensors.temperature as temp_mod

            reload(temp_mod)
            sensor = temp_mod.DS18B20()
            assert sensor._sensor is None
        finally:
            w1_mod.W1ThermSensor = original

    def test_init_when_generic_exception(self):
        w1_mod = sys.modules["w1thermsensor"]
        original = w1_mod.W1ThermSensor
        w1_mod.W1ThermSensor = MagicMock(side_effect=RuntimeError("bus error"))
        try:
            from importlib import reload

            import sensors.temperature as temp_mod

            reload(temp_mod)
            sensor = temp_mod.DS18B20()
            assert sensor._sensor is None
        finally:
            w1_mod.W1ThermSensor = original


class TestAvailable:
    def test_available_true_when_sensor_exists(self):
        from sensors.temperature import DS18B20

        sensor = DS18B20()
        sensor._sensor = MagicMock()
        assert sensor.available() is True

    def test_available_false_when_no_sensor(self):
        from sensors.temperature import DS18B20

        sensor = DS18B20()
        sensor._sensor = None
        assert sensor.available() is False


class TestReadTempC:
    def test_read_returns_temperature(self):
        from sensors.temperature import DS18B20

        sensor = DS18B20()
        sensor._sensor = MagicMock()
        sensor._sensor.get_temperature.return_value = 22.5
        assert sensor.read_temp_c() == 22.5

    def test_read_returns_none_when_no_sensor(self):
        from sensors.temperature import DS18B20

        sensor = DS18B20()
        sensor._sensor = None
        assert sensor.read_temp_c() is None

    def test_read_returns_none_on_exception(self):
        from sensors.temperature import DS18B20

        sensor = DS18B20()
        sensor._sensor = MagicMock()
        sensor._sensor.get_temperature.side_effect = RuntimeError("read fail")
        assert sensor.read_temp_c() is None

    def test_read_negative_temperature(self):
        from sensors.temperature import DS18B20

        sensor = DS18B20()
        sensor._sensor = MagicMock()
        sensor._sensor.get_temperature.return_value = -5.2
        assert sensor.read_temp_c() == -5.2

    def test_read_zero_temperature(self):
        from sensors.temperature import DS18B20

        sensor = DS18B20()
        sensor._sensor = MagicMock()
        sensor._sensor.get_temperature.return_value = 0.0
        assert sensor.read_temp_c() == 0.0
