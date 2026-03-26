"""
ORP (Oxidation-Reduction Potential) Sensor Module

Reads ORP from ADS1115 AIN3 through LM324 conditioning and
diode clamping (D18-D22, 1N4148). Direct millivolt reading with
offset calibration.
"""

import logging
from statistics import median

from sensors.ads1115 import ADS1115
from utils.config import ADC_CH_ORP, PH_VREF

logger = logging.getLogger("wqm1.orp")


class ORPSensor:
    """ORP sensor with offset calibration."""

    def __init__(self, adc: ADS1115):
        self._adc = adc
        self._window = []
        self._window_size = 5
        self._offset_mv = 0.0  # default, overridden by CalibrationManager

    def set_offset(self, known_mv: float, measured_mv: float) -> None:
        """
        Calibrate offset using a known ORP standard solution.

        Args:
            known_mv: Expected ORP in mV (from standard solution)
            measured_mv: Actual measured mV
        """
        self._offset_mv = known_mv - measured_mv
        self._window.clear()
        logger.info("ORP offset set to %.1f mV", self._offset_mv)

    def read(self) -> float | None:
        """
        Read ORP in millivolts.

        Returns:
            ORP in mV or None on read failure.
            Typical range: -2000 to +2000 mV (clamped by diode network in HW)
        """
        try:
            voltage = self._adc.read_voltage(ADC_CH_ORP)
        except Exception as e:
            logger.error("ORP ADC read failed: %s", e)
            return None

        # The conditioning circuit biases around the 2.048V reference.
        # ORP_mV = (voltage - Vref) * 1000 + offset
        orp_mv = (voltage - PH_VREF) * 1000.0 + self._offset_mv

        # Moving median filter
        self._window.append(orp_mv)
        if len(self._window) > self._window_size:
            self._window = self._window[-self._window_size :]

        return round(float(median(self._window)), 1)
