"""
Turbidity Sensor Module

Reads turbidity from ADS1115 AIN2. Linear mapping:
4.1V = 0 NTU (clear water), 0.5V ≈ 3000 NTU.
"""

import logging
from statistics import median

from sensors.ads1115 import ADS1115
from utils.config import ADC_CH_TURBIDITY, TURB_NTU_MAX, TURB_V_CLEAR, TURB_V_MAX

logger = logging.getLogger("wqm1.turbidity")


class TurbiditySensor:
    """Turbidity sensor with voltage-to-NTU linear conversion."""

    def __init__(self, adc: ADS1115):
        self._adc = adc
        self._window = []
        self._window_size = 5
        self._v_clear = TURB_V_CLEAR

    def set_clear_water_voltage(self, voltage: float) -> None:
        """Calibrate with clear water voltage."""
        self._v_clear = voltage
        self._window.clear()
        logger.info("Turbidity clear-water voltage set to %.3f V", voltage)

    def read(self) -> float | None:
        """
        Read turbidity in NTU.

        Returns:
            Turbidity in NTU (0-3000) or None on read failure
        """
        try:
            voltage = self._adc.read_voltage(ADC_CH_TURBIDITY)
        except Exception as e:
            logger.error("Turbidity ADC read failed: %s", e)
            return None

        # Linear mapping: v_clear -> 0 NTU, v_max -> NTU_MAX
        # NTU = (v_clear - voltage) * NTU_MAX / (v_clear - v_max)
        v_range = self._v_clear - TURB_V_MAX
        if v_range <= 0:
            logger.error("Invalid turbidity voltage range")
            return None

        ntu = (self._v_clear - voltage) * TURB_NTU_MAX / v_range

        # Clamp
        ntu = max(0.0, min(TURB_NTU_MAX, ntu))

        # Moving median filter
        self._window.append(ntu)
        if len(self._window) > self._window_size:
            self._window = self._window[-self._window_size :]

        return round(float(median(self._window)), 1)
