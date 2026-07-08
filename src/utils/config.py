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

    # Sensors
    orp_enabled: bool = False  # No ORP hardware on PCBA Fin_3; enable when connected

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
    "orp_enabled": SettingSpec(bool, hot=False),
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
