"""
ADS1115 16-bit ADC Driver

Direct I2C register access via smbus2 for the ADS1115 on the WQM-1 HAT.
Single-shot mode, PGA ±4.096V, 128 SPS.
"""

import logging
import time

import smbus2

from utils.config import ADS1115_ADDR, I2C_BUS

logger = logging.getLogger("wqm1.adc")

# ADS1115 register addresses
_REG_CONVERSION = 0x00
_REG_CONFIG = 0x01

# Config register bit layout (MSB first)
# [15]    OS       : 1 = start single conversion
# [14:12] MUX      : channel select (100=AIN0, 101=AIN1, 110=AIN2, 111=AIN3)
# [11:9]  PGA      : gain (001 = ±4.096V)
# [8]     MODE     : 1 = single-shot
# [7:5]   DR       : data rate (100 = 128 SPS)
# [4]     COMP_MODE: 0 = traditional
# [3]     COMP_POL : 0 = active low
# [2]     COMP_LAT : 0 = non-latching
# [1:0]   COMP_QUE : 11 = disable comparator

_OS_START = 0x8000
_MUX_BASE = 0x4000  # AIN0 vs GND; add channel << 12
_PGA_4096 = 0x0200  # ±4.096V (LSB = 125 µV)
_MODE_SINGLE = 0x0100
_DR_128 = 0x0080
_COMP_DISABLE = 0x0003

_BASE_CONFIG = _OS_START | _PGA_4096 | _MODE_SINGLE | _DR_128 | _COMP_DISABLE

# Full-scale voltage for PGA ±4.096V
_FSR = 4.096
# LSB size = FSR / 32768 (15-bit + sign)
_LSB_V = _FSR / 32768.0

# Conversion time at 128 SPS = ~7.8 ms; poll up to 20 ms
_CONV_TIMEOUT_S = 0.020
_POLL_INTERVAL_S = 0.001


class ADS1115:
    """ADS1115 16-bit ADC over I2C (smbus2)."""

    def __init__(self, bus: int = I2C_BUS, address: int = ADS1115_ADDR):
        self._address = address
        try:
            self._bus = smbus2.SMBus(bus)
            # Verify device is reachable by reading config register
            self._bus.read_word_data(self._address, _REG_CONFIG)
            logger.info("ADS1115 initialised at 0x%02X on bus %d", address, bus)
        except Exception as e:
            self._bus = None
            raise RuntimeError(f"ADS1115 init failed: {e}") from e

    def read_raw(self, channel: int) -> int:
        """
        Read raw signed 16-bit value from a single-ended channel.

        Args:
            channel: 0-3 (AIN0-AIN3)

        Returns:
            Signed 16-bit ADC value (-32768 to 32767)
        """
        if not 0 <= channel <= 3:
            raise ValueError(f"Channel must be 0-3, got {channel}")
        if self._bus is None:
            raise RuntimeError("ADS1115 bus not available")

        # Build config: set MUX for single-ended channel
        mux = (0x04 + channel) << 12
        config = _OS_START | mux | _PGA_4096 | _MODE_SINGLE | _DR_128 | _COMP_DISABLE

        # Write config to start conversion (big-endian)
        config_bytes = [(config >> 8) & 0xFF, config & 0xFF]
        self._bus.write_i2c_block_data(self._address, _REG_CONFIG, config_bytes)

        # Poll for conversion complete (OS bit goes high)
        deadline = time.monotonic() + _CONV_TIMEOUT_S
        while time.monotonic() < deadline:
            data = self._bus.read_i2c_block_data(self._address, _REG_CONFIG, 2)
            if data[0] & 0x80:  # OS bit set = conversion done
                break
            time.sleep(_POLL_INTERVAL_S)

        # Read conversion result (big-endian signed 16-bit)
        result = self._bus.read_i2c_block_data(self._address, _REG_CONVERSION, 2)
        raw = (result[0] << 8) | result[1]
        # Convert to signed
        if raw >= 0x8000:
            raw -= 0x10000
        return raw

    def read_voltage(self, channel: int) -> float:
        """
        Read voltage from a single-ended channel.

        Args:
            channel: 0-3 (AIN0-AIN3)

        Returns:
            Voltage in volts (0 to ~4.096V for positive inputs)
        """
        raw = self.read_raw(channel)
        return raw * _LSB_V

    def read_all(self) -> dict:
        """
        Read voltage from all 4 channels.

        Returns:
            Dict mapping channel number (0-3) to voltage.
        """
        return {ch: self.read_voltage(ch) for ch in range(4)}

    def close(self):
        """Release I2C bus."""
        if self._bus:
            self._bus.close()
            self._bus = None
