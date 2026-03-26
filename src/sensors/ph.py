"""
pH Sensor Module

Reads pH from ADS1115 AIN0 through the LMP91200 instrumentation amplifier
and LM324 signal conditioning chain. Uses two-point calibration (pH 4.0 / 7.0)
with Nernst equation and temperature compensation.
"""

import logging
from statistics import median

from sensors.ads1115 import ADS1115
from utils.config import ADC_CH_PH, NERNST_F, NERNST_R, NERNST_SLOPE_25C

logger = logging.getLogger("wqm1.ph")


def _nernst_slope(temp_c: float) -> float:
    """Calculate Nernst slope (V/pH) at a given temperature."""
    t_kelvin = temp_c + 273.15
    return (NERNST_R * t_kelvin) / NERNST_F


class PHSensor:
    """pH electrode reading with two-point calibration."""

    def __init__(self, adc: ADS1115):
        self._adc = adc
        self._window = []
        self._window_size = 5

        # Default calibration (overridden by CalibrationManager via set_calibration)
        self._v_ph4 = 1.04
        self._v_ph7 = 1.50
        self._recalc_slope()

    def _recalc_slope(self) -> None:
        """Recalculate slope from two-point calibration voltages."""
        dv = self._v_ph7 - self._v_ph4
        if abs(dv) < 0.001:
            # Prevent division by zero — fall back to Nernst theoretical
            self._slope = NERNST_SLOPE_25C
            logger.warning("pH cal voltages too close, using default Nernst slope")
        else:
            # slope = ΔpH / ΔV = (7.0 - 4.0) / (V_ph7 - V_ph4)
            self._slope = (7.0 - 4.0) / dv

    def set_calibration(self, v_ph4: float, v_ph7: float) -> None:
        """
        Set two-point calibration.

        Args:
            v_ph4: Voltage reading in pH 4.0 buffer
            v_ph7: Voltage reading in pH 7.0 buffer
        """
        self._v_ph4 = v_ph4
        self._v_ph7 = v_ph7
        self._recalc_slope()
        self._window.clear()
        logger.info("pH calibrated: V@4=%.4f V@7=%.4f slope=%.4f pH/V", v_ph4, v_ph7, self._slope)

    def read(self, temp_c: float | None = 25.0) -> float | None:
        """
        Read pH value.

        Args:
            temp_c: Water temperature for Nernst compensation (default 25°C)

        Returns:
            pH value (0-14) or None on read failure
        """
        try:
            voltage = self._adc.read_voltage(ADC_CH_PH)
        except Exception as e:
            logger.error("pH ADC read failed: %s", e)
            return None

        # Apply temperature compensation to slope
        if temp_c is not None and temp_c != 25.0:
            temp_factor = _nernst_slope(temp_c) / _nernst_slope(25.0)
        else:
            temp_factor = 1.0

        # pH = 7.0 + (V_measured - V_ph7) * slope * temp_factor
        ph = 7.0 + (voltage - self._v_ph7) * self._slope * temp_factor

        # Clamp to valid range
        ph = max(0.0, min(14.0, ph))

        # Moving median filter
        self._window.append(ph)
        if len(self._window) > self._window_size:
            self._window = self._window[-self._window_size :]

        return round(float(median(self._window)), 2)
