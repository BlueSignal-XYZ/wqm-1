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

import serial

from utils.config import GPS_BAUD, GPS_EXTINT, GPS_UART_PORT

logger = logging.getLogger("wqm1.gps")

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
        self._lock = threading.Lock()
        # What we BELIEVE the EXTINT toggle last did. Never trusted on its own
        # — see the power-save block below for how the loop is closed.
        self._power_saving = False
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

        # Flush stale data
        self._serial.reset_input_buffer()

        while time.monotonic() < deadline:
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

            if line.startswith(("$GPGGA", "$GNGGA")):
                gga_seen += 1

            parsed = _parse_gga(line)
            if parsed is not None:
                fix = parsed
                break

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
    def last_fix(self) -> GPSFix | None:
        with self._lock:
            return self._last_fix

    def power_cycle(self) -> None:
        """
        Pulse EXTINT to reset/wake the GPS module.
        u-blox EXTINT: pulse low for >100 ms to toggle power save.
        """
        if GPIO is None:
            logger.info("GPS power cycle skipped — EXTINT pin not wired on this host")
            return
        logger.info("GPS power cycle via EXTINT")
        GPIO.output(GPS_EXTINT, GPIO.HIGH)
        time.sleep(0.2)
        GPIO.output(GPS_EXTINT, GPIO.LOW)
        time.sleep(1.0)

    # ── Power save between daily fixes ──────────────────────────────────────
    #
    # EXTINT is a TOGGLE with no readback: the pulse flips power-save on or
    # off and the module never tells us which state it landed in. Open-loop
    # control over hardware that cannot be interrogated is precisely how a
    # `Fan ON` line came to mean "a GPIO is high" on a unit with no fan, so
    # this does not trust its own bookkeeping.
    #
    # Instead `sleep()` records what it BELIEVES, `wake()` acts on that belief,
    # and `GpsWorker` closes the loop through the only observable that matters:
    # whether NMEA actually arrives. A failed attempt pulses again and retries,
    # so a desynchronised toggle self-corrects within one cycle instead of
    # costing every fix from then on, silently.
    #
    # NOT MEASURED: whether this materially lowers pack draw. A u-blox part
    # typically falls from ~25-40 mA acquiring to microamps in backup, which
    # on a 3.3 V rail is on the order of 1 Wh/day — real, but small next to a
    # panel that is undersized. Confirm it on a bench meter before anybody
    # counts it in an energy budget.

    def sleep(self) -> None:
        """Ask the module to enter power save until the next fix is due."""
        if GPIO is None or self._power_saving:
            return
        self._toggle_extint()
        self._power_saving = True
        logger.info("GPS asked to enter power save until the next fix is due")

    def wake(self) -> None:
        """Bring the module out of power save. Safe to call when already awake."""
        if GPIO is None or not self._power_saving:
            return
        self._toggle_extint()
        self._power_saving = False
        logger.info("GPS woken from power save")

    def resync(self) -> None:
        """Pulse once and invert the belief, after an attempt found no NMEA.

        This is the self-correcting half: if `wake()` actually put the module
        to sleep because the two sides had drifted, one more pulse restores it.
        """
        if GPIO is None:
            return
        self._toggle_extint()
        self._power_saving = not self._power_saving
        logger.warning(
            "GPS produced nothing; pulsed EXTINT again (now believed %s)",
            "asleep" if self._power_saving else "awake",
        )

    def _toggle_extint(self) -> None:
        GPIO.output(GPS_EXTINT, GPIO.HIGH)
        time.sleep(0.2)
        GPIO.output(GPS_EXTINT, GPIO.LOW)
        time.sleep(0.5)

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
