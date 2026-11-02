"""Issue #163: lock wlan0 between setup's join and the Wi-Fi retry timer.

``join_network`` (the phone on the setup page) and ``retry_saved_station``
(``bluesignal-wifi-retry.timer``) both drive ``wlan0``. Without a lock that
spans the two processes, a join can take the hotspot down in the milliseconds
after retry's last client count and before retry's own ``stop_ap``.

The lock is an exclusive ``flock`` on ``/run/wqm1/wifi.lock`` (overridable
with ``WQM1_WIFI_LOCK``). Join waits. Retry skips when the radio is busy.
"""

from __future__ import annotations

import fcntl
import os
import threading
import time
from pathlib import Path

import pytest

from tests.test_netctl import FakeNm


@pytest.fixture
def nm(monkeypatch):
    from utils import netctl

    fake = FakeNm()
    monkeypatch.setattr(netctl, "_run", fake.run)
    monkeypatch.setattr(netctl, "reach_gateway_or_dns", lambda *a, **k: fake.reach)
    return fake


@pytest.fixture
def lock_path(tmp_path, monkeypatch):
    path = tmp_path / "wifi.lock"
    monkeypatch.setenv("WQM1_WIFI_LOCK", str(path))
    return path


def _hold_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


class TestWifiLockPath:
    def test_default_lock_is_under_run_wqm1(self, monkeypatch):
        monkeypatch.delenv("WQM1_WIFI_LOCK", raising=False)
        from utils.netctl import wifi_lock_path

        assert wifi_lock_path() == Path("/run/wqm1/wifi.lock")

    def test_env_override_is_used(self, tmp_path, monkeypatch):
        path = tmp_path / "custom.lock"
        monkeypatch.setenv("WQM1_WIFI_LOCK", str(path))
        from utils.netctl import wifi_lock_path

        assert wifi_lock_path() == path


class TestRetrySkipsWhenJoinHoldsTheLock:
    def test_retry_does_not_touch_wlan0_while_lock_is_held(
        self, nm, lock_path, monkeypatch, tmp_path
    ):
        from utils.netctl import retry_saved_station

        monkeypatch.setenv("WQM1_PORTAL_MODE_FILE", str(tmp_path / "portal-mode"))
        monkeypatch.setenv("WQM1_STATUS_FILE", str(tmp_path / "wqm1-status.txt"))
        nm.ap = True
        nm.saved = ["Batcave"]
        nm.visible = ["Batcave"]
        nm.station_up_rc = 0
        fd = _hold_lock(lock_path)
        try:
            result = retry_saved_station("WQM1-0001", "rain-delta-29", sleep=lambda _s: None)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        assert result == {"skipped": "wlan0 in use"}
        assert nm.ap is True
        assert not any(
            c[:3] == ["systemctl", "stop", "wqm1-setup-hostapd.service"] for c in nm.calls
        )
        assert not any(c[1:3] == ["connection", "up"] for c in nm.calls)


class TestJoinWaitsForTheLock:
    def test_join_waits_then_takes_wlan0_after_retry_releases(
        self, nm, lock_path, monkeypatch, tmp_path
    ):
        from utils.netctl import join_network

        monkeypatch.setenv("WQM1_PORTAL_MODE_FILE", str(tmp_path / "portal-mode"))
        monkeypatch.setenv("WQM1_STATUS_FILE", str(tmp_path / "wqm1-status.txt"))
        nm.ap = True
        held = threading.Event()
        release = threading.Event()
        finished: list[dict] = []

        def holder() -> None:
            fd = _hold_lock(lock_path)
            held.set()
            release.wait(timeout=5)
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

        def do_join() -> None:
            finished.append(
                join_network(
                    "PondHouse",
                    "hunter22",
                    ap_ssid="WQM1-0001",
                    ap_passphrase="river-cedar-42",
                    sleep=lambda _s: None,
                )
            )

        threading.Thread(target=holder, daemon=True).start()
        assert held.wait(timeout=1)
        worker = threading.Thread(target=do_join)
        worker.start()
        time.sleep(0.2)
        assert not finished, "join_network must wait while the retry holds the lock"
        assert nm.ap is True
        assert not any(
            c[:3] == ["systemctl", "stop", "wqm1-setup-hostapd.service"] for c in nm.calls
        )
        release.set()
        worker.join(timeout=2)
        assert worker.is_alive() is False
        assert finished and finished[0]["ok"] is True
        assert any(c[:3] == ["systemctl", "stop", "wqm1-setup-hostapd.service"] for c in nm.calls)


class TestJoinHoldsTheLockAgainstRetry:
    def test_retry_skips_for_the_whole_join(self, nm, lock_path, monkeypatch, tmp_path):
        from utils import netctl
        from utils.netctl import join_network, retry_saved_station

        monkeypatch.setenv("WQM1_PORTAL_MODE_FILE", str(tmp_path / "portal-mode"))
        monkeypatch.setenv("WQM1_STATUS_FILE", str(tmp_path / "wqm1-status.txt"))
        nm.ap = True
        nm.saved = ["PondHouse"]
        inside = threading.Event()
        resume = threading.Event()
        real_stop = netctl.stop_ap

        def pause_after_stop(iface="wlan0"):
            real_stop(iface)
            inside.set()
            resume.wait(timeout=5)

        monkeypatch.setattr(netctl, "stop_ap", pause_after_stop)
        joined: list[dict] = []

        def do_join() -> None:
            joined.append(
                join_network(
                    "PondHouse",
                    "hunter22",
                    ap_ssid="WQM1-0001",
                    ap_passphrase="river-cedar-42",
                    sleep=lambda _s: None,
                )
            )

        worker = threading.Thread(target=do_join)
        worker.start()
        assert inside.wait(timeout=2)
        retry = retry_saved_station("WQM1-0001", "rain-delta-29", sleep=lambda _s: None)
        assert retry == {"skipped": "wlan0 in use"}
        resume.set()
        worker.join(timeout=2)
        assert joined and joined[0]["ok"] is True
