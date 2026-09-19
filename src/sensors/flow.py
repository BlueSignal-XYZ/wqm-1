"""
Flow meters — the WQM-1 as a CT clamp for water.

A current transformer tells a solar system how much energy crossed a wire; a
flow meter tells the WQM-1 how much water crossed the AWG's production pipe.
Two drivers, one contract:

- ``PulseFlowMeter`` — an inline hall-effect turbine meter (YF-S201 class) on a
  GPIO. Every pulse is one fixed slug of water; the lifetime pulse count IS the
  meter's register. It is the shunt: cheap, exact, and free to fit on the new
  plumbing every AWG install already lays. Counted by lgpio edge alerts (the
  same mechanism the LoRa DIO1 line uses — RPi.GPIO's add_event_detect is
  broken on kernel 6.6+), debounced in the kernel, and the count is persisted
  by the sampling worker so a reboot never restarts the totalizer.
- ``ModbusFlowMeter`` — a clamp-on ultrasonic transit-time meter (TUF-2000M
  class) on the shared RS485 bus. The true CT analog: strapped to the outside
  of an existing pipe, nothing cut. The meter keeps its own totalizer; the
  driver reads it.

Both report the same two channels, and the cloud does not care which:

- ``flow_total_gal`` — the lifetime totalizer, gallons. THE EVIDENCE. The
  cloud accrues the positive differences between samples; a decrease is a
  counter reset, which the cloud detects from the register itself
  (functions/v2/flowMeter.js). There is deliberately NO ``counter_reset``
  status: in the payload contract a status displaces the value
  (docs/cloud-payload.md §2, "the value wins"), and the value is exactly the
  thing that must travel on the cycle a reset happens.
- ``flow_rate_gpm`` — the instantaneous rate. A display figure, never the
  evidence: a rate integrated over a sample interval is an estimate, a
  register is a count.

A meter that is idle (tank full, night cycle) legitimately reports a frozen
total and a zero rate for hours. The cloud exempts both channels from its
flatline sweep; the quiet-probe job still fires when the channel goes SILENT.
A pulse meter cannot tell "idle" from "disconnected" — a turbine that is not
turning produces no edges either way — and no status pretends otherwise.

Cloud contract: docs/cloud-payload.md. Cloud accrual and holds:
marketplace docs/security/flow-metering.md.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from sensors.modbus import ModbusBus, ModbusError, decode_float_abcd, decode_int32_abcd
from sensors.status import OK, OUT_OF_RANGE, READ_FAILED, SensorResult

logger = logging.getLogger("wqm1.flow")

FLOW_TOTAL_KEY = "flow_total_gal"
FLOW_RATE_KEY = "flow_rate_gpm"
FLOW_CHANNELS = (FLOW_TOTAL_KEY, FLOW_RATE_KEY)

# Mirrors the cloud's SENSOR_RANGES for flow_rate_gpm. A rate above this from
# a residential AWG line is a bus glitch or a wrong K-factor, not water.
RATE_MAX_GPM = 500.0

GAL_PER_M3 = 264.172
GAL_PER_L = 0.264172


# ---------------------------------------------------------------------------
# Pulse meter
# ---------------------------------------------------------------------------


class PulseFlowMeter:
    """
    Inline pulse meter on a GPIO, counted by lgpio edge alerts.

    ``k_ppg`` is the meter's K-factor in pulses per gallon — set once from a
    bucket test (CalibrationManager.calibrate_flow). The default is the
    YF-S201 datasheet figure (450 pulses/L); every real meter differs by a few
    percent, which is why the bucket test exists.

    ``initial_count`` restores the lifetime count persisted by the previous
    run; ``persist`` is called with the new lifetime count on every sample so
    the register survives a reboot. The meter is the register, the Pi is the
    display: losing the count is a counter reset the cloud will record, not a
    silent restart from zero.

    ``lgpio_module`` is injected in tests. On hardware it is the real
    ``lgpio``; on a board with no direct headers main.py never builds this.
    """

    name = "flow"

    def __init__(
        self,
        gpio: int,
        k_ppg: float = 1703.4,
        initial_count: int = 0,
        persist: Callable[[int], None] | None = None,
        debounce_us: int = 500,
        chip: int = 0,
        lgpio_module: Any = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if gpio < 0 or gpio > 27:
            raise ValueError(f"flow pulse GPIO out of range: {gpio}")
        self._gpio = gpio
        self._k = float(k_ppg) if k_ppg and k_ppg > 0 else 1703.4
        self._count = max(0, int(initial_count or 0))
        self._persist = persist
        self._debounce_us = int(debounce_us)
        self._clock = clock
        self._lock = threading.Lock()
        self._last_read_count = self._count
        self._last_read_at: float | None = None
        self._lg = lgpio_module
        self._handle: Any = None
        self._callback: Any = None
        if self._lg is None:  # pragma: no cover - hardware path
            import lgpio

            self._lg = lgpio
        self._handle = self._lg.gpiochip_open(chip)
        self._lg.gpio_claim_alert(self._handle, gpio, self._lg.RISING_EDGE)
        # Kernel-side debounce: hall meters are clean, but the pump beside
        # them is not, and a 500 µs floor still passes 2 kHz — ten times the
        # fastest pulse train a residential meter produces.
        with_debounce = getattr(self._lg, "gpio_set_debounce_micros", None)
        if with_debounce is not None:
            with_debounce(self._handle, gpio, self._debounce_us)
        self._callback = self._lg.callback(self._handle, gpio, self._lg.RISING_EDGE, self._on_edge)
        logger.info(
            "Flow pulse meter on GPIO %d: K=%.1f pulses/gal, lifetime count restored at %d",
            gpio,
            self._k,
            self._count,
        )

    # lgpio calls back as (chip, gpio, level, timestamp).
    def _on_edge(self, *_args: Any) -> None:
        with self._lock:
            self._count += 1

    @property
    def k_ppg(self) -> float:
        return self._k

    def set_calibration(self, k_ppg: float) -> None:
        """Set the K-factor (pulses per gallon) from a bucket test."""
        if k_ppg <= 0:
            raise ValueError(f"K-factor must be positive: {k_ppg}")
        self._k = float(k_ppg)
        logger.info("Flow K-factor set to %.1f pulses/gal", self._k)

    @property
    def lifetime_pulses(self) -> int:
        with self._lock:
            return self._count

    def read_detailed(self) -> dict[str, SensorResult]:
        """Both channels for this cycle, keyed by cloud channel name."""
        now = self._clock()
        with self._lock:
            count = self._count
        total_gal = count / self._k
        rate: float | None = None
        if self._last_read_at is not None and now > self._last_read_at:
            minutes = (now - self._last_read_at) / 60.0
            rate = (count - self._last_read_count) / self._k / minutes
        self._last_read_count = count
        self._last_read_at = now
        if self._persist is not None:
            try:
                self._persist(count)
            except Exception as e:  # noqa: BLE001 — persistence must not stop sampling
                logger.warning("flow pulse count persist failed: %s", e)

        out = {FLOW_TOTAL_KEY: SensorResult(round(total_gal, 3), OK)}
        if rate is None:
            # First sample after start: the count is real, the rate has no
            # interval yet. Omit rather than publish 0.0 — the cloud treats
            # absence as absence, and 0.0 would be a number nobody measured.
            return out
        if rate > RATE_MAX_GPM:
            out[FLOW_RATE_KEY] = SensorResult(None, OUT_OF_RANGE, f"{rate:.1f} gpm")
        else:
            out[FLOW_RATE_KEY] = SensorResult(round(rate, 3), OK)
        return out

    def read_all(self) -> dict[str, float | None]:
        return {k: v.value for k, v in self.read_detailed().items()}

    def close(self) -> None:
        if self._callback is not None:
            with contextlib.suppress(Exception):
                self._callback.cancel()
            self._callback = None
        if self._handle is not None:
            with contextlib.suppress(Exception):
                self._lg.gpiochip_close(self._handle)
            self._handle = None


# ---------------------------------------------------------------------------
# Modbus (clamp-on ultrasonic) meter
# ---------------------------------------------------------------------------

# Per-model register tables. Register NUMBERS below are the manual's 1-based
# numbers; the Modbus address on the wire is number - 1.
#
# TUF-2000M (transcribed from the TUF-2000M user manual, Modbus register
# table): registers 1-2 flow rate (REAL4, unit per register 1437, m³/h by
# default); 9-10 positive accumulator (LONG, in the accumulator unit scaled by
# register 1438); 1437 flow unit (0 = m³, 1 = L, 2 = gal, 3 = ft³, 4 = US
# barrel …); 1438 accumulator multiplier index (0-7 → 0.001 … 10000). REAL4 and
# LONG are big-endian words, high word first (ABCD). FC03.
#
# THIS TABLE HAS NOT YET BEEN VERIFIED AGAINST A LIVE METER. The first bench
# read is the verification; a wrong register reads as an implausible number,
# which the cloud holds rather than accrues.
FLOW_METER_MODELS: dict[str, dict[str, Any]] = {
    "tuf2000m": {
        "label": "TUF-2000M clamp-on ultrasonic",
        "fc": 0x03,
        "rate_reg": 1 - 1,  # REAL4, flow rate
        "total_reg": 9 - 1,  # LONG, positive accumulator
        "unit_reg": 1437 - 1,  # flow unit index
        "multiplier_reg": 1438 - 1,  # accumulator multiplier index
        "word_order": "abcd",
    },
}

# TUF-2000M unit index -> gallons per unit (register 1437). Only the volume
# units a water meter would be set to; anything else refuses rather than
# guesses.
_TUF_UNIT_GAL = {0: GAL_PER_M3, 1: GAL_PER_L, 2: 1.0, 3: 7.48052, 4: 42.0}
# Accumulator multiplier index (register 1438).
_TUF_MULTIPLIER = {0: 0.001, 1: 0.01, 2: 0.1, 3: 1.0, 4: 10.0, 5: 100.0, 6: 1000.0, 7: 10000.0}


class ModbusFlowMeter:
    """Clamp-on ultrasonic flow meter on the shared RS485 bus."""

    name = "flow"

    def __init__(self, bus: ModbusBus, address: int = 4, model: str = "tuf2000m") -> None:
        if model not in FLOW_METER_MODELS:
            raise ValueError(f"unknown flow meter model: {model!r}")
        self._bus = bus
        self.address = address
        self.model = model
        self._map = FLOW_METER_MODELS[model]
        self._gal_per_unit: float | None = None
        self._multiplier: float | None = None

    def _read(self, start: int, count: int) -> list[int]:
        if self._map["fc"] == 0x04:
            return self._bus.read_input_registers(self.address, start, count)
        return self._bus.read_registers(self.address, start, count)

    def _resolve_units(self) -> None:
        """Read the meter's own unit + multiplier registers once."""
        if self._gal_per_unit is not None and self._multiplier is not None:
            return
        unit_idx = self._read(self._map["unit_reg"], 1)[0]
        mult_idx = self._read(self._map["multiplier_reg"], 1)[0]
        if unit_idx not in _TUF_UNIT_GAL or mult_idx not in _TUF_MULTIPLIER:
            raise ModbusError(f"unsupported unit/multiplier registers: {unit_idx}/{mult_idx}")
        self._gal_per_unit = _TUF_UNIT_GAL[unit_idx]
        self._multiplier = _TUF_MULTIPLIER[mult_idx]
        logger.info(
            "Flow meter %s (addr %d): unit index %d (%.4f gal/unit), multiplier x%g",
            self.model,
            self.address,
            unit_idx,
            self._gal_per_unit,
            self._multiplier,
        )

    def read_detailed(self) -> dict[str, SensorResult]:
        """Both channels, or `read_failed` on both when the bus is silent."""
        try:
            self._resolve_units()
            total_regs = self._read(self._map["total_reg"], 2)
            rate_regs = self._read(self._map["rate_reg"], 2)
        except ModbusError as e:
            logger.warning("Flow meter read failed (addr %d): %s", self.address, e)
            failed = SensorResult(None, READ_FAILED, str(e)[:80])
            return {FLOW_TOTAL_KEY: failed, FLOW_RATE_KEY: failed}

        assert self._gal_per_unit is not None and self._multiplier is not None
        total_units = decode_int32_abcd(total_regs) * self._multiplier
        total_gal = total_units * self._gal_per_unit
        rate_per_h = decode_float_abcd(rate_regs)  # unit per hour
        rate_gpm = rate_per_h * self._gal_per_unit / 60.0

        out: dict[str, SensorResult] = {}
        if total_gal < 0:
            out[FLOW_TOTAL_KEY] = SensorResult(None, OUT_OF_RANGE, f"{total_gal:.1f} gal")
        else:
            out[FLOW_TOTAL_KEY] = SensorResult(round(total_gal, 3), OK)
        if rate_gpm != rate_gpm or rate_gpm < 0 or rate_gpm > RATE_MAX_GPM:  # NaN-safe
            out[FLOW_RATE_KEY] = SensorResult(None, OUT_OF_RANGE, f"{rate_gpm:.1f} gpm")
        else:
            out[FLOW_RATE_KEY] = SensorResult(round(rate_gpm, 3), OK)
        return out

    def read_all(self) -> dict[str, float | None]:
        return {k: v.value for k, v in self.read_detailed().items()}
