"""
Virtual WQM-1 sensors — the drivers a unit runs when ``simulate_enabled`` is set.

Every driver here presents the SAME surface as the hardware driver it stands
in for (``read_detailed()`` returning a :class:`~sensors.status.SensorResult`,
``read_temp_c()``, ``get_fix()``), so :class:`~app.workers.SamplingWorker`
and :class:`~cloud.client.CloudClient` run unchanged. That is the rule this
package exists to keep: **the simulator never carries its own copy of a
reading, a payload, or a serial format.** A simulator with its own JSON
builder makes every test green and every test worthless.

Values are bounded random walks — a probe drifts, it does not roll dice —
and faults are scripted per channel so a test can say "the TDS electrode
comes out of the water at cycle 40" and watch the exact status the cloud
receives. ``integrations/smart_breaker/fake.py`` is the precedent; this is
its sibling for the sensing side, and unlike that one it IS selected from
config, because a whole unit has to be able to boot on a laptop.

A virtual unit's identity comes from a provisioned identity file carrying a
``SIM-WQM1-NNNNN`` serial (utils/identity.SIM_SERIAL_PREFIX) — a prefix that
is neither a label nor a Pi-derived id, so nothing simulated can ever be
mistaken for a real record.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sensors.gps import GPSFix
from sensors.status import (
    NO_CONDUCTION,
    OK,
    OUT_OF_RANGE,
    READ_FAILED,
    UNCALIBRATED,
    SensorResult,
)

logger = logging.getLogger("wqm1.sim")

# Channels a fault may be scripted on. `flow` covers both flow channels;
# `clock` is consumed by the lite tier (sim/unit.py), which owns the clock.
FAULT_CHANNELS = frozenset(
    {"ph", "tds", "turbidity", "temperature", "flow", "gps", "clock", "cloud"}
)
# What a fault can do. Per-channel meaning:
#   no_conduction / out_of_range / uncalibrated / read_failed — the driver
#       returns that status (and no value) from the cycle on
#   flatline — the walk freezes at its current value
#   reset — flow only: the totalizer restarts from zero (a meter swap)
#   jump — clock only: the unit's clock leaps forward one day
#   lost — gps only: no fix from the cycle on
#   down — cloud only: the ingest endpoint stops answering
FAULT_KINDS = frozenset(
    {
        NO_CONDUCTION,
        OUT_OF_RANGE,
        UNCALIBRATED,
        READ_FAILED,
        "flatline",
        "reset",
        "jump",
        "lost",
        "down",
    }
)


@dataclass(frozen=True)
class Fault:
    channel: str
    kind: str
    at_cycle: int = 0  # the sampling cycle the fault begins on (0 = from boot)


def parse_faults(spec: str) -> list[Fault]:
    """``"tds:no_conduction@40,ph:flatline,flow:reset@100"`` → faults.

    Refuses an unknown channel or kind with ``ValueError`` — a typo in a fault
    script must fail the run, not silently script nothing.
    """
    faults: list[Fault] = []
    for raw in (spec or "").split(","):
        item = raw.strip()
        if not item:
            continue
        at = 0
        if "@" in item:
            item, at_s = item.split("@", 1)
            at = int(at_s)
        if ":" not in item:
            raise ValueError(f"fault {raw!r}: expected channel:kind[@cycle]")
        channel, kind = item.split(":", 1)
        channel, kind = channel.strip(), kind.strip()
        if channel not in FAULT_CHANNELS:
            raise ValueError(f"fault {raw!r}: unknown channel {channel!r}")
        if kind not in FAULT_KINDS:
            raise ValueError(f"fault {raw!r}: unknown kind {kind!r}")
        faults.append(Fault(channel, kind, at))
    return faults


class Cycle:
    """The sampling-cycle counter every simulated driver shares, so a fault
    scripted "at cycle 40" means the 40th sampling step, not the 40th read
    of one particular probe."""

    def __init__(self) -> None:
        self.n = 0

    def tick(self) -> None:
        self.n += 1


@dataclass
class _Walk:
    """Bounded random walk: value drifts by at most `step` per tick and is
    clamped to [lo, hi]."""

    value: float
    lo: float
    hi: float
    step: float
    rng: random.Random
    frozen: bool = False

    def tick(self) -> float:
        if not self.frozen:
            self.value = min(
                self.hi, max(self.lo, self.value + self.rng.uniform(-self.step, self.step))
            )
        return self.value


class _SimChannel:
    """Common behaviour: one walk, a fault list, a shared cycle counter."""

    channel = ""

    def __init__(self, walk: _Walk, cycle: Cycle, faults: list[Fault]) -> None:
        self._walk = walk
        self._cycle = cycle
        self._faults = [f for f in faults if f.channel == self.channel]

    def _active_fault(self) -> Fault | None:
        for f in self._faults:
            if self._cycle.n >= f.at_cycle:
                return f
        return None

    def read_detailed(self, **_: Any) -> SensorResult:
        fault = self._active_fault()
        if fault is not None and fault.kind in (
            NO_CONDUCTION,
            OUT_OF_RANGE,
            UNCALIBRATED,
            READ_FAILED,
        ):
            return SensorResult(None, fault.kind, f"simulated {fault.kind}")
        self._walk.frozen = fault is not None and fault.kind == "flatline"
        return SensorResult(round(self._walk.tick(), 3), OK, "simulated")

    def read(self, **kwargs: Any) -> float | None:
        return self.read_detailed(**kwargs).value


class SimPH(_SimChannel):
    channel = "ph"

    def set_calibration(self, *_: Any, **__: Any) -> None:  # hardware-driver parity
        return None

    @property
    def calibrated(self) -> bool:
        return True


class SimTDS(_SimChannel):
    channel = "tds"

    def set_calibration(self, *_: Any, **__: Any) -> None:
        return None


class SimTurbidity(_SimChannel):
    channel = "turbidity"

    def set_clear_water_voltage(self, *_: Any, **__: Any) -> None:
        return None


class SimTemperature(_SimChannel):
    """DS18B20 parity: ``read_temp_c()`` and ``available``."""

    channel = "temperature"

    @property
    def available(self) -> bool:
        return True

    def read_temp_c(self) -> float | None:
        return self.read_detailed().value


class SimFlowMeter:
    """Flow-meter parity (sensors/flow.py): ``read_detailed()`` returns
    ``{"flow_total_gal": SensorResult, "flow_rate_gpm": SensorResult}``.

    The totalizer is monotonic — a real meter's register only goes up — until
    a scripted ``reset``, at which point it restarts from zero the way a
    swapped meter does. The cloud must record that as a counter reset, never
    as negative production; this is the fault that exercises that path.
    """

    channel = "flow"

    def __init__(
        self,
        cycle: Cycle,
        faults: list[Fault],
        rng: random.Random,
        gpm: float = 0.35,
        gpd_cap: float = 60.0,
    ) -> None:
        self._cycle = cycle
        self._faults = [f for f in faults if f.channel == self.channel]
        self._rate = _Walk(gpm, 0.0, gpd_cap / 1440.0 * 1.5, gpm * 0.15, rng)
        self.total_gal = 0.0
        self._reset_done: set[int] = set()

    def read_detailed(self) -> dict[str, SensorResult | None]:
        for f in self._faults:
            if (
                f.kind == "reset"
                and self._cycle.n >= f.at_cycle
                and f.at_cycle not in self._reset_done
            ):
                self._reset_done.add(f.at_cycle)
                logger.info("simulated flow meter reset at cycle %d", self._cycle.n)
                self.total_gal = 0.0
            elif f.kind in (READ_FAILED, NO_CONDUCTION) and self._cycle.n >= f.at_cycle:
                return {
                    "flow_total_gal": SensorResult(None, f.kind, "simulated"),
                    "flow_rate_gpm": SensorResult(None, f.kind, "simulated"),
                }
        rate = self._rate.tick()
        # One sampling cycle stands for one minute of production at the
        # default cadence; the lite tier compresses wall time, not this.
        self.total_gal += rate
        return {
            "flow_total_gal": SensorResult(round(self.total_gal, 3), OK, "simulated"),
            "flow_rate_gpm": SensorResult(round(rate, 3), OK, "simulated"),
        }

    def close(self) -> None:
        return None


class SimGPS:
    """GPS parity: ``get_fix()`` / ``last_fix`` / ``power_cycle()`` / ``close()``.

    Sits a few metres around a fixed site — a mounted unit does not move —
    and stops fixing on a scripted ``lost`` fault (the metal enclosure case).
    """

    channel = "gps"

    def __init__(
        self,
        cycle: Cycle,
        faults: list[Fault],
        rng: random.Random,
        lat: float = 30.4515,
        lon: float = -97.9911,
    ) -> None:
        self._cycle = cycle
        self._faults = [f for f in faults if f.channel == self.channel]
        self._rng = rng
        self._lat, self._lon = lat, lon
        self._last: GPSFix | None = None
        self.power_cycles = 0

    def get_fix(self, timeout_s: float = 10.0) -> GPSFix | None:
        for f in self._faults:
            if f.kind == "lost" and self._cycle.n >= f.at_cycle:
                return None
        self._last = GPSFix(
            latitude=self._lat + self._rng.uniform(-3e-5, 3e-5),
            longitude=self._lon + self._rng.uniform(-3e-5, 3e-5),
            altitude=300.0,
            satellites=9,
            hdop=1.1,
            timestamp=datetime.now(UTC),
            fix_quality=1,
        )
        return self._last

    def last_fix(self) -> GPSFix | None:
        return self._last

    def power_cycle(self) -> None:
        self.power_cycles += 1

    def close(self) -> None:
        return None


@dataclass
class SimulatedSensors:
    """What ``build_simulated_sensors`` hands main.py — the same slots the
    hardware branch fills, plus the shared cycle counter so the sampling
    loop can advance it once per step."""

    cycle: Cycle
    temperature: SimTemperature | None
    ph: SimPH | None
    tds: SimTDS | None
    turbidity: SimTurbidity | None
    flow: SimFlowMeter | None
    gps: SimGPS
    faults: list[Fault] = field(default_factory=list)

    def as_worker_dict(self) -> dict[str, Any]:
        return {
            "temperature": self.temperature,
            "ph": self.ph,
            "tds": self.tds,
            "turbidity": self.turbidity,
            "orp": None,
            "chlorine": None,
            "multi485": None,
            "flow": self.flow,
        }


def build_simulated_sensors(settings: Any, faults: list[Fault] | None = None) -> SimulatedSensors:
    """Build the drivers a virtual unit samples, honouring the same fitment
    declarations a real unit does (``ph_enabled`` … ``flow_pulse_enabled``),
    so a simulated Wi-Fi-only unit with no meter is exactly that."""
    faults = list(
        faults if faults is not None else parse_faults(getattr(settings, "simulate_faults", ""))
    )
    rng = random.Random(int(getattr(settings, "simulate_seed", 0) or 0))  # nosec B311 — not security
    cycle = Cycle()

    def walk(v: float, lo: float, hi: float, step: float) -> _Walk:
        return _Walk(v + rng.uniform(-step, step), lo, hi, step, rng)

    fitted = lambda key: bool(getattr(settings, key, True))  # noqa: E731
    return SimulatedSensors(
        cycle=cycle,
        temperature=SimTemperature(walk(21.5, 4.0, 38.0, 0.08), cycle, faults)
        if fitted("temperature_enabled")
        else None,
        ph=SimPH(walk(7.2, 5.5, 9.0, 0.02), cycle, faults) if fitted("ph_enabled") else None,
        tds=SimTDS(walk(320.0, 40.0, 1500.0, 2.5), cycle, faults)
        if fitted("tds_enabled")
        else None,
        turbidity=SimTurbidity(walk(4.0, 0.2, 60.0, 0.3), cycle, faults)
        if fitted("turbidity_enabled")
        else None,
        flow=SimFlowMeter(cycle, faults, rng)
        if (fitted("flow_pulse_enabled") or getattr(settings, "rs485_flow_enabled", False))
        else None,
        gps=SimGPS(cycle, faults, rng),
        faults=faults,
    )
