"""
The lite virtual unit — the real sampling loop and the real cloud client, no
threads, no Flask, driven from a loop with a compressed clock.

Two tiers exist because they prove different things (commissioning plan,
Track B):

* **Full** (N ≤ 10) — real ``main.py`` processes with ``simulate_enabled``
  set, plus their Service Windows, driven over HTTP. That proves the
  commissioning wizard. ``scripts/simulate-fleet.py`` spawns them.
* **Lite** (N ≥ 100) — this class. One :class:`~app.workers.SamplingWorker`
  over the simulated drivers, one :class:`~storage.database.WQM1Database`,
  one :class:`~cloud.client.CloudClient`, stepped by hand. That proves the
  cloud: a hundred units' worth of real payloads through the real ingest.

Neither tier builds a payload of its own. The reading comes out of
``SamplingWorker.step`` and the JSON out of ``CloudClient.reading_to_json``,
exactly as on a Pi.

The endpoint guard lives here rather than in the script so a test can pin
it: a virtual unit **refuses** to talk to anything but a loopback host, and
there is no flag that overrides that. A fleet seeder pointed at production
is the single most damaging thing this package could do to a real fleet.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

from app.state import StateStore
from app.workers import SamplingWorker
from sensors.sim import Fault, SimulatedSensors, build_simulated_sensors, parse_faults
from utils.identity import SIM_SERIAL_PREFIX, is_simulated_serial

logger = logging.getLogger("wqm1.sim.unit")

# The only hosts a virtual unit may report to. Not configurable, not
# overridable: the firebase emulator suite and a local marketplace functions
# emulator both answer on loopback, and nothing else should ever receive
# simulated readings.
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0"})  # nosec B104


class NotAnEmulator(ValueError):
    """Raised for any cloud URL whose host is not loopback."""


def assert_emulator_endpoint(url: str) -> str:
    """Return ``url`` if its host is loopback, else raise :class:`NotAnEmulator`.

    An empty URL is refused too — "no endpoint" would fall through to the
    firmware's production defaults.
    """
    if not url:
        raise NotAnEmulator("virtual units need an explicit loopback cloud URL")
    host = (urlparse(url).hostname or "").lower()
    if host not in LOOPBACK_HOSTS:
        raise NotAnEmulator(
            f"refusing cloud endpoint {url!r}: virtual units may only report to a loopback "
            "emulator (localhost / 127.0.0.1 / ::1). There is no override."
        )
    return url


def sim_serial(index: int) -> str:
    """``SIM-WQM1-00001`` for index 1. Five digits like the printed label,
    on the reserved prefix so it can never collide with one."""
    if not 1 <= index <= 99999:
        raise ValueError("virtual unit index must be 1..99999")
    return f"{SIM_SERIAL_PREFIX}{index:05d}"


def sim_dev_eui(index: int) -> str:
    """A DevEUI in the IEEE test range (FE-FF-FF…) that is unique per index
    and obviously synthetic. Never registered anywhere."""
    return f"FEFFFF00{index:08X}"


# A virtual unit raises no access point, so this is a placeholder for the
# identity file's shape — nothing accepts it as a credential anywhere.
_SIM_AP_PASSPHRASE = "simulated"  # nosec B105


def sim_identity(index: int, ap_passphrase: str = _SIM_AP_PASSPHRASE) -> dict[str, Any]:
    """The identity file a virtual unit boots from — same shape the factory
    script writes to a real card, on the reserved serial."""
    return {
        "serial": sim_serial(index),
        "dev_eui": sim_dev_eui(index),
        "ap_passphrase": ap_passphrase,
        "provisioned_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pi_serial_at_provision": None,
        "simulated": True,
    }


class SimSamplingWorker(SamplingWorker):
    """SamplingWorker that advances the shared fault cycle once per step, so
    a fault scripted "@40" means the 40th sample whatever the cadence."""

    def __init__(self, sim: SimulatedSensors, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._sim = sim

    def step(self) -> None:
        self._sim.cycle.tick()
        super().step()


class SimClock:
    """A compressed wall clock: every ``advance()`` moves it by one sampling
    interval, and a scripted ``clock:jump`` fault leaps it a day forward."""

    def __init__(self, start: datetime, interval_s: int, faults: list[Fault]) -> None:
        self._t = start
        self._interval = timedelta(seconds=interval_s)
        self._jumps = sorted(
            f.at_cycle for f in faults if f.channel == "clock" and f.kind == "jump"
        )
        self._cycle = 0
        self.jumped_at: list[int] = []

    def advance(self) -> None:
        self._cycle += 1
        self._t += self._interval
        while self._jumps and self._cycle >= self._jumps[0]:
            self._jumps.pop(0)
            self._t += timedelta(days=1)
            self.jumped_at.append(self._cycle)
            logger.info("simulated clock jump at cycle %d → %s", self._cycle, self._t.isoformat())

    def now(self) -> datetime:
        return self._t


class VirtualUnit:
    """One lite-tier virtual WQM-1.

    Args:
        index: 1-based; decides the serial (``SIM-WQM1-00001``) and DevEUI.
        db_path: where this unit's SQLite buffer lives (one file per unit).
        cloud_api_base / ingest_url: MUST be loopback — see
            :func:`assert_emulator_endpoint`.
        api_key: the key the emulator's claim issued for this serial.
        settings: a ``Settings`` (or any object with the fitment fields and
            ``sensor_read_s``); defaults are a fully-fitted unit with a meter.
        faults: scripted faults; overrides ``settings.simulate_faults``.
        start: the simulated clock's first timestamp.
    """

    def __init__(
        self,
        index: int,
        db_path: str,
        cloud_api_base: str,
        ingest_url: str,
        api_key: str,
        settings: Any,
        faults: str | list[Fault] | None = None,
        start: datetime | None = None,
        fw_version: str = "sim",
    ) -> None:
        from cloud import CloudClient
        from storage.database import WQM1Database
        from utils.health import HealthReporter

        assert_emulator_endpoint(cloud_api_base)
        assert_emulator_endpoint(ingest_url)
        self.serial = sim_serial(index)
        assert is_simulated_serial(self.serial)
        self.dev_eui = sim_dev_eui(index)
        self.settings = settings
        fault_list = parse_faults(faults) if isinstance(faults, str) else list(faults or [])
        if faults is None:
            fault_list = parse_faults(getattr(settings, "simulate_faults", ""))
        self.sim = build_simulated_sensors(settings, fault_list)
        self.state = StateStore()
        self.db = WQM1Database(path=db_path)
        self.db.rotate_pending = not getattr(settings, "cloud_enabled", True)
        self.health = HealthReporter(fw_version)
        self.clock = SimClock(
            start or datetime.now(UTC).replace(microsecond=0),
            int(getattr(settings, "sensor_read_s", 60)),
            fault_list,
        )
        self.sampler = SimSamplingWorker(
            self.sim,
            lambda: settings,
            sensors=self.sim.as_worker_dict(),
            db=self.db,
            rules=None,
            relays=None,
            leds=None,
            health=self.health,
            state=self.state,
            now_utc=self.clock.now,
        )
        self.cloud = CloudClient(
            device_id=self.serial,
            ingest_url=ingest_url,
            command_url=f"{cloud_api_base.rstrip('/')}/v2/devices/{self.serial}/commands",
            api_base=cloud_api_base,
            api_key=api_key,
            fw_version=fw_version,
            batch_size=int(getattr(settings, "batch_size", 50)),
            max_retries=1,
            retry_delays=(0,),
            health_provider=self.health.get_report,
        )
        # No sleeping between retries in a simulation.
        self.cloud._sleep = lambda *_: None
        self.samples = 0
        self.uploaded = 0

    def fix_gps(self) -> None:
        """Take one simulated GPS fix into state (what GpsWorker does)."""
        fix = self.sim.gps.get_fix()
        if fix:
            self.state.set_gps(fix.latitude, fix.longitude, fix.altitude, fix.satellites)

    def sample(self, n: int = 1) -> int:
        """Run ``n`` sampling cycles, advancing the compressed clock each time."""
        for _ in range(n):
            self.clock.advance()
            self.sampler.step()
            self.samples += 1
        return self.samples

    def sync(self) -> int:
        """Drain the buffer to the emulator through the real client."""
        n = self.cloud.sync_readings(self.db)
        self.uploaded += n
        return n

    def heartbeat(self) -> bool:
        """POST one heartbeat built by the real HealthReporter."""
        body = self.health.build_heartbeat(db=self.db, error_counts=self.state.error_counts())
        return bool(self.cloud.send_heartbeat(body))

    def pending(self) -> int:
        return self.db.get_state_count("pending")

    def run(
        self, cycles: int, sync_every: int = 50, on_progress: Callable[[int], None] | None = None
    ) -> None:
        """``cycles`` samples with a sync every ``sync_every``; a month at the
        60 s default is 43,200 cycles."""
        self.fix_gps()
        for i in range(1, cycles + 1):
            self.sample()
            if i % sync_every == 0:
                self.sync()
                if on_progress:
                    on_progress(i)
        self.sync()

    def close(self) -> None:
        self.db.close()
