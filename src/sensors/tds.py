"""
TDS (Total Dissolved Solids) Sensor Module

Reads TDS from ADS1115 AIN0 (VIN0) through CD4060 AC excitation and LM324
conditioning with R57/R58 voltage divider (ratio 0.3125).
Temperature compensation applied per reading.
"""

import logging
from statistics import median

from sensors.ads1115 import ADS1115
from utils.config import (
    ADC_CH_TDS,
    ADC_FULL_SCALE_V,
    ADC_RAIL_MARGIN_V,
    TDS_DIVIDER_RATIO,
    TDS_TEMP_COEFF,
)

logger = logging.getLogger("wqm1.tds")


class TDSSensor:
    """TDS sensor with temperature compensation."""

    def __init__(self, adc: ADS1115) -> None:
        self._adc = adc
        self._window: list[float] = []
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

        # Same open-input problem as pH: a disconnected TDS probe leaves AIN0
        # floating at a rail, and `max(0.0, ...)` below used to turn that into
        # a small, entirely believable ppm figure. Reject it here instead.
        if adc_voltage <= ADC_RAIL_MARGIN_V or adc_voltage >= ADC_FULL_SCALE_V - ADC_RAIL_MARGIN_V:
            logger.warning(
                "TDS input at %.3f V is against a rail (0-%.3f V); probe "
                "disconnected or dry — reporting no reading",
                adc_voltage,
                ADC_FULL_SCALE_V,
            )
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

        # A negative ppm is not a dilute sample, it is a broken signal path or
        # a bad calibration constant. Clamping it to 0.0 published a number the
        # electronics never measured; say nothing instead.
        if tds_ppm < 0:
            logger.warning(
                "TDS %.1f ppm computed from %.3f V is negative; calibration or "
                "signal path fault — reporting no reading",
                tds_ppm,
                adc_voltage,
            )
            return None

        # Moving median filter
        self._window.append(tds_ppm)
        if len(self._window) > self._window_size:
            self._window = self._window[-self._window_size :]

        return round(float(median(self._window)), 1)
