"""
MAX-M10S GPS Driver

NMEA sentence parser over UART for the u-blox MAX-M10S.
Parses GGA (fix) and RMC (time/date) sentences.
"""

import logging
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime

import RPi.GPIO as GPIO
import serial
from utils.config import GPS_BAUD, GPS_EXTINT, GPS_UART_PORT

logger = logging.getLogger("wqm1.gps")


@dataclass
class GPSFix:
    """GPS fix data."""

    latitude: float
    longitude: float
    altitude: float | None = None
    satellites: int | None = None
    hdop: float | None = None
    timestamp: datetime | None = None
    fix_quality: int = 0  # 0=none, 1=GPS, 2=DGPS


class GPS:
    """MAX-M10S GPS receiver over UART with NMEA parsing."""

    def __init__(self, port: str = GPS_UART_PORT, baud: int = GPS_BAUD):
        self._port_name = port
        self._baud = baud
        self._serial = None
        self._last_fix: GPSFix | None = None
        self._lock = threading.Lock()

        # Setup EXTINT pin for power cycling
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        GPIO.setup(GPS_EXTINT, GPIO.OUT, initial=GPIO.LOW)

        try:
            self._serial = serial.Serial(
                port=port,
                baudrate=baud,
                timeout=1.0,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
            )
            logger.info("GPS UART opened: %s @ %d baud", port, baud)
        except Exception as e:
            logger.error("GPS UART open failed: %s", e)

    def get_fix(self, timeout_s: float = 10.0) -> GPSFix | None:
        """
        Attempt to get a GPS fix by reading NMEA sentences.

        Args:
            timeout_s: Maximum time to wait for a valid fix.

        Returns:
            GPSFix or None if no fix obtained within timeout.
        """
        if self._serial is None or not self._serial.is_open:
            return self._last_fix

        deadline = time.monotonic() + timeout_s
        fix = None

        # Flush stale data
        self._serial.reset_input_buffer()

        while time.monotonic() < deadline:
            try:
                line = self._serial.readline().decode("ascii", errors="ignore").strip()
            except Exception as e:
                logger.debug("GPS read error: %s", e)
                break

            if not line:
                continue

            # Validate NMEA checksum
            if not _verify_checksum(line):
                continue

            parsed = _parse_gga(line)
            if parsed is not None:
                fix = parsed
                break

        if fix is not None:
            with self._lock:
                self._last_fix = fix
            logger.info(
                "GPS fix: %.6f, %.6f alt=%.1fm sats=%s",
                fix.latitude,
                fix.longitude,
                fix.altitude or 0,
                fix.satellites,
            )
        return fix

    @property
    def last_fix(self) -> GPSFix | None:
        with self._lock:
            return self._last_fix

    def power_cycle(self) -> None:
        """
        Pulse EXTINT to reset/wake the GPS module.
        u-blox EXTINT: pulse low for >100 ms to toggle power save.
        """
        logger.info("GPS power cycle via EXTINT")
        GPIO.output(GPS_EXTINT, GPIO.HIGH)
        time.sleep(0.2)
        GPIO.output(GPS_EXTINT, GPIO.LOW)
        time.sleep(1.0)

    def close(self) -> None:
        """Close UART port."""
        if self._serial and self._serial.is_open:
            self._serial.close()
            logger.info("GPS UART closed")


# ---------------------------------------------------------------------------
# NMEA parsing helpers
# ---------------------------------------------------------------------------


def _verify_checksum(sentence: str) -> bool:
    """Verify NMEA sentence checksum (*XX at end)."""
    if not sentence.startswith("$") or "*" not in sentence:
        return False
    body, _, chk = sentence[1:].partition("*")
    try:
        expected = int(chk, 16)
    except ValueError:
        return False
    computed = 0
    for c in body:
        computed ^= ord(c)
    return computed == expected


def _parse_gga(sentence: str) -> GPSFix | None:
    """
    Parse $GPGGA or $GNGGA sentence.

    Format: $G?GGA,HHMMSS.ss,DDMM.mmm,N/S,DDDMM.mmm,E/W,Q,SS,HDOP,ALT,M,...
    """
    if not (sentence.startswith("$GPGGA") or sentence.startswith("$GNGGA")):
        return None

    # Strip checksum for splitting
    body = sentence.split("*")[0]
    parts = body.split(",")
    if len(parts) < 10:
        return None

    try:
        fix_quality = int(parts[6]) if parts[6] else 0
        if fix_quality == 0:
            return None

        # Latitude: DDMM.mmm
        lat_raw, lat_dir = parts[2], parts[3]
        if not lat_raw or not lat_dir:
            return None
        lat_deg = float(lat_raw[:2])
        lat_min = float(lat_raw[2:])
        latitude = lat_deg + lat_min / 60.0
        if lat_dir == "S":
            latitude = -latitude

        # Longitude: DDDMM.mmm
        lon_raw, lon_dir = parts[4], parts[5]
        if not lon_raw or not lon_dir:
            return None
        lon_deg = float(lon_raw[:3])
        lon_min = float(lon_raw[3:])
        longitude = lon_deg + lon_min / 60.0
        if lon_dir == "W":
            longitude = -longitude

        satellites = int(parts[7]) if parts[7] else None
        hdop = float(parts[8]) if parts[8] else None
        altitude = float(parts[9]) if parts[9] else None

        # Parse time (HHMMSS.ss)
        timestamp = None
        if parts[1]:
            try:
                h = int(parts[1][:2])
                m = int(parts[1][2:4])
                s = int(float(parts[1][4:]))
                now = datetime.now(UTC)
                timestamp = now.replace(hour=h, minute=m, second=s, microsecond=0)
            except (ValueError, IndexError):
                pass

        return GPSFix(
            latitude=latitude,
            longitude=longitude,
            altitude=altitude,
            satellites=satellites,
            hdop=hdop,
            timestamp=timestamp,
            fix_quality=fix_quality,
        )

    except (ValueError, IndexError):
        return None
