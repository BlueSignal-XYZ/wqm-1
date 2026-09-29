"""
utils/clock — clock confidence and GPS discipline (commissioning plan, PR 5).

Time is settlement evidence: the QC accrual judges every totalizer delta over
the interval between two timestamps. These pin that an implausible system
clock is corrected from GPS and the correction recorded, that NTP always
wins, and that every reading can say which clock it trusted.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from utils import clock as clockmod
from utils.clock import (
    EARLIEST_PLAUSIBLE_UTC,
    GPS_TRUST_S,
    MAX_GPS_SKEW_S,
    ClockDiscipline,
    system_time_plausible,
)

GPS_NOW = datetime(2026, 9, 21, 14, 0, 0, tzinfo=UTC)


class FakeHost:
    def __init__(self, ntp: str = "no"):
        self.ntp = ntp
        self.set_calls: list[datetime] = []
        self.mono = 1000.0
        self.set_ok = True

    def run(self, argv):
        if argv[:2] == ["timedatectl", "show"]:
            return 0, self.ntp
        return 1, ""

    def set_time(self, when):
        self.set_calls.append(when)
        return self.set_ok

    def monotonic(self):
        return self.mono


def make(ntp="no"):
    host = FakeHost(ntp)
    cd = ClockDiscipline(run=host.run, set_time=host.set_time, monotonic=host.monotonic)
    return host, cd


class TestPlausibility:
    def test_epoch_boot_is_implausible(self):
        assert system_time_plausible(datetime(1970, 1, 1, tzinfo=UTC)) is False

    def test_a_date_after_the_firmware_existed_is_plausible(self):
        assert system_time_plausible(EARLIEST_PLAUSIBLE_UTC + timedelta(days=1)) is True


class TestDiscipline:
    def test_ntp_synced_means_gps_is_only_confirmation(self):
        host, cd = make(ntp="yes")
        r = cd.observe_gps(GPS_NOW, now=datetime(1970, 1, 1, tzinfo=UTC))
        assert r == {"action": "none", "reason": "ntp"}
        assert host.set_calls == []
        assert cd.source() == "ntp"

    def test_implausible_clock_is_set_from_gps_and_recorded(self):
        host, cd = make()
        r = cd.observe_gps(GPS_NOW, now=datetime(1970, 1, 1, tzinfo=UTC))
        assert r["action"] == "set"
        assert r["reason"] == "implausible"
        assert host.set_calls == [GPS_NOW]
        assert cd.corrections == 1
        assert cd.last_correction["to"] == "2026-09-21T14:00:00Z"
        assert cd.source() == "gps"

    def test_large_skew_is_corrected(self):
        host, cd = make()
        r = cd.observe_gps(GPS_NOW, now=GPS_NOW - timedelta(seconds=MAX_GPS_SKEW_S + 1))
        assert r["action"] == "set" and r["reason"] == "skew"
        assert host.set_calls == [GPS_NOW]

    def test_small_skew_is_agreement_not_a_write(self):
        host, cd = make()
        r = cd.observe_gps(GPS_NOW, now=GPS_NOW + timedelta(seconds=2))
        assert r["action"] == "agree"
        assert host.set_calls == []
        assert cd.source() == "gps"

    def test_gps_trust_expires(self):
        host, cd = make()
        cd.observe_gps(GPS_NOW, now=GPS_NOW)
        assert cd.source() == "gps"
        host.mono += GPS_TRUST_S + 1
        # The NTP answer is cached for a minute; force a fresh (still "no") read.
        cd._ntp_cached = None
        assert cd.source() == "unsynced"

    def test_no_gps_time_changes_nothing(self):
        host, cd = make()
        assert cd.observe_gps(None) == {"action": "none", "reason": "no_gps_time"}
        assert cd.source() == "unsynced"

    def test_failed_set_is_reported_not_hidden(self):
        host, cd = make()
        host.set_ok = False
        r = cd.observe_gps(GPS_NOW, now=datetime(1970, 1, 1, tzinfo=UTC))
        assert r["action"] == "set_failed"
        assert cd.corrections == 0
        assert cd.source() == "unsynced"

    def test_missing_timedatectl_is_not_synchronised(self):
        cd = ClockDiscipline(run=lambda argv: (127, ""), set_time=lambda w: True)
        assert cd.ntp_synced() is False
        assert cd.source() == "unsynced"


class TestSystemTimeCommand:
    def test_uses_sudo_date_with_a_fixed_argv(self, monkeypatch):
        seen = {}

        def fake_run(argv, timeout=5.0):
            seen["argv"] = argv
            return 0, ""

        monkeypatch.setattr(clockmod, "_run", fake_run)
        assert clockmod._set_system_time(GPS_NOW) is True
        assert seen["argv"] == ["sudo", "-n", "date", "-u", "-s", "2026-09-21 14:00:00"]
