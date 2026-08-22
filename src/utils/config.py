"""
WQM-1 Configuration

Hardware constants (from schematic, immutable) and runtime settings
(from YAML config file, mutable). Merges the previous hardware.py
and settings.py into a single module.

v2 adds a declarative settings schema (types, bounds, hot-reloadable vs
restart-required), layered loading (base config.yaml overlaid by the
remotely-managed config.d/remote.yaml), validation, and live reload — the
foundations for cloud-pushed configuration that can never brick a device.
"""

import json
import logging
import threading
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger("wqm1.config")

# ===========================================================================
# Hardware constants (PCBA rev Fin_3, BCM numbering) — DO NOT MODIFY
# ===========================================================================

# I2C Bus 1
I2C_BUS = 1
I2C_SDA = 2
I2C_SCL = 3
ADS1115_ADDR = 0x48
ADS1115_ALERT_RDY = 5

# ADC channel assignments (match PCBA Fin_3 schematic, BST.ADC.SchDoc)
ADC_CH_TDS = 0  # AIN0 = VIN0, CD4060 + LM324 TDS chain, 0-2.3 V
ADC_CH_TURBIDITY = 1  # AIN1 = VIN1, LMV321 turbidity buffer, 0-4.5 V
ADC_CH_PH = 2  # AIN2 = PH_INP, LMP91200 pH AFE via R12
ADC_CH_ORP = 3  # AIN3 = PH_INN / spare (no ORP hardware on Fin_3)

# ADS1115 full scale at the PGA the driver configures (±4.096 V).
ADC_FULL_SCALE_V = 4.096

# How close to either rail counts as "not a measurement".
#
# A disconnected analog probe leaves its ADC input floating and it drifts to a
# rail. No conditioned probe signal on this board sits within 50 mV of 0 V or
# of full scale — the TDS chain spans 0-2.3 V and the pH AFE is biased near
# mid-supply — so a sample this close to either end is an open input, not a
# reading.
#
# BENCH-VALIDATE before relying on this in the field: confirm with a real
# electrode in pH 4 and pH 10 buffer that neither endpoint lands inside the
# margin. Widen it only with a measurement to justify the number.
ADC_RAIL_MARGIN_V = 0.05

# The sentence above — "no conditioned probe signal sits within 50 mV of 0 V" —
# is TRUE for the pH AFE, which is biased near mid-supply and cannot approach
# either rail in real water. It is FALSE for TDS, whose valid range STARTS at
# zero: less conductive water means less voltage, and clean water genuinely
# sits near the bottom of the span.
#
# Applying the pH margin to TDS therefore threw away real measurements. With
# TDS_DIVIDER_RATIO = 0.3125 and the default 500 ppm/V calibration, 0.05 V at
# the ADC is 0.16 V at the probe, or **80 ppm** — so every sample below 80 ppm
# was discarded and logged as "probe disconnected or dry". A customer with
# clean water lost the channel entirely (2026-08-21).
#
# An input that is genuinely open or in air is not merely low, it is flat zero.
# The ADS1115 LSB at this PGA is 125 µV, so 3 mV is ~24 counts — far above the
# noise floor of a real zero, far below any conducting sample.
#
# BENCH-VALIDATE: with the probe lifted clear of the water and dried, confirm
# the reading sits below this; with the probe in the most dilute water the unit
# will ever see, confirm it sits above.
ADC_OPEN_INPUT_V = 0.003

# Documented top of the TDS conditioning chain (see ADC_CH_TDS above). Distinct
# from ADC_FULL_SCALE_V, which is the converter's range, not this channel's: a
# TDS signal at 4 V is impossible and means the chain has railed, but comparing
# against 4.096 could never detect that.
TDS_V_MAX = 2.3

# How far above the clear-water calibration point still counts as clear water.
#
# Turbidity is inverse — more light through, higher voltage — so water CLEANER
# than the calibration reference computes a slightly negative NTU. That is the
# best case the instrument can see, and refusing it published nothing at all
# for the clearest water. Within this band the reading is reported as 0.0 NTU;
# beyond it the clear-water reference is genuinely stale and says so.
TURB_CLEAR_TOLERANCE_V = 0.25

# Largest pH spread, across one filter window, that real water can produce.
#
# The rail check above catches a probe whose input floats to a supply rail —
# which is what the TDS chain does, and it works. The pH front-end does not:
# the LMP91200 biases an open electrode input to MID-SCALE, so a disconnected
# probe yields voltages that are electrically valid and convert to perfectly
# in-range pH. A first field unit with no electrode attached published 11.32,
# 5.80, 9.06 and 10.19 over five minutes and every one passed both guards.
#
# What gives it away is the SPREAD. A real electrode in a body of water does
# not move 5.5 pH in five minutes; a floating input wanders across the scale.
# 2.0 pH across the window is already far beyond anything chemistry does at a
# 60 s sample interval, including active dosing.
#
# BENCH-VALIDATE before trusting this in the field: dose a test volume as hard
# as a real site ever would and confirm the window spread stays under the
# limit. Raising it is cheap; a limit that suppresses real excursions is not.
PH_MAX_WINDOW_SPAN = 2.0

# SPI0 — SX1262 LoRa
SPI_BUS = 0
SPI_DEVICE = 0
SPI_CS = 8
LORA_RST = 18
LORA_BUSY = 20
LORA_DIO1 = 16

# LoRa radio parameters
LORA_FREQUENCY = 915_000_000
LORA_BANDWIDTH = 4  # 125 kHz
LORA_SPREADING_FACTOR = 9
LORA_CODING_RATE = 1  # CR 4/5
LORA_TX_POWER = 22
LORA_PREAMBLE_LEN = 8
LORA_SYNC_WORD = 0x3444
LORA_CRC_ON = True
LORA_PA_DUTY_CYCLE = 0x04
LORA_HP_MAX = 0x07
LORA_PA_DEVICE_SEL = 0x00
LORA_PA_LUT = 0x01

# UART — GPS
GPS_UART_PORT = "/dev/serial0"
# The module fitted on every WQM-1 talks 38400. This was 9600 (the u-blox
# NEO-6/7/8 default) and did not match the hardware, so out of the box the
# firmware read the receiver's output as noise and never obtained a fix.
GPS_BAUD = 38400
GPS_EXTINT = 19

# ── GPS fix cadence ─────────────────────────────────────────────────────────
#
# A WQM-1 is bolted to a fixed structure. Its coordinate is established at
# commissioning and cannot change without someone physically moving the unit,
# so re-acquiring it every ten minutes — the old default — bought nothing and
# spent power on a solar-fed installation.
#
# The bounds are the founder's, 2026-08-21, the night a pack ran flat because
# the panel does not cover the load: **at most once a day, at least once every
# fifteen days.** Both ends are deliberate. Daily is often enough to notice a
# unit that has been moved or stolen; fifteen days is the point past which a
# stale coordinate stops being trustworthy for a map pin.
#
# A stored config carrying the old 600 is now out of range, which the validator
# REJECTS with a logged error and falls back to the default — so an existing
# unit lands on daily and says so, rather than silently keeping the old rate.
GPS_FIX_MIN_S = 86_400  # once a day
GPS_FIX_MAX_S = 1_296_000  # once every fifteen days

# 1-Wire
ONEWIRE_PIN = 4

# Relays (active-high)
RELAY_1 = 17
RELAY_2 = 27
RELAY_3 = 22
RELAY_4 = 23
RELAY_PINS = (RELAY_1, RELAY_2, RELAY_3, RELAY_4)

# LEDs (active-high, 470Ω)
LED_1 = 24
LED_2 = 25
LED_3 = 12
LED_4 = 13
LED_PINS = (LED_1, LED_2, LED_3, LED_4)
LED_HEARTBEAT = LED_1
LED_LORA_TX = LED_2
LED_GPS_FIX = LED_3
LED_ERROR = LED_4

# Fan
FAN_EN = 21

# Analog signal chain
PH_VREF = 2.048
NERNST_SLOPE_25C = 0.05916
NERNST_R = 8.314
NERNST_F = 96485.0
TDS_DIVIDER_RATIO = 1000.0 / (2200.0 + 1000.0)
TDS_TEMP_COEFF = 0.02
TURB_V_CLEAR = 4.1
TURB_V_MAX = 0.5
TURB_NTU_MAX = 3000.0

# ===========================================================================
# Runtime settings (loaded from YAML)
# ===========================================================================

_DEFAULT_CONFIG_PATH = "/etc/bluesignal/config.yaml"
_REMOTE_OVERLAY_PATH = "/etc/bluesignal/config.d/remote.yaml"


def _read_version_file() -> str:
    """Firmware version comes from the repo-root VERSION file — the single
    source of truth (CI enforces tag == VERSION == pyproject)."""
    version_file = Path(__file__).resolve().parent.parent.parent / "VERSION"
    try:
        return version_file.read_text().strip()
    except Exception as e:
        logger.debug("Could not read VERSION file: %s", e)
        return "0.0.0"


FIRMWARE_VERSION = _read_version_file()


@dataclass
class Settings:
    """Runtime configuration loaded from YAML."""

    # Timing
    sensor_read_s: int = 60
    lora_tx_s: int = 300
    # Daily. See GPS_FIX_MIN_S — this is a fixed installation on a solar budget,
    # and the coordinate does not move.
    gps_fix_s: int = 86400
    gps_fix_timeout_s: int = 60

    # GPS
    # 38400 is what the module on the WQM-1 actually uses — verified in the
    # field against raw NMEA. Overridable because u-blox parts vary (NEO-6/7/8
    # default to 9600, some custom-flashed boards ship at 115200), but on this
    # board the default should need no override. If reads come back garbled,
    # diagnostics.sh sweeps the common rates and names the one that works.
    gps_baud: int = 38400

    # LoRaWAN OTAA credentials. Both are issued by the cloud when the device is
    # claimed and must match what is registered on the network server — a unit
    # whose app_key is still the all-zero sentinel has never been provisioned
    # and cannot join. app_eui was a hardcoded placeholder in identity.py; it
    # lives here so a TTN application can be pointed at without a firmware
    # release.
    app_key: str = "00000000000000000000000000000000"
    app_eui: str = "0000000000000000"

    # Cloud sync (HTTP/WiFi transport — coexists with LoRaWAN). Enable once the
    # device has an api_key; set via the service window /provision/cloud page.
    cloud_enabled: bool = False
    cloud_ingest_url: str = (
        "https://us-central1-waterquality-trading.cloudfunctions.net/ingestReading"
    )
    cloud_command_url: str = (
        "https://us-central1-waterquality-trading.cloudfunctions.net/deviceCommands"
    )
    # Base URL of the v2 device API (heartbeat, events, config, OTA poll/report).
    cloud_api_base: str = "https://us-central1-waterquality-trading.cloudfunctions.net/app"
    command_poll_s: int = 5  # how often to poll for relay commands over HTTP
    api_key: str = ""  # device API key (X-API-Key), bound to this device id
    batch_size: int = 50
    sync_interval_s: int = 300
    max_retries: int = 3
    retry_delays: list[int] = field(default_factory=lambda: [5, 15, 30])

    # Telemetry
    heartbeat_s: int = 600

    # OTA agent
    ota_enabled: bool = True
    ota_poll_s: int = 900  # 15 min, jittered by the agent
    ota_max_bundle_bytes: int = 64 * 1024 * 1024
    ota_self_test_timeout_s: int = 600
    ota_keep_releases: int = 2

    # Storage
    db_path: str = "/var/lib/bluesignal/wqm1.db"
    log_path: str = "/var/log/bluesignal/wqm1.log"
    log_max_bytes: int = 10 * 1024 * 1024
    log_backup_count: int = 5
    db_max_rows: int = 100_000

    # Host board. "auto" sniffs /proc/device-tree/model; explicit ids pin it
    # (rpi-zero-2w, arduino-uno-q, arduino-ventuno-q, generic-linux). On
    # boards where Linux can't reach the headers (the Arduino Q family), the
    # firmware runs digital-first: RS485 probes + USB GPS + Wi-Fi sync.
    board: str = "auto"

    # Sensors
    orp_enabled: bool = False  # No ORP hardware on PCBA Fin_3; enable when connected

    # Which of the four core analog probes are PHYSICALLY FITTED.
    #
    # These used to be assumed present — health.py said so outright ("core
    # analog probes are always fitted") — so the firmware read them whatever
    # was attached. An unfitted channel is an open input, and an open input is
    # noise the conversion happily turns into a number: a bench unit with no
    # electrode published pH for nine hours and raised 38 threshold alerts.
    #
    # A declared probe is read. An undeclared one is not read at all, so it
    # cannot invent data. Same contract ORP, chlorine and the 5-in-1 already
    # have; the core four were the exception, and the exception was the bug.
    #
    # Defaults stay True so an existing unit that IS fitted keeps reporting
    # across an upgrade. Commissioning sets them honestly per site.
    ph_enabled: bool = True
    tds_enabled: bool = True
    turbidity_enabled: bool = True
    temperature_enabled: bool = True

    # RS485 (Modbus-RTU) sensors — Honde probes via the USB adapter.
    # Data over USB; the probes take 12V from the unit's rail. Each probe
    # ships at address 1: multi-drop requires re-addressing during setup
    # (the wizard walks through it one probe at a time).
    rs485_port: str = "/dev/ttyUSB0"
    rs485_chlorine_enabled: bool = False
    rs485_chlorine_addr: int = 1
    rs485_orp_enabled: bool = False  # digital ORP supersedes analog orp_enabled
    rs485_orp_addr: int = 2
    rs485_multi_enabled: bool = False  # 5-in-1: its pH/TDS/temp supersede analog
    rs485_multi_addr: int = 3

    # Smarter sensing (v2.1): adaptive sampling + sensor-health monitoring.
    adaptive_sampling_enabled: bool = False
    sensor_read_fast_s: int = 15
    adaptive_delta_threshold: float = 5.0  # % of sensor range per minute
    adaptive_hold_s: int = 600
    flatline_window_min: int = 20
    spike_z_threshold: float = 6.0
    drift_check_enabled: bool = True
    calibration_max_age_days: int = 90

    # Reliability
    hardware_watchdog_enabled: bool = False

    # Thermal
    fan_on_temp_c: float = 60.0
    fan_off_temp_c: float = 55.0

    # Automation rules
    rules: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Settings schema — the contract for validation and remote config.
#
# hot=True keys apply live (workers re-read them each cycle); hot=False keys
# need a service restart (paths, radio credentials, hardware toggles). Keys
# NOT in this schema can never arrive via remote config — notably api_key and
# the cloud URLs, which are provisioning-time-only so a compromised cloud
# account cannot redirect a device (mirrored server-side in
# functions/v2/deviceTelemetry.js CONFIG_SCHEMA).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SettingSpec:
    type: type
    hot: bool
    min: float | None = None
    max: float | None = None
    max_length: int | None = None
    remote: bool = True  # may this key arrive via cloud desired-config?


SETTINGS_SCHEMA: dict[str, SettingSpec] = {
    # Timing (hot — the workers consult settings every cycle)
    "sensor_read_s": SettingSpec(int, hot=True, min=5, max=3600),
    "lora_tx_s": SettingSpec(int, hot=True, min=60, max=86400),
    "gps_fix_s": SettingSpec(int, hot=True, min=GPS_FIX_MIN_S, max=GPS_FIX_MAX_S),
    "gps_fix_timeout_s": SettingSpec(int, hot=True, min=5, max=300),
    "command_poll_s": SettingSpec(int, hot=True, min=5, max=3600),
    "sync_interval_s": SettingSpec(int, hot=True, min=30, max=86400),
    "heartbeat_s": SettingSpec(int, hot=True, min=60, max=86400),
    "batch_size": SettingSpec(int, hot=True, min=1, max=200),
    "max_retries": SettingSpec(int, hot=True, min=1, max=10),
    # GPS / radio / credentials — restart-required, never remote
    "gps_baud": SettingSpec(int, hot=False, min=1200, max=921600, remote=False),
    "app_key": SettingSpec(str, hot=False, max_length=32, remote=False),
    "app_eui": SettingSpec(str, hot=False, max_length=16, remote=False),
    "cloud_enabled": SettingSpec(bool, hot=False, remote=False),
    "cloud_ingest_url": SettingSpec(str, hot=False, max_length=256, remote=False),
    "cloud_command_url": SettingSpec(str, hot=False, max_length=256, remote=False),
    "cloud_api_base": SettingSpec(str, hot=False, max_length=256, remote=False),
    "api_key": SettingSpec(str, hot=False, max_length=128, remote=False),
    # OTA
    "ota_enabled": SettingSpec(bool, hot=True),
    "ota_poll_s": SettingSpec(int, hot=True, min=300, max=86400),
    "ota_max_bundle_bytes": SettingSpec(int, hot=True, min=1_000_000, max=512 * 1024 * 1024),
    "ota_self_test_timeout_s": SettingSpec(int, hot=True, min=60, max=3600),
    "ota_keep_releases": SettingSpec(int, hot=True, min=1, max=10),
    # Storage — restart-required, never remote
    "db_path": SettingSpec(str, hot=False, max_length=256, remote=False),
    "log_path": SettingSpec(str, hot=False, max_length=256, remote=False),
    "log_max_bytes": SettingSpec(int, hot=False, min=1024, max=1024**3, remote=False),
    "log_backup_count": SettingSpec(int, hot=False, min=0, max=50, remote=False),
    "db_max_rows": SettingSpec(int, hot=True, min=1000, max=10_000_000),
    # Sensors
    "board": SettingSpec(str, hot=False, max_length=32, remote=False),
    "orp_enabled": SettingSpec(bool, hot=False),
    # Physical fitment — a restart is required because the sensor objects are
    # constructed at start-up, exactly like orp_enabled above.
    "ph_enabled": SettingSpec(bool, hot=False),
    "tds_enabled": SettingSpec(bool, hot=False),
    "turbidity_enabled": SettingSpec(bool, hot=False),
    "temperature_enabled": SettingSpec(bool, hot=False),
    "rs485_port": SettingSpec(str, hot=False, max_length=64, remote=False),
    "rs485_chlorine_enabled": SettingSpec(bool, hot=False),
    "rs485_chlorine_addr": SettingSpec(int, hot=False, min=1, max=247),
    "rs485_orp_enabled": SettingSpec(bool, hot=False),
    "rs485_orp_addr": SettingSpec(int, hot=False, min=1, max=247),
    "rs485_multi_enabled": SettingSpec(bool, hot=False),
    "rs485_multi_addr": SettingSpec(int, hot=False, min=1, max=247),
    # Smarter sensing (hot — safe to tune live)
    "adaptive_sampling_enabled": SettingSpec(bool, hot=True),
    "sensor_read_fast_s": SettingSpec(int, hot=True, min=5, max=600),
    "adaptive_delta_threshold": SettingSpec(float, hot=True, min=0.0, max=1000.0),
    "adaptive_hold_s": SettingSpec(int, hot=True, min=30, max=86400),
    "flatline_window_min": SettingSpec(int, hot=True, min=5, max=1440),
    "spike_z_threshold": SettingSpec(float, hot=True, min=1.0, max=20.0),
    "drift_check_enabled": SettingSpec(bool, hot=True),
    "calibration_max_age_days": SettingSpec(int, hot=True, min=1, max=3650),
    # Reliability
    "hardware_watchdog_enabled": SettingSpec(bool, hot=False, remote=False),
    # Thermal
    "fan_on_temp_c": SettingSpec(float, hot=True, min=30.0, max=90.0),
    "fan_off_temp_c": SettingSpec(float, hot=True, min=25.0, max=85.0),
}

_SETTINGS_FIELDS = {f.name for f in fields(Settings)}


def validate_values(
    values: dict[str, Any], remote: bool = False
) -> tuple[dict[str, Any], list[str]]:
    """
    Validate a {key: value} map against the schema.

    Returns (accepted, errors). With remote=True, keys marked remote=False in
    the schema are rejected outright (credentials/URLs can never be pushed).
    Type checks are strict except int-where-float-expected, which is widened.
    """
    accepted: dict[str, Any] = {}
    errors: list[str] = []
    for key, value in values.items():
        spec = SETTINGS_SCHEMA.get(key)
        if spec is None:
            errors.append(f"unknown key: {key}")
            continue
        if remote and not spec.remote:
            errors.append(f"key not remotely configurable: {key}")
            continue
        if spec.type is bool:
            if not isinstance(value, bool):
                errors.append(f"{key} must be a boolean")
                continue
        elif spec.type is int:
            if isinstance(value, bool) or not isinstance(value, int):
                errors.append(f"{key} must be an integer")
                continue
            if (spec.min is not None and value < spec.min) or (
                spec.max is not None and value > spec.max
            ):
                errors.append(f"{key} must be in [{spec.min}, {spec.max}]")
                continue
        elif spec.type is float:
            if isinstance(value, bool) or not isinstance(value, int | float):
                errors.append(f"{key} must be a number")
                continue
            value = float(value)
            if (spec.min is not None and value < spec.min) or (
                spec.max is not None and value > spec.max
            ):
                errors.append(f"{key} must be in [{spec.min}, {spec.max}]")
                continue
        elif spec.type is str:
            if not isinstance(value, str):
                errors.append(f"{key} must be a string")
                continue
            if spec.max_length is not None and len(value) > spec.max_length:
                errors.append(f"{key} must be at most {spec.max_length} chars")
                continue
        accepted[key] = value
    return accepted, errors


def hot_keys(values: dict[str, Any]) -> set[str]:
    """Subset of the given keys that apply without a restart."""
    return {k for k in values if k in SETTINGS_SCHEMA and SETTINGS_SCHEMA[k].hot}


def restart_keys(values: dict[str, Any]) -> set[str]:
    """Subset of the given keys that require a service restart to apply."""
    return {k for k in values if k in SETTINGS_SCHEMA and not SETTINGS_SCHEMA[k].hot}


class ConfigManager:
    """
    Layered, reloadable settings.

    Layers (later wins): Settings defaults <- base config.yaml <- remote
    overlay (config.d/remote.yaml, written by the cloud worker after
    validation). ``reload()`` rebuilds the Settings object in place-of the old
    one; consumers that keep a reference to the manager see updates via
    ``settings`` after reload, and the applied remote config version is
    reported in every heartbeat.
    """

    def __init__(
        self,
        base_path: str | None = None,
        remote_path: str | None = None,
    ) -> None:
        self._base_path = Path(base_path or _DEFAULT_CONFIG_PATH)
        self._remote_path = Path(remote_path or _REMOTE_OVERLAY_PATH)
        self._lock = threading.Lock()
        self._settings = Settings()
        self._remote_version: int | None = None
        self.reload()

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def remote_version(self) -> int | None:
        """Version of the applied remote overlay (None = never configured)."""
        return self._remote_version

    def reload(self) -> Settings:
        """Rebuild settings from all layers. Never raises; on any layer's
        failure that layer is skipped (a bad file can't take the device down)."""
        with self._lock:
            s = Settings()
            self._apply_file(s, self._base_path, remote=False)
            self._remote_version = self._apply_remote_overlay(s)
            self._settings = s
            return s

    def _apply_file(self, s: Settings, path: Path, remote: bool) -> None:
        if not path.exists():
            logger.info("No config at %s, using defaults", path)
            return
        try:
            with path.open() as f:
                raw = yaml.safe_load(f) or {}
        except Exception as e:
            logger.warning("Failed to load config from %s: %s", path, e)
            return
        if not isinstance(raw, dict):
            logger.warning("Config at %s is not a mapping — ignored", path)
            return
        # rules is free-form (validated by the RulesEngine), everything else
        # goes through the schema. Unknown-but-dataclass keys are accepted from
        # the LOCAL base file for backward compatibility, with a warning.
        values = dict(raw)
        rules = values.pop("rules", None)
        values.pop("service_window", None)  # read by the Flask app, not us
        accepted, errors = validate_values(values, remote=remote)
        for err in errors:
            # Local files may carry legacy keys (e.g. api_endpoint) — tolerate
            # with a warning so an old config never blocks boot.
            if not remote and err.startswith("unknown key:"):
                key = err.split(": ", 1)[1]
                if key in _SETTINGS_FIELDS:
                    accepted[key] = values[key]
                    continue
                logger.warning("Config %s: ignoring %s", path, err)
            else:
                logger.warning("Config %s: %s", path, err)
        for key, val in accepted.items():
            setattr(s, key, val)
        if isinstance(rules, list):
            s.rules = rules
        logger.info("Config loaded from %s", path)

    def _apply_remote_overlay(self, s: Settings) -> int | None:
        """Apply the persisted remote overlay; returns its version or None."""
        path = self._remote_path
        if not path.exists():
            return None
        try:
            with path.open() as f:
                doc = yaml.safe_load(f) or {}
            version = doc.get("version")
            values = doc.get("values") or {}
            accepted, errors = validate_values(values, remote=True)
            for err in errors:
                logger.warning("Remote overlay %s: %s", path, err)
            for key, val in accepted.items():
                setattr(s, key, val)
            logger.info("Remote config overlay v%s applied from %s", version, path)
            return version if isinstance(version, int) else None
        except Exception as e:
            logger.warning("Failed to apply remote overlay %s: %s", path, e)
            return None

    def apply_remote_config(self, version: int, values: dict[str, Any]) -> tuple[bool, list[str]]:
        """
        Validate + persist a cloud-desired config, then reload.

        Returns (ok, errors). On validation failure NOTHING is persisted or
        applied and the previous overlay stays in force (last-known-good).
        The caller reports config_applied / config_rejected to the cloud.
        """
        accepted, errors = validate_values(values, remote=True)
        if errors:
            return False, errors
        doc = {"version": version, "values": accepted}
        try:
            self._remote_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._remote_path.with_suffix(".tmp")
            with tmp.open("w") as f:
                yaml.safe_dump(doc, f)
            tmp.replace(self._remote_path)
        except Exception as e:
            return False, [f"persist failed: {e}"]
        self.reload()
        return True, []


_manager: ConfigManager | None = None


def get_config_manager(
    config_path: str | None = None, remote_path: str | None = None
) -> ConfigManager:
    """Process-wide ConfigManager singleton."""
    global _manager
    if _manager is None:
        _manager = ConfigManager(config_path, remote_path)
    return _manager


def get_settings(config_path: str | None = None) -> Settings:
    """Load or return cached settings (compatibility shim over ConfigManager)."""
    return get_config_manager(config_path).settings


def atomic_json_write(path: str, data: dict[str, Any]) -> None:
    """Write JSON file atomically (write to .tmp then rename)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    try:
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2, default=str)
        tmp.replace(p)
    except Exception as e:
        logger.error("Atomic write to %s failed: %s", path, e)
        if tmp.exists():
            tmp.unlink()
        raise
