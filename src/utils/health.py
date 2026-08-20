"""
Health Telemetry Reporter

v1 reported batteryLevel/signalStrength/lastSeen per reading (and in practice
never left the device). v2 builds the full heartbeat payload for
POST /v2/devices/:id/heartbeat: uptime, buffer depth, disk/memory, CPU
temperature, link quality, error counters, firmware + config versions.
Every field is best-effort — a heartbeat must still go out when a source
fails (that failure IS the telemetry).

── No battery level, deliberately (2026-08-20) ──────────────────────────────
There was a `get_battery_level()` here that turned a pack voltage into a
percentage on a linear 21V=0% / 28V=100% curve. It has been removed, for two
independent reasons:

1. **Nothing ever fed it.** `update_battery()` had no callers anywhere and
   `workers.py` hardcoded `battery_v: None`, so it returned None for the whole
   life of the field. There is no ADC input free for a divider either — all
   four ADS1115 channels are allocated (AIN0 TDS, AIN1 turbidity, AIN2 pH,
   AIN3 tied to PH_INN), so wiring it is a board revision, not a patch.
2. **The maths was wrong anyway.** The packs are dumb 24V NMC on two wires,
   no comms. NMC's open-circuit curve is flat through the middle — roughly
   20–80% state of charge lives inside 25.2–27.3V on a 7S pack — so a linear
   interpolation reads 60–90% across most of the real range and then falls off
   a cliff. Under load it sags by I×R on top of that.

If battery reporting comes back on a hardware revision that can measure it,
report **volts** plus a coarse state (charging / discharging / low / critical).
Voltage is honest at the ends of the curve, which is where it matters. Never a
percentage.
"""

import contextlib
import logging
import shutil
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("wqm1.health")

_PROC_MEMINFO = "/proc/meminfo"
_THERMAL_ZONE = "/sys/class/thermal/thermal_zone0/temp"
_PROC_WIRELESS = "/proc/net/wireless"


def read_cpu_temp_c() -> float | None:
    """SoC temperature in °C (thermal_zone0), None off-Pi."""
    try:
        with open(_THERMAL_ZONE) as f:
            return int(f.read().strip()) / 1000.0
    except (OSError, ValueError):
        return None


def read_mem_free_bytes() -> int | None:
    """MemAvailable from /proc/meminfo."""
    try:
        with open(_PROC_MEMINFO) as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def read_wifi_rssi_dbm() -> int | None:
    """Signal level of the first wireless interface from /proc/net/wireless."""
    try:
        with open(_PROC_WIRELESS) as f:
            lines = f.readlines()
        for line in lines[2:]:  # first two lines are headers
            parts = line.split()
            if len(parts) >= 4:
                return int(float(parts[3].rstrip(".")))
    except (OSError, ValueError, IndexError):
        pass
    return None


class HealthReporter:
    """Collects device health telemetry and builds heartbeat payloads."""

    def __init__(
        self,
        firmware_version: str,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._firmware_version = firmware_version
        self._clock = clock
        self._started_at = clock()
        self._last_rssi: int | None = None
        self._last_seen: int = 0

    def update_rssi(self, rssi_dbm: int) -> None:
        """Update last known RSSI from LoRa radio."""
        self._last_rssi = rssi_dbm

    def update_last_seen(self) -> None:
        """Mark current time as last sensor reading."""
        self._last_seen = int(time.time() * 1000)

    def get_signal_strength(self) -> int | None:
        """Last LoRa RSSI in dBm, or None if the radio never reported one."""
        return self._last_rssi

    def uptime_s(self) -> int:
        return int(self._clock() - self._started_at)

    def get_report(self) -> dict[str, Any]:
        """Legacy per-reading health shape (kept for the service window)."""
        return {
            "signalStrength": self.get_signal_strength(),
            "lastSeen": self._last_seen,
            "firmwareVersion": self._firmware_version,
        }

    def build_heartbeat(
        self,
        db: Any = None,
        error_counts: dict[str, int] | None = None,
        sensor_health: dict[str, Any] | None = None,
        config_version: int | None = None,
        ota_phase: str | None = None,
        db_path: str | None = None,
    ) -> dict[str, Any]:
        """
        Full heartbeat payload for POST /v2/devices/:id/heartbeat.
        Every source is independently best-effort; a missing field is simply
        omitted (the endpoint treats all fields as optional).
        """
        hb: dict[str, Any] = {
            "uptimeS": self.uptime_s(),
            "firmwareVersion": self._firmware_version,
        }
        if self._last_rssi is not None:
            hb["loraRssi"] = self._last_rssi
        wifi = read_wifi_rssi_dbm()
        if wifi is not None:
            hb["wifiRssi"] = wifi
        cpu = read_cpu_temp_c()
        if cpu is not None:
            hb["cpuTempC"] = cpu
        mem = read_mem_free_bytes()
        if mem is not None:
            hb["memFreeBytes"] = mem
        if db_path:
            with contextlib.suppress(OSError):
                hb["diskFreeBytes"] = shutil.disk_usage(db_path).free
        if db is not None:
            try:
                hb["bufferDepth"] = db.get_state_count("pending")
                hb["bufferFailedRows"] = db.get_state_count("failed_permanent")
            except Exception as e:  # noqa: BLE001 — telemetry must not take down the caller
                logger.debug("buffer depth read failed: %s", e)
        if error_counts:
            hb["errorCounts"] = dict(error_counts)
        if sensor_health:
            hb["sensorHealth"] = sensor_health
        if config_version is not None:
            hb["configVersion"] = config_version
        if ota_phase:
            hb["otaPhase"] = ota_phase
        return hb
