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
GPS_BAUD = 9600
GPS_EXTINT = 19

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
    gps_fix_s: int = 600
    gps_fix_timeout_s: int = 60

    # GPS
    # u-blox modules vary: NEO-6/7/8 default to 9600, NEO-M9N defaults to
    # 38400, some custom-flashed boards ship at 115200. If GPS reads return
    # garbled bytes, try the alternative bauds.
    gps_baud: int = 9600

    # LoRaWAN
    app_key: str = "00000000000000000000000000000000"

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
    "gps_fix_s": SettingSpec(int, hot=True, min=60, max=86400),
    "gps_fix_timeout_s": SettingSpec(int, hot=True, min=5, max=300),
    "command_poll_s": SettingSpec(int, hot=True, min=5, max=3600),
    "sync_interval_s": SettingSpec(int, hot=True, min=30, max=86400),
    "heartbeat_s": SettingSpec(int, hot=True, min=60, max=86400),
    "batch_size": SettingSpec(int, hot=True, min=1, max=200),
    "max_retries": SettingSpec(int, hot=True, min=1, max=10),
    # GPS / radio / credentials — restart-required, never remote
    "gps_baud": SettingSpec(int, hot=False, min=1200, max=921600, remote=False),
    "app_key": SettingSpec(str, hot=False, max_length=32, remote=False),
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


# ===========================================================================
# Relay channel control config (Commercial tier)
# ===========================================================================
#
# THE ONE PHYSICAL FACT THIS SECTION IS BUILT ON:
#
#   A dead Pi cannot energise a coil.
#
# The only state a crashed, hung, or unpowered device can hold is
# DE-ENERGISED. So "fail-safe" always means de-energised, and the contact type
# decides what that does physically:
#
#   contact=NC  ->  de-energised = load RUNNING
#   contact=NO  ->  de-energised = load STOPPED
#
# fail_safe_state and contact are therefore NOT independent. A channel asking
# for fail_safe_state=run on an NO contact is asking for a fail-safe that
# cannot happen, and is rejected.
#
# These channel settings deliberately live in policies.yaml, NOT in Settings /
# SETTINGS_SCHEMA. Remote config can only carry SETTINGS_SCHEMA keys, so a
# compromised cloud account can never re-declare a channel's contact type,
# raise its current limit, or mark it commissioned.
# ---------------------------------------------------------------------------

# Omron G5Q-14-DC24 contact ratings on the Fin_3 board.
RELAY_MAX_CURRENT_A_NO = 5.0  # 5 A @ 30 VDC through the normally-open contact
RELAY_MAX_CURRENT_A_NC = 3.0  # 3 A through the normally-closed contact
RELAY_MAX_VOLTAGE_DC = 30.0

CONTACT_NO = "NO"
CONTACT_NC = "NC"
CONTACTS = (CONTACT_NO, CONTACT_NC)

FAIL_SAFE_RUN = "run"
FAIL_SAFE_STOP = "stop"
FAIL_SAFE_STATES = (FAIL_SAFE_RUN, FAIL_SAFE_STOP)

# Roles whose loss kills livestock. Called out by name so the operator gets a
# specific error, but the general invariant below catches every other role too.
LIFE_CRITICAL_ROLES = ("aeration", "circulation")
CHANNEL_ROLES = (
    "aeration",
    "circulation",
    "dosing",
    "valve",
    "heater",
    "chiller",
    "lighting",
    "auxiliary",
)

# These outputs are PILOT DUTY. They switch the coil of a contactor, a 24 V
# solenoid, or a VFD enable input — they never carry a motor or a line-voltage
# load directly.
LOAD_TYPES = ("contactor_coil", "solenoid_24v", "vfd_enable", "pilot_relay")

# Transition causes recorded in the audit trail.
CAUSE_SETPOINT = "setpoint"
CAUSE_MANUAL = "manual"
CAUSE_STALENESS = "staleness"
CAUSE_WATCHDOG = "watchdog"
CAUSE_COMMISSIONING_TEST = "commissioning_test"
CAUSE_OTA = "ota"
TRANSITION_CAUSES = (
    CAUSE_SETPOINT,
    CAUSE_MANUAL,
    CAUSE_STALENESS,
    CAUSE_WATCHDOG,
    CAUSE_COMMISSIONING_TEST,
    CAUSE_OTA,
)


@dataclass
class ChannelConfig:
    """Per-channel control configuration. Inert until commissioned."""

    channel: int
    role: str = "auxiliary"
    contact: str = CONTACT_NO
    fail_safe_state: str = FAIL_SAFE_STOP
    load_type: str = "contactor_coil"
    expected_current_a: float = 0.0

    # Anti-chatter. These protect contactor coils and pump motors; they are
    # ALWAYS bypassed for a fail-safe reversion.
    deadband: float = 0.0
    min_on_s: int = 0
    min_off_s: int = 0
    min_interval_s: int = 0

    # Consecutive 60 s cycles without a valid driving reading before the
    # channel reverts to fail_safe_state.
    stale_cycles: int = 3

    # Set only by the commissioning wizard's test-fire step. A channel that has
    # not been commissioned never actuates.
    commissioned: bool = False

    @property
    def failsafe_is_energised(self) -> bool:
        """
        Always False. Fail-safe is de-energised, by construction.

        Kept explicit so any future refactor that tries to make a fail-safe
        state require energising a coil has to delete this and confront why.
        """
        return False


def _limit_for_contact(contact: str) -> float:
    return RELAY_MAX_CURRENT_A_NC if contact == CONTACT_NC else RELAY_MAX_CURRENT_A_NO


def validate_channel_config(raw: dict[str, Any]) -> tuple[ChannelConfig | None, list[str]]:
    """
    Validate one channel's config.

    Returns (config, errors). config is None when errors is non-empty — an
    invalid channel is never partially applied.
    """
    errors: list[str] = []

    try:
        channel = int(raw.get("channel", 0))
    except (TypeError, ValueError):
        return None, ["channel must be an integer 1-4"]
    if not 1 <= channel <= 4:
        return None, [f"channel must be 1-4, got {channel}"]

    role = str(raw.get("role", "auxiliary"))
    contact = str(raw.get("contact", CONTACT_NO)).upper()
    fail_safe_state = str(raw.get("fail_safe_state", FAIL_SAFE_STOP)).lower()
    load_type = str(raw.get("load_type", "contactor_coil"))

    if role not in CHANNEL_ROLES:
        errors.append(
            f"CH{channel}: unknown role '{role}' (expected one of {', '.join(CHANNEL_ROLES)})"
        )
    if contact not in CONTACTS:
        errors.append(f"CH{channel}: contact must be NO or NC, got '{contact}'")
    if fail_safe_state not in FAIL_SAFE_STATES:
        errors.append(f"CH{channel}: fail_safe_state must be run or stop, got '{fail_safe_state}'")
    if load_type not in LOAD_TYPES:
        errors.append(
            f"CH{channel}: unknown load_type '{load_type}'. These outputs are pilot duty — "
            f"expected one of {', '.join(LOAD_TYPES)}. They drive contactor coils, 24 V "
            f"solenoids, or VFD enable inputs, never a line-voltage load directly."
        )

    # --- The core invariant ------------------------------------------------
    # A dead Pi cannot energise a coil, so fail_safe_state=run is only
    # achievable on an NC contact.
    if fail_safe_state == FAIL_SAFE_RUN and contact == CONTACT_NO:
        if role in LIFE_CRITICAL_ROLES:
            errors.append(
                f"CH{channel}: role '{role}' is life-critical and must be wired NC. "
                f"On an NO contact, loss of power or a crashed process de-energises the "
                f"coil and STOPS the load. Rewire to NC, or the fail-safe cannot hold."
            )
        else:
            errors.append(
                f"CH{channel}: fail_safe_state=run requires contact=NC. A de-energised NO "
                f"contact is open, so the load stops — the requested fail-safe is "
                f"physically unachievable on a crashed or unpowered device."
            )

    # A life-critical load declared fail-safe=stop is legal but almost never
    # intended, so it is called out loudly rather than silently accepted.
    if role in LIFE_CRITICAL_ROLES and fail_safe_state == FAIL_SAFE_STOP:
        logger.warning(
            "CH%d: role '%s' is life-critical but fail_safe_state=stop — the load will "
            "STOP on power loss or crash. Confirm this is intended.",
            channel,
            role,
        )

    # --- Pilot-duty current ------------------------------------------------
    try:
        current = float(raw.get("expected_current_a", 0.0))
    except (TypeError, ValueError):
        current = -1.0
        errors.append(f"CH{channel}: expected_current_a must be a number")

    if current < 0:
        if current == -1.0 and any("expected_current_a" in e for e in errors):
            pass
        else:
            errors.append(f"CH{channel}: expected_current_a must be >= 0")
    elif contact in CONTACTS:
        limit = _limit_for_contact(contact)
        if current > limit:
            errors.append(
                f"CH{channel}: expected_current_a {current:.2f} A exceeds the "
                f"{limit:.1f} A limit for a {contact} contact "
                f"(Omron G5Q-14-DC24: {RELAY_MAX_CURRENT_A_NO:.0f} A @ "
                f"{RELAY_MAX_VOLTAGE_DC:.0f} VDC on NO, {RELAY_MAX_CURRENT_A_NC:.0f} A on NC). "
                f"These are pilot-duty outputs — switch a contactor coil, not the load."
            )

    def _non_negative(key: str, default: Any) -> Any:
        try:
            value = type(default)(raw.get(key, default))
        except (TypeError, ValueError):
            errors.append(f"CH{channel}: {key} must be a number")
            return default
        if value < 0:
            errors.append(f"CH{channel}: {key} must be >= 0, got {value}")
            return default
        return value

    deadband = _non_negative("deadband", 0.0)
    min_on_s = _non_negative("min_on_s", 0)
    min_off_s = _non_negative("min_off_s", 0)
    min_interval_s = _non_negative("min_interval_s", 0)

    try:
        stale_cycles = int(raw.get("stale_cycles", 3))
    except (TypeError, ValueError):
        stale_cycles = 3
        errors.append(f"CH{channel}: stale_cycles must be an integer")
    if stale_cycles < 1:
        errors.append(f"CH{channel}: stale_cycles must be >= 1, got {stale_cycles}")

    if errors:
        return None, errors

    return (
        ChannelConfig(
            channel=channel,
            role=role,
            contact=contact,
            fail_safe_state=fail_safe_state,
            load_type=load_type,
            expected_current_a=current,
            deadband=deadband,
            min_on_s=min_on_s,
            min_off_s=min_off_s,
            min_interval_s=min_interval_s,
            stale_cycles=stale_cycles,
            commissioned=bool(raw.get("commissioned", False)),
        ),
        [],
    )


def load_channel_configs(policies: dict[str, Any]) -> tuple[dict[int, ChannelConfig], list[str]]:
    """
    Build {channel: ChannelConfig} from a policies.yaml dict.

    Invalid channels are dropped, never partially applied — a channel we cannot
    validate stays inert rather than actuating on a half-understood config.
    """
    configs: dict[int, ChannelConfig] = {}
    all_errors: list[str] = []
    for raw in policies.get("channels", []) or []:
        if not isinstance(raw, dict):
            all_errors.append(f"channel entry must be a mapping, got {type(raw).__name__}")
            continue
        cfg, errors = validate_channel_config(raw)
        if errors:
            all_errors.extend(errors)
            for e in errors:
                logger.error("Channel config rejected — %s", e)
            continue
        if cfg is not None:
            configs[cfg.channel] = cfg
    return configs, all_errors
