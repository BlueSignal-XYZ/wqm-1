"""The setup hotspot comes back after a join that cannot get onto the router.

Jacques, 2026-11-17, Pi 4 on card 50c7151: "the device forgets its own
hotspot when connecting to wifi". The card read ``hotspot: down``: the join
never got onto the router, and BlueSignal Setup never came back.

Pinned here:

* the restore keeps trying for about 45 seconds instead of six, cycles the
  link between tries, and clears systemd's start-limit count before every
  hostapd start, so a hostapd that died on a busy radio cannot lock it out;
* when it still cannot come back, hostapd's and NetworkManager's own lines
  go onto the card (``wqm1-apfail.txt``) without the typed password;
* a unit on neither network gets the hotspot back within about a minute
  (``bluesignal-hotspot-guard.timer``), even with nobody at the setup page.
"""

from __future__ import annotations

import fcntl
import importlib.util
import os
from pathlib import Path

import pytest

from tests.test_netctl import FakeNm

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def nm(monkeypatch):
    from utils import netctl

    fake = FakeNm()
    monkeypatch.setattr(netctl, "_run", fake.run)
    monkeypatch.setattr(netctl, "reach_gateway_or_dns", lambda *a, **k: fake.reach)
    monkeypatch.setattr(netctl, "probe_sign_in_page", lambda *a, **k: "clear")
    monkeypatch.setattr(netctl, "reach_bluesignal", lambda *a, **k: {"ok": True})
    return fake


@pytest.fixture
def clock():
    now = [0.0]

    class Clock:
        def __call__(self):
            return now[0]

        def sleep(self, s):
            now[0] += s

        def advance(self, s):
            now[0] += s

        @property
        def now(self):
            return now[0]

    return Clock()


def _hostapd_starts(nm):
    return [c for c in nm.calls if c[:3] == ["systemctl", "start", "wqm1-setup-hostapd.service"]]


def _failed_join(netctl, clock, password="hunter22"):
    return netctl.join_network(
        "PondHouse",
        password,
        ap_ssid="BlueSignal Setup",
        ap_passphrase=None,
        sleep=clock.sleep,
        clock=clock,
    )


class TestTheHotspotAlwaysComesBack:
    def test_every_hostapd_start_clears_the_start_limit_first(self, nm):
        from utils import netctl

        assert netctl.start_ap("BlueSignal Setup")["ok"] is True
        flat = [tuple(c[:2]) for c in nm.calls]
        reset = flat.index(("systemctl", "reset-failed"))
        start = nm.calls.index(_hostapd_starts(nm)[0])
        assert reset < start
        assert nm.calls[reset][2:] == ["wqm1-setup-hostapd.service", "wqm1-setup-dnsmasq.service"]

    def test_a_radio_that_takes_half_a_minute_to_let_go_still_gets_the_hotspot(self, nm, clock):
        """The old restore gave up after three tries two seconds apart."""
        from utils import netctl

        nm.ap = True
        nm.join_rc = 4  # "Secrets were required": a fast failure
        nm.hotspot_fails = 6
        r = _failed_join(netctl, clock)
        assert r["ok"] is False
        assert r["ap_restored"] is True
        assert nm.ap is True
        assert len(_hostapd_starts(nm)) == 7
        downs = [c for c in nm.calls if c == ["ip", "link", "set", "wlan0", "down"]]
        assert len(downs) == 6

    def test_only_the_first_try_scans(self, nm, clock, monkeypatch):
        from utils import netctl

        nm.ap = True
        nm.join_rc = 4
        nm.hotspot_fails = 2
        scans = []
        real = netctl.start_ap

        def start_ap(*a, **k):
            scans.append(k.get("scan", True))
            return real(*a, **k)

        monkeypatch.setattr(netctl, "start_ap", start_ap)
        assert _failed_join(netctl, clock)["ap_restored"] is True
        assert scans == [True, False, False]

    def test_the_restore_stops_after_about_45_seconds(self, nm, clock, monkeypatch):
        from utils import netctl

        nm.ap = True
        nm.join_rc = 4
        nm.hotspot_rc = 1
        real = netctl.start_ap

        def slow_start(*a, **k):
            clock.advance(6.0)  # a real failed start: units, nmcli, the hostapd wait
            return real(*a, **k)

        monkeypatch.setattr(netctl, "start_ap", slow_start)
        began = clock.now
        r = _failed_join(netctl, clock)
        assert r["ap_restored"] is False
        tries = len(_hostapd_starts(nm))
        assert netctl.AP_ATTEMPTS < tries < netctl.AP_RESTORE_MAX_TRIES
        assert clock.now - began <= netctl.JOIN_BUDGET_S + netctl.AP_RESTORE_S + 10

    def test_a_restore_that_never_works_is_still_bounded(self, nm, clock):
        from utils import netctl

        nm.ap = True
        nm.join_rc = 4
        nm.hotspot_rc = 1
        assert _failed_join(netctl, clock)["ap_restored"] is False
        assert len(_hostapd_starts(nm)) == netctl.AP_RESTORE_MAX_TRIES

    def test_a_hotspot_that_will_not_come_back_leaves_hostapds_lines_on_the_card(
        self, nm, clock, monkeypatch, tmp_path
    ):
        from utils import netctl

        clue = tmp_path / "boot" / "wqm1-apfail.txt"
        monkeypatch.setenv("WQM1_APFAIL_FILE", str(clue))
        lines = {
            "wqm1-setup-hostapd.service": "hostapd: nl80211: Could not configure driver mode\n",
            "NetworkManager.service": "NM: device (wlan0): tried psk hunter22secret\n",
        }
        monkeypatch.setattr(netctl, "_journal_for_clue", lambda unit, n: lines[unit])
        nm.ap = True
        nm.join_rc = 4
        nm.hotspot_rc = 1
        nm.hotspot_err = "Job for wqm1-setup-hostapd.service failed."
        assert _failed_join(netctl, clock, password="hunter22secret")["ap_restored"] is False
        text = clue.read_text(encoding="utf-8")
        assert text.startswith("setup hotspot did not come back: Job for wqm1-setup-hostapd")
        assert "Could not configure driver mode" in text
        assert "--- NetworkManager.service" in text
        assert "hunter22secret" not in text

    def test_a_restore_that_failed_gives_the_radio_back_to_networkmanager(self, nm, clock):
        from utils import netctl

        nm.ap = True
        nm.join_rc = 4
        nm.hotspot_rc = 1
        assert _failed_join(netctl, clock)["ap_restored"] is False
        last_down = max(
            i for i, c in enumerate(nm.calls) if c == ["ip", "link", "set", "wlan0", "down"]
        )
        tail = nm.calls[last_down:]
        assert ["ip", "link", "set", "wlan0", "up"] in tail
        assert nm.managed == "yes"

    def test_the_retry_service_outlasts_its_budget_plus_a_long_restore(self):
        from utils import netctl

        unit = (ROOT / "systemd" / "bluesignal-wifi-retry.service").read_text()
        timeout = int(unit.split("TimeoutStartSec=", 1)[1].split()[0])
        assert timeout >= netctl.RETRY_BUDGET_S + netctl.AP_RESTORE_S + 30

    def test_a_hotspot_that_comes_back_writes_no_clue(self, nm, clock, monkeypatch, tmp_path):
        from utils import netctl

        clue = tmp_path / "boot" / "wqm1-apfail.txt"
        monkeypatch.setenv("WQM1_APFAIL_FILE", str(clue))
        nm.ap = True
        nm.join_rc = 4
        nm.hotspot_fails = 1
        assert _failed_join(netctl, clock)["ap_restored"] is True
        assert not clue.exists()

    def test_the_saved_network_retry_uses_the_same_restore(self, nm, clock):
        from utils import netctl

        nm.ap = True
        nm.saved = ["PondHouse"]
        nm.hotspot_fails = 5
        r = netctl.retry_saved_station(
            "BlueSignal Setup", None, sleep=clock.sleep, clock=clock, wait_s=1.0
        )
        assert r["ap"] is True
        assert len(_hostapd_starts(nm)) == 6


class TestTheGuardRaisesTheHotspot:
    def _guard(self, clock):
        from utils import netctl

        return netctl.guard_hotspot("BlueSignal Setup", None, sleep=clock.sleep, clock=clock)

    def test_a_unit_on_neither_network_gets_the_hotspot_back(self, nm, clock):
        r = self._guard(clock)
        assert r["raised"] is True
        assert nm.ap is True

    def test_it_leaves_a_hotspot_that_is_up_alone(self, nm, clock):
        nm.ap = True
        assert self._guard(clock) == {"skipped": "hotspot up"}
        assert not _hostapd_starts(nm)

    def test_it_leaves_a_unit_on_its_site_network_alone(self, nm, clock):
        nm.station = True
        assert self._guard(clock) == {"skipped": "on the site network"}
        assert not _hostapd_starts(nm)

    def test_it_waits_while_a_join_holds_the_radio(self, nm, clock, tmp_path):
        from utils import netctl

        path = netctl.wifi_lock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            assert self._guard(clock) == {"skipped": "wlan0 in use"}
        finally:
            os.close(fd)
        assert not _hostapd_starts(nm)

    def test_it_waits_for_the_boot_fallback(self, nm, clock, monkeypatch):
        from utils import netctl

        real = nm.run

        def run(argv, timeout=15.0):
            if argv[:3] == ["systemctl", "is-active", "bluesignal-ap-fallback.service"]:
                return 3, "activating", ""
            return real(argv, timeout=timeout)

        monkeypatch.setattr(netctl, "_run", run)
        assert self._guard(clock) == {"skipped": "bluesignal-ap-fallback.service running"}
        assert not _hostapd_starts(nm)

    def test_a_saved_network_gets_five_minutes_to_come_back_first(self, nm, clock):
        from utils import netctl

        nm.saved = ["PondHouse"]
        assert self._guard(clock)["skipped"] == "waiting for the saved network"
        clock.advance(netctl.GUARD_SAVED_GRACE_S - 1)
        assert self._guard(clock)["skipped"] == "waiting for the saved network"
        clock.advance(1)
        assert self._guard(clock)["raised"] is True

    def test_the_wait_starts_over_once_the_unit_is_back_on_its_network(self, nm, clock):
        from utils import netctl

        nm.saved = ["PondHouse"]
        self._guard(clock)
        clock.advance(netctl.GUARD_SAVED_GRACE_S - 10)
        nm.station = True
        self._guard(clock)
        assert not netctl.guard_state_path().exists()
        nm.station = False
        clock.advance(20)
        assert self._guard(clock)["skipped"] == "waiting for the saved network"

    def test_a_card_held_for_setup_does_not_wait_for_its_old_network(self, nm, clock, monkeypatch):
        from utils import netctl

        nm.saved = ["PondHouse"]
        monkeypatch.setattr(netctl, "wifi_reconfirm_required", lambda: True)
        assert self._guard(clock)["raised"] is True

    def test_a_radio_that_will_not_take_the_hotspot_is_left_alone_for_ten_minutes(self, nm, clock):
        from utils import netctl

        nm.hotspot_rc = 1
        assert self._guard(clock)["raised"] is False
        tries = len(_hostapd_starts(nm))
        clock.advance(60)
        assert self._guard(clock)["skipped"] == "the last raise failed"
        clock.advance(netctl.GUARD_RETRY_AFTER_FAIL_S)
        nm.hotspot_rc = 0
        assert self._guard(clock)["raised"] is True
        assert len(_hostapd_starts(nm)) == tries + 1

    def test_a_unit_with_no_wifi_interface_is_left_alone(self, nm, clock, monkeypatch):
        from utils import netctl

        monkeypatch.setattr(netctl, "iface_state", lambda iface="wlan0": None)
        assert self._guard(clock) == {"skipped": "no wlan0"}
        assert not _hostapd_starts(nm)

    def test_it_says_on_the_card_that_the_hotspot_is_back(self, nm, clock, tmp_path, monkeypatch):
        status = tmp_path / "boot" / "wqm1-status.txt"
        monkeypatch.setenv("WQM1_STATUS_FILE", str(status))
        self._guard(clock)
        text = status.read_text(encoding="utf-8")
        assert "hotspot: up" in text


class TestTheGuardIsInstalled:
    def test_setup_installs_and_enables_the_guard_timer(self):
        setup = (ROOT / "setup.sh").read_text()
        assert 'systemd/bluesignal-hotspot-guard.service" /etc/systemd/system/' in setup
        assert 'systemd/bluesignal-hotspot-guard.timer" /etc/systemd/system/' in setup
        assert "systemctl enable bluesignal-hotspot-guard.timer" in setup

    def test_the_timer_runs_about_once_a_minute_after_the_boot_fallback(self):
        timer = (ROOT / "systemd" / "bluesignal-hotspot-guard.timer").read_text()
        assert "OnUnitInactiveSec=1min" in timer
        assert "OnBootSec=4min" in timer
        assert "WantedBy=timers.target" in timer

    def test_the_service_runs_the_guard_as_root(self):
        unit = (ROOT / "systemd" / "bluesignal-hotspot-guard.service").read_text()
        assert "wqm1-ap-fallback.py --guard" in unit
        assert "User=root" in unit
        assert "Type=oneshot" in unit

    def test_the_script_raises_the_hotspot_and_starts_the_page(self, nm, monkeypatch, tmp_path):
        from utils import captive_portal, join_service

        spec = importlib.util.spec_from_file_location(
            "ap_fallback_guard", ROOT / "scripts" / "wqm1-ap-fallback.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        started = []
        monkeypatch.setattr(
            captive_portal,
            "ensure_portal_service",
            lambda: started.append("portal") or {"ok": True},
        )
        monkeypatch.setattr(
            join_service, "ensure_join_service", lambda: started.append("join") or {"ok": True}
        )
        monkeypatch.setattr(captive_portal, "wait_until_reachable", lambda *a, **k: "up (80)")
        monkeypatch.setattr(mod.sys, "argv", ["wqm1-ap-fallback.py", "--guard"])
        status = tmp_path / "boot" / "wqm1-status.txt"
        monkeypatch.setenv("WQM1_STATUS_FILE", str(status))
        assert mod.main() == 0
        assert nm.ap is True
        assert started == ["portal", "join"]
        assert "portal: up (80)" in status.read_text(encoding="utf-8")

    def test_the_script_exits_0_when_the_guard_fails(self, nm, monkeypatch):
        from utils import netctl

        spec = importlib.util.spec_from_file_location(
            "ap_fallback_guard2", ROOT / "scripts" / "wqm1-ap-fallback.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        def boom(*_a, **_k):
            raise RuntimeError("nmcli missing")

        monkeypatch.setattr(netctl, "guard_hotspot", boom)
        assert mod.guard() == 0
