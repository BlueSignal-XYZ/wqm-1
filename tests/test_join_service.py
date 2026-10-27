"""The root half of the port-80 page: join and rescan over a local socket.

The page runs as the install user and a join needs root (hostapd, the AP
address, ``iw scan ap-force``). ``utils.join_service`` accepts two requests
on a Unix socket only root and the install user may open. These tests run
the real socket in a temp directory with ``utils.join.start_join`` and the
scan stubbed.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time

import pytest

from utils import join, join_service, netctl

SECRET = "Sp3cial-Pa55-wqm"  # nosec B105 — a test value searched for below


@pytest.fixture
def service(tmp_path, monkeypatch):
    path = tmp_path / "run" / "wifi-join.sock"
    monkeypatch.setenv("WQM1_JOIN_SOCKET", str(path))
    monkeypatch.setenv("BLUESIGNAL_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("WQM1_VIRTUAL_NET", raising=False)
    monkeypatch.setattr(join_service, "_use_socket", lambda: True)
    started: list[dict] = []

    def fake_start(ssid, password, *, hidden=False, **_kw):
        started.append({"ssid": ssid, "password": password, "hidden": hidden})
        return {"state": "joining", "ssid": ssid}

    monkeypatch.setattr(join, "start_join", fake_start)
    monkeypatch.setattr(
        netctl, "refresh_scan_cache", lambda iface="wlan0": {"ok": True, "count": 2}
    )
    halt = threading.Event()
    owner = (os.getuid(), os.getgid())
    thread = threading.Thread(
        target=join_service.serve,
        kwargs={"path": path, "stop": halt.is_set, "poll_s": 0.05, "owner": owner},
        daemon=True,
    )
    thread.start()
    end = time.monotonic() + 3
    while not path.exists() and time.monotonic() < end:
        time.sleep(0.01)
    yield path, started
    halt.set()
    thread.join(timeout=3)


def _raw(path, data: bytes) -> bytes:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(3)
        conn.connect(str(path))
        conn.sendall(data)
        out = b""
        while True:
            try:
                chunk = conn.recv(4096)
            except ConnectionResetError:
                # The service closed on a request it would not read.
                break
            if not chunk:
                break
            out += chunk
    return out


class TestSocket:
    def test_a_join_crosses_the_socket(self, service):
        _path, started = service
        answer = join_service.request_join("PondHouse", "hunter2222", hidden=True)
        assert answer == {"state": "joining", "ssid": "PondHouse"}
        assert started == [{"ssid": "PondHouse", "password": "hunter2222", "hidden": True}]

    def test_a_rescan_crosses_the_socket(self, service):
        assert join_service.request_rescan() == {"ok": True, "count": 2}

    def test_the_socket_is_the_install_users_alone(self, service):
        path, _ = service
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.stat().st_uid == os.getuid()

    def test_a_removed_socket_comes_back(self, service):
        path, _ = service
        path.unlink()
        end = time.monotonic() + 3
        while not path.exists() and time.monotonic() < end:
            time.sleep(0.02)
        assert join_service.request_rescan() == {"ok": True, "count": 2}

    def test_no_service_reads_as_unavailable(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WQM1_JOIN_SOCKET", str(tmp_path / "missing.sock"))
        monkeypatch.setattr(join_service, "_use_socket", lambda: True)
        assert join_service.request_join("PondHouse", "hunter2222") == {"error": "unavailable"}
        assert join_service.request_rescan() == {"error": "unavailable"}

    @pytest.mark.parametrize(
        ("data", "answer"),
        [
            (b"not json\n", {"error": "bad_request"}),
            (b'{"op": "format_disk"}\n', {"error": "bad_request"}),
            (b"[1, 2]\n", {"error": "bad_request"}),
            (b"x" * (join_service.MAX_REQUEST + 10), {"error": "bad_request"}),
        ],
        ids=["not-json", "unknown-op", "not-an-object", "too-long"],
    )
    def test_anything_else_is_a_bad_request(self, service, data, answer):
        path, started = service
        out = _raw(path, data)
        # Too long may be cut off by the close before the answer is read.
        assert out == b"" or json.loads(out) == answer
        if len(data) <= join_service.MAX_REQUEST:
            assert json.loads(out) == answer
        assert started == []

    def test_the_password_is_never_logged(self, service, caplog):
        caplog.set_level(logging.DEBUG)
        join_service.request_join("PondHouse", SECRET)
        time.sleep(0.05)
        assert "PondHouse" in caplog.text
        assert SECRET not in caplog.text


class TestPeers:
    def test_root_and_the_install_user_only(self):
        assert join_service._allowed(0, (1000, 1000))
        assert join_service._allowed(1000, (1000, 1000))
        assert not join_service._allowed(1001, (1000, 1000))
        assert not join_service._allowed(None, (1000, 1000))
        assert join_service._allowed(0, None)
        assert not join_service._allowed(1000, None)

    def test_a_refused_peer_starts_nothing(self, service, monkeypatch):
        path, started = service
        monkeypatch.setattr(join_service, "peer_uid", lambda _conn: 4242)
        out = _raw(path, b'{"op": "join", "ssid": "PondHouse", "password": "hunter2222"}\n')
        assert out == b""
        assert started == []

    def test_peer_uid_reads_the_kernels_answer(self):
        a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            assert join_service.peer_uid(a) in (os.getuid(), None)
        finally:
            a.close()
            b.close()


class TestRequests:
    @pytest.fixture
    def started(self, monkeypatch):
        calls: list[tuple] = []

        def fake_start(ssid, password, *, hidden=False, **_kw):
            calls.append((ssid, password, hidden))
            return {"state": "joining", "ssid": ssid}

        monkeypatch.setattr(join, "start_join", fake_start)
        return calls

    @pytest.mark.parametrize(
        ("request_", "answer"),
        [
            ({"op": "join", "ssid": ""}, {"error": "network_name_bad"}),
            ({"op": "join", "ssid": "x" * 33}, {"error": "network_name_bad"}),
            ({"op": "join", "ssid": 7}, {"error": "network_name_bad"}),
            ({"op": "join", "ssid": "PondHouse", "password": 5}, {"error": "bad_request"}),
            ({"op": "join", "ssid": "PondHouse", "password": "short"}, {"error": "password_short"}),
            ({"op": "nope"}, {"error": "bad_request"}),
            ("join", {"error": "bad_request"}),
        ],
    )
    def test_checked_again_on_the_root_side(self, started, request_, answer):
        assert join_service.handle_request(request_) == answer
        assert started == []

    def test_a_blank_password_is_an_open_network(self, started):
        join_service.handle_request({"op": "join", "ssid": "Cafe", "password": ""})
        assert started == [("Cafe", None, False)]

    def test_hidden_must_be_true_not_truthy(self, started):
        join_service.handle_request(
            {"op": "join", "ssid": "Cafe", "password": None, "hidden": "yes"}
        )
        assert started == [("Cafe", None, False)]

    def test_no_rescan_while_a_join_owns_the_radio(self, monkeypatch):
        monkeypatch.setattr(join, "_lock_held", lambda: True)
        called = []
        monkeypatch.setattr(netctl, "refresh_scan_cache", lambda: called.append(1))
        assert join_service.handle_request({"op": "rescan"}) == {"ok": False, "count": 0}
        assert called == []

    def test_only_the_install_user_on_a_real_unit_uses_the_socket(self, tmp_path, monkeypatch):
        # conftest keeps every other test off the socket; undo that here.
        monkeypatch.undo()
        monkeypatch.delenv("WQM1_VIRTUAL_NET", raising=False)
        monkeypatch.setattr(os, "geteuid", lambda: 1000)
        assert join_service._use_socket() is True
        monkeypatch.setattr(os, "geteuid", lambda: 0)
        assert join_service._use_socket() is False
        monkeypatch.setattr(os, "geteuid", lambda: 1000)
        monkeypatch.setenv("WQM1_VIRTUAL_NET", str(tmp_path / "netsim.json"))
        assert join_service._use_socket() is False


class TestConcurrency:
    """#169 review: the service was strictly serial, so a join sent while a
    rescan ran waited past the page's 10 s timeout, was reported as failed,
    and then ran anyway."""

    def test_a_join_is_answered_while_a_rescan_is_still_scanning(self, service, monkeypatch):
        _path, started = service
        scanning = threading.Event()
        release = threading.Event()

        def slow_scan(iface="wlan0"):
            scanning.set()
            assert release.wait(timeout=10)
            return {"ok": True, "count": 3}

        monkeypatch.setattr(netctl, "refresh_scan_cache", slow_scan)
        rescans: list[dict] = []
        worker = threading.Thread(target=lambda: rescans.append(join_service.request_rescan()))
        worker.start()
        try:
            assert scanning.wait(timeout=3)
            began = time.monotonic()
            answer = join_service.request_join("PondHouse", "hunter2222")
            assert time.monotonic() - began < 2.0
            assert answer == {"state": "joining", "ssid": "PondHouse"}
            assert started[-1]["ssid"] == "PondHouse"
        finally:
            release.set()
            worker.join(timeout=5)
        assert rescans == [{"ok": True, "count": 3}]

    def test_one_scan_at_a_time(self, monkeypatch):
        running = 0
        most = 0
        guard = threading.Lock()

        def scan(iface="wlan0"):
            nonlocal running, most
            with guard:
                running += 1
                most = max(most, running)
            time.sleep(0.05)
            with guard:
                running -= 1
            return {"ok": True, "count": 1}

        monkeypatch.setattr(netctl, "refresh_scan_cache", scan)
        monkeypatch.setattr(join, "_lock_held", lambda: False)
        workers = [
            threading.Thread(target=join_service.handle_request, args=({"op": "rescan"},))
            for _ in range(4)
        ]
        for w in workers:
            w.start()
        for w in workers:
            w.join(timeout=5)
        assert most == 1


class TestRecovery:
    @pytest.fixture
    def radio(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BLUESIGNAL_STATE_DIR", str(tmp_path / "state"))
        monkeypatch.setenv("WQM1_BOOT_ID", "boot-now")
        raised: list[tuple] = []
        state = {"ap": False, "station": False}
        monkeypatch.setattr(netctl, "ap_active", lambda iface="wlan0": state["ap"])
        monkeypatch.setattr(netctl, "station_connected", lambda iface="wlan0": state["station"])
        monkeypatch.setattr(netctl, "ap_credentials", lambda: ("WQM1-0001", "cedar-river-42"))
        monkeypatch.setattr(
            netctl,
            "start_ap",
            lambda ssid, pw, iface="wlan0": raised.append((ssid, pw)) or {"ok": True},
        )
        return state, raised

    def test_a_join_that_died_mid_way_raises_the_setup_network(self, radio):
        _state, raised = radio
        join._write_state({"state": "joining", "ssid": "PondHouse"})
        assert join_service.recover_interrupted_join() is True
        assert raised == [("WQM1-0001", "cedar-river-42")]

    def test_right_after_boot_a_crash_in_this_boot_still_recovers(self, radio):
        """#169 review: a fixed grace period left the unit unreachable when the
        service died in the first ten minutes. The boot id replaces it."""
        _state, raised = radio
        join._write_state({"state": "joining", "ssid": "PondHouse", "at": "2024-01-01T00:00:00Z"})
        assert join_service.recover_interrupted_join() is True
        assert raised

    def test_a_record_from_an_earlier_boot_is_the_ap_fallbacks(self, radio, monkeypatch):
        _state, raised = radio
        join._write_state({"state": "joining", "ssid": "PondHouse"})
        monkeypatch.setenv("WQM1_BOOT_ID", "boot-after-a-power-cut")
        assert join_service.recover_interrupted_join() is False
        assert raised == []

    def test_a_record_with_no_boot_id_is_left_alone(self, radio):
        _state, raised = radio
        join.state_path().parent.mkdir(parents=True, exist_ok=True)
        join.state_path().write_text(json.dumps({"state": "joining", "ssid": "PondHouse"}))
        assert join_service.recover_interrupted_join() is False
        assert raised == []

    @pytest.mark.parametrize("field", ["ap", "station"])
    def test_nothing_to_do_when_the_radio_is_already_somewhere(self, radio, field):
        state, raised = radio
        state[field] = True
        join._write_state({"state": "joining", "ssid": "PondHouse"})
        assert join_service.recover_interrupted_join() is False
        assert raised == []

    def test_a_finished_join_is_left_alone(self, radio):
        _state, raised = radio
        join._write_state({"state": "failed", "ssid": "PondHouse", "reason": "no_dhcp"})
        assert join_service.recover_interrupted_join() is False
        assert raised == []

    def test_a_join_another_process_still_runs_is_left_alone(self, radio, monkeypatch):
        _state, raised = radio
        join._write_state({"state": "joining", "ssid": "PondHouse"})
        monkeypatch.setattr(join, "_lock_held", lambda: True)
        assert join_service.recover_interrupted_join() is False
        assert raised == []


class TestFilesForTheInstallUser:
    def test_root_hands_the_join_files_to_the_state_dir_owner(self, tmp_path, monkeypatch):
        state = tmp_path / "state"
        state.mkdir()
        monkeypatch.setenv("BLUESIGNAL_STATE_DIR", str(state))
        monkeypatch.setattr(os, "geteuid", lambda: 0)
        monkeypatch.setattr(join, "_state_owner", lambda: (1000, 1000))
        chowned: list[tuple] = []
        monkeypatch.setattr(os, "chown", lambda p, u, g: chowned.append((str(p), u, g)))
        join._write_state({"state": "failed", "ssid": "PondHouse", "reason": "no_dhcp"})
        fd = join._try_lock()
        assert fd is not None
        join._release(fd)
        assert (str(join.state_path()), 1000, 1000) in chowned
        assert (str(join.lock_path()), 1000, 1000) in chowned

    def test_a_non_root_writer_changes_no_owner(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BLUESIGNAL_STATE_DIR", str(tmp_path / "state"))
        monkeypatch.setattr(os, "geteuid", lambda: 1000)
        chowned: list[tuple] = []
        monkeypatch.setattr(os, "chown", lambda *a: chowned.append(a))
        join._write_state({"state": "idle"})
        assert chowned == []


def test_the_unit_runs_as_root_on_a_socket_not_a_port():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    unit = (root / "systemd" / join_service.UNIT_NAME).read_text()
    setup = (root / "setup.sh").read_text()
    assert "User=root" in unit
    assert "ExecStart=/usr/bin/python3 -m utils.join_service" in unit
    assert not any(line.startswith("RuntimeDirectory") for line in unit.splitlines()), (
        "a root RuntimeDirectory would take /run/bluesignal"
    )
    assert f"systemctl enable {join_service.UNIT_NAME}" in setup
    src = (root / "src" / "utils" / "join_service.py").read_text()
    assert "AF_INET" not in src


def test_the_unit_is_hardened_without_breaking_a_join():
    """#169 review: a root service parsing local requests gets the systemd
    hardening that cannot reach what a join does. The ones that would are
    named in the unit and must stay off: the clock step (ProtectClock,
    NoNewPrivileges for sudo), the boot status file (ProtectSystem) and
    /dev/rfkill (PrivateDevices)."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    unit = (root / "systemd" / join_service.UNIT_NAME).read_text()
    directives = {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in unit.splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }
    for key in (
        "PrivateTmp",
        "ProtectKernelModules",
        "ProtectKernelLogs",
        "ProtectKernelTunables",
        "ProtectControlGroups",
        "RestrictSUIDSGID",
        "RestrictRealtime",
        "RestrictNamespaces",
        "LockPersonality",
    ):
        assert directives.get(key) == "yes", key
    assert directives.get("ProtectHome") == "read-only"
    for key in ("ProtectClock", "ProtectSystem", "NoNewPrivileges", "PrivateDevices"):
        assert key not in directives, f"{key} would break a join"
