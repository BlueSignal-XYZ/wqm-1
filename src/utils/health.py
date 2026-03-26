"""
Health Telemetry Reporter

Reports device health metrics matching the platform contract:
batteryLevel, signalStrength, lastSeen, firmwareVersion.
"""

import logging
import time

logger = logging.getLogger("wqm1.health")


class HealthReporter:
    """Collects and reports device health telemetry."""

    def __init__(self, firmware_version: str = "1.0.0"):
        self._firmware_version = firmware_version
        self._last_rssi: int | None = None
        self._last_seen: int = 0
        self._battery_v: float | None = None

    def update_rssi(self, rssi_dbm: int) -> None:
        """Update last known RSSI from LoRa radio."""
        self._last_rssi = rssi_dbm

    def update_last_seen(self) -> None:
        """Mark current time as last sensor reading."""
        self._last_seen = int(time.time() * 1000)

    def update_battery(self, voltage: float) -> None:
        """Update battery voltage reading."""
        self._battery_v = voltage

    def get_battery_level(self) -> int:
        """
        Convert battery voltage to percentage.

        24V system: 21V = 0%, 28V = 100% (linear approximation).
        Returns 0-100 clamped.
        """
        if self._battery_v is None:
            return 0
        pct = (self._battery_v - 21.0) / (28.0 - 21.0) * 100.0
        return max(0, min(100, int(pct)))

    def get_signal_strength(self) -> int:
        """Return last RSSI in dBm. Returns -120 if unknown."""
        return self._last_rssi if self._last_rssi is not None else -120

    def get_report(self) -> dict:
        """
        Build health telemetry report matching platform contract.

        Returns:
            {"batteryLevel": int, "signalStrength": int,
             "lastSeen": int (unix ms), "firmwareVersion": str}
        """
        return {
            "batteryLevel": self.get_battery_level(),
            "signalStrength": self.get_signal_strength(),
            "lastSeen": self._last_seen,
            "firmwareVersion": self._firmware_version,
        }
