"""
ADS1115 16-bit ADC Driver

Direct I2C register access via smbus2 for the ADS1115 on the WQM-1 HAT.
Single-shot mode, 128 SPS, programmable gain chosen PER CHANNEL to match the
conditioning chain in front of each input (PCBA rev Fin_3):

    AIN0  TDS        LM324 rectifier + LPF, silk 0-2.3 V   -> PGA ±4.096 V
    AIN1  Turbidity  LMV321 buffer, silk 0-4.5 V           -> PGA ±6.144 V
    AIN2  pH         LMP91200 VOUT (biased near 1-2 V)    -> PGA ±4.096 V
    AIN3  pH ref     LMP91200 VOCM (no ORP circuit)        -> PGA ±4.096 V

The converter is powered from the +5VA rail, so an input up to ~5 V is
legal; what limited the turbidity channel was the PGA. At ±4.096 V a clear-
water signal of 4.1-4.5 V saturates at 4.096 V, so the cleanest ~0.4 V of the
range (roughly the first 300 NTU above "clear") read as one flat value and a
clear-water calibration was taken AT the saturation point. ±6.144 V keeps the
whole 0-4.5 V chain inside the converter's range (LSB 187.5 µV).
"""

import logging
import time
from typing import Any

from utils.config import ADS1115_ADDR, I2C_BUS

logger = logging.getLogger("wqm1.adc")

try:
    import smbus2
except ImportError:  # non-Pi host (e.g. Arduino UNO Q): kernel I2C is not
    smbus2 = None  # exposed to Linux — the board gate never constructs this

# ADS1115 register addresses
_REG_CONVERSION = 0x00
_REG_CONFIG = 0x01

# Config register bit layout (MSB first)
# [15]    OS       : 1 = start single conversion
# [14:12] MUX      : channel select (100=AIN0, 101=AIN1, 110=AIN2, 111=AIN3)
# [11:9]  PGA      : gain (000 = ±6.144V, 001 = ±4.096V, 010 = ±2.048V ...)
# [8]     MODE     : 1 = single-shot
# [7:5]   DR       : data rate (100 = 128 SPS)
# [4]     COMP_MODE: 0 = traditional
# [3]     COMP_POL : 0 = active low
# [2]     COMP_LAT : 0 = non-latching
# [1:0]   COMP_QUE : 11 = disable comparator

_OS_START = 0x8000
_PGA_6144 = 0x0000  # ±6.144V (LSB = 187.5 µV)
_PGA_4096 = 0x0200  # ±4.096V (LSB = 125 µV)
_MODE_SINGLE = 0x0100
_DR_128 = 0x0080
_COMP_DISABLE = 0x0003

# Full-scale voltage per PGA code
_FSR_BY_PGA = {_PGA_6144: 6.144, _PGA_4096: 4.096}

# PGA per single-ended channel — see module docstring. Anything not listed
# uses ±4.096 V.
_PGA_BY_CHANNEL = {
    0: _PGA_4096,  # TDS, 0-2.3 V chain
    1: _PGA_6144,  # turbidity, 0-4.5 V chain
    2: _PGA_4096,  # pH VOUT
    3: _PGA_4096,  # pH VOCM / spare
}
_DEFAULT_PGA = _PGA_4096

# Kept for callers that import them: the default (non-turbidity) scale.
_FSR = _FSR_BY_PGA[_DEFAULT_PGA]
_LSB_V = _FSR / 32768.0

# Conversion time at 128 SPS = ~7.8 ms; poll up to 20 ms
_CONV_TIMEOUT_S = 0.020
_POLL_INTERVAL_S = 0.001


def channel_full_scale_v(channel: int) -> float:
    """Full-scale voltage of the PGA the driver uses for ``channel``."""
    return _FSR_BY_PGA[_PGA_BY_CHANNEL.get(channel, _DEFAULT_PGA)]


class ADS1115:
    """ADS1115 16-bit ADC over I2C (smbus2)."""

    def __init__(self, bus: int = I2C_BUS, address: int = ADS1115_ADDR) -> None:
        if smbus2 is None:
            raise RuntimeError("smbus2 not installed — no direct I2C on this host")
        self._address = address
        self._bus: Any = None
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
            Signed 16-bit ADC value (-32768 to 32767), scaled to the
            channel's PGA (see ``channel_full_scale_v``).
        """
        if not 0 <= channel <= 3:
            raise ValueError(f"Channel must be 0-3, got {channel}")
        if self._bus is None:
            raise RuntimeError("ADS1115 bus not available")

        # Build config: set MUX for single-ended channel + this channel's PGA
        mux = (0x04 + channel) << 12
        pga = _PGA_BY_CHANNEL.get(channel, _DEFAULT_PGA)
        config = _OS_START | mux | pga | _MODE_SINGLE | _DR_128 | _COMP_DISABLE

        # Write config to start conversion (big-endian)
        config_bytes = [(config >> 8) & 0xFF, config & 0xFF]
        self._bus.write_i2c_block_data(self._address, _REG_CONFIG, config_bytes)

        # Poll for conversion complete (OS bit goes high)
        deadline = time.monotonic() + _CONV_TIMEOUT_S
        done = False
        while time.monotonic() < deadline:
            data = self._bus.read_i2c_block_data(self._address, _REG_CONFIG, 2)
            if data[0] & 0x80:  # OS bit set = conversion done
                done = True
                break
            time.sleep(_POLL_INTERVAL_S)
        if not done:
            # The conversion register would hold the PREVIOUS channel's
            # result; publishing that as this channel is worse than no number.
            raise RuntimeError(f"ADS1115 conversion on AIN{channel} did not complete")

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
            Voltage in volts (0 to the channel's full scale for positive inputs)
        """
        raw = self.read_raw(channel)
        return raw * channel_full_scale_v(channel) / 32768.0

    def read_all(self) -> dict[int, float]:
        """
        Read voltage from all 4 channels.

        Returns:
            Dict mapping channel number (0-3) to voltage.
        """
        return {ch: self.read_voltage(ch) for ch in range(4)}

    def close(self) -> None:
        """Release I2C bus."""
        if self._bus:
            self._bus.close()
            self._bus = None
