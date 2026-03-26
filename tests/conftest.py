"""
Shared fixtures for firmware tests.

All hardware is mocked at the sys.modules level so tests run on any
platform without RPi.GPIO, smbus2, spidev, or w1thermsensor installed.
"""

import sys
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Install mock hardware modules before any firmware code is imported.
# This runs at collection time, before tests execute.
# ---------------------------------------------------------------------------

# RPi.GPIO
_gpio = MagicMock()
_gpio.BCM = 11
_gpio.OUT = 0
_gpio.IN = 1
_gpio.HIGH = 1
_gpio.LOW = 0
_gpio.RISING = 31
_rpi = MagicMock()
_rpi.GPIO = _gpio
sys.modules["RPi"] = _rpi
sys.modules["RPi.GPIO"] = _gpio

# smbus2
_smbus2 = MagicMock()
_bus = MagicMock()
_bus.read_word_data.return_value = 0x8583
_bus.read_i2c_block_data.return_value = [0x85, 0x83]
_smbus2.SMBus.return_value = _bus
sys.modules["smbus2"] = _smbus2

# spidev
_spidev = MagicMock()
_spi = MagicMock()
_spi.xfer2.return_value = [0] * 10
_spidev.SpiDev.return_value = _spi
sys.modules["spidev"] = _spidev

# serial (pyserial)
_serial_mod = MagicMock()
_serial_inst = MagicMock()
_serial_inst.is_open = True
_serial_inst.readline.return_value = b""
_serial_mod.Serial.return_value = _serial_inst
_serial_mod.EIGHTBITS = 8
_serial_mod.PARITY_NONE = "N"
_serial_mod.STOPBITS_ONE = 1
sys.modules["serial"] = _serial_mod

# w1thermsensor
_w1 = MagicMock()
_w1_sensor = MagicMock()
_w1_sensor.id = "28-0000abcdef"
_w1_sensor.get_temperature.return_value = 22.5
_w1.W1ThermSensor.return_value = _w1_sensor
_w1.NoSensorFoundError = type("NoSensorFoundError", (Exception,), {})
sys.modules["w1thermsensor"] = _w1


@pytest.fixture(autouse=True)
def mock_hardware():
    """Provide access to mock hardware objects and reset between tests."""
    _gpio.reset_mock()
    _smbus2.reset_mock()
    _bus.reset_mock()
    _smbus2.SMBus.side_effect = None
    _smbus2.SMBus.return_value = _bus
    _bus.read_word_data.return_value = 0x8583
    _bus.read_i2c_block_data.return_value = [0x85, 0x83]
    _bus.read_i2c_block_data.side_effect = None
    _spidev.reset_mock()
    _spi.reset_mock()
    _spi.xfer2.return_value = [0] * 10
    _spidev.SpiDev.return_value = _spi
    _serial_mod.reset_mock()
    _serial_inst.reset_mock()
    _serial_inst.is_open = True
    _serial_inst.readline.return_value = b""
    _serial_mod.Serial.return_value = _serial_inst
    _serial_mod.EIGHTBITS = 8
    _serial_mod.PARITY_NONE = "N"
    _serial_mod.STOPBITS_ONE = 1

    yield {
        "gpio": _gpio,
        "smbus2": _smbus2,
        "bus": _bus,
        "spidev": _spidev,
        "spi": _spi,
        "serial_mod": _serial_mod,
        "serial": _serial_inst,
        "w1": _w1,
        "w1_sensor": _w1_sensor,
    }
