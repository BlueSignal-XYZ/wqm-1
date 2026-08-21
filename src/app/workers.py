"""
Worker threads for the WQM-1 v2 firmware.

Each worker owns one I/O domain and exposes:
- ``name``       — liveness/error-counter key
- ``interval_s`` — current cadence (re-read every cycle, so hot config
                   changes and adaptive sampling apply live)
- ``step()``     — one unit of work; exceptions are caught by the runner

``run_forever(stop_event, state, on_error)`` is the shared runner: step,
beat, wait. A crashing step never kills the thread — it logs, bumps the
error counter, applies backoff, and tries again (graceful degradation:
sampling + relay rules keep running while cloud/GPS/LoRa are down).
"""

import json
import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from app.state import StateStore

logger = logging.getLogger("wqm1.workers")

# After this many consecutive failures a worker backs off to its max backoff
# and the supervisor lights the FAULT LED.
FAULT_STREAK = 5
_BACKOFF_S = (5, 15, 30, 60)


class Worker:
    """Base class — subclasses set name/error_bucket and implement step()."""

    name = "worker"
    error_bucket = "sensor"

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self.consecutive_errors = 0

    def interval_s(self) -> float:
        raise NotImplementedError

    def _read_channel(
        self,
        name: str,
        channel: str,
        sensor: Any,
        status_out: dict[str, str],
        **kwargs: Any,
    ) -> float | None:
        """Read one channel, recording WHY when there is no number.

        Drivers that expose `read_detailed()` return a reason along with the
        value (see sensors/status.py); the rest — pH, ORP, chlorine, the RS485
        probe, and any driver written later — only have `read()`. Falling back
        rather than requiring the richer method keeps this a per-driver upgrade
        instead of a flag day, and means a driver that has not been converted
        behaves exactly as it did before.
        """
        detailed = getattr(sensor, "read_detailed", None)
        if detailed is None:
            return self._safe_read(name, lambda: sensor.read(**kwargs))

        result = self._safe_read(name, lambda: detailed(**kwargs))
        if result is None:
            # _safe_read swallowed an exception; it already logged and counted.
            return None
        if not result.ok:
            status_out[channel] = result.status
        return result.value

    def step(self) -> None:
        raise NotImplementedError

    def run_forever(
        self,
        stop_event: threading.Event,
        state: StateStore,
        on_fault: Callable[[str], None] | None = None,
    ) -> None:
        """Run until stop_event; called as a thread target by the supervisor."""
        while not stop_event.is_set():
            started = self._clock()
            try:
                self.step()
                self.consecutive_errors = 0
            except Exception as e:  # noqa: BLE001 — the runner is the failure boundary
                self.consecutive_errors += 1
                state.incr_error(self.error_bucket)
                logger.error(
                    "%s step failed (streak %d): %s",
                    self.name,
                    self.consecutive_errors,
                    e,
                    exc_info=self.consecutive_errors == 1,
                )
                if self.consecutive_errors >= FAULT_STREAK and on_fault:
                    on_fault(self.name)
            state.beat(self.name)
            elapsed = self._clock() - started
            wait = max(0.0, self.interval_s() - elapsed)
            if self.consecutive_errors:
                backoff = _BACKOFF_S[min(self.consecutive_errors - 1, len(_BACKOFF_S) - 1)]
                wait = max(wait, float(backoff))
            stop_event.wait(wait)


# The reading columns that carry an actual measurement. Position/battery/relay
# state are metadata: a row holding only those is not a reading, and the cloud
# rejects it as "Missing sensors data".
_SENSOR_FIELDS = (
    "ph",
    "tds_ppm",
    "turbidity_ntu",
    "orp_mv",
    "temp_c",
    "chlorine_mgl",
    "conductivity_uscm",
    "salinity_ppt",
)


class SamplingWorker(Worker):
    """Reads all sensors, evaluates relay rules, stores the reading.

    Optional collaborators (wired when the sensing package is enabled):
    ``monitor`` (SensorMonitor: flatline/spike/drift events + rule
    suspension), ``adaptive`` (AdaptiveSampler: dynamic cadence).
    """

    name = "sampling"
    error_bucket = "sensor"

    def __init__(
        self,
        settings_provider: Callable[[], Any],
        sensors: dict[str, Any],
        db: Any,
        rules: Any,
        relays: Any,
        leds: Any,
        health: Any,
        state: StateStore,
        monitor: Any = None,
        adaptive: Any = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(clock)
        self._settings = settings_provider
        self._sensors = sensors  # {"temperature": DS18B20, "ph": ..., "tds": ..., ...}
        self._db = db
        self._rules = rules
        self._relays = relays
        self._leds = leds
        self._health = health
        self._state = state
        self.monitor = monitor
        self.adaptive = adaptive
        self._no_sensor_cycles = 0
        self._empty_cycles = 0

    def interval_s(self) -> float:
        if self.adaptive is not None:
            try:
                return float(self.adaptive.current_interval_s())
            except Exception as e:  # noqa: BLE001 — adaptive is advisory
                logger.debug("adaptive interval failed: %s", e)
        return float(self._settings().sensor_read_s)

    def _safe_read(self, name: str, fn: Callable[[], float | None]) -> float | None:
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 — one dead sensor must not stop the rest
            logger.warning("%s read failed: %s", name, e)
            self._state.incr_error("sensor")
            if self._leds:
                self._leds.error_pattern(2)
            return None

    def step(self) -> None:
        # No probe is fitted at all — record nothing.
        #
        # A reading with every sensor field null is not a measurement, and
        # manufacturing one has a cost that is invisible until you go looking:
        # the cloud rejects a sensor-less row outright, the firmware marks it
        # failed_permanent, and it is never retried. A unit sitting with no
        # probes connected quietly builds a graveyard — one field unit reached
        # 13,938 dead rows that way while every heartbeat and diagnostic
        # reported healthy.
        #
        # This first guard catches the cheap case — nothing declared, so there
        # is nothing to sample. It is NOT sufficient on its own: a driver may
        # hand back a live object for absent hardware (DS18B20.__init__ catches
        # NoSensorFoundError and still returns an instance), so a unit that
        # declares a probe it does not physically have sails past here and
        # produces all-null rows anyway. One did, for 1,459 rows in a night.
        # The second guard, below the read, is what actually holds.
        if not any(self._sensors.values()):
            if self._no_sensor_cycles == 0:
                logger.warning(
                    "No probes declared fitted — not recording readings. "
                    "Declare the fitted probes in the Service Window "
                    "(Sensors) or in /etc/bluesignal/config.yaml."
                )
            self._no_sensor_cycles += 1
            # Roughly hourly at the default 60 s cadence, so the reason stays
            # discoverable in the journal without flooding it.
            if self._no_sensor_cycles % 60 == 0:
                logger.warning(
                    "Still no probes declared fitted — %d cycles with nothing to record.",
                    self._no_sensor_cycles,
                )
            return
        self._no_sensor_cycles = 0

        temp = self._sensors.get("temperature")
        temp_c = self._safe_read("Temperature", temp.read_temp_c) if temp else None

        # RS485 5-in-1 probe first: one bus read yields pH/EC/temp/TDS/salinity,
        # and its values supersede the analog equivalents (precedence decided
        # at wiring time by which sensors main.py enables). A failed digital
        # read falls back to the analog probes for this cycle rather than
        # blanking the reading.
        multi_s = self._sensors.get("multi485")
        # isinstance rather than a cast: _safe_read is annotated float|None for
        # the scalar probes, and a belt-and-braces check here also protects the
        # cycle if a driver ever returns a scalar where a dict was expected.
        multi_raw = self._safe_read("5-in-1", multi_s.read_all) if multi_s else None
        multi: dict = multi_raw if isinstance(multi_raw, dict) else {}
        if multi.get("temp_c") is not None:
            temp_c = multi["temp_c"]

        ph_s, tds_s, turb_s, orp_s, chlorine_s = (
            self._sensors.get("ph"),
            self._sensors.get("tds"),
            self._sensors.get("turbidity"),
            self._sensors.get("orp"),
            self._sensors.get("chlorine"),
        )
        ph = multi.get("ph")
        if ph is None and ph_s:
            ph = self._safe_read("pH", lambda: ph_s.read(temp_c=temp_c))
        # Analog TDS and turbidity report a STATUS as well as a value, so a
        # channel with no number this cycle can say why instead of vanishing.
        # See sensors/status.py: clean water is a value, an open or dry probe
        # is a fault, and both must reach the customer's dashboard as
        # themselves. `sensor_status` below carries the non-ok ones.
        channel_status: dict[str, str] = {}
        tds = multi.get("tds_ppm")
        if tds is None and tds_s:
            tds = self._read_channel("TDS", "tds", tds_s, channel_status, temp_c=temp_c)
        turb = (
            self._read_channel("Turbidity", "turbidity", turb_s, channel_status)
            if turb_s
            else None
        )
        orp = self._safe_read("ORP", orp_s.read) if orp_s else None
        chlorine = self._safe_read("Chlorine", chlorine_s.read) if chlorine_s else None

        gps = self._state.gps()
        reading = {
            "timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "ph": ph,
            "tds_ppm": tds,
            "turbidity_ntu": turb,
            "orp_mv": orp,
            "temp_c": temp_c,
            "chlorine_mgl": chlorine,
            "conductivity_uscm": multi.get("conductivity_uscm"),
            "salinity_ppt": multi.get("salinity_ppt"),
            "lat": gps.lat,
            "lon": gps.lon,
            "alt_m": gps.alt_m,
            "relay_state": self._relays.get_state_bitmask() if self._relays else 0,
            # Only non-ok channels appear, so a healthy cycle stores nothing
            # extra. None rather than "{}" keeps the column NULL for those.
            "sensor_status": json.dumps(channel_status) if channel_status else None,
        }

        # Sensor-health monitoring first: a stuck sensor's rules are suspended
        # BEFORE this cycle's rule evaluation, so a flatlined probe can't keep
        # (or start) actuating a relay on frozen data.
        suspended: set[str] = set()
        if self.monitor is not None:
            try:
                for event in self.monitor.observe(reading):
                    if not self._state.emit_event(event):
                        logger.warning("event queue full — dropped %s", event.get("type"))
                suspended = set(self.monitor.suspended_sensors())
            except Exception as e:  # noqa: BLE001 — monitoring must not block sampling
                logger.error("Sensor monitor error: %s", e)

        if self._rules:
            try:
                if suspended and hasattr(self._rules, "set_suspended_sensors"):
                    self._rules.set_suspended_sensors(suspended)
                self._rules.evaluate(reading)
            except Exception as e:  # noqa: BLE001
                logger.error("Rules evaluation error: %s", e)

        if self.adaptive is not None:
            try:
                self.adaptive.observe(reading)
            except Exception as e:  # noqa: BLE001
                logger.debug("Adaptive sampler error: %s", e)

        # Nothing measured this cycle — do not persist it.
        #
        # Every sensor field null means no measurement was taken, whatever the
        # reason. The cloud rejects such a row ("Missing sensors data"), the
        # firmware marks it failed_permanent, and it is never retried — so
        # storing it does not record the fault, it only builds a graveyard.
        #
        # The fault is still surfaced, through the channel built for it:
        # _safe_read has already logged each failure and incremented the
        # "sensor" error counter, which rides out on the heartbeat. That is
        # where a dead probe should show up — not as a row the server throws
        # away.
        if all(reading[field] is None for field in _SENSOR_FIELDS):
            self._empty_cycles += 1
            if self._empty_cycles == 1 or self._empty_cycles % 60 == 0:
                logger.warning(
                    "No sensor produced a value (%d cycle(s)) — nothing recorded. "
                    "Check that every declared probe is fitted and reading.",
                    self._empty_cycles,
                )
            return
        self._empty_cycles = 0

        try:
            row_id = self._db.insert_reading(reading)
            self._health.update_last_seen()
            logger.info(
                "Stored id=%d: pH=%s TDS=%s Turb=%s ORP=%s T=%s",
                row_id,
                f"{ph:.2f}" if ph is not None else "N/A",
                f"{tds:.1f}" if tds is not None else "N/A",
                f"{turb:.1f}" if turb is not None else "N/A",
                f"{orp:.1f}" if orp is not None else "N/A",
                f"{temp_c:.1f}°C" if temp_c is not None else "N/A",
            )
        except Exception:
            self._state.incr_error("db")
            if self._leds:
                self._leds.error_pattern(3)
            raise


class GpsWorker(Worker):
    """Acquires GPS fixes (blocking UART reads, up to gps_fix_timeout_s) —
    isolated in its own thread so a slow fix never stalls sampling or LoRa."""

    name = "gps"
    error_bucket = "gps"

    def __init__(
        self,
        settings_provider: Callable[[], Any],
        gps: Any,
        leds: Any,
        state: StateStore,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(clock)
        self._settings = settings_provider
        self._gps = gps
        self._leds = leds
        self._state = state

    def interval_s(self) -> float:
        return float(self._settings().gps_fix_s)

    def step(self) -> None:
        if self._gps is None:
            return
        if self._leds:
            self._leds.gps_fix_on()
        try:
            fix = self._gps.get_fix(timeout_s=self._settings().gps_fix_timeout_s)
            if fix:
                self._state.set_gps(fix.latitude, fix.longitude, fix.altitude, fix.satellites)
                logger.info("GPS fix: %.6f, %.6f", fix.latitude, fix.longitude)
            elif self._state.gps().lat is None:
                self._gps.power_cycle()
        finally:
            if self._leds:
                self._leds.gps_fix_off()


class RadioWorker(Worker):
    """Owns the SX1262 exclusively: LoRa uplinks + Class-A downlink windows.
    Never blocked by GPS/cloud I/O so RX1/RX2 timing holds."""

    name = "radio"
    error_bucket = "lora"

    def __init__(
        self,
        settings_provider: Callable[[], Any],
        lorawan: Any,
        radio: Any,
        db: Any,
        rules: Any,
        leds: Any,
        health: Any,
        state: StateStore,
        persist_session: Callable[[], None],
        cayenne_encode: Callable[[dict[str, Any]], bytes],
        fport_app: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(clock)
        self._settings = settings_provider
        self._lorawan = lorawan
        self._radio = radio
        self._db = db
        self._rules = rules
        self._leds = leds
        self._health = health
        self._state = state
        self._persist_session = persist_session
        self._encode = cayenne_encode
        self._fport = fport_app

    def interval_s(self) -> float:
        return float(self._settings().lora_tx_s)

    def step(self) -> None:
        if not self._lorawan or not self._lorawan.session.joined:
            return
        latest = self._db.get_latest()
        if not latest:
            return
        payload = self._encode(latest)
        if not payload:
            return
        if self._leds:
            self._leds.lora_tx_on()
        try:
            downlink = self._lorawan.send_uplink(payload, fport=self._fport)
            self._persist_session()
            if self._radio:
                rssi = self._radio.get_rssi()
                self._health.update_rssi(rssi)
                self._state.set_lora_rssi(rssi)
            if downlink and self._rules:
                self._rules.process_downlink_command(downlink[0], downlink[1:])
        finally:
            if self._leds:
                self._leds.lora_tx_off()


class CloudSyncWorker(Worker):
    """Drains the SQLite buffer to the cloud (store-and-forward + backfill)."""

    name = "cloud-sync"
    error_bucket = "cloud"

    def __init__(
        self,
        settings_provider: Callable[[], Any],
        cloud: Any,
        db: Any,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(clock)
        self._settings = settings_provider
        self._cloud = cloud
        self._db = db

    def interval_s(self) -> float:
        return float(self._settings().sync_interval_s)

    def step(self) -> None:
        n = self._cloud.sync_readings(self._db)
        if n:
            logger.info("Cloud sync: uploaded %d reading(s)", n)


class CommandWorker(Worker):
    """Polls the command queue, applies commands, uploads queued device
    events, and reconciles remote config when the poll signals a new version."""

    name = "cloud-cmd"
    error_bucket = "cloud"

    def __init__(
        self,
        settings_provider: Callable[[], Any],
        cloud: Any,
        state: StateStore,
        apply_command: Callable[[dict[str, Any]], None],
        on_config_version: Callable[[int | None], None] | None = None,
        on_ota_pending: Callable[[], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(clock)
        self._settings = settings_provider
        self._cloud = cloud
        self._state = state
        self._apply_command = apply_command
        self._on_config_version = on_config_version
        self._on_ota_pending = on_ota_pending

    def interval_s(self) -> float:
        return float(self._settings().command_poll_s)

    def step(self) -> None:
        poll = self._cloud.poll()
        for cmd in poll.get("commands", []):
            try:
                self._apply_command(cmd)
            except Exception as e:  # noqa: BLE001 — one bad command must not stop the rest
                logger.error("Apply command %s failed: %s", cmd.get("id"), e)

        config_version = poll.get("configVersion")
        if self._on_config_version:
            # Fire even when the server reports no config version (None):
            # the handler also owns the periodic water-profile refresh, which
            # must run for devices that have never been remotely configured.
            self._on_config_version(config_version if isinstance(config_version, int) else None)

        if poll.get("otaPending") and self._on_ota_pending:
            self._on_ota_pending()

        # Ship queued device events (monitor findings, config acks, ...).
        for event in self._state.drain_events():
            self._cloud.send_event(
                event.get("type", "degraded"),
                message=event.get("message"),
                sensor=event.get("sensor"),
                details=event.get("details"),
            )


class HeartbeatWorker(Worker):
    """Periodic self-report: uptime, buffer depth, disk, temps, link quality,
    error counters, per-sensor plain-language health, config/firmware versions."""

    name = "heartbeat"
    error_bucket = "cloud"

    def __init__(
        self,
        settings_provider: Callable[[], Any],
        cloud: Any,
        db: Any,
        health: Any,
        state: StateStore,
        config_version_provider: Callable[[], int | None],
        sensor_health_provider: Callable[[], dict[str, Any] | None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(clock)
        self._settings = settings_provider
        self._cloud = cloud
        self._db = db
        self._health = health
        self._state = state
        self._config_version = config_version_provider
        self._sensor_health = sensor_health_provider

    def interval_s(self) -> float:
        return float(self._settings().heartbeat_s)

    def send_now(self) -> bool:
        """Build and send one heartbeat (also used by the `diagnostics` command)."""
        sensor_health = None
        if self._sensor_health is not None:
            try:
                sensor_health = self._sensor_health()
            except Exception as e:  # noqa: BLE001
                logger.debug("sensor health provider failed: %s", e)
        payload = self._health.build_heartbeat(
            db=self._db,
            error_counts=self._state.error_counts(),
            sensor_health=sensor_health,
            config_version=self._config_version(),
            db_path=self._settings().db_path,
        )
        return bool(self._cloud.send_heartbeat(payload))

    def step(self) -> None:
        self.send_now()
