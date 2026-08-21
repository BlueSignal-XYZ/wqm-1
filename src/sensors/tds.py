"""
TDS (Total Dissolved Solids) Sensor Module

Reads TDS from ADS1115 AIN0 (VIN0) through CD4060 AC excitation and LM324
conditioning with R57/R58 voltage divider (ratio 0.3125).
Temperature compensation applied per reading.
"""

import logging
from statistics import median

from sensors.ads1115 import ADS1115
from sensors.status import (
    NO_CONDUCTION,
    OK,
    OUT_OF_RANGE,
    READ_FAILED,
    UNCALIBRATED,
    SensorResult,
)
from utils.config import (
    ADC_CH_TDS,
    ADC_OPEN_INPUT_V,
    ADC_RAIL_MARGIN_V,
    TDS_DIVIDER_RATIO,
    TDS_TEMP_COEFF,
    TDS_V_MAX,
)

logger = logging.getLogger("wqm1.tds")


def compensated_voltage(adc_voltage: float, temp_c: float | None = 25.0) -> float:
    """The voltage that `tds_k` is defined against — NOT the ADC voltage.

    Two corrections sit between the converter and the number `k` multiplies:
    the R57/R58 divider (0.3125) and temperature compensation. Both are applied
    here, in one place, because the calibration wizard has to invert exactly
    what the sensor applies. When they were written out separately the wizard
    solved `k = ppm / V_adc` while the sensor computed `ppm = (V_adc / 0.3125)
    * k`, so a correct calibration entry produced readings 3.2x high — with no
    error anywhere, because both halves were individually self-consistent.
    """
    actual_voltage = adc_voltage / TDS_DIVIDER_RATIO
    comp_coeff = 1.0 + TDS_TEMP_COEFF * (temp_c - 25.0) if temp_c is not None else 1.0
    if comp_coeff <= 0:  # a nonsense temperature must not invert the reading
        comp_coeff = 1.0
    return actual_voltage / comp_coeff


def ppm_from_adc(adc_voltage: float, temp_c: float | None, k: float) -> float:
    """Forward conversion: ADC volts -> ppm. The sensor's only arithmetic."""
    return compensated_voltage(adc_voltage, temp_c) * k


def k_from_reference(known_ppm: float, adc_voltage: float, temp_c: float | None = 25.0) -> float:
    """Inverse of `ppm_from_adc`: solve for k from a reference solution.

    Raises:
        ValueError: if the observed voltage cannot yield a calibration — a
            non-conducting probe would otherwise produce an enormous k that
            makes every later reading wrong without ever looking wrong.
    """
    v = compensated_voltage(adc_voltage, temp_c)
    if v <= 0:
        raise ValueError(f"probe voltage {adc_voltage:.4f} V is not conducting")
    return known_ppm / v


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

        Kept for every existing caller: the value, or None for anything that is
        not a measurement. Use `read_detailed()` when the REASON matters — which
        it does anywhere the result reaches a person.
        """
        return self.read_detailed(temp_c=temp_c).value

    def read_detailed(self, temp_c: float | None = 25.0) -> SensorResult:
        """
        Read TDS in ppm, with the reason when there is no number.

        Args:
            temp_c: Water temperature for compensation (default 25°C)

        Returns:
            SensorResult — value + status. Clean water is a VALUE, however low.
        """
        try:
            adc_voltage = self._adc.read_voltage(ADC_CH_TDS)
        except Exception as e:
            logger.error("TDS ADC read failed: %s", e)
            return SensorResult(None, READ_FAILED, str(e)[:80])

        # An open or dry electrode conducts nothing and reads flat zero. This
        # threshold is deliberately tiny (see ADC_OPEN_INPUT_V): it used to be
        # the pH rail margin of 50 mV, which through the 0.3125 divider at the
        # default calibration threw away everything below 80 ppm — i.e. clean
        # water, reported as a disconnected probe. Low is a reading. Zero is not.
        if adc_voltage <= ADC_OPEN_INPUT_V:
            logger.warning(
                "TDS input at %.4f V — no conduction; probe is disconnected or "
                "out of the water. Reporting the fault, not a number.",
                adc_voltage,
            )
            return SensorResult(None, NO_CONDUCTION, f"input at {adc_voltage:.4f} V")

        # Railed high against THIS channel's chain (0-2.3 V), not the
        # converter's ±4.096 V range — comparing to the latter could never
        # detect a railed TDS signal at all.
        if adc_voltage >= TDS_V_MAX - ADC_RAIL_MARGIN_V:
            logger.warning(
                "TDS input at %.3f V is at the top of the 0-%.1f V chain; "
                "out of range — reporting the fault, not a number.",
                adc_voltage,
                TDS_V_MAX,
            )
            return SensorResult(None, OUT_OF_RANGE, f"input at {adc_voltage:.3f} V")

        # Divider + temperature compensation, then ppm = voltage * k. Shared
        # with the calibration wizard so the two can never disagree.
        tds_ppm = ppm_from_adc(adc_voltage, temp_c, self._k)

        # A negative ppm is not a dilute sample, it is a broken signal path or
        # a bad calibration constant. Clamping it to 0.0 published a number the
        # electronics never measured; say nothing instead.
        if tds_ppm < 0:
            logger.warning(
                "TDS %.1f ppm computed from %.3f V is negative; calibration or "
                "signal path fault — reporting the fault, not a number.",
                tds_ppm,
                adc_voltage,
            )
            return SensorResult(None, UNCALIBRATED, f"{tds_ppm:.1f} ppm from {adc_voltage:.3f} V")

        # Moving median filter
        self._window.append(tds_ppm)
        if len(self._window) > self._window_size:
            self._window = self._window[-self._window_size :]

        return SensorResult(round(float(median(self._window)), 1), OK)
