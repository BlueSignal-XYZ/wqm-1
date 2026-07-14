#!/usr/bin/env python3
"""
WQM-1 Firmware — Main Entry Point (v2)

Initialises hardware, wires the worker threads, and hands control to the
Supervisor. The v1 single-threaded 1s-tick loop is replaced by supervised
workers (see src/app/): sampling, GPS, LoRa radio, cloud sync, command poll,
heartbeat — so a slow GPS fix or a cloud retry can never stall sensor reads,
relay rules, or LoRa receive windows. The supervisor pets the systemd
watchdog only while every worker is provably alive.

Designed for Raspberry Pi Zero 2W + WQM-1 HAT (PCBA rev Fin_3).
"""

import atexit
import contextlib
import json
import logging
import signal
import socket
import sys
import threading
import types
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import yaml

from app.state import StateStore
from app.supervisor import Supervisor
from app.workers import (
    CloudSyncWorker,
    CommandWorker,
    GpsWorker,
    HeartbeatWorker,
    RadioWorker,
    SamplingWorker,
    Worker,
)
from calibration.calibrate import CalibrationManager
from control.led import StatusLEDs
from control.relay import RelayController
from control.rules import RulesEngine
from radio.cayenne import encode as cayenne_encode
from radio.lorawan import FPORT_APP, LoRaWANMAC, LoRaWANSession
from radio.sx1262 import SX1262
from sensors.ads1115 import ADS1115
from sensors.gps import GPS
from sensors.orp import ORPSensor
from sensors.ph import PHSensor
from sensors.tds import TDSSensor
from sensors.temperature import DS18B20
from sensors.turbidity import TurbiditySensor
from storage.database import WQM1Database
from utils.config import FIRMWARE_VERSION, get_config_manager, hot_keys, restart_keys
from utils.health import HealthReporter
from utils.identity import APP_EUI, get_dev_eui, get_device_id
from utils.sdnotify import SdNotifier
from utils.watchdog import FanController, HardwareWatchdog

logger = logging.getLogger("wqm1")

FW_VERSION = FIRMWARE_VERSION

# The OTA agent (separate service) polls this flag file for "check now" nudges
# from the command channel / otaPending hints.
OTA_CHECK_FLAG = Path("/var/lib/bluesignal/ota-check-now")


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------


def _setup_logging() -> None:
    settings = get_config_manager().settings
    root = logging.getLogger("wqm1")
    root.setLevel(logging.INFO)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    log_path = Path(settings.log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fh = RotatingFileHandler(
            str(log_path),
            maxBytes=settings.log_max_bytes,
            backupCount=settings.log_backup_count,
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except Exception as e:
        root.warning("Could not open log file %s: %s", log_path, e)


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------


class WQM1App:
    """Firmware wiring: builds hardware + workers, runs the supervisor."""

    def __init__(self) -> None:
        self._config = get_config_manager()
        self._settings_provider = lambda: self._config.settings
        self._state = StateStore()
        self._supervisor: Supervisor | None = None

        # Device identity
        self._device_id = get_device_id()
        self._dev_eui = get_dev_eui()

        # Components (lazy-initialised in start(); typed Any to avoid
        # union-attr noise — init order is guaranteed by start())
        self._relays: Any = None
        self._leds: Any = None
        self._fan: Any = None
        self._adc: Any = None
        self._temp: Any = None
        self._gps: Any = None
        self._radio: Any = None
        self._lorawan: Any = None
        self._ph: Any = None
        self._tds: Any = None
        self._turbidity: Any = None
        self._orp: Any = None
        self._modbus_bus: Any = None
        self._chlorine: Any = None
        self._multi: Any = None
        self._db: Any = None
        self._cloud: Any = None
        self._health: Any = None
        self._cal: Any = None
        self._rules: Any = None
        self._monitor: Any = None
        self._adaptive: Any = None
        self._heartbeat_worker: HeartbeatWorker | None = None
        self._cmd_sock: socket.socket | None = None
        self._cmd_thread: threading.Thread | None = None
        self._running = False

    @property
    def _settings(self) -> Any:
        return self._config.settings

    def start(self) -> None:
        """Initialise all hardware and build the worker set."""
        logger.info("WQM-1 firmware v%s starting (device=%s)", FW_VERSION, self._device_id)

        # --- GPIO outputs ---
        self._relays = RelayController()
        self._relays.all_off()
        self._leds = StatusLEDs()
        self._leds.startup_test()
        self._fan = FanController(
            on_temp=self._settings.fan_on_temp_c,
            off_temp=self._settings.fan_off_temp_c,
        )

        # --- ADC + sensors ---
        self._adc = ADS1115()
        self._temp = DS18B20()
        self._cal = CalibrationManager()
        self._ph = PHSensor(self._adc)
        self._tds = TDSSensor(self._adc)
        self._turbidity = TurbiditySensor(self._adc)
        if self._settings.orp_enabled and not self._settings.rs485_orp_enabled:
            self._orp = ORPSensor(self._adc)
        elif not self._settings.rs485_orp_enabled:
            logger.info("ORP disabled (orp_enabled=false); AIN3 is spare on PCBA Fin_3")

        # --- RS485 (Modbus) sensors — Honde probes on the shared USB bus.
        # The digital ORP supersedes the analog one; the 5-in-1's pH/TDS/temp
        # supersede their analog equivalents inside SamplingWorker.step().
        s = self._settings
        if s.rs485_chlorine_enabled or s.rs485_orp_enabled or s.rs485_multi_enabled:
            from sensors.honde import HondeChlorineSensor, HondeMultiSensor, HondeOrpSensor
            from sensors.modbus import ModbusBus

            self._modbus_bus = ModbusBus(s.rs485_port)
            if s.rs485_chlorine_enabled:
                self._chlorine = HondeChlorineSensor(self._modbus_bus, s.rs485_chlorine_addr)
            if s.rs485_orp_enabled:
                self._orp = HondeOrpSensor(self._modbus_bus, s.rs485_orp_addr)
                logger.info("Digital ORP (RS485 addr %d) supersedes analog", s.rs485_orp_addr)
            if s.rs485_multi_enabled:
                self._multi = HondeMultiSensor(self._modbus_bus, s.rs485_multi_addr)
            logger.info(
                "RS485 bus on %s (chlorine=%s orp=%s multi=%s)",
                s.rs485_port,
                s.rs485_chlorine_enabled,
                s.rs485_orp_enabled,
                s.rs485_multi_enabled,
            )

        # Apply calibration to sensors
        cal = self._cal.data
        self._ph.set_calibration(cal.ph_v_at_4, cal.ph_v_at_7)
        self._tds.set_calibration(cal.tds_k)
        self._turbidity.set_clear_water_voltage(cal.turbidity_v_clear)
        if self._orp and hasattr(self._orp, "set_offset"):
            # Analog ORP only — the digital probe is factory-calibrated and
            # holds its own state.
            self._orp.set_offset(cal.orp_offset_mv, 0.0)

        # --- Database ---
        self._db = WQM1Database()

        # --- Health reporter ---
        self._health = HealthReporter(FW_VERSION)

        # --- Smarter sensing (v2.1 modules; optional so a partial install
        # degrades to fixed-cadence sampling rather than failing to boot) ---
        try:
            from sensing.adaptive import AdaptiveSampler
            from sensing.monitor import SensorMonitor

            self._adaptive = AdaptiveSampler(self._settings_provider)
            self._monitor = SensorMonitor(self._settings_provider)
            logger.info("Sensing modules enabled (adaptive + monitor)")
        except ImportError:
            logger.info("Sensing modules not present — fixed-cadence sampling")

        # --- GPS ---
        try:
            self._gps = GPS(baud=self._settings.gps_baud)
        except Exception as e:
            logger.warning("GPS init failed: %s", e)

        # --- LoRa + LoRaWAN ---
        try:
            self._radio = SX1262()
            self._radio.init()
            app_key = bytes.fromhex(self._settings.app_key)
            self._lorawan = LoRaWANMAC(self._radio, self._dev_eui, APP_EUI, app_key)

            # Restore session from DB (join, if needed, happens on the radio
            # worker's thread so a missing gateway can't stall boot).
            saved = self._db.load_session()
            if saved and saved.get("joined"):
                session = LoRaWANSession(
                    dev_addr=bytes(saved["dev_addr"]) if saved["dev_addr"] else b"\x00" * 4,
                    nwk_skey=bytes(saved["nwk_skey"]) if saved["nwk_skey"] else b"\x00" * 16,
                    app_skey=bytes(saved["app_skey"]) if saved["app_skey"] else b"\x00" * 16,
                    fcnt_up=saved["fcnt_up"],
                    fcnt_down=saved["fcnt_down"],
                    joined=True,
                )
                self._lorawan.restore_session(session)
        except Exception as e:
            logger.warning("LoRa init failed: %s", e)

        # --- Cloud (HTTP/WiFi) transport — coexists with LoRaWAN ---
        if self._settings.cloud_enabled and self._settings.api_key:
            from cloud import CloudClient

            self._cloud = CloudClient(
                device_id=self._device_id,
                ingest_url=self._settings.cloud_ingest_url,
                command_url=self._settings.cloud_command_url,
                api_base=self._settings.cloud_api_base,
                api_key=self._settings.api_key,
                fw_version=FW_VERSION,
                batch_size=self._settings.batch_size,
                max_retries=self._settings.max_retries,
                retry_delays=self._settings.retry_delays,
                radios_provider=self._radios_snapshot,
                health_provider=self._health.get_report,
            )
            logger.info("Cloud HTTP transport enabled (ingest=%s)", self._settings.cloud_ingest_url)
        else:
            self._cloud = None

        # --- Rules engine ---
        self._rules = RulesEngine(self._relays)
        self._load_policies()
        if self._settings.rules:
            self._rules.load_rules(self._settings.rules)

        # --- Command listener (for service window) ---
        self._start_cmd_listener()

        # --- Heartbeat LED ---
        self._leds.heartbeat_start()

        # --- Supervisor + workers ---
        self._supervisor = Supervisor(
            workers=self._build_workers(),
            state=self._state,
            fan=self._fan,
            leds=self._leds,
            db=self._db,
            notifier=SdNotifier(),
            hw_watchdog=(HardwareWatchdog() if self._settings.hardware_watchdog_enabled else None),
        )

        # --- Signal handlers ---
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)
        atexit.register(self._shutdown)

        logger.info("All subsystems initialised")

    def _build_workers(self) -> list[Worker]:
        workers: list[Worker] = [
            SamplingWorker(
                self._settings_provider,
                sensors={
                    "temperature": self._temp,
                    "ph": self._ph,
                    "tds": self._tds,
                    "turbidity": self._turbidity,
                    "orp": self._orp,
                    "chlorine": self._chlorine,
                    "multi485": self._multi,
                },
                db=self._db,
                rules=self._rules,
                relays=self._relays,
                leds=self._leds,
                health=self._health,
                state=self._state,
                monitor=self._monitor,
                adaptive=self._adaptive,
            )
        ]
        if self._gps is not None:
            workers.append(GpsWorker(self._settings_provider, self._gps, self._leds, self._state))
        if self._lorawan is not None:
            workers.append(
                _JoiningRadioWorker(
                    self._settings_provider,
                    lorawan=self._lorawan,
                    radio=self._radio,
                    db=self._db,
                    rules=self._rules,
                    leds=self._leds,
                    health=self._health,
                    state=self._state,
                    persist_session=self._persist_session,
                    cayenne_encode=cayenne_encode,
                    fport_app=FPORT_APP,
                )
            )
        if self._cloud is not None:
            workers.append(CloudSyncWorker(self._settings_provider, self._cloud, self._db))
            workers.append(
                CommandWorker(
                    self._settings_provider,
                    self._cloud,
                    self._state,
                    apply_command=self._apply_cloud_command,
                    on_config_version=self._reconcile_remote_config,
                    on_ota_pending=self._nudge_ota_agent,
                )
            )
            self._heartbeat_worker = HeartbeatWorker(
                self._settings_provider,
                self._cloud,
                self._db,
                self._health,
                self._state,
                config_version_provider=lambda: self._config.remote_version,
                sensor_health_provider=(
                    self._monitor.health if self._monitor is not None else None
                ),
            )
            workers.append(self._heartbeat_worker)
        return workers

    # -- service-window command socket ---------------------------------------

    _CMD_SOCK_PATH = "/var/run/bluesignal/cmd.sock"

    def _start_cmd_listener(self) -> None:
        """Start Unix domain socket listener for service window commands."""
        sock_path = Path(self._CMD_SOCK_PATH)
        try:
            sock_path.parent.mkdir(parents=True, exist_ok=True)
            if sock_path.exists():
                sock_path.unlink()
            self._cmd_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._cmd_sock.bind(str(sock_path))
            self._cmd_sock.listen(2)
            self._cmd_sock.settimeout(1.0)
            self._cmd_thread = threading.Thread(target=self._cmd_listener_loop, daemon=True)
            self._cmd_thread.start()
            logger.info("Command listener started on %s", sock_path)
        except Exception as e:
            logger.warning("Could not start command listener: %s", e)

    def _cmd_listener_loop(self) -> None:
        """Accept connections and handle commands."""
        assert self._cmd_sock is not None
        while self._running:
            try:
                conn, _ = self._cmd_sock.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            try:
                data = conn.recv(4096)
                if data:
                    response = self._handle_cmd(json.loads(data.decode()))
                    conn.sendall(json.dumps(response).encode())
            except Exception as e:
                with contextlib.suppress(Exception):
                    conn.sendall(json.dumps({"ok": False, "error": str(e)}).encode())
            finally:
                conn.close()

    def _handle_cmd(self, cmd: dict) -> dict:
        """Dispatch a command from the service window."""
        action = cmd.get("action")
        if action == "relay_set":
            channel = cmd.get("channel")
            state = cmd.get("state")
            if not isinstance(channel, int) or channel < 1 or channel > 4:
                return {"ok": False, "error": "channel must be 1-4"}
            if not isinstance(state, bool):
                return {"ok": False, "error": "state must be boolean"}
            if self._relays:
                self._relays.set(channel, state)
                return {"ok": True, "channel": channel, "state": state}
            return {"ok": False, "error": "relays not initialised"}
        if action == "restart":
            self._state.request_restart()
            return {"ok": True, "restarting": True}
        if action == "config_reload":
            self._config.reload()
            return {"ok": True, "configVersion": self._config.remote_version}
        if action == "health":
            return {"ok": True, "health": self._health.get_report()}
        return {"ok": False, "error": f"unknown action: {action}"}

    _POLICIES_PATHS: list[str | Path] = [
        "/opt/bluesignal/config/policies.yaml",
        Path(__file__).parent.parent / "config" / "policies.yaml",
    ]

    def _load_policies(self) -> None:
        """Load relay automation policies from policies.yaml."""
        for p in self._POLICIES_PATHS:
            path = Path(p)
            if path.exists():
                try:
                    with path.open() as f:
                        policies = yaml.safe_load(f) or {}
                    self._rules.load_policies(policies)
                    # Also load rules from policies if config rules are empty
                    if not self._settings.rules and policies.get("rules"):
                        self._rules.load_rules(policies["rules"])
                    logger.info("Policies loaded from %s", path)
                    return
                except Exception as e:
                    logger.warning("Failed to load policies from %s: %s", path, e)
        logger.info("No policies.yaml found, using defaults")

    def _persist_session(self) -> None:
        """Save LoRaWAN session to database."""
        s = self._lorawan.session
        self._db.save_session(s.dev_addr, s.nwk_skey, s.app_skey, s.fcnt_up, s.fcnt_down, s.joined)

    def _radios_snapshot(self) -> dict[str, Any] | None:
        """Current radio status for the cloud Radios card — LoRa presence + GPS
        fix. Read at each cloud sync; cheap and safe to call anytime."""
        snap: dict[str, Any] = {}
        if self._radio is not None:
            snap["lora"] = {
                "present": True,
                "chip": "SX1262",
                "mode": "LoRaWAN" if self._lorawan is not None else None,
                "cs": 0,
            }
        if self._gps is not None:
            gps = self._state.gps()
            snap["gps"] = {
                "fix": gps.lat is not None and gps.lon is not None,
                "sats": gps.sats,
                "lat": gps.lat,
                "lon": gps.lon,
            }
        return snap or None

    # -- cloud command handling ------------------------------------------------

    def _apply_cloud_command(self, cmd: dict) -> None:
        """Actuate a single cloud command via the shared dispatcher, then ack."""
        cmd_id = cmd.get("id")
        cmd_type = cmd.get("type")
        try:
            if cmd_type == "relay":
                channel = int(cmd.get("channel") or 1)
                state = bool(cmd.get("state"))
                result = self._handle_cmd(
                    {"action": "relay_set", "channel": channel, "state": state}
                )
                if not result.get("ok"):
                    raise ValueError(result.get("error", "relay_set failed"))
                # Optional auto-off after a duration (cloud sends durationSeconds).
                duration = cmd.get("durationSeconds")
                if state and duration:
                    threading.Timer(
                        float(duration),
                        lambda ch=channel: self._handle_cmd(
                            {"action": "relay_set", "channel": ch, "state": False}
                        ),
                    ).start()
                self._cloud.ack_command(cmd_id, "done")
                logger.info("Cloud relay command: CH%d -> %s", channel, "on" if state else "off")
            elif cmd_type == "restart":
                self._cloud.ack_command(cmd_id, "done")
                self._state.request_restart()
            elif cmd_type == "config_reload":
                self._config.reload()
                self._cloud.ack_command(cmd_id, "done")
            elif cmd_type == "ota_check":
                self._nudge_ota_agent()
                self._cloud.ack_command(cmd_id, "done")
            elif cmd_type == "diagnostics":
                ok = self._heartbeat_worker.send_now() if self._heartbeat_worker else False
                self._cloud.ack_command(
                    cmd_id, "done" if ok else "error", None if ok else "heartbeat send failed"
                )
            else:
                self._cloud.ack_command(cmd_id, "error", f"unsupported type: {cmd_type}")
        except Exception as e:
            logger.error("Apply cloud command %s failed: %s", cmd_id, e)
            with contextlib.suppress(Exception):
                self._cloud.ack_command(cmd_id, "error", str(e))

    def _reconcile_remote_config(self, cloud_version: int) -> None:
        """Fetch + validate + apply desired config when the poll reports a new
        version. Rejection keeps last-known-good and tells the cloud why."""
        if cloud_version == self._config.remote_version:
            return
        fetched = self._cloud.fetch_config()
        if not fetched:
            return
        version, values = fetched
        ok, errors = self._config.apply_remote_config(version, values)
        if ok:
            logger.info("Remote config v%d applied (%d keys)", version, len(values))
            self._state.emit_event(
                {
                    "type": "config_applied",
                    "message": f"Applied remote config v{version}",
                    "details": {"version": version, "keys": sorted(values.keys())},
                }
            )
            applied_hot = hot_keys(values)
            needs_restart = restart_keys(values)
            if applied_hot:
                logger.info("Hot-applied: %s", ", ".join(sorted(applied_hot)))
            if needs_restart:
                logger.info(
                    "Restart-required keys applied (%s) — requesting graceful restart",
                    ", ".join(sorted(needs_restart)),
                )
                self._state.request_restart()
        else:
            logger.warning("Remote config v%d rejected: %s", version, "; ".join(errors))
            self._state.emit_event(
                {
                    "type": "config_rejected",
                    "message": f"Rejected remote config v{version}",
                    "details": {"version": version, "errors": errors[:5]},
                }
            )

    def _nudge_ota_agent(self) -> None:
        """Touch the flag file the OTA agent watches for an immediate check."""
        try:
            OTA_CHECK_FLAG.parent.mkdir(parents=True, exist_ok=True)
            OTA_CHECK_FLAG.touch()
        except OSError as e:
            logger.warning("Could not nudge OTA agent: %s", e)

    # -- lifecycle ---------------------------------------------------------------

    def run(self) -> None:
        """Start workers and block in the supervisor loop."""
        assert self._supervisor is not None
        self._running = True
        self._supervisor.start()
        self._supervisor.run()
        self._running = False
        self._supervisor.stop()

    def _handle_signal(self, signum: int, frame: types.FrameType | None) -> None:
        logger.info("Received signal %d, shutting down...", signum)
        self._running = False
        if self._supervisor:
            self._supervisor.stop_event.set()

    def _shutdown(self) -> None:
        logger.info("Shutting down...")
        # Close command socket
        if self._cmd_sock:
            with contextlib.suppress(Exception):
                self._cmd_sock.close()
            sock_path = Path(self._CMD_SOCK_PATH)
            if sock_path.exists():
                with contextlib.suppress(Exception):
                    sock_path.unlink()
        for obj, method in [
            (self._cloud, "stop"),
            (self._leds, "heartbeat_stop"),
            (self._relays, "all_off"),
            (self._radio, "close"),
            (self._gps, "close"),
            (self._modbus_bus, "close"),
            (self._adc, "close"),
            (self._db, "close"),
            (self._leds, "cleanup"),
        ]:
            if obj:
                with contextlib.suppress(Exception):
                    getattr(obj, method)()
        logger.info("Shutdown complete")


class _JoiningRadioWorker(RadioWorker):
    """RadioWorker that performs the OTAA join on its own thread first, so a
    missing gateway delays LoRa only — never sampling or cloud sync."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._join_attempts = 0

    def step(self) -> None:
        if not self._lorawan.session.joined:
            self._join_attempts += 1
            logger.info("OTAA join attempt %d", self._join_attempts)
            if self._lorawan.join(timeout_s=10.0):
                self._persist_session()
            return
        super().step()


def main() -> None:
    _setup_logging()
    app = WQM1App()
    try:
        app.start()
        app.run()
    except Exception as e:
        logger.critical("Fatal error: %s", e, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
