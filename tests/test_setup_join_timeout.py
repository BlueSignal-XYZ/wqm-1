"""The setup join can take ~150 s. The HTTP request must not hold that long.

PR #153 finding 3: ``join_network`` waits for a scan, then up to three 40 s
nmcli attempts. The phone is on the hotspot that goes down, so the page
must start the join and poll a status endpoint instead of one long POST.
"""

import shutil
import subprocess
import threading
import time
from pathlib import Path

from tests.test_service_window_setup import make_app


def _authed_client(tmp_path, mock_hardware, monkeypatch):
    from service_window import captive
    from utils import netctl, netinfo

    monkeypatch.setattr(captive, "from_hotspot", lambda: True)
    monkeypatch.setattr(netctl, "ap_active", lambda iface="wlan0": True)
    monkeypatch.setattr(
        netctl,
        "scan_networks",
        lambda iface="wlan0": [{"ssid": "PondHouse", "signal": 72, "secured": True}],
    )
    monkeypatch.setattr(netctl, "ap_credentials", lambda: ("WQM1-0001", "river-cedar-42"))
    monkeypatch.setattr(
        netinfo,
        "wifi_status",
        lambda: {"state": "down", "ssid": None, "rssi_dbm": None, "ip": "192.168.4.1"},
    )
    app = make_app(tmp_path, mock_hardware)
    client = app.test_client()
    with client.session_transaction() as sess:
        sess.clear()
    return client, app


class TestJoinDoesNotHoldTheHttpRequest:
    def test_page_returns_and_polls_while_join_runs(self, tmp_path, mock_hardware, monkeypatch):
        from utils import netctl

        started = threading.Event()
        release = threading.Event()

        def slow_join(*_a, **_k):
            started.set()
            assert release.wait(timeout=5)
            return {
                "ok": True,
                "connected": True,
                "ap_restored": False,
                "error": None,
                "ip": "192.168.1.50",
            }

        monkeypatch.setattr(netctl, "join_network", slow_join)
        client, _app = _authed_client(tmp_path, mock_hardware, monkeypatch)

        posted: dict[str, object] = {}

        def _post() -> None:
            posted["response"] = client.post(
                "/setup/network",
                data={"action": "join", "ssid": "PondHouse", "password": "hunter22"},
            )

        worker = threading.Thread(target=_post, name="wqm1-test-join-post")
        worker.start()
        assert started.wait(timeout=2), "join_network was never called"
        # The HTTP handler must return while join_network is still blocked.
        worker.join(timeout=1.0)
        assert not worker.is_alive(), "POST /setup/network held the request until join finished"
        response = posted["response"]
        assert response.status_code == 200
        assert b"join-status" in response.data
        status = client.get("/setup/join-status")
        assert status.status_code == 200
        body = status.get_json()
        assert body["state"] == "joining"
        assert body["ssid"] == "PondHouse"

        release.set()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            body = client.get("/setup/join-status").get_json()
            if body and body.get("state") == "ok":
                break
            time.sleep(0.05)
        assert body["state"] == "ok"
        worker.join(timeout=1)

    def test_join_exception_marks_the_job_failed(self, tmp_path, mock_hardware, monkeypatch):
        from utils import netctl

        def boom(*_a, **_k):
            raise RuntimeError("nmcli missing")

        monkeypatch.setattr(netctl, "join_network", boom)
        client, app = _authed_client(tmp_path, mock_hardware, monkeypatch)
        response = client.post(
            "/setup/network",
            data={"action": "join", "ssid": "PondHouse", "password": "hunter22"},
        )
        assert response.status_code == 302
        assert response.headers["Location"].endswith("/setup/network")
        body = client.get("/setup/join-status").get_json()
        assert body["state"] == "fail"
        notice = app.config.get("LAST_JOIN") or {}
        assert notice.get("ssid") == "PondHouse"
        assert notice.get("error")
        # A later join must not be stuck on the polling page.
        monkeypatch.setattr(
            netctl,
            "join_network",
            lambda *_a, **_k: {
                "ok": True,
                "connected": True,
                "ap_restored": False,
                "error": None,
                "ip": "192.168.1.50",
            },
        )
        again = client.post(
            "/setup/network",
            data={"action": "join", "ssid": "PondHouse", "password": "hunter22"},
        )
        assert again.status_code == 200
        assert b"Joined PondHouse" in again.data

    def test_join_exception_brings_the_hotspot_back(self, tmp_path, mock_hardware, monkeypatch):
        """Bugbot on #157: an exception that escapes join_network skips its own
        hotspot restore, which strands the phone. The worker restores it."""
        from utils import netctl

        state = {"ap": True, "started": []}

        def boom(*_a, **_k):
            state["ap"] = False  # join_network took the hotspot down first
            raise RuntimeError("nmcli missing")

        client, app = _authed_client(tmp_path, mock_hardware, monkeypatch)
        monkeypatch.setattr(netctl, "join_network", boom)
        monkeypatch.setattr(netctl, "ap_active", lambda iface="wlan0": state["ap"])

        def start_ap(ssid, passphrase, iface="wlan0"):
            state["started"].append((ssid, passphrase))
            state["ap"] = True
            return {"ok": True}

        monkeypatch.setattr(netctl, "start_ap", start_ap)
        client.post(
            "/setup/network",
            data={"action": "join", "ssid": "PondHouse", "password": "hunter22"},
        )
        assert state["started"] == [("WQM1-0001", "river-cedar-42")]
        assert (app.config.get("LAST_JOIN") or {}).get("ap_restored") is True

    def test_setup_record_error_after_join_is_not_a_failed_join(
        self, tmp_path, mock_hardware, monkeypatch
    ):
        """Bugbot on #157: once the radio is on the site network, a config
        write error is logged, and the installer is not told to retry."""
        from service_window.routes import setup as setup_routes
        from utils import netctl

        client, app = _authed_client(tmp_path, mock_hardware, monkeypatch)
        monkeypatch.setattr(
            netctl,
            "join_network",
            lambda *_a, **_k: {
                "ok": True,
                "connected": True,
                "ap_restored": False,
                "error": None,
                "ip": "192.168.1.50",
            },
        )
        started = []
        monkeypatch.setattr(netctl, "start_ap", lambda *a, **k: started.append(a) or {"ok": True})

        def broken_finish(_ssid):
            raise OSError("config.yaml is read-only")

        monkeypatch.setattr(setup_routes, "_finish_setup", broken_finish)
        response = client.post(
            "/setup/network",
            data={"action": "join", "ssid": "PondHouse", "password": "hunter22"},
        )
        assert response.status_code == 200
        assert b"Joined PondHouse" in response.data
        assert client.get("/setup/join-status").get_json()["state"] == "ok"
        assert "LAST_JOIN" not in app.config
        assert started == []

    def test_a_raising_register_kick_still_records_setup(
        self, tmp_path, mock_hardware, monkeypatch
    ):
        """Bugbot on #157 (ce248b1): the kick and the setup record shared one
        try, so a kick that raised left setup unrecorded."""
        from service_window.routes import setup as setup_routes
        from utils import netctl

        client, _app = _authed_client(tmp_path, mock_hardware, monkeypatch)
        monkeypatch.setattr(
            netctl,
            "join_network",
            lambda *_a, **_k: {
                "ok": True,
                "connected": True,
                "ap_restored": False,
                "error": None,
                "ip": "192.168.1.50",
            },
        )

        def broken_kick():
            raise OSError("config.yaml unreadable")

        recorded = []
        monkeypatch.setattr(setup_routes, "_kick_register", broken_kick)
        monkeypatch.setattr(
            setup_routes,
            "_finish_setup",
            lambda ssid: recorded.append(ssid) or {"forgotten": []},
        )
        client.post(
            "/setup/network",
            data={"action": "join", "ssid": "PondHouse", "password": "hunter22"},
        )
        assert recorded == ["PondHouse"]
        assert client.get("/setup/join-status").get_json()["state"] == "ok"

    def test_hotspot_restore_retries_a_settling_radio(self, tmp_path, mock_hardware, monkeypatch):
        """Bugbot on #157 (ce248b1): one start_ap was not enough while the
        radio settles. The restore uses join_network's AP_ATTEMPTS tries."""
        from service_window.routes import setup as setup_routes
        from utils import netctl

        state = {"ap": True, "calls": 0}

        def boom(*_a, **_k):
            state["ap"] = False
            raise RuntimeError("nmcli missing")

        def start_ap(ssid, passphrase, iface="wlan0"):
            state["calls"] += 1
            if state["calls"] < netctl.AP_ATTEMPTS:
                return {"ok": False}
            state["ap"] = True
            return {"ok": True}

        client, app = _authed_client(tmp_path, mock_hardware, monkeypatch)
        monkeypatch.setattr(netctl, "join_network", boom)
        monkeypatch.setattr(netctl, "ap_active", lambda iface="wlan0": state["ap"])
        monkeypatch.setattr(netctl, "start_ap", start_ap)
        monkeypatch.setattr(setup_routes, "_restore_sleep", lambda _s: None)
        client.post(
            "/setup/network",
            data={"action": "join", "ssid": "PondHouse", "password": "hunter22"},
        )
        assert state["calls"] == netctl.AP_ATTEMPTS
        assert (app.config.get("LAST_JOIN") or {}).get("ap_restored") is True

    def test_second_join_post_keeps_the_running_ssid(self, tmp_path, mock_hardware, monkeypatch):
        from utils import netctl

        started = threading.Event()
        release = threading.Event()

        def slow_join(ssid, *_a, **_k):
            started.set()
            assert release.wait(timeout=5)
            return {
                "ok": True,
                "connected": True,
                "ap_restored": False,
                "error": None,
                "ip": "192.168.1.50",
                "ssid": ssid,
            }

        monkeypatch.setattr(netctl, "join_network", slow_join)
        monkeypatch.setattr(
            netctl,
            "scan_networks",
            lambda iface="wlan0": [
                {"ssid": "PondHouse", "signal": 72, "secured": True},
                {"ssid": "Barn", "signal": 60, "secured": True},
            ],
        )
        client, _app = _authed_client(tmp_path, mock_hardware, monkeypatch)

        posted: dict[str, object] = {}

        def _post() -> None:
            posted["response"] = client.post(
                "/setup/network",
                data={"action": "join", "ssid": "PondHouse", "password": "hunter22"},
            )

        worker = threading.Thread(target=_post, name="wqm1-test-join-post")
        worker.start()
        assert started.wait(timeout=2)
        worker.join(timeout=1.0)
        assert not worker.is_alive()

        second = client.post(
            "/setup/network",
            data={"action": "join", "ssid": "Barn", "password": "hunter22"},
        )
        assert second.status_code == 200
        assert b"PondHouse" in second.data
        assert b"Joining Barn" not in second.data
        body = client.get("/setup/join-status").get_json()
        assert body["state"] == "joining"
        assert body["ssid"] == "PondHouse"
        release.set()
        worker.join(timeout=1)

    def test_joining_poll_treats_a_dropped_hotspot(self, tmp_path, mock_hardware, monkeypatch):
        """Runs joining-poll.js under node (the page only wires those functions)."""
        from utils import netctl

        node = shutil.which("node")
        assert node, "node is required to run joining-poll.js"

        started = threading.Event()
        release = threading.Event()

        def slow_join(*_a, **_k):
            started.set()
            release.wait(timeout=5)
            return {
                "ok": False,
                "connected": False,
                "ap_restored": True,
                "error": "x",
            }

        monkeypatch.setattr(netctl, "join_network", slow_join)
        client, _app = _authed_client(tmp_path, mock_hardware, monkeypatch)
        posted: dict[str, object] = {}

        def _post() -> None:
            posted["response"] = client.post(
                "/setup/network",
                data={"action": "join", "ssid": "PondHouse", "password": "hunter22"},
            )

        worker = threading.Thread(target=_post, name="wqm1-test-join-post")
        worker.start()
        assert started.wait(timeout=2)
        worker.join(timeout=1.0)
        page = posted["response"].get_data(as_text=True)
        assert "joining-poll.js" in page
        assert "poll.RECONNECT" in page
        script = (
            Path(__file__).resolve().parent.parent / "src/service_window/static/joining-poll.js"
        )
        result = subprocess.run(  # nosec B603 — node runs this repo's poll helper
            [
                node,
                "-e",
                (
                    "const p = require(process.argv[1]);"
                    "const forbiddenJson = {status: 403, headers: {get: () => 'application/json'}};"
                    "if (!p.droppedHotspot(forbiddenJson)) process.exit(2);"
                    "if (!p.droppedHotspot({status: 200, contentType: 'text/html'}))"
                    " process.exit(3);"
                    "const json = {status: 200, headers: {get: () => 'application/json'}};"
                    "if (p.droppedHotspot(json)) process.exit(4);"
                    "if (p.afterPoll(null, 88) !== 'retry') process.exit(5);"
                    "if (p.afterPoll(null, 89) !== 'reconnect') process.exit(6);"
                    "if (p.afterPoll({state: 'ok'}, 1) !== 'ok') process.exit(7);"
                    "if (p.afterPoll({state: 'fail'}, 1) !== 'fail') process.exit(8);"
                    "if (p.afterPoll({state: 'joining'}, 90) !== 'reconnect') process.exit(9);"
                    "if (p.RECONNECT.indexOf('BlueSignal Cloud') < 0) process.exit(10);"
                    "console.log('ok');"
                ),
                str(script),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr + result.stdout
        release.set()
        worker.join(timeout=1)
