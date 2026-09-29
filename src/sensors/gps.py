"""
MAX-M10S GPS Driver

NMEA sentence parser over UART for the u-blox MAX-M10S.

Parses GGA for the fix (position, quality, satellites, HDOP, altitude) and
RMC for the DATE + time. GGA carries only a time-of-day; for years this file
grafted that time onto whatever date the system clock happened to hold and
called it a timestamp — on a Pi with no RTC that is the 1970 date, or the
last fake-hwclock save, wearing a real second. ``GPSFix.timestamp`` is now
set only from an RMC sentence with a valid (``A``) status, and is ``None``
otherwise. ``utils.clock`` uses it to discipline the system clock when NTP is
absent (commissioning plan, PR 5).
"""

import logging
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime

import serial

from utils.config import GPS_BAUD, GPS_EXTINT, GPS_UART_PORT

logger = logging.getLogger("wqm1.gps")

# After a GGA fix arrives without an RMC in hand, keep reading this long for
# the RMC that carries the date. One NMEA burst is a second; 1.5 s covers a
# burst boundary without stretching a 10 s fix timeout noticeably.
_RMC_GRACE_S = 1.5

try:
    import RPi.GPIO as GPIO
except ImportError:  # non-Pi host (e.g. Arduino UNO Q): a USB GPS still works
    GPIO = None  # over pyserial — only the EXTINT power-cycle pin is absent


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

    def __init__(self, port: str = GPS_UART_PORT, baud: int = GPS_BAUD) -> None:
        self._port_name = port
        self._baud = baud
        self._serial = None
        self._last_fix: GPSFix | None = None
        # Most recent RMC date+time the receiver reported with a valid status
        # — what utils.clock disciplines the system clock from when NTP is
        # absent. None until the receiver has locked.
        self._last_rmc_time: datetime | None = None
        self._lock = threading.Lock()
        # Rate-limiting state for _explain_no_fix.
        self._last_no_fix_log = 0.0
        self._last_no_fix_detail = ""

        # Setup EXTINT pin for power cycling (direct-header boards only)
        if GPIO is not None:
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
            # Say so. This used to return silently, so a unit whose UART never
            # opened logged nothing at all — the only visible symptom was a
            # power cycle every gps_fix_s with no stated reason.
            self._explain_no_fix("UART is not open (check /dev/serial0 and dialout membership)")
            return self._last_fix

        deadline = time.monotonic() + timeout_s
        fix = None
        # Counted so a failure can say WHICH failure it was. Reading nothing,
        # reading noise, and reading good sentences that carry no fix are three
        # different faults with three different remedies, and they were
        # indistinguishable from the log.
        lines = 0
        bad_checksum = 0
        gga_seen = 0
        # The RMC date+time seen in this burst. The M10S emits RMC before GGA
        # in each one-second burst, so it is usually already in hand when the
        # fix arrives; if GGA came first we read on for up to _RMC_GRACE_S to
        # collect it rather than return a fix with no date.
        rmc_time: datetime | None = None
        rmc_wait_until: float | None = None

        # Flush stale data
        self._serial.reset_input_buffer()

        while time.monotonic() < deadline:
            if fix is not None and (
                rmc_time is not None or time.monotonic() >= (rmc_wait_until or 0)
            ):
                break
            try:
                line = self._serial.readline().decode("ascii", errors="ignore").strip()
            except Exception as e:
                logger.warning("GPS read error: %s", e)
                break

            if not line:
                continue
            lines += 1

            # Validate NMEA checksum
            if not _verify_checksum(line):
                bad_checksum += 1
                continue

            rmc = _parse_rmc(line)
            if rmc is not None:
                rmc_time = rmc
                continue

            if line.startswith(("$GPGGA", "$GNGGA")):
                gga_seen += 1

            if fix is None:
                parsed = _parse_gga(line)
                if parsed is not None:
                    fix = parsed
                    rmc_wait_until = time.monotonic() + _RMC_GRACE_S

        if fix is not None:
            fix.timestamp = rmc_time
            with self._lock:
                self._last_rmc_time = rmc_time or self._last_rmc_time

        if fix is None:
            if lines == 0:
                why = "no bytes on the UART at all"
            elif bad_checksum == lines:
                why = (
                    f"all {lines} line(s) failed checksum — this is what a baud "
                    f"mismatch looks like (gps_baud is {self._baud})"
                )
            elif gga_seen == 0:
                why = f"{lines} valid sentence(s) but no GGA among them"
            else:
                why = (
                    f"{gga_seen} GGA sentence(s) but none carried a fix "
                    "(quality 0 — the receiver is talking but has not locked)"
                )
            self._explain_no_fix(f"no fix in {timeout_s:.0f}s: {why}")

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

    def _explain_no_fix(self, detail: str) -> None:
        """Log why a fix attempt failed, at most once a minute.

        Rate-limited rather than silent: the attempt runs every gps_fix_s and
        an unconditional warning would be noise, but saying nothing at all is
        how a GPS that never worked went unnoticed for weeks.
        """
        now = time.monotonic()
        if detail != self._last_no_fix_detail or now - self._last_no_fix_log > 60.0:
            logger.warning("GPS: %s", detail)
            self._last_no_fix_log = now
            self._last_no_fix_detail = detail

    @property
    def last_time(self) -> datetime | None:
        """Most recent valid RMC UTC date+time, or None before lock."""
        with self._lock:
            return self._last_rmc_time

    @property
    def last_fix(self) -> GPSFix | None:
        with self._lock:
            return self._last_fix

    def power_cycle(self) -> None:
        """
        Pulse EXTINT HIGH for 200 ms to wake the receiver.
        u-blox EXTINT: a high level forces the receiver out of power-save; in
        continuous mode (the default) the pulse is harmless.
        """
        if GPIO is None:
            logger.info("GPS power cycle skipped — EXTINT pin not wired on this host")
            return
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
    if not sentence.startswith(("$GPGGA", "$GNGGA")):
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

        # GGA carries a time-of-day and NO date. It used to be grafted onto
        # datetime.now(UTC) here, which on a Pi with no RTC produced a real
        # second on a fictional day. The date comes from RMC (_parse_rmc);
        # get_fix attaches it. Until then the fix has no timestamp.
        return GPSFix(
            latitude=latitude,
            longitude=longitude,
            altitude=altitude,
            satellites=satellites,
            hdop=hdop,
            timestamp=None,
            fix_quality=fix_quality,
        )

    except (ValueError, IndexError):
        return None


def _parse_rmc(sentence: str) -> datetime | None:
    """
    Parse $GPRMC / $GNRMC for the UTC date + time.

    Format: $G?RMC,HHMMSS.ss,A,DDMM.mmm,N,DDDMM.mmm,W,SPD,COG,DDMMYY,...

    Returns an aware UTC datetime only when the status field is ``A``
    (valid) and both time and date are present — a ``V`` (void) sentence
    is the receiver saying its clock is not yet trustworthy, and a
    receiver's untrusted time is no better than the Pi's.
    """
    if not sentence.startswith(("$GPRMC", "$GNRMC")):
        return None
    body = sentence.split("*")[0]
    parts = body.split(",")
    if len(parts) < 10:
        return None
    if parts[2] != "A" or not parts[1] or not parts[9]:
        return None
    try:
        hh, mm = int(parts[1][:2]), int(parts[1][2:4])
        ss = int(float(parts[1][4:]))
        dd, mo = int(parts[9][:2]), int(parts[9][2:4])
        yy = int(parts[9][4:6])
        # NMEA years are two digits; the GPS week rollover means a receiver
        # cannot express a pre-2000 date anyway, so 20yy is the only reading.
        return datetime(2000 + yy, mo, dd, hh, mm, ss, tzinfo=UTC)
    except (ValueError, IndexError):
        return None
