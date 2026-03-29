"""
TDS (Total Dissolved Solids) Sensor Module

Reads TDS from ADS1115 AIN1 through CD4060 AC excitation and LM324
conditioning with R57/R58 voltage divider (ratio 0.3125).
Temperature compensation applied per reading.
"""

import logging
from statistics import median

from sensors.ads1115 import ADS1115
from utils.config import ADC_CH_TDS, TDS_DIVIDER_RATIO, TDS_TEMP_COEFF

logger = logging.getLogger("wqm1.tds")


class TDSSensor:
    """TDS sensor with temperature compensation."""

    def __init__(self, adc: ADS1115):
        self._adc = adc
        self._window = []
        self._window_size = 5
        self._k = 500.0  # default, overridden by CalibrationManager

    def set_calibration(self, k: float) -> None:
        """Set TDS calibration coefficient (ppm per volt)."""
        self._k = k
        self._window.clear()
        logger.info("TDS calibration coefficient set to %.2f", k)

    def read(self, temp_c: float | None = 25.0) -> float | None:
        """
        Read TDS in ppm.

        Args:
            temp_c: Water temperature for compensation (default 25°C)

        Returns:
            TDS in ppm or None on read failure
        """
        try:
            adc_voltage = self._adc.read_voltage(ADC_CH_TDS)
        except Exception as e:
            logger.error("TDS ADC read failed: %s", e)
            return None

        # Compensate for voltage divider: actual = adc / ratio
        actual_voltage = adc_voltage / TDS_DIVIDER_RATIO

        # Temperature compensation: adjust for deviation from 25°C
        comp_coeff = 1.0 + TDS_TEMP_COEFF * (temp_c - 25.0) if temp_c is not None else 1.0

        # Avoid division by zero
        if comp_coeff <= 0:
            comp_coeff = 1.0

        compensated_voltage = actual_voltage / comp_coeff

        # Convert voltage to TDS: ppm = voltage * k
        tds_ppm = compensated_voltage * self._k

        # Clamp to non-negative
        tds_ppm = max(0.0, tds_ppm)

        # Moving median filter
        self._window.append(tds_ppm)
        if len(self._window) > self._window_size:
            self._window = self._window[-self._window_size :]

        return round(float(median(self._window)), 1)
