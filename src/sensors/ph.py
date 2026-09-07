"""
pH Sensor Module

Reads pH from ADS1115 AIN2 (PH_INP) through the LMP91200 instrumentation amplifier
and LM324 signal conditioning chain. Uses two-point calibration (pH 4.0 / 7.0)
with Nernst equation and temperature compensation.
"""

import logging
from statistics import median

from sensors.ads1115 import ADS1115
from sensors.status import OK, OUT_OF_RANGE, READ_FAILED, UNCALIBRATED, SensorResult
from utils.config import (
    ADC_CH_PH,
    ADC_FULL_SCALE_V,
    ADC_RAIL_MARGIN_V,
    NERNST_F,
    NERNST_R,
    NERNST_SLOPE_25C,
    PH_MAX_WINDOW_SPAN,
)

logger = logging.getLogger("wqm1.ph")


def _nernst_slope(temp_c: float) -> float:
    """Calculate Nernst slope (V/pH) at a given temperature."""
    t_kelvin = temp_c + 273.15
    return (NERNST_R * t_kelvin) / NERNST_F


class PHSensor:
    """pH electrode reading with two-point calibration."""

    def __init__(self, adc: ADS1115, calibrated: bool = True) -> None:
        """
        Args:
            adc: ADS1115 driver
            calibrated: whether the coefficients in force describe a real
                electrode. main.py passes ``CalibrationManager.is_calibrated``
                here (and via set_calibration); with False the sensor reports
                ``uncalibrated`` and no number.
        """
        self._adc = adc
        self._window: list[float] = []
        self._window_size = 5

        # Placeholder calibration. These are NOT the Fin_3 front-end's numbers:
        # the LMP91200 rides the electrode voltage on VOCM (~1 V) with pH 4
        # ABOVE pH 7, so a unit still on these would publish pH that is both
        # offset and inverted. The firmware therefore gates publishing on a
        # stored two-point calibration (see ``calibrated``).
        self._v_ph4 = 1.04
        self._v_ph7 = 1.50
        self._calibrated = bool(calibrated)
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

    def set_calibration(self, v_ph4: float, v_ph7: float, calibrated: bool = True) -> None:
        """
        Set two-point calibration.

        Args:
            v_ph4: Voltage reading in pH 4.0 buffer
            v_ph7: Voltage reading in pH 7.0 buffer
            calibrated: False when the values are factory placeholders rather
                than a measurement — pH then reports ``uncalibrated`` and no
                number until a real calibration is stored.
        """
        self._v_ph4 = v_ph4
        self._v_ph7 = v_ph7
        self._calibrated = bool(calibrated)
        self._recalc_slope()
        self._window.clear()
        if self._calibrated:
            logger.info(
                "pH calibrated: V@4=%.4f V@7=%.4f slope=%.4f pH/V", v_ph4, v_ph7, self._slope
            )
        else:
            logger.warning(
                "pH probe fitted but never calibrated — no pH will be published until a "
                "two-point calibration is done (Service Window > Calibration)"
            )

    @property
    def calibrated(self) -> bool:
        return self._calibrated

    def read(self, temp_c: float | None = 25.0) -> float | None:
        """
        Read pH value.

        Kept for every existing caller: the value, or None for anything that
        is not a measurement. Use `read_detailed()` when the reason matters.
        """
        return self.read_detailed(temp_c=temp_c).value

    def read_detailed(self, temp_c: float | None = 25.0) -> SensorResult:
        """
        Read pH, with the reason when there is no number.

        Args:
            temp_c: Water temperature for Nernst compensation (default 25°C)
        """
        if not self._calibrated:
            return SensorResult(None, UNCALIBRATED, "no two-point calibration stored")
        try:
            voltage = self._adc.read_voltage(ADC_CH_PH)
        except Exception as e:
            logger.error("pH ADC read failed: %s", e)
            return SensorResult(None, READ_FAILED, str(e)[:80])
        return self._convert(voltage, temp_c)

    def _convert(self, voltage: float, temp_c: float | None) -> SensorResult:
        """Voltage -> filtered pH, with the reason when it is not a measurement."""

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
            return SensorResult(None, OUT_OF_RANGE, f"input at {voltage:.3f} V")

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
            return SensorResult(None, UNCALIBRATED, f"pH {ph:.1f} from {voltage:.3f} V")

        # Moving median filter
        self._window.append(ph)
        if len(self._window) > self._window_size:
            self._window = self._window[-self._window_size :]

        # Volatility gate — the guard the rail check cannot provide for pH.
        # An open electrode input sits at mid-scale and wanders; real water
        # does not. See PH_MAX_WINDOW_SPAN for the field data behind this.
        #
        # Three samples is the smallest window that can distinguish a wander
        # from a step: two points differ, three establish that they keep
        # differing. Gating on a FULL window instead would have meant reporting
        # nothing until five samples had accumulated — a five-minute blind spot
        # at startup, and it breaks the read-once contract that calibration and
        # temperature-compensation checks legitimately rely on.
        span = max(self._window) - min(self._window)
        if len(self._window) >= 3 and span > PH_MAX_WINDOW_SPAN:
            logger.warning(
                "pH spread %.2f over the last %d samples exceeds %.2f (values "
                "%s) — electrode disconnected, loose, or not yet settled; "
                "reporting no reading",
                span,
                len(self._window),
                PH_MAX_WINDOW_SPAN,
                ", ".join(f"{v:.2f}" for v in self._window),
            )
            return SensorResult(
                None, OUT_OF_RANGE, f"spread {span:.2f} pH over {len(self._window)}"
            )

        return SensorResult(round(float(median(self._window)), 2), OK)
