"""
WQM-1 Configuration

Hardware constants (from schematic, immutable) and runtime settings
(from YAML config file, mutable). Merges the previous hardware.py
and settings.py into a single module.
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

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

# ADC channel assignments
ADC_CH_PH = 0
ADC_CH_TDS = 1
ADC_CH_TURBIDITY = 2
ADC_CH_ORP = 3

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


@dataclass
class Settings:
    """Runtime configuration loaded from YAML."""

    # Timing
    sensor_read_s: int = 60
    lora_tx_s: int = 300
    gps_fix_s: int = 600
    gps_fix_timeout_s: int = 60

    # LoRaWAN
    app_key: str = "00000000000000000000000000000000"

    # Cloud sync
    api_endpoint: str = "https://your-cloud-endpoint.example.com/api/v1/readings"
    api_key: str = ""
    batch_size: int = 50
    sync_interval_s: int = 300
    max_retries: int = 3
    retry_delays: list[int] = field(default_factory=lambda: [5, 15, 30])

    # Storage
    db_path: str = "/var/lib/bluesignal/wqm1.db"
    log_path: str = "/var/log/bluesignal/wqm1.log"
    log_max_bytes: int = 10 * 1024 * 1024
    log_backup_count: int = 5
    db_max_rows: int = 100_000

    # Thermal
    fan_on_temp_c: float = 60.0
    fan_off_temp_c: float = 55.0

    # Automation rules
    rules: list[dict] = field(default_factory=list)

    # Version
    firmware_version: str = "1.0.0"


_settings: Settings | None = None


def get_settings(config_path: str | None = None) -> Settings:
    """Load or return cached settings."""
    global _settings
    if _settings is None:
        _settings = _load_settings(config_path or _DEFAULT_CONFIG_PATH)
    return _settings


def _load_settings(path: str) -> Settings:
    """Load settings from YAML file, falling back to defaults."""
    s = Settings()
    p = Path(path)
    if not p.exists():
        logger.info("No config at %s, using defaults", path)
        return s

    try:
        with open(p) as f:
            raw = yaml.safe_load(f) or {}
        for key, val in raw.items():
            if hasattr(s, key):
                setattr(s, key, val)
        logger.info("Config loaded from %s", path)
    except Exception as e:
        logger.warning("Failed to load config from %s: %s", path, e)

    return s


def atomic_json_write(path: str, data: dict) -> None:
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
