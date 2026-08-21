"""
Turbidity Sensor Module

Reads turbidity from ADS1115 AIN1 (VIN1) via LMV321 buffer. Linear mapping:
4.1V = 0 NTU (clear water), 0.5V ≈ 3000 NTU.
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
    ADC_CH_TURBIDITY,
    ADC_OPEN_INPUT_V,
    TURB_CLEAR_TOLERANCE_V,
    TURB_NTU_MAX,
    TURB_V_CLEAR,
    TURB_V_MAX,
)

logger = logging.getLogger("wqm1.turbidity")


class TurbiditySensor:
    """Turbidity sensor with voltage-to-NTU linear conversion."""

    def __init__(self, adc: ADS1115) -> None:
        self._adc = adc
        self._window: list[float] = []
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

        Kept for every existing caller: the value, or None for anything that is
        not a measurement. Use `read_detailed()` when the reason matters.
        """
        return self.read_detailed().value

    def read_detailed(self) -> SensorResult:
        """
        Read turbidity in NTU, with the reason when there is no number.

        Returns:
            SensorResult. Water CLEARER than the calibration reference is the
            best case this instrument can see and reports as 0.0 NTU — not as
            nothing.
        """
        try:
            voltage = self._adc.read_voltage(ADC_CH_TURBIDITY)
        except Exception as e:
            logger.error("Turbidity ADC read failed: %s", e)
            return SensorResult(None, READ_FAILED, str(e)[:80])

        # Turbidity is inverse: more light through means higher voltage, so the
        # valid band is [TURB_V_MAX, v_clear]. Below TURB_V_MAX the signal is
        # past full scale, and in practice the input has floated toward 0 —
        # a disconnected or dry probe. Without this the linear map would
        # fabricate a confident 3000 NTU, which can drive relay automation.
        if voltage < TURB_V_MAX:
            status = NO_CONDUCTION if voltage <= ADC_OPEN_INPUT_V else OUT_OF_RANGE
            logger.warning(
                "Turbidity voltage %.4f V below the %.1f V full-scale point; %s "
                "— reporting the fault, not a number.",
                voltage,
                TURB_V_MAX,
                "no conduction, probe disconnected or out of the water"
                if status == NO_CONDUCTION
                else "past maximum turbidity",
            )
            return SensorResult(None, status, f"input at {voltage:.4f} V")

        v_range = self._v_clear - TURB_V_MAX
        if v_range <= 0:
            logger.error("Invalid turbidity voltage range")
            return SensorResult(None, UNCALIBRATED, "clear-water reference below full scale")

        # Linear mapping: v_clear -> 0 NTU, v_max -> NTU_MAX
        ntu = (self._v_clear - voltage) * TURB_NTU_MAX / v_range

        # CLEAR WATER IS A RESULT, NOT AN ABSENCE.
        #
        # Water cleaner than the clear-water reference computes a slightly
        # negative NTU. This used to be refused outright, so the clearest water
        # the instrument can see produced no reading at all — the channel went
        # quiet exactly when the news was good. Within the tolerance band that
        # is clear water and reports as 0.0; only past it is the reference
        # genuinely stale, and then it says so rather than claiming pristine.
        if ntu < 0.0:
            if voltage - self._v_clear <= TURB_CLEAR_TOLERANCE_V:
                ntu = 0.0
            else:
                logger.warning(
                    "Turbidity %.3f V is %.3f V above the %.3f V clear-water "
                    "reference — beyond tolerance, so the reference is stale. "
                    "Reporting the fault, not a fabricated 0 NTU.",
                    voltage,
                    voltage - self._v_clear,
                    self._v_clear,
                )
                return SensorResult(
                    None,
                    UNCALIBRATED,
                    f"{voltage:.3f} V above clear reference {self._v_clear:.3f} V",
                )

        if ntu > TURB_NTU_MAX:
            logger.warning(
                "Turbidity %.1f NTU computed from %.3f V exceeds %.0f; "
                "calibration invalid — reporting the fault, not a number.",
                ntu,
                voltage,
                TURB_NTU_MAX,
            )
            return SensorResult(None, UNCALIBRATED, f"{ntu:.1f} NTU from {voltage:.3f} V")

        # Moving median filter
        self._window.append(ntu)
        if len(self._window) > self._window_size:
            self._window = self._window[-self._window_size :]

        return SensorResult(round(float(median(self._window)), 1), OK)
