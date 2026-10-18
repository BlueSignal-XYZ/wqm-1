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
import os
import signal
import socket
import sys
import threading
import time
import types
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import yaml

from app.reboot import REBOOT_REQUEST_FLAG, request_host_reboot
from app.state import StateStore
from app.supervisor import LIVENESS_FILE, Supervisor
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
from control.irrigation_hold import IrrigationHold
from control.led import StatusLEDs
from control.relay import RelayController
from control.rules import RulesEngine
from integrations.apple_home import AppleHomeBridge
from integrations.home_assistant import HomeAssistantPublisher
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
from utils.clock import ClockDiscipline
from utils.config import (
    ADC_CH_ORP,
    ADC_CH_PH,
    ADC_CH_TDS,
    ADC_CH_TURBIDITY,
    FIRMWARE_VERSION,
    get_config_manager,
    hot_keys,
    restart_keys,
)
from utils.health import HealthReporter
from utils.identity import APP_EUI, get_dev_eui, get_device_id
from utils.sdnotify import SdNotifier
from utils.watchdog import FanController, HardwareWatchdog


def _hat_nets() -> dict[str, int]:
    """Every HAT net the direct-header stack drives, by name → BCM number."""
    from utils.config import (
        FAN_EN,
        GPS_EXTINT,
        LED_PINS,
        LORA_BUSY,
        LORA_DIO1,
        LORA_RST,
        RELAY_PINS,
    )

    nets: dict[str, int] = {}
    for i, pin in enumerate(RELAY_PINS, start=1):
        nets[f"relay_{i}"] = pin
    for i, pin in enumerate(LED_PINS, start=1):
        nets[f"led_{i}"] = pin
    nets["lora_rst"] = LORA_RST
    nets["lora_busy"] = LORA_BUSY
    nets["lora_dio1"] = LORA_DIO1
    nets["gps_extint"] = GPS_EXTINT
    nets["fan_en"] = FAN_EN
    return nets


logger = logging.getLogger("wqm1")

FW_VERSION = FIRMWARE_VERSION

# How often a failed ADS1115 (or other sensor) init is tried again. The first
# failure is logged once; retries are silent until the part answers or the
# process restarts. 60s is long enough that a wedged I2C bus is not polled
# into a log flood, and short enough that reseating the board at the bench
# shows up without a reboot.
ADC_RETRY_S = 60.0

# The OTA agent (separate service) polls this flag file for "check now" nudges
# from the command channel / otaPending hints.
OTA_CHECK_FLAG = Path("/var/lib/bluesignal/ota-check-now")

# Learned water profile, cached locally so a LoRa-only or offline site keeps
# its adaptive rules across restarts. Written on every config-channel sync.
WATER_PROFILE_CACHE = Path("/var/lib/bluesignal/water-profile.json")
# Re-fetch cadence for the profile (the cloud rebuilds it at most daily).
PROFILE_REFRESH_S = 6 * 3600


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

    # Class-level default so a partially built app (tests build one with
    # __new__ to exercise _handle_cmd) still answers relay_set.
    _hold: Any = None

    def __init__(self, config_path: str | None = None) -> None:
        # An explicit path lets N virtual units run on one host, each with its
        # own config; a real unit passes nothing and gets /etc/bluesignal.
        self._config = get_config_manager(config_path) if config_path else get_config_manager()
        self._settings_provider = lambda: self._config.settings
        self._state = StateStore()
        self._supervisor: Supervisor | None = None
        # Clock confidence: NTP when timesyncd has it, GPS RMC when it does
        # not, and 'unsynced' stamped on every reading otherwise.
        self._clock = ClockDiscipline()

        # Device identity
        self._device_id = get_device_id()
        self._dev_eui = get_dev_eui()

        # Components (lazy-initialised in start(); typed Any to avoid
        # union-attr noise — init order is guaranteed by start())
        self._pins: Any = None  # platform_support.HostPins on direct-header boards
        self._relays: Any = None
        self._leds: Any = None
        self._fan: Any = None
        self._adc: Any = None
        # Set when the converter did not answer. None means "not tried" or
        # "up". Distinct from a headerless board, which never sets it.
        self._adc_error: str | None = None
        self._adc_next_retry: float = 0.0
        self._faulted_channels: set[str] = set()
        self._fault_logged: set[str] = set()
        self._sensor_map: dict[str, Any] | None = None
        self._sampler: Any = None
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
        self._flow: Any = None
        self._db: Any = None
        self._cloud: Any = None
        self._health: Any = None
        self._cal: Any = None
        self._rules: Any = None
        self._hold: Any = None
        self._monitor: Any = None
        self._adaptive: Any = None
        self._smart_breaker: Any = None
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

        # --- Host board: decides whether Linux can reach the headers ---
        from platform_support import active_pins, detect_board, set_active_board

        self._board = detect_board(override=self._settings.board)
        # Every driver asks platform_support for the active profile from
        # here on, so the config override reaches them too.
        set_active_board(self._board)
        direct = self._board.has_direct_headers
        # Where the HAT's nets land on this host: bus numbers, the GPS UART
        # and a (chip, line) per BCM number. On the Pi this is the identity
        # map; on the Orange Pi Zero 3W it is the published pinout plus the
        # pins the bench read off the board (scripts/host-pins.py).
        self._pins = active_pins() if direct else None
        if self._pins is not None:
            logger.info(
                "Host pins: %s backend, i2c-%d, spidev%d.%d, GPS on %s (%s)",
                self._pins.backend,
                self._pins.i2c_bus,
                self._pins.spi_bus,
                self._pins.spi_device,
                self._pins.gps_port,
                self._pins.source,
            )
            missing = self._pins.unresolved(_hat_nets())
            if missing:
                # Say exactly which nets cannot be driven, and the fix. The
                # driver that needs one of them refuses at construction
                # (below), so this line is the one that explains the refusal.
                logger.error(
                    "%d HAT net(s) have no known line on %s: %s — run "
                    "scripts/host-pins.py --from-readall on the board and "
                    "restart (see docs/platforms.md)",
                    len(missing),
                    self._board.name,
                    ", ".join(f"{net}=BCM{bcm}(pin {phys})" for net, bcm, phys in missing),
                )
        if not direct:
            # Arduino Q family (UNO Q / VENTUNO Q): headers belong to the
            # MCU, so analog probes, LoRa, relays, LEDs, and the fan are
            # unavailable from Linux. Digital-first mode: RS485 probes over
            # the USB adapter, USB GPS, and Wi-Fi cloud sync carry the unit.
            logger.info(
                "Board %s has no direct header access — running digital-first "
                "(RS485 + GPS + cloud); analog/LoRa/relay disabled",
                self._board.name,
            )

        # --- Virtual unit: synthetic drivers, no hardware at all ---
        # `simulate_enabled` is never remotely settable (config schema), so a
        # field unit cannot be switched into this branch from the cloud. The
        # drivers present the hardware drivers' surface; everything below the
        # sensors (DB, cloud client, workers, command socket) is the real code.
        self._sim = None
        if self._settings.simulate_enabled:
            from sensors.sim import build_simulated_sensors

            self._sim = build_simulated_sensors(self._settings)
            direct = False
            logger.warning(
                "SIMULATED UNIT (simulate_enabled=true) — synthetic sensors, no hardware. "
                "Device %s must carry a SIM-WQM1- serial; faults=%r",
                self._device_id,
                self._settings.simulate_faults,
            )

        # --- GPIO outputs (direct-header boards only) ---
        # A missing board must not take the process down. GPIO lives on the
        # Pi, so this usually succeeds with the HAT unseated; if it does not,
        # relays and lamps stay off and the rest of the unit still boots.
        if direct:
            try:
                self._relays = RelayController()
                self._relays.all_off()
            except Exception as e:  # noqa: BLE001 — degraded boot, not a crash loop
                self._relays = None
                logger.error("Relay init failed — relays left off, continuing: %s", e)
            try:
                self._leds = StatusLEDs()
                self._leds.startup_test()
            except Exception as e:  # noqa: BLE001
                self._leds = None
                logger.error("Status LED init failed — continuing: %s", e)
            try:
                self._fan = FanController(
                    on_temp=self._settings.fan_on_temp_c,
                    off_temp=self._settings.fan_off_temp_c,
                )
            except Exception as e:  # noqa: BLE001
                self._fan = None
                logger.error("Fan init failed — continuing: %s", e)

        # --- ADC + sensors (direct-header boards only) ---
        self._cal = CalibrationManager()
        if self._sim is not None:
            self._temp = self._sim.temperature
            self._ph = self._sim.ph
            self._tds = self._sim.tds
            self._turbidity = self._sim.turbidity
            self._flow = self._sim.flow
        if direct:
            # ADS1115 init used to raise out of start() and systemd restarted
            # the service forever (field: 37 exits, "Input/output error" on
            # read_word_data, no setup page because nothing else mattered
            # once the process was down). A missing or unseated board now
            # leaves the analog channels faulted and the process up.
            self._bring_up_adc()
            self._init_direct_sensors(log_fitment=True)

        # --- RS485 (Modbus) sensors — Honde probes on the shared USB bus.
        # The digital ORP supersedes the analog one; the 5-in-1's pH/TDS/temp
        # supersede their analog equivalents inside SamplingWorker.step().
        s = self._settings
        if self._sim is None and (
            s.rs485_chlorine_enabled
            or s.rs485_orp_enabled
            or s.rs485_multi_enabled
            or s.rs485_flow_enabled
        ):
            try:
                from sensors.honde import HondeChlorineSensor, HondeMultiSensor, HondeOrpSensor
                from sensors.modbus import ModbusBus

                self._modbus_bus = ModbusBus(s.rs485_port, baudrate=s.rs485_baud)
                if s.rs485_flow_enabled and not s.flow_pulse_enabled:
                    from sensors.flow import ModbusFlowMeter

                    self._flow = ModbusFlowMeter(
                        self._modbus_bus, s.rs485_flow_addr, model=s.rs485_flow_model
                    )
                    logger.info(
                        "Flow meter: %s over RS485 addr %d", s.rs485_flow_model, s.rs485_flow_addr
                    )
                if s.rs485_chlorine_enabled:
                    self._chlorine = HondeChlorineSensor(self._modbus_bus, s.rs485_chlorine_addr)
                if s.rs485_orp_enabled:
                    self._orp = HondeOrpSensor(self._modbus_bus, s.rs485_orp_addr)
                    logger.info("Digital ORP (RS485 addr %d) supersedes analog", s.rs485_orp_addr)
                if s.rs485_multi_enabled:
                    self._multi = HondeMultiSensor(self._modbus_bus, s.rs485_multi_addr)
                logger.info(
                    "RS485 bus on %s @ %d (chlorine=%s orp=%s multi=%s flow=%s)",
                    s.rs485_port,
                    s.rs485_baud,
                    s.rs485_chlorine_enabled,
                    s.rs485_orp_enabled,
                    s.rs485_multi_enabled,
                    s.rs485_flow_enabled,
                )
            except Exception as e:  # noqa: BLE001 — a dead bus must not kill boot
                logger.error("RS485 init failed — digital probes faulted, continuing: %s", e)
                self._modbus_bus = None

        self._apply_analog_calibration()

        # --- Database ---
        self._db = WQM1Database()
        # LoRa-only / offline sites never mark rows synced; cap the buffer
        # by dropping the oldest pending rows instead of growing for ever.
        self._db.rotate_pending = not self._settings.cloud_enabled

        # --- Flow meter, pulse type (direct-header boards only; needs the DB
        # for its lifetime count, which is why it is built after it) ---
        if direct and self._settings.flow_pulse_enabled:
            try:
                from sensors.flow import PulseFlowMeter

                db = self._db
                saved = db.get_meta("flow_pulse_count")
                # The BCM number names the net; the host says which chip and
                # line that is (chip 0 / the same number on the Pi).
                flow_chip, flow_line = self._pins.line(self._settings.flow_pulse_gpio)
                self._flow = PulseFlowMeter(
                    gpio=self._settings.flow_pulse_gpio,
                    k_ppg=self._cal.data.flow_k_ppg,
                    initial_count=int(saved) if saved else 0,
                    persist=lambda n: db.set_meta("flow_pulse_count", str(n)),
                    chip=flow_chip,
                    line=flow_line,
                )
                if self._settings.rs485_flow_enabled:
                    logger.warning(
                        "Both flow meters declared — the pulse meter on GPIO %d is used, "
                        "the RS485 meter is ignored",
                        self._settings.flow_pulse_gpio,
                    )
            except Exception as e:
                logger.error("Flow pulse meter init failed: %s", e)
                self._flow = None

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
        if self._sim is not None:
            self._gps = self._sim.gps
        elif not self._settings.gps_enabled:
            # Declared not fitted at the network step: no UART opened, no
            # power-cycles of a receiver that is not there, no amber card.
            logger.info("GPS not fitted (gps_enabled: false) — skipping")
        else:
            try:
                gps_kwargs: dict[str, Any] = {"baud": self._settings.gps_baud}
                if self._pins is not None:
                    gps_kwargs["port"] = self._pins.gps_port
                self._gps = GPS(**gps_kwargs)
            except Exception as e:
                logger.warning("GPS init failed: %s", e)

        # --- LoRa + LoRaWAN (direct-header boards only: SX1262 is SPI) ---
        try:
            if not direct:
                raise RuntimeError("no direct SPI on this board")
            if not self._settings.lora_enabled:
                raise RuntimeError("LoRa not fitted (lora_enabled: false)")
            self._radio = SX1262(pins=self._pins)
            self._radio.init()
            app_key = bytes.fromhex(self._settings.app_key)
            # Config wins over the compiled-in placeholder so a TTN application
            # can be pointed at without cutting a firmware release. The join
            # will not be accepted unless this matches the JoinEUI the device
            # is registered under on the network server.
            app_eui = bytes.fromhex(self._settings.app_eui) if self._settings.app_eui else APP_EUI
            self._lorawan = LoRaWANMAC(
                self._radio,
                self._dev_eui,
                app_eui,
                app_key,
                sub_band=self._settings.lora_sub_band,
                persist_hook=self._persist_session,
            )

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
                self._lorawan.restore_session(session, saved.get("mac_params"))
            elif saved and saved.get("mac_params"):
                # Not joined, but the DevNonce counter and last channel plan
                # still matter for the next join attempt.
                self._lorawan.restore_mac_params(saved["mac_params"])
        except Exception as e:
            logger.warning("LoRa init failed: %s", e)

        # --- Cloud (HTTP/WiFi) transport — coexists with LoRaWAN ---
        #
        # Say why the transport is NOT starting. This used to be a bare `if`
        # with no else: a unit whose config said cloud_enabled: true but whose
        # api_key had silently failed to save produced not one line of log, and
        # sat offline while its dashboard said "no sensor data". The operator
        # has no way to tell "not configured" from "configured and broken"
        # without a message, and this is the cheapest message in the system.
        if self._settings.cloud_enabled and not self._settings.api_key:
            logger.error(
                "Cloud reporting is ENABLED but no API key is set — the device "
                "cannot report. Claim it in the cloud dashboard and paste the "
                "key into the Service Window (Provisioning, or the setup "
                "wizard's Cloud step), then restart this service."
            )
        elif not self._settings.cloud_enabled and self._settings.api_key:
            logger.warning(
                "An API key is set but cloud_enabled is false — readings are "
                "being stored locally and never uploaded. Enable cloud mode in "
                "the Service Window to start reporting."
            )
        elif not self._settings.cloud_enabled:
            logger.info(
                "Cloud reporting disabled (cloud_enabled=false, no API key). "
                "Readings are stored locally only."
            )

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
                irrigation_hold_provider=self._irrigation_hold_payload,
            )
            logger.info("Cloud HTTP transport enabled (ingest=%s)", self._settings.cloud_ingest_url)
        else:
            self._cloud = None

        # --- Irrigation hold ---
        # Its own engine, not a Rule: rules cannot arrive from the cloud, and
        # every rules-engine guard would cut an energised hold short. Built
        # before the rules engine so rules on its channel are set aside (with a
        # WARNING) at load. A virtual unit has no relay controller; it gets an
        # in-memory one so the hold can be driven in the emulator.
        hold_relays = self._relays
        if hold_relays is None and self._sim is not None:
            from sensors.sim import SimRelays

            hold_relays = SimRelays()
        self._hold = IrrigationHold(hold_relays)
        self._hold.configure(self._settings)

        # --- Apple Home ---
        # Opt-in HomeKit accessory on the LAN: readings out, nothing in. Built
        # here so the sampling worker can hand it every cycle; it starts
        # advertising only once apple_home_enabled is true.
        self._apple_home = AppleHomeBridge(
            self._device_id,
            FW_VERSION,
            Path(self._settings.db_path).parent,
        )
        self._apple_home.configure(self._settings)
        self._health.set_apple_home(self._apple_home)

        # --- Home Assistant ---
        # Opt-in MQTT discovery to the owner's broker: readings out, nothing
        # in. Same shape as the Apple Home bridge; connects only once
        # mqtt_enabled is true and a host is set.
        self._home_assistant = HomeAssistantPublisher(self._device_id, FW_VERSION)
        self._home_assistant.configure(self._settings)
        self._health.set_home_assistant(self._home_assistant)

        # --- Rules engine ---
        self._rules = RulesEngine(self._relays)
        self._rules.set_reserved_channels_provider(self._hold.reserved_channels)
        self._load_policies()
        # The hard on-time ceiling lives in the relay controller so it binds
        # every source — rules, cloud, Service Window, LoRa downlink alike.
        if self._relays is not None and self._rules.max_continuous_on_s:
            self._relays.max_on_s = float(self._rules.max_continuous_on_s)
        if self._settings.rules:
            self._rules.load_rules(self._settings.rules)
        # Monotonic time of the last water-profile sync attempt; -inf forces a
        # fetch on the first command poll after boot.
        self._profile_synced_mono = float("-inf")
        self._load_cached_water_profile()

        # --- Smart breaker / AWG circuit (optional; off unless configured) ---
        # Talks TO the customer's breaker; the G5Q relays stay the interlock.
        # Built after the relays so relay_only and the interlock can use them.
        try:
            from integrations.smart_breaker import build_smart_breaker

            self._smart_breaker = build_smart_breaker(
                self._settings_provider,
                relays=self._relays,
                event_sink=self._state.emit_event,
            )
        except ImportError:
            logger.info("Smart breaker integration not present")

        # --- Command listener (for service window) ---
        self._start_cmd_listener()

        # --- Heartbeat LED ---
        if self._leds:
            self._leds.heartbeat_start()

        # --- Supervisor + workers ---
        self._supervisor = Supervisor(
            workers=self._build_workers(),
            state=self._state,
            fan=self._fan,
            leds=self._leds,
            db=self._db,
            notifier=SdNotifier(),
            hw_watchdog=(
                HardwareWatchdog() if direct and self._settings.hardware_watchdog_enabled else None
            ),
            liveness_path=Path(self._settings.db_path).parent / LIVENESS_FILE,
        )

        # --- Signal handlers ---
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)
        atexit.register(self._shutdown)

        logger.info("All subsystems initialised")
        self.publish_boot_status(fatal="none")

    def _bring_up_adc(self) -> None:
        """Open the ADS1115. A failure marks the analog channels faulted.

        Logged once per outage. A retry that fails the same way is debug,
        so a missing board cannot fill the log the way the old crash loop did.
        """
        try:
            self._adc = ADS1115(bus=self._pins.i2c_bus)
        except Exception as e:  # noqa: BLE001 — degraded boot, not fatal
            self._adc = None
            # The driver already says "ADS1115 init failed: ...". Prefixing
            # it again made the log line say it twice.
            message = str(e)
            self._adc_error = message
            self._adc_next_retry = time.monotonic() + ADC_RETRY_S
            self._mark_analog_faulted()
            if "adc" not in self._fault_logged:
                self._fault_logged.add("adc")
                logger.error(
                    "%s — analog channels faulted; the rest of the unit is up and will retry",
                    message,
                )
            else:
                logger.debug("ADS1115 still not responding: %s", e)
            return
        recovered = "adc" in self._fault_logged or self._adc_error is not None
        self._adc_error = None
        self._fault_logged.discard("adc")
        self._clear_analog_faults()
        if recovered:
            logger.info("ADS1115 is responding — analog channels restored")

    def _mark_analog_faulted(self) -> None:
        """Enabled analog channels that have no converter are faulted.

        A channel declared not fitted stays not fitted. Temperature and the
        digital probes are not analog and are left alone.
        """
        s0 = self._settings
        faulted: set[str] = set()
        if s0.ph_enabled:
            faulted.add("ph")
        if s0.tds_enabled:
            faulted.add("tds")
        if s0.turbidity_enabled:
            faulted.add("turbidity")
        if s0.orp_enabled and not s0.rs485_orp_enabled:
            faulted.add("orp")
        kept = {
            name for name in self._faulted_channels if name not in ("ph", "tds", "turbidity", "orp")
        }
        self._faulted_channels = kept | faulted
        self._ph = None
        self._tds = None
        self._turbidity = None
        if not s0.rs485_orp_enabled:
            self._orp = None

    def _clear_analog_faults(self) -> None:
        self._faulted_channels -= {"ph", "tds", "turbidity", "orp"}

    def _try_channel(self, name: str, build: Any) -> Any:
        """Construct one sensor. A constructor that raises faults that channel."""
        try:
            sensor = build()
        except Exception as e:  # noqa: BLE001 — one sensor must not stop the others
            self._faulted_channels.add(name)
            if name not in self._fault_logged:
                self._fault_logged.add(name)
                logger.error("%s init failed — that channel is faulted, continuing: %s", name, e)
            return None
        self._faulted_channels.discard(name)
        self._fault_logged.discard(name)
        return sensor

    def _init_direct_sensors(self, *, log_fitment: bool) -> None:
        """Build the direct-header sensors. Analog ones need a live ADC.

        An undeclared channel is left as None, and SamplingWorker skips a
        None sensor — so an open input can never be read, converted, and
        published as a measurement. The alternative (read everything and try
        to spot nonsense downstream) is what let a probe-less unit emit pH
        for nine hours and raise 38 threshold alerts.
        """
        s0 = self._settings
        if self._sim is None and s0.temperature_enabled and self._temp is None:
            self._temp = self._try_channel("temperature", DS18B20)
        elif not s0.temperature_enabled:
            self._temp = None
        if self._adc is None:
            self._mark_analog_faulted()
        else:
            self._ph = (
                self._try_channel("ph", lambda: PHSensor(self._adc)) if s0.ph_enabled else None
            )
            self._tds = (
                self._try_channel("tds", lambda: TDSSensor(self._adc)) if s0.tds_enabled else None
            )
            self._turbidity = (
                self._try_channel("turbidity", lambda: TurbiditySensor(self._adc))
                if s0.turbidity_enabled
                else None
            )
            if not s0.ph_enabled:
                self._faulted_channels.discard("ph")
            if not s0.tds_enabled:
                self._faulted_channels.discard("tds")
            if not s0.turbidity_enabled:
                self._faulted_channels.discard("turbidity")
            if s0.orp_enabled and not s0.rs485_orp_enabled:
                self._orp = self._try_channel("orp", lambda: ORPSensor(self._adc))
            elif not s0.rs485_orp_enabled:
                self._orp = None
                self._faulted_channels.discard("orp")
        if log_fitment:
            for name, fitted in (
                ("temperature", s0.temperature_enabled),
                ("pH", s0.ph_enabled),
                ("TDS", s0.tds_enabled),
                ("turbidity", s0.turbidity_enabled),
            ):
                if not fitted:
                    logger.info("%s probe not fitted (declared in config) — not sampled", name)
            if not s0.orp_enabled and not s0.rs485_orp_enabled:
                logger.info("ORP disabled (orp_enabled=false); AIN3 is spare on PCBA Fin_3")

    def _apply_analog_calibration(self) -> None:
        """Push the stored calibration into whichever analog sensors exist.

        Digital probes hold their own calibration on the probe. Called at
        boot and again when a retry finally constructs the analog sensors.
        """
        if self._cal is None:
            return
        cal = self._cal.data
        if self._ph:
            # A never-calibrated electrode publishes nothing (status
            # "uncalibrated") — the factory placeholders do not describe the
            # LMP91200 front-end and would print inverted, offset pH.
            self._ph.set_calibration(
                cal.ph_v_at_4, cal.ph_v_at_7, calibrated=self._cal.is_calibrated("ph")
            )
        if self._tds:
            self._tds.set_calibration(cal.tds_k)
        if self._turbidity:
            self._turbidity.set_clear_water_voltage(cal.turbidity_v_clear)
        if self._orp and hasattr(self._orp, "set_offset"):
            self._orp.set_offset(cal.orp_offset_mv, 0.0)

    def _hardware_note(self) -> str | None:
        if self._adc_error:
            return f"{self._adc_error} — analog channels faulted; the unit is up and will retry"
        return None

    def _sync_sampler(self) -> None:
        """Push sensors built by a retry into the worker that already holds them."""
        if self._sensor_map is not None:
            self._sensor_map.update(
                {
                    "temperature": self._temp,
                    "ph": self._ph,
                    "tds": self._tds,
                    "turbidity": self._turbidity,
                    "orp": self._orp,
                    "chlorine": self._chlorine,
                    "multi485": self._multi,
                    "flow": self._flow,
                }
            )
        if self._sampler is not None:
            self._sampler.hardware_note = self._hardware_note()
            if self._adc is not None:
                self._sampler._no_sensor_cycles = 0
                self._sampler._empty_cycles = 0

    def _retry_deferred_sensors(self) -> None:
        """Try the ADS1115 again. Sampling calls this; it does not raise."""
        if self._pins is None or self._adc is not None or not self._adc_error:
            return
        now = time.monotonic()
        if now < self._adc_next_retry:
            return
        self._bring_up_adc()
        if self._adc is None:
            return
        self._init_direct_sensors(log_fitment=False)
        self._apply_analog_calibration()
        self._sync_sampler()
        self.publish_boot_status(fatal="none")

    def publish_boot_status(self, fatal: str | None = None) -> None:
        """Write the FAT/status snapshot. Never raises into the caller."""
        try:
            from diagnostics.boot_status import gps_line, onewire_line, write_boot_status

            board = None
            profile = getattr(self, "_board", None)
            if profile is not None:
                board = f"{profile.name} ({profile.id})"
            if self._adc is not None:
                adc = "ok"
            elif self._adc_error:
                adc = "faulted"
            elif self._pins is None:
                adc = "absent"
            else:
                adc = "unknown"
            write_boot_status(
                board=board,
                adc=adc,
                adc_error=self._adc_error or "",
                onewire=onewire_line(self._temp),
                gps=gps_line(self._gps),
                faulted=sorted(self._faulted_channels),
                fatal=fatal if fatal is not None else "none",
            )
        except Exception as e:  # noqa: BLE001 — status is diagnostic, not load-bearing
            logger.warning("Could not write boot status: %s", e)

    def hardware_status(self) -> dict[str, Any]:
        """What the Service Window shows when the board did not come up."""
        if self._adc is not None:
            adc = "ok"
        elif self._adc_error:
            adc = "faulted"
        elif self._pins is None:
            adc = "absent"
        else:
            adc = "unknown"
        return {
            "adc": adc,
            "adcError": self._adc_error,
            "faulted": sorted(self._faulted_channels),
            "fatal": None,
        }

    def _build_workers(self) -> list[Worker]:
        sampler_cls: Any = SamplingWorker
        sampler_args: tuple[Any, ...] = ()
        if self._sim is not None:
            from sensors.sim.unit import SimSamplingWorker

            sampler_cls = SimSamplingWorker
            sampler_args = (self._sim,)
        self._sensor_map = {
            "temperature": self._temp,
            "ph": self._ph,
            "tds": self._tds,
            "turbidity": self._turbidity,
            "orp": self._orp,
            "chlorine": self._chlorine,
            "multi485": self._multi,
            "flow": self._flow,
        }
        sampler = sampler_cls(
            *sampler_args,
            self._settings_provider,
            sensors=self._sensor_map,
            db=self._db,
            rules=self._rules,
            relays=self._relays,
            leds=self._leds,
            health=self._health,
            state=self._state,
            monitor=self._monitor,
            adaptive=self._adaptive,
            clock_source=self._clock.source,
            irrigation_hold=self._hold,
            apple_home=self._apple_home,
            home_assistant=self._home_assistant,
            maintenance=self._retry_deferred_sensors,
        )
        sampler.hardware_note = self._hardware_note()
        self._sampler = sampler
        workers: list[Worker] = [sampler]
        if self._gps is not None:
            workers.append(
                GpsWorker(
                    self._settings_provider,
                    self._gps,
                    self._leds,
                    self._state,
                    on_time=self._clock.observe_gps,
                )
            )
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
        if self._smart_breaker is not None and self._smart_breaker.vendor == "ableedge":
            from integrations.smart_breaker import SmartBreakerWorker

            workers.append(SmartBreakerWorker(self._settings_provider, self._smart_breaker))
        return workers

    # -- service-window command socket ---------------------------------------

    @property
    def _cmd_sock_path(self) -> str:
        return str(self._settings.cmd_sock)

    def _start_cmd_listener(self) -> None:
        """Start Unix domain socket listener for service window commands."""
        sock_path = Path(self._cmd_sock_path)
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
            if self._hold is not None and self._hold.owns(channel):
                # A manual OFF would silently release a hold; a manual ON would
                # leave a coil the hold then thinks it controls. Cloud commands
                # and the Service Window both arrive here; LoRa FPort 100 is
                # refused in RulesEngine.process_downlink_command.
                return {"ok": False, "error": f"irrigation hold owns relay {channel}"}
            duration = cmd.get("duration_s")
            if duration is not None and (
                isinstance(duration, bool) or not isinstance(duration, int | float) or duration < 0
            ):
                return {"ok": False, "error": "duration_s must be a non-negative number"}
            if self._relays:
                self._relays.set(channel, state)
                result: dict[str, Any] = {"ok": True, "channel": channel, "state": state}
                arm = getattr(self._relays, "arm_auto_off", None)
                if state and duration and callable(arm):
                    result["autoOffS"] = arm(channel, float(duration))
                return result
            return {"ok": False, "error": "relays not initialised"}
        if action == "lora_rejoin":
            if self._lorawan is None:
                return {"ok": False, "error": "LoRa not initialised"}
            self._lorawan.forget_session()
            self._persist_session()
            return {"ok": True, "rejoining": True}
        if action == "restart":
            self._state.request_restart()
            return {"ok": True, "restarting": True}
        if action == "reboot":
            # Whole-host reboot, not just this service — the Service Window's
            # break-glass path when a unit is reachable on :8080 but its sshd
            # is not. We hold no privilege to do it ourselves: relays go to
            # fail-safe here, the root OTA agent does the reboot.
            return request_host_reboot(self._relays, REBOOT_REQUEST_FLAG)
        if action == "config_reload":
            self._config.reload()
            self._reconfigure_hold()
            self._reconfigure_apple_home()
            self._reconfigure_home_assistant()
            return {"ok": True, "configVersion": self._config.remote_version}
        if action == "irrigation_hold_status":
            if self._hold is None:
                return {"ok": False, "error": "irrigation hold not initialised"}
            return {"ok": True, **self._hold.status()}
        if action == "apple_home_status":
            # Service Window only (LAN, logged in): carries the setup code.
            if self._apple_home is None:
                return {"ok": False, "error": "apple home not initialised"}
            return {"ok": True, **self._apple_home.status()}
        if action == "home_assistant_status":
            # Service Window only: names the broker, never the password.
            if getattr(self, "_home_assistant", None) is None:
                return {"ok": False, "error": "home assistant not initialised"}
            return {"ok": True, **self._home_assistant.status()}
        if action == "health":
            return {"ok": True, "health": self._health.get_report()}
        if action == "hardware_status":
            return {"ok": True, **self.hardware_status()}
        if action in ("awg_set", "circuit_set"):
            # "AWG circuit on/off" — vendor-agnostic. Goes through the smart
            # breaker controller (interlock relay + breaker), never straight to
            # a coil, so fail-safe bookkeeping sees every request. `circuit_set`
            # is the spelling the first skeleton (PR #112) advertised; kept as
            # an alias so nothing written against it breaks.
            state = cmd.get("state")
            if not isinstance(state, bool):
                return {"ok": False, "error": "state must be boolean"}
            if self._smart_breaker is None:
                return {"ok": False, "error": "smart breaker integration not configured"}
            source = str(cmd.get("source") or "service_window")
            reason = cmd.get("reason")
            return self._smart_breaker.request(
                state, source=source, reason=str(reason) if reason else None
            )
        if action == "awg_status":
            if self._smart_breaker is None:
                return {"ok": True, "configured": False, "vendor": "none"}
            return {"ok": True, "configured": True, **self._smart_breaker.status()}
        if action == "adc_voltages":
            return self._adc_voltages()
        return {"ok": False, "error": f"unknown action: {action}"}

    # Channel order is the wizard's order, not the chip's.
    _ADC_CHANNELS: tuple[tuple[str, int], ...] = (
        ("ph", ADC_CH_PH),
        ("tds", ADC_CH_TDS),
        ("turbidity", ADC_CH_TURBIDITY),
        ("orp", ADC_CH_ORP),
    )

    def _adc_voltages(self) -> dict:
        """Raw ADC voltages, for the Service Window's calibration wizards.

        Every wizard tells the installer to "read the voltage" and, until this
        existed, nothing in the product displayed one — so the only way to
        calibrate a unit was to SSH in and poke the ADC by hand, which is not a
        thing a field installer does. The channel therefore stayed on its
        factory-nominal constants, and a nominal pH reading looks exactly like
        a calibrated one.

        This runs in the daemon rather than in the Service Window process for a
        specific reason: the daemon owns the I2C bus. Two processes driving an
        ADS1115's single conversion register cannot be made safe by a lock in
        either one of them (see ADS1115._lock) — the Service Window must ask,
        never read.

        Returns raw volts only. Anything derived (divider, temperature, ppm) is
        computed from the one shared function the sensor itself uses, so a
        wizard can never be arithmetically out of step with the measurement.
        """
        if self._adc is None:
            if self._adc_error:
                return {"ok": False, "error": self._adc_error}
            return {"ok": False, "error": "no ADC on this board"}
        channels: dict[str, dict] = {}
        for name, ch in self._ADC_CHANNELS:
            try:
                channels[name] = {"channel": ch, "adcVolts": round(self._adc.read_voltage(ch), 5)}
            except Exception as e:  # noqa: BLE001 — one dead channel must not hide the other three
                channels[name] = {"channel": ch, "error": str(e)[:80]}
        return {"ok": True, "channels": channels}

    # First match wins. /etc/bluesignal is the only location that survives an
    # upgrade: setup.sh and the OTA agent install each release into its own
    # /opt/bluesignal/releases/<version>/ tree with the STOCK policies.yaml, so
    # a customer's rules and their `manual.override: false` edited in the
    # release tree were silently reverted by the next update. setup.sh now
    # seeds /etc/bluesignal/policies.yaml once and never overwrites it.
    _POLICIES_PATHS: list[str | Path] = [
        "/etc/bluesignal/policies.yaml",
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
        self._db.save_session(
            s.dev_addr,
            s.nwk_skey,
            s.app_skey,
            s.fcnt_up,
            s.fcnt_down,
            s.joined,
            mac_params=self._lorawan.mac_params,
        )

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
                req: dict[str, Any] = {"action": "relay_set", "channel": channel, "state": state}
                # Optional auto-off after a duration (cloud sends durationSeconds);
                # enforced by the relay controller's own timer, so it fires even
                # if this process is busy, and a later command replaces it.
                duration = cmd.get("durationSeconds")
                if state and duration:
                    req["duration_s"] = float(duration)
                result = self._handle_cmd(req)
                if not result.get("ok"):
                    raise ValueError(result.get("error", "relay_set failed"))
                self._cloud.ack_command(cmd_id, "done")
                logger.info("Cloud relay command: CH%d -> %s", channel, "on" if state else "off")
            elif cmd_type in ("awg", "circuit"):  # `circuit`: PR #112 alias
                # Cloud asks for the AWG circuit on/off. Same dispatcher as the
                # Service Window so one code path owns interlock + fail-safe.
                state = bool(cmd.get("state"))
                result = self._handle_cmd(
                    {
                        "action": "awg_set",
                        "state": state,
                        "source": "cloud",
                        "reason": cmd.get("reason") or f"cloud command {cmd_id}",
                    }
                )
                duration = cmd.get("durationSeconds")
                if result.get("ok") and state and duration:
                    threading.Timer(
                        float(duration),
                        lambda: self._handle_cmd(
                            {
                                "action": "awg_set",
                                "state": False,
                                "source": "cloud",
                                "reason": f"cloud command {cmd_id} duration elapsed",
                            }
                        ),
                    ).start()
                if result.get("ok"):
                    self._cloud.ack_command(cmd_id, "done")
                else:
                    self._cloud.ack_command(
                        cmd_id, "error", str(result.get("error") or "awg_set failed")
                    )
                logger.info(
                    "Cloud AWG command: %s -> %s (%s)",
                    "on" if state else "off",
                    "ok" if result.get("ok") else "error",
                    result.get("breaker", "-"),
                )
            elif cmd_type == "restart":
                self._cloud.ack_command(cmd_id, "done")
                self._state.request_restart()
            elif cmd_type == "config_reload":
                self._config.reload()
                self._reconfigure_hold()
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

    def _reconcile_remote_config(self, cloud_version: int | None) -> None:
        """Fetch + validate + apply desired config when the poll reports a new
        version. Rejection keeps last-known-good and tells the cloud why.

        Also owns the periodic water-profile refresh (rung 5, edge sync): the
        learned baseline rides the same GET /config response, so this fetches
        every PROFILE_REFRESH_S even when the config version is unchanged —
        or None, for devices that were never remotely configured.
        """
        profile_stale = (time.monotonic() - self._profile_synced_mono) >= PROFILE_REFRESH_S
        config_new = isinstance(cloud_version, int) and cloud_version != self._config.remote_version
        if not config_new and not profile_stale:
            return
        # Count the attempt now — a 204/failed fetch must not retry every poll.
        self._profile_synced_mono = time.monotonic()
        fetched = self._cloud.fetch_config()
        if not fetched:
            return
        version, values, profile = fetched
        if profile is not None:
            self._apply_water_profile(profile)
        if not config_new or version is None:
            return
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
                self._reconfigure_hold()
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

    def _reconfigure_hold(self) -> None:
        """Re-snapshot the irrigation hold's (hot) settings after a reload, so
        a disable or a relay change takes effect now rather than next sample."""
        if self._hold is not None:
            try:
                self._hold.configure(self._settings)
            except Exception as e:  # noqa: BLE001 — a reload must not fail on this
                logger.error("Irrigation hold reconfigure failed: %s", e)

    def _reconfigure_apple_home(self) -> None:
        """Start or stop the HomeKit accessory after a reload."""
        if getattr(self, "_apple_home", None) is not None:
            try:
                self._apple_home.configure(self._settings)
            except Exception as e:  # noqa: BLE001 — a reload must not fail on this
                logger.error("Apple Home reconfigure failed: %s", e)

    def _reconfigure_home_assistant(self) -> None:
        """Start, restart or stop the MQTT publisher after a reload."""
        if getattr(self, "_home_assistant", None) is not None:
            try:
                self._home_assistant.configure(self._settings)
            except Exception as e:  # noqa: BLE001 — a reload must not fail on this
                logger.error("Home Assistant reconfigure failed: %s", e)

    def _irrigation_hold_payload(self) -> dict[str, Any] | None:
        """metadata.irrigationHold at upload time; None omits the key."""
        return self._hold.payload() if self._hold is not None else None

    def _apply_water_profile(self, profile: dict, persist: bool = True) -> None:
        """Install the learned baseline into the rules engine and (by default)
        cache it atomically so adaptive rules survive restarts offline."""
        try:
            self._rules.set_baselines(profile)
        except Exception as e:  # noqa: BLE001 — a bad profile must not kill the poll
            logger.warning("Water profile rejected: %s", e)
            return
        if not persist:
            return
        try:
            WATER_PROFILE_CACHE.parent.mkdir(parents=True, exist_ok=True)
            tmp = WATER_PROFILE_CACHE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(profile))
            tmp.replace(WATER_PROFILE_CACHE)
        except OSError as e:
            logger.warning("Could not cache water profile: %s", e)

    def _load_cached_water_profile(self) -> None:
        """Boot-time load of the last synced profile (edge sync, offline half)."""
        try:
            if not WATER_PROFILE_CACHE.exists():
                return
            profile = json.loads(WATER_PROFILE_CACHE.read_text())
        except (OSError, ValueError) as e:
            logger.warning("Could not load cached water profile: %s", e)
            return
        if isinstance(profile, dict):
            self._apply_water_profile(profile, persist=False)

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
            sock_path = Path(self._cmd_sock_path)
            if sock_path.exists():
                with contextlib.suppress(Exception):
                    sock_path.unlink()
        for obj, method in [
            (self._cloud, "stop"),
            (getattr(self, "_apple_home", None), "stop"),
            (getattr(self, "_home_assistant", None), "stop"),
            (self._leds, "heartbeat_stop"),
            (self._smart_breaker, "shutdown"),
            (self._relays, "all_off"),
            (self._radio, "close"),
            (self._gps, "close"),
            (self._flow, "close"),
            (self._modbus_bus, "close"),
            (self._adc, "close"),
            (self._db, "close"),
            (self._leds, "cleanup"),
        ]:
            if obj:
                with contextlib.suppress(Exception):
                    getattr(obj, method)()
        logger.info("Shutdown complete")


JOIN_BACKOFF_MAX_S = 3600.0


class _JoiningRadioWorker(RadioWorker):
    """RadioWorker that performs the OTAA join on its own thread first, so a
    missing gateway delays LoRa only — never sampling or cloud sync."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._join_attempts = 0

    def interval_s(self) -> float:
        base = super().interval_s()
        if self._lorawan is None or self._lorawan.session.joined:
            return base
        # Join backoff: three attempts at the normal cadence, then doubling up
        # to an hour. An un-gatewayed unit used to burn a DevNonce every
        # 300 s for ever (288 a day), and the network server refuses nonces
        # it has already seen.
        extra = max(0, self._join_attempts - 3)
        return float(min(base * (2**extra), JOIN_BACKOFF_MAX_S))

    def step(self) -> None:
        if not self._lorawan.session.joined:
            self._join_attempts += 1
            logger.info("OTAA join attempt %d", self._join_attempts)
            if self._lorawan.join(timeout_s=10.0):
                self._join_attempts = 0
                self._persist_session()
            return
        super().step()


def _config_path_from_argv(argv: list[str]) -> str | None:
    """``--config PATH`` (or ``BLUESIGNAL_CONFIG``) — used by the fleet
    simulator; a real unit runs with neither and reads /etc/bluesignal."""
    for i, arg in enumerate(argv):
        if arg == "--config" and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith("--config="):
            return arg.split("=", 1)[1]
    return os.environ.get("BLUESIGNAL_CONFIG") or None


def main() -> None:
    config_path = _config_path_from_argv(sys.argv[1:])
    if config_path:
        # Settings are read once at construction; logging needs them, so the
        # manager must be primed with the path BEFORE _setup_logging runs.
        get_config_manager(config_path)
    _setup_logging()
    app = WQM1App(config_path)
    try:
        app.start()
        app.run()
    except Exception as e:
        logger.critical("Fatal error: %s", e, exc_info=True)
        try:
            app.publish_boot_status(fatal=f"{type(e).__name__}: {e}")
        except Exception:  # noqa: BLE001 — the process is already exiting
            logger.debug("boot status was not written on the way out", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
