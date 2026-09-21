"""
Clock discipline and clock confidence (commissioning plan, PR 5).

Time is settlement evidence, not housekeeping. The QC accrual on the cloud
walks positive totalizer deltas between consecutive samples and holds any
delta whose implied gal/day exceeds the nameplate, judged over the interval
between the two timestamps. A Pi with no RTC that boots to 1970, or jumps
forward the moment a link returns, produces intervals that are fiction — a
spurious hold or a bad accrual on a real statement.

Three sources, one answer per reading:

* ``ntp``      — systemd-timesyncd reports the clock synchronised. The ordinary
                 case on a Wi-Fi or LTE unit; ``setup.sh`` enables timesyncd.
* ``gps``      — no NTP, but the GPS has delivered an RMC date+time and the
                 system clock agrees with it (or was just set from it). The
                 only clock a dark LoRa-only unit has.
* ``unsynced`` — neither. The reading is still stored and still uploaded;
                 it simply wears its true confidence, and the cloud may treat
                 its interval accordingly.

The firmware runs as an unprivileged user, so setting the clock goes through
``sudo -n date -u -s`` under the one sudoers line ``setup.sh`` installs for
exactly that command. Every correction is logged with its offset — a clock
that moved is a fact the settlement audit may need.
"""

from __future__ import annotations

import logging
import shutil
import subprocess  # nosec B404 - fixed argv, no shell, resolved binaries only
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger("wqm1.clock")

# Any system time before this is impossible for this firmware to be running
# at: a Pi with no RTC and no battery boots to the epoch (or to the last
# fake-hwclock save), and a reading stamped before the firmware existed is
# fiction. Bump when a release is cut; being a few months stale only widens
# the "plausible" window, never narrows it.
EARLIEST_PLAUSIBLE_UTC = datetime(2026, 9, 1, tzinfo=UTC)

# Beyond this disagreement between the system clock and a GPS RMC time, the
# system clock is wrong (a GPS second is authoritative to the microsecond).
MAX_GPS_SKEW_S = 300.0

# A GPS agreement is trusted for this long without being renewed; after that
# the source degrades to "unsynced" until the next fix carries a date.
GPS_TRUST_S = 6 * 3600

_NTP_CACHE_S = 60.0

SOURCE_NTP = "ntp"
SOURCE_GPS = "gps"
SOURCE_UNSYNCED = "unsynced"


def _run(argv: list[str], timeout: float = 5.0) -> tuple[int, str]:
    exe = shutil.which(argv[0])
    if not exe:
        return 127, ""
    try:
        out = subprocess.run(  # nosec B603 - resolved path, fixed argv, shell=False
            [exe, *argv[1:]], capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.debug("%s failed: %s", argv[0], e)
        return 1, ""
    return out.returncode, (out.stdout or "").strip()


def _set_system_time(when: datetime) -> bool:
    """Set the system clock. Needs the sudoers line setup.sh installs."""
    stamp = when.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")
    rc, _ = _run(["sudo", "-n", "date", "-u", "-s", stamp])
    return rc == 0


def system_time_plausible(now: datetime | None = None) -> bool:
    now = now or datetime.now(UTC)
    return now >= EARLIEST_PLAUSIBLE_UTC


class ClockDiscipline:
    """One instance per process; ``main.py`` owns it and the GPS worker feeds it.

    ``run`` / ``set_time`` / ``monotonic`` are injectable so the tests never
    touch timedatectl or the real clock.
    """

    def __init__(
        self,
        run: Callable[[list[str]], tuple[int, str]] | None = None,
        set_time: Callable[[datetime], bool] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._run = run or (lambda argv: _run(argv))
        self._set_time = set_time or _set_system_time
        self._mono = monotonic
        self._ntp_cached: tuple[float, bool] | None = None
        self._gps_agreed_mono: float | None = None
        self.last_correction: dict[str, Any] | None = None
        self.corrections = 0

    # -- NTP ------------------------------------------------------------------

    def ntp_synced(self) -> bool:
        """systemd-timesyncd's own verdict, cached for a minute. False when
        timedatectl is absent (a laptop running the simulator, a non-systemd
        host) — absence is not synchronisation."""
        now = self._mono()
        if self._ntp_cached and now - self._ntp_cached[0] < _NTP_CACHE_S:
            return self._ntp_cached[1]
        rc, out = self._run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"])
        synced = rc == 0 and out.strip().lower() == "yes"
        self._ntp_cached = (now, synced)
        return synced

    # -- GPS ------------------------------------------------------------------

    def observe_gps(self, gps_utc: datetime | None, now: datetime | None = None) -> dict[str, Any]:
        """Feed one GPS RMC date+time. Returns what was done, never raises.

        * NTP synced → nothing; NTP is the better clock and GPS only confirms.
        * System clock implausible or more than MAX_GPS_SKEW_S from GPS → set
          it from GPS and record the correction.
        * Otherwise → the clock agrees with GPS; note the agreement so the
          source reads ``gps`` for GPS_TRUST_S.
        """
        if gps_utc is None or gps_utc.tzinfo is None:
            return {"action": "none", "reason": "no_gps_time"}
        now = now or datetime.now(UTC)
        if self.ntp_synced():
            return {"action": "none", "reason": "ntp"}
        offset_s = (gps_utc - now).total_seconds()
        if not system_time_plausible(now) or abs(offset_s) > MAX_GPS_SKEW_S:
            ok = False
            try:
                ok = bool(self._set_time(gps_utc))
            except Exception as e:  # noqa: BLE001 — a failed correction is logged, never fatal
                logger.error("clock correction failed: %s", e)
            record = {
                "action": "set" if ok else "set_failed",
                "reason": "implausible" if not system_time_plausible(now) else "skew",
                "offset_s": round(offset_s, 3),
                "from": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "to": gps_utc.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            if ok:
                self.corrections += 1
                self.last_correction = record
                self._gps_agreed_mono = self._mono()
                logger.warning(
                    "System clock set from GPS: %s -> %s (offset %.1f s)",
                    record["from"],
                    record["to"],
                    offset_s,
                )
            else:
                logger.error(
                    "System clock is %s (offset %.1f s from GPS) and could not be set",
                    record["reason"],
                    offset_s,
                )
            return record
        self._gps_agreed_mono = self._mono()
        return {"action": "agree", "offset_s": round(offset_s, 3)}

    # -- verdict --------------------------------------------------------------

    def source(self) -> str:
        """``ntp`` | ``gps`` | ``unsynced`` — stamped on every reading."""
        if self.ntp_synced():
            return SOURCE_NTP
        if self._gps_agreed_mono is not None and self._mono() - self._gps_agreed_mono < GPS_TRUST_S:
            return SOURCE_GPS
        return SOURCE_UNSYNCED
