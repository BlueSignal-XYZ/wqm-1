"""
pH Sensor Module

Reads pH from ADS1115 AIN2 (PH_INP) through the LMP91200 instrumentation amplifier
and LM324 signal conditioning chain. Uses two-point calibration (pH 4.0 / 7.0)
with Nernst equation and temperature compensation.
"""

import logging
from statistics import median

from sensors.ads1115 import ADS1115
from utils.config import (
    ADC_CH_PH,
    ADC_FULL_SCALE_V,
    ADC_RAIL_MARGIN_V,
    NERNST_F,
    NERNST_R,
    NERNST_SLOPE_25C,
)

logger = logging.getLogger("wqm1.ph")


def _nernst_slope(temp_c: float) -> float:
    """Calculate Nernst slope (V/pH) at a given temperature."""
    t_kelvin = temp_c + 273.15
    return (NERNST_R * t_kelvin) / NERNST_F


class PHSensor:
    """pH electrode reading with two-point calibration."""

    def __init__(self, adc: ADS1115) -> None:
        self._adc = adc
        self._window: list[float] = []
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

        # A disconnected electrode leaves AIN2 floating and it drifts toward a
        # rail. That is not a measurement, and it must not be dressed up as
        # one: reject it before the conversion can make it look plausible.
        if voltage <= ADC_RAIL_MARGIN_V or voltage >= ADC_FULL_SCALE_V - ADC_RAIL_MARGIN_V:
            logger.warning(
                "pH input at %.3f V is against a rail (0-%.3f V); electrode "
                "disconnected or front-end fault — reporting no reading",
                voltage,
                ADC_FULL_SCALE_V,
            )
            return None

        # pH = 7.0 + (V_measured - V_ph7) * slope * temp_factor
        ph = 7.0 + (voltage - self._v_ph7) * self._slope * temp_factor

        # NOT clamped. Clamping is what turned a floating input into a
        # confident, in-range lie: a first field unit with no probe attached
        # reported pH 0.00 and pH 14.00 — the clamp rails — and the cloud
        # raised four critical threshold alerts from pure noise. A value
        # outside 0-14 is not a pH the electrode could produce, so the honest
        # answer is "no reading", exactly as turbidity already does for its
        # own out-of-band rail.
        if not 0.0 <= ph <= 14.0:
            logger.warning(
                "pH %.2f computed from %.3f V is outside 0-14; probe "
                "disconnected or calibration invalid — reporting no reading",
                ph,
                voltage,
            )
            return None

        # Moving median filter
        self._window.append(ph)
        if len(self._window) > self._window_size:
            self._window = self._window[-self._window_size :]

        return round(float(median(self._window)), 2)
