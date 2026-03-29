#!/usr/bin/env python3
"""
WQM-1 Firmware — Main Entry Point

Initialises all hardware, runs the sensor loop, and coordinates
LoRaWAN transmission, GPS fixes, cloud sync, and database storage.

Designed for Raspberry Pi Zero 2W + WQM-1 HAT (PCBA rev Fin_3).
"""

import atexit
import contextlib
import logging
import signal
import sys
import time
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

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
from utils.config import get_settings
from utils.health import HealthReporter
from utils.identity import APP_EUI, get_dev_eui, get_device_id
from utils.watchdog import FanController

logger = logging.getLogger("wqm1")


def _read_version() -> str:
    """Read firmware version from VERSION file."""
    version_file = Path(__file__).parent.parent / "VERSION"
    try:
        return version_file.read_text().strip()
    except Exception:
        return "1.0.0"


FW_VERSION = _read_version()


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------


def _setup_logging() -> None:
    settings = get_settings()
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
    """Main firmware application."""

    def __init__(self):
        self._running = False
        self._settings = get_settings()

        # Timestamps for interval tracking
        self._last_sensor_read = 0.0
        self._last_lora_tx = 0.0
        self._last_gps_fix = 0.0
        self._last_db_rotate = 0.0

        # GPS cache
        self._gps_lat = None
        self._gps_lon = None
        self._gps_alt = None

        # Device identity
        self._device_id = get_device_id()
        self._dev_eui = get_dev_eui()

        # Components
        self._relays = None
        self._leds = None
        self._fan = None
        self._adc = None
        self._temp = None
        self._gps = None
        self._radio = None
        self._lorawan = None
        self._ph = None
        self._tds = None
        self._turbidity = None
        self._orp = None
        self._db = None
        self._cloud = None
        self._health = None
        self._cal = None
        self._rules = None

    def start(self) -> None:
        """Initialise all hardware and start background threads."""
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
        self._orp = ORPSensor(self._adc)

        # Apply calibration to sensors
        cal = self._cal.data
        self._ph.set_calibration(cal.ph_v_at_4, cal.ph_v_at_7)
        self._tds.set_calibration(cal.tds_k)
        self._turbidity.set_clear_water_voltage(cal.turbidity_v_clear)
        self._orp.set_offset(cal.orp_offset_mv, 0.0)

        # --- Database ---
        self._db = WQM1Database()

        # --- Health reporter ---
        self._health = HealthReporter(FW_VERSION)

        # --- GPS ---
        try:
            self._gps = GPS()
        except Exception as e:
            logger.warning("GPS init failed: %s", e)

        # --- LoRa + LoRaWAN ---
        try:
            self._radio = SX1262()
            self._radio.init()
            app_key = bytes.fromhex(self._settings.app_key)
            self._lorawan = LoRaWANMAC(self._radio, self._dev_eui, APP_EUI, app_key)

            # Restore session from DB
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
            else:
                self._otaa_join()
        except Exception as e:
            logger.warning("LoRa init failed: %s", e)

        # --- Cloud sync ---
        self._cloud = None  # Cloud sync removed for open-source release
        pass  # Cloud sync disabled

        # --- Rules engine ---
        self._rules = RulesEngine(self._relays)
        if self._settings.rules:
            self._rules.load_rules(self._settings.rules)

        # --- Heartbeat LED ---
        self._leds.heartbeat_start()

        # --- Signal handlers ---
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)
        atexit.register(self._shutdown)

        logger.info("All subsystems initialised")

    def _otaa_join(self) -> None:
        """Attempt OTAA join with backoff."""
        for attempt in range(5):
            logger.info("OTAA join attempt %d/5", attempt + 1)
            try:
                if self._lorawan.join(timeout_s=10.0):
                    self._persist_session()
                    return
            except Exception as e:
                logger.warning("Join attempt %d failed: %s", attempt + 1, e)
            time.sleep(min(30 * (2**attempt), 300))
        logger.error("OTAA join failed after 5 attempts")

    def _persist_session(self) -> None:
        """Save LoRaWAN session to database."""
        s = self._lorawan.session
        self._db.save_session(s.dev_addr, s.nwk_skey, s.app_skey, s.fcnt_up, s.fcnt_down, s.joined)

    def run(self) -> None:
        """Main loop."""
        self._running = True
        self._do_sensor_read()
        self._last_sensor_read = time.monotonic()

        while self._running:
            now = time.monotonic()

            try:
                self._fan.update()
            except Exception as e:
                logger.error("Fan update error: %s", e)

            if now - self._last_sensor_read >= self._settings.sensor_read_s:
                self._do_sensor_read()
                self._last_sensor_read = now

            if now - self._last_gps_fix >= self._settings.gps_fix_s:
                self._do_gps_fix()
                self._last_gps_fix = now

            if self._lorawan and now - self._last_lora_tx >= self._settings.lora_tx_s:
                self._do_lora_tx()
                self._last_lora_tx = now

            if now - self._last_db_rotate >= 600:
                try:
                    self._db.rotate()
                except Exception as e:
                    logger.error("DB rotate error: %s", e)
                self._last_db_rotate = now

            time.sleep(1.0)

    def _do_sensor_read(self) -> None:
        """Read all sensors, apply rules, store in DB."""
        logger.info("Reading sensors...")

        temp_c = None
        try:
            temp_c = self._temp.read_temp_c()
        except Exception as e:
            logger.warning("Temperature read failed: %s", e)

        ph = self._safe_read("pH", lambda: self._ph.read(temp_c=temp_c))
        tds = self._safe_read("TDS", lambda: self._tds.read(temp_c=temp_c))
        turb = self._safe_read("Turbidity", lambda: self._turbidity.read())
        orp = self._safe_read("ORP", lambda: self._orp.read())

        reading = {
            "timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "ph": ph,
            "tds_ppm": tds,
            "turbidity_ntu": turb,
            "orp_mv": orp,
            "temp_c": temp_c,
            "lat": self._gps_lat,
            "lon": self._gps_lon,
            "alt_m": self._gps_alt,
            "battery_v": None,
            "relay_state": self._relays.get_state_bitmask() if self._relays else 0,
        }

        if self._rules:
            try:
                self._rules.evaluate(reading)
            except Exception as e:
                logger.error("Rules evaluation error: %s", e)

        try:
            row_id = self._db.insert_reading(reading)
            self._health.update_last_seen()
            logger.info(
                "Stored id=%d: pH=%s TDS=%s Turb=%s ORP=%s T=%s",
                row_id,
                f"{ph:.2f}" if ph else "N/A",
                f"{tds:.1f}" if tds else "N/A",
                f"{turb:.1f}" if turb else "N/A",
                f"{orp:.1f}" if orp else "N/A",
                f"{temp_c:.1f}°C" if temp_c else "N/A",
            )
        except Exception as e:
            logger.error("DB insert failed: %s", e)
            self._leds.error_pattern(3)

    def _safe_read(self, name: str, fn) -> float | None:
        try:
            return fn()
        except Exception as e:
            logger.warning("%s read failed: %s", name, e)
            self._leds.error_pattern(2)
            return None

    def _do_gps_fix(self) -> None:
        if self._gps is None:
            return
        self._leds.gps_fix_on()
        try:
            fix = self._gps.get_fix(timeout_s=self._settings.gps_fix_timeout_s)
            if fix:
                self._gps_lat = fix.latitude
                self._gps_lon = fix.longitude
                self._gps_alt = fix.altitude
                logger.info("GPS fix: %.6f, %.6f", fix.latitude, fix.longitude)
            else:
                if self._gps_lat is None:
                    self._gps.power_cycle()
        except Exception as e:
            logger.warning("GPS error: %s", e)
        finally:
            self._leds.gps_fix_off()

    def _do_lora_tx(self) -> None:
        if not self._lorawan or not self._lorawan.session.joined:
            return

        latest = self._db.get_latest()
        if not latest:
            return

        payload = cayenne_encode(latest)
        if not payload:
            return

        self._leds.lora_tx_on()
        try:
            downlink = self._lorawan.send_uplink(payload, fport=FPORT_APP)
            self._persist_session()

            if self._radio:
                self._health.update_rssi(self._radio.get_rssi())

            if downlink and self._rules:
                fport = downlink[0]
                data = downlink[1:]
                self._rules.process_downlink_command(fport, data)
        except Exception as e:
            logger.error("LoRa TX failed: %s", e)
        finally:
            self._leds.lora_tx_off()

    def _handle_signal(self, signum, frame) -> None:
        logger.info("Received signal %d, shutting down...", signum)
        self._running = False

    def _shutdown(self) -> None:
        logger.info("Shutting down...")
        for _name, obj, method in [
            ("cloud", self._cloud, "stop"),
            ("heartbeat", self._leds, "heartbeat_stop"),
            ("relays", self._relays, "all_off"),
            ("radio", self._radio, "close"),
            ("gps", self._gps, "close"),
            ("adc", self._adc, "close"),
            ("db", self._db, "close"),
            ("leds", self._leds, "cleanup"),
        ]:
            if obj:
                with contextlib.suppress(Exception):
                    getattr(obj, method)()
        logger.info("Shutdown complete")


def main():
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
