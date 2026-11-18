"""The setup page stays reachable while BlueSignal Setup is on the air.

Audit, 2026-11-18. Two ways the page could go dark with the hotspot up:

* Three units declare ``RuntimeDirectory=bluesignal``. systemd removes a
  unit's runtime directory whenever that unit stops or restarts, unless
  ``RuntimeDirectoryPreserve=`` says otherwise, and there is no count of the
  other units still using it. A firmware restart (a crash, the 180 s
  watchdog on a slow first boot) deleted ``/run/bluesignal``, and with it
  ``captive-portal.mode``. The page reads a missing mode file as "down" and
  stops answering, while hostapd keeps beaconing. The join socket in the same
  directory already rebuilt itself after this; the mode file did not.
* The page and the join helper restart 2 s after a crash, and systemd's
  default start limit (5 in 10 s) then refuses them until a reboot. The
  service the AP fallback and the guard use to start them, ``enable --now``,
  cannot get past that limit either.

The hotspot guard also re-arms the page when the hotspot is up and the mode
file says otherwise.
"""

from __future__ import annotations

import configparser
from pathlib import Path

import pytest

from tests.test_netctl import FakeNm

ROOT = Path(__file__).resolve().parent.parent
UNITS = ROOT / "systemd"


def _unit(name: str) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    parser.read_string((UNITS / name).read_text(encoding="utf-8"))
    return parser


def _units_with_runtime_dir() -> list[str]:
    found = []
    for path in sorted(UNITS.glob("*.service")):
        parser = _unit(path.name)
        if parser.has_option("Service", "RuntimeDirectory"):
            found.append(path.name)
    return found


class TestRunTimeDirectorySurvivesARestart:
    def test_the_shared_directory_is_declared_by_more_than_one_unit(self):
        """Why this matters: one unit's restart would take it from the others."""
        assert len(_units_with_runtime_dir()) >= 2

    @pytest.mark.parametrize("name", _units_with_runtime_dir())
    def test_every_unit_keeps_the_directory_when_it_stops(self, name):
        parser = _unit(name)
        assert parser.get("Service", "RuntimeDirectory") == "bluesignal"
        assert parser.get("Service", "RuntimeDirectoryPreserve") == "yes", name

    def test_the_directory_is_still_made_at_every_boot(self):
        """/run is tmpfs: a reboot starts clean, and tmpfiles makes the folder."""
        setup = (ROOT / "setup.sh").read_text(encoding="utf-8")
        assert "d /var/run/bluesignal 0755 $INSTALL_USER $INSTALL_USER -" in setup


class TestThePageIsAlwaysRestarted:
    @pytest.mark.parametrize(
        "name", ["bluesignal-captive-portal.service", "bluesignal-wifi-join.service"]
    )
    def test_no_start_limit_on_the_page_or_its_join_helper(self, name):
        parser = _unit(name)
        assert parser.get("Unit", "StartLimitIntervalSec") == "0", name
        assert not parser.has_option("Service", "StartLimitIntervalSec")
        assert parser.get("Service", "Restart") in ("always", "on-failure")

    def test_starting_a_packaged_unit_clears_its_failed_state_first(self, monkeypatch, tmp_path):
        from utils import captive_portal

        calls: list[tuple[str, ...]] = []
        monkeypatch.setattr(captive_portal.os, "geteuid", lambda: 0)
        monkeypatch.setattr(captive_portal, "_systemctl", lambda *a: calls.append(a) or (0, "", ""))
        unit = tmp_path / "bluesignal-captive-portal.service"
        unit.write_text("[Unit]\n", encoding="utf-8")
        real_path = captive_portal.Path

        def fake_path(*parts):
            p = real_path(*parts)
            if str(p) == "/etc/systemd/system":
                return tmp_path
            return p

        monkeypatch.setattr(captive_portal, "Path", fake_path)
        assert captive_portal.ensure_packaged_unit(unit.name, as_service_user=True) == {"ok": True}
        assert calls == [
            ("reset-failed", unit.name),
            ("enable", "--now", unit.name),
        ]


class TestTheGuardReArmsThePage:
    @pytest.fixture
    def nm(self, monkeypatch, tmp_path):
        from utils import netctl

        fake = FakeNm()
        monkeypatch.setattr(netctl, "_run", fake.run)
        monkeypatch.setenv("WQM1_PORTAL_MODE_FILE", str(tmp_path / "captive-portal.mode"))
        return fake

    def _guard(self):
        from utils import netctl

        return netctl.guard_hotspot("BlueSignal Setup", None, sleep=lambda s: None)

    def test_a_hotspot_whose_page_lost_its_mode_file_gets_the_page_back(self, nm):
        from utils import captive_portal

        nm.ap = True
        assert captive_portal.read_mode() == "down: stopped"
        r = self._guard()
        assert r == {"skipped": "hotspot up", "rearmed": True}
        assert captive_portal.read_mode() == "up"

    def test_a_page_that_is_up_is_left_alone(self, nm):
        from utils import captive_portal

        nm.ap = True
        captive_portal.arm_portal()
        assert self._guard() == {"skipped": "hotspot up"}
        assert captive_portal.read_mode() == "up"

    def test_a_unit_on_its_site_network_never_arms_the_page(self, nm):
        from utils import captive_portal

        nm.station = True
        captive_portal.disarm_portal("station")
        assert self._guard() == {"skipped": "on the site network"}
        assert captive_portal.read_mode() == "down: station"
