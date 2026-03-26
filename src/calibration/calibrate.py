"""
Sensor Calibration Manager

Unified calibration storage and application for all water quality sensors.
Calibration coefficients persist to a YAML file and can be updated via
the platform commissioning workflow (step 6).
"""

import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger("wqm1.calibration")

_DEFAULT_CAL_PATH = "/etc/bluesignal/calibration.yaml"


@dataclass
class CalibrationData:
    """Calibration coefficients for all sensors."""

    # pH two-point calibration
    ph_v_at_4: float = 1.04
    ph_v_at_7: float = 1.50
    ph_slope: float = 6.52  # (7-4)/(1.50-1.04)

    # TDS
    tds_k: float = 500.0  # ppm per volt (after divider correction)

    # Turbidity
    turbidity_v_clear: float = 4.1  # voltage at 0 NTU

    # ORP
    orp_offset_mv: float = 0.0

    # Platform-applied offsets (from commissioning step 6)
    platform_offsets: dict = field(default_factory=dict)


class CalibrationManager:
    """Manages sensor calibration coefficients with persistent storage."""

    def __init__(self, path: str | None = None):
        self._path = Path(path or _DEFAULT_CAL_PATH)
        self._data = CalibrationData()
        self._load()

    def _load(self) -> None:
        """Load calibration from YAML file if it exists."""
        if not self._path.exists():
            logger.info("No calibration file at %s, using defaults", self._path)
            return
        try:
            with open(self._path) as f:
                raw = yaml.safe_load(f) or {}
            for key, val in raw.items():
                if hasattr(self._data, key):
                    setattr(self._data, key, val)
            logger.info("Calibration loaded from %s", self._path)
        except Exception as e:
            logger.warning("Failed to load calibration: %s", e)

    def _save(self) -> None:
        """Save calibration to YAML file (atomic write)."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        try:
            with open(tmp, "w") as f:
                yaml.safe_dump(asdict(self._data), f, default_flow_style=False)
            tmp.replace(self._path)
            logger.info("Calibration saved to %s", self._path)
        except Exception as e:
            logger.error("Failed to save calibration: %s", e)
            if tmp.exists():
                tmp.unlink()

    @property
    def data(self) -> CalibrationData:
        return self._data

    def calibrate_ph(self, v_ph4: float, v_ph7: float) -> float:
        """
        Two-point pH calibration.

        Args:
            v_ph4: Voltage in pH 4.0 buffer
            v_ph7: Voltage in pH 7.0 buffer

        Returns:
            Computed slope (pH/V)
        """
        dv = v_ph7 - v_ph4
        if abs(dv) < 0.001:
            logger.error("pH calibration voltages too close (%.4f, %.4f)", v_ph4, v_ph7)
            return self._data.ph_slope

        slope = (7.0 - 4.0) / dv
        self._data.ph_v_at_4 = v_ph4
        self._data.ph_v_at_7 = v_ph7
        self._data.ph_slope = slope
        self._save()
        logger.info("pH calibrated: V@4=%.4f V@7=%.4f slope=%.4f", v_ph4, v_ph7, slope)
        return slope

    def calibrate_tds(self, known_ppm: float, measured_v: float) -> float:
        """
        Single-point TDS calibration.

        Args:
            known_ppm: Known TDS of calibration solution
            measured_v: Voltage reading (after divider correction)

        Returns:
            Computed k coefficient
        """
        if measured_v <= 0:
            logger.error("TDS calibration voltage must be positive")
            return self._data.tds_k
        k = known_ppm / measured_v
        self._data.tds_k = k
        self._save()
        logger.info("TDS calibrated: k=%.2f (known=%s ppm, V=%.4f)", k, known_ppm, measured_v)
        return k

    def calibrate_turbidity(self, clear_water_v: float) -> None:
        """Set clear water voltage for turbidity zero-point."""
        self._data.turbidity_v_clear = clear_water_v
        self._save()
        logger.info("Turbidity calibrated: clear water V=%.3f", clear_water_v)

    def calibrate_orp(self, known_mv: float, measured_mv: float) -> float:
        """
        ORP offset calibration.

        Returns:
            Computed offset in mV
        """
        offset = known_mv - measured_mv
        self._data.orp_offset_mv = offset
        self._save()
        logger.info("ORP calibrated: offset=%.1f mV", offset)
        return offset

    def apply_platform_offsets(self, offsets: dict) -> None:
        """
        Apply calibration offsets from platform commissioning step 6.

        Args:
            offsets: Dict from platform (e.g. {"ph": 0.1, "tds": -5.0})
        """
        self._data.platform_offsets = offsets
        self._save()
        logger.info("Platform calibration offsets applied: %s", offsets)

    def get_platform_offset(self, sensor: str) -> float:
        """Get platform-applied offset for a sensor. Returns 0.0 if not set."""
        return float(self._data.platform_offsets.get(sensor, 0.0))
