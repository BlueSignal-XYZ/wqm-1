"""
Honde Tech RS485 probe drivers (Modbus-RTU over the shared bus).

Three probes, one protocol family (9600 8N1, FC 03/06/16):

- ``HondeChlorineSensor`` — constant-voltage residual chlorine, float CDAB
  value, 2-point (zero/slope) calibration in a flow cell.
- ``HondeOrpSensor`` — RD-ORP-WE-01 digital ORP electrode, signed int mV.
- ``HondeMultiSensor`` — RD-PETSTS-01 5-in-1: pH, EC, temperature, TDS and
  salinity in a single 5-register read.

All ``read*`` methods return None on bus errors (matching the analog
driver contract consumed by ``SamplingWorker._safe_read``); calibration
and provisioning methods raise ``ModbusError`` so wizards can show the
failure to the installer.

Register maps transcribed from the Honde datasheets (2026-07-14).
"""

import logging
import math

from sensors.modbus import (
    ModbusBus,
    ModbusError,
    decode_float_cdab,
    encode_float_cdab,
    to_int16,
)

logger = logging.getLogger("wqm1.honde")

# Sane hard bounds from the datasheets — a bus glitch that survives CRC
# must not put a physically impossible number in the database.
CHLORINE_MAX_MGL = 20.0
ORP_MAX_MV = 1999.0


class HondeChlorineSensor:
    """Residual chlorine probe (constant voltage), value in mg/L."""

    # Register map (chlorine family — note it differs from the other probes)
    REG_VALUE = 0x0001  # 2 regs, float CDAB, mg/L
    REG_WARNING = 0x0007  # 1 reg, 0 = normal
    REG_OFFSET = 0x0012  # 2 regs, float CDAB, measured-value offset
    REG_ADDRESS = 0x0019  # FC06, new device address
    REG_CAL_VALUE = 0x0024  # 2 regs, float CDAB, reference solution mg/L
    REG_CAL_CONFIRM_ZERO = 0x003E  # FC06, write 0x00FF after stable AD
    REG_CAL_CONFIRM_SLOPE = 0x003F  # FC06, write 0x00FF after stable AD
    REG_AD = 0x0066  # 1 reg, raw AD counts (stability check)

    CAL_CONFIRM_MAGIC = 0x00FF

    name = "chlorine"

    def __init__(self, bus: ModbusBus, address: int = 1) -> None:
        self._bus = bus
        self.address = address

    def read(self) -> float | None:
        """Residual chlorine in mg/L, or None on failure."""
        try:
            regs = self._bus.read_registers(self.address, self.REG_VALUE, 2)
        except ModbusError as e:
            logger.warning("Chlorine read failed (addr %d): %s", self.address, e)
            return None
        value = decode_float_cdab(regs)
        if not math.isfinite(value) or value < 0 or value > CHLORINE_MAX_MGL:
            logger.warning("Chlorine value out of range: %r", value)
            return None
        return round(value, 3)

    def read_warning(self) -> int | None:
        """Device warning register (0 means normal)."""
        try:
            return self._bus.read_registers(self.address, self.REG_WARNING, 1)[0]
        except ModbusError:
            return None

    def read_ad(self) -> int | None:
        """Raw AD counts — the calibration wizard polls this for stability."""
        try:
            return self._bus.read_registers(self.address, self.REG_AD, 1)[0]
        except ModbusError:
            return None

    # -- calibration (raises on failure so the wizard can surface it) ----

    def calibrate_zero(self) -> None:
        """Confirm the zero point (probe in 0 mg/L sample, AD stable)."""
        self._bus.write_register(self.address, self.REG_CAL_CONFIRM_ZERO, self.CAL_CONFIRM_MAGIC)

    def calibrate_slope(self, reference_mgl: float) -> None:
        """Confirm the high point against a known reference concentration."""
        if not 0.0 < reference_mgl <= CHLORINE_MAX_MGL:
            raise ValueError(f"reference out of range: {reference_mgl}")
        self._bus.write_registers(
            self.address, self.REG_CAL_VALUE, list(encode_float_cdab(reference_mgl))
        )
        self._bus.write_register(self.address, self.REG_CAL_CONFIRM_SLOPE, self.CAL_CONFIRM_MAGIC)

    def set_address(self, new_address: int) -> None:
        """Re-address the probe (chlorine family uses register 0x0019)."""
        if not 1 <= new_address <= 247:
            raise ValueError(f"invalid Modbus address: {new_address}")
        self._bus.write_register(self.address, self.REG_ADDRESS, new_address)
        self.address = new_address


class HondeOrpSensor:
    """RD-ORP-WE-01 digital ORP electrode, value in mV (signed)."""

    REG_VALUE = 0x0000  # 1 reg, signed int, mV
    REG_ADDRESS = 0x0050  # FC06, new device address

    name = "orp"

    def __init__(self, bus: ModbusBus, address: int = 1) -> None:
        self._bus = bus
        self.address = address

    def read(self) -> float | None:
        """ORP in mV, or None on failure (drop-in for the analog ORPSensor)."""
        try:
            regs = self._bus.read_registers(self.address, self.REG_VALUE, 1)
        except ModbusError as e:
            logger.warning("ORP read failed (addr %d): %s", self.address, e)
            return None
        value = float(to_int16(regs[0]))
        if abs(value) > ORP_MAX_MV:
            logger.warning("ORP value out of range: %r", value)
            return None
        return value

    def set_address(self, new_address: int) -> None:
        if not 1 <= new_address <= 247:
            raise ValueError(f"invalid Modbus address: {new_address}")
        self._bus.write_register(self.address, self.REG_ADDRESS, new_address)
        self.address = new_address


class HondeMultiSensor:
    """
    RD-PETSTS-01 5-in-1 probe: pH, EC, temperature, TDS, salinity.

    One FC03 read of 5 registers starting at 0x0000 returns, in order:
    pH x100, EC µS/cm, temperature x10 °C, TDS ppm, salinity x100 ppt.
    """

    REG_START = 0x0000
    REG_COUNT = 5
    REG_ADDRESS = 0x0050  # FC06, new device address
    REG_EC_SLOPE = 0x000A  # FC06, slope x1000 (default 1.000)
    REG_PH_OFFSET = 0x000B  # FC06, offset x100
    # pH buffer calibration: write the magic value to the matching register
    # once the reading is stable in that buffer.
    PH_CAL_POINTS: dict[str, tuple[int, int]] = {
        "9.18": (0x005A, 0x000A),
        "6.86": (0x005B, 0x000B),
        "4.01": (0x005C, 0x000C),
    }

    name = "multi485"

    def __init__(self, bus: ModbusBus, address: int = 1) -> None:
        self._bus = bus
        self.address = address

    def read_all(self) -> dict[str, float] | None:
        """
        All five parameters in one bus transaction, keyed by the reading-dict
        field names the rest of the firmware uses. None on failure.
        """
        try:
            regs = self._bus.read_registers(self.address, self.REG_START, self.REG_COUNT)
        except ModbusError as e:
            logger.warning("5-in-1 read failed (addr %d): %s", self.address, e)
            return None
        return {
            "ph": round(regs[0] / 100.0, 2),
            "conductivity_uscm": float(regs[1]),
            "temp_c": round(to_int16(regs[2]) / 10.0, 1),
            "tds_ppm": float(regs[3]),
            "salinity_ppt": round(regs[4] / 100.0, 2),
        }

    # -- calibration ------------------------------------------------------

    def calibrate_ph_point(self, buffer: str) -> None:
        """Confirm a pH buffer point ("4.01", "6.86" or "9.18") once stable."""
        try:
            register, magic = self.PH_CAL_POINTS[buffer]
        except KeyError:
            raise ValueError(f"unknown pH buffer: {buffer!r}") from None
        self._bus.write_register(self.address, register, magic)

    def set_ph_offset(self, offset: float) -> None:
        """Single-point pH adjustment (offset in pH units, e.g. 1.00)."""
        if not -14.0 <= offset <= 14.0:
            raise ValueError(f"pH offset out of range: {offset}")
        self._bus.write_register(self.address, self.REG_PH_OFFSET, int(round(offset * 100)))

    def set_ec_slope(self, slope: float) -> None:
        """EC slope multiplier (1.000 = factory default)."""
        if not 0.1 <= slope <= 10.0:
            raise ValueError(f"EC slope out of range: {slope}")
        self._bus.write_register(self.address, self.REG_EC_SLOPE, int(round(slope * 1000)))

    def set_address(self, new_address: int) -> None:
        if not 1 <= new_address <= 247:
            raise ValueError(f"invalid Modbus address: {new_address}")
        self._bus.write_register(self.address, self.REG_ADDRESS, new_address)
        self.address = new_address
