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

# lgpio (sx1262 DIO1 edge detection; imported defensively since HW1)
_lgpio = MagicMock()
sys.modules["lgpio"] = _lgpio

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


# ---------------------------------------------------------------------------
# Pin-level recorder.
#
# A bare MagicMock records that output() was called but not what the pin is
# *now*, so it cannot answer the only question that matters for relay safety:
# "is this coil energised?". Fail-safe means de-energised, so the tests have to
# assert physical level, not call history. This layers a small state recorder
# over the existing GPIO mock — call assertions in existing tests keep working.
# ---------------------------------------------------------------------------

_pin_levels: dict[int, int] = {}


def _record_output(pin, level):
    _pin_levels[int(pin)] = int(level)


def _record_setup(pin, _mode, initial=None, **_kwargs):
    if initial is not None:
        _pin_levels[int(pin)] = int(initial)


def _install_pin_recorder() -> None:
    """(Re)attach the recorder — reset_mock() drops side effects."""
    _gpio.output.side_effect = _record_output
    _gpio.setup.side_effect = _record_setup


_install_pin_recorder()


@pytest.fixture
def gpio_pins():
    """
    Live {bcm_pin: level} map, where 1 = energised coil and 0 = de-energised.

    De-energised is the fail-safe state for every channel: it is the only state
    a crashed or unpowered Pi can hold. What that does physically depends on the
    contact — NC de-energised leaves the load RUNNING, NO leaves it STOPPED.
    """
    return _pin_levels


@pytest.fixture(autouse=True)
def mock_hardware():
    """Provide access to mock hardware objects and reset between tests."""
    _gpio.reset_mock()
    _pin_levels.clear()
    _install_pin_recorder()
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
    _serial_mod.Serial.side_effect = None
    _serial_mod.Serial.return_value = _serial_inst
    _serial_mod.EIGHTBITS = 8
    _serial_mod.PARITY_NONE = "N"
    _serial_mod.STOPBITS_ONE = 1

    yield {
        "gpio": _gpio,
        "pins": _pin_levels,
        "smbus2": _smbus2,
        "bus": _bus,
        "spidev": _spidev,
        "spi": _spi,
        "serial_mod": _serial_mod,
        "serial": _serial_inst,
        "w1": _w1,
        "w1_sensor": _w1_sensor,
    }
