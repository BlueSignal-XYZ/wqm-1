"""The setup join can take ~150 s. The HTTP request must not hold that long.

PR #153 finding 3: ``join_network`` waits for a scan, then up to three 40 s
nmcli attempts. The phone is on the hotspot that goes down, so the page
must start the join and poll a status endpoint instead of one long POST.
"""

import threading
import time

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

    def test_joining_page_explains_a_dropped_hotspot(self, tmp_path, mock_hardware, monkeypatch):
        from utils import netctl

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
        assert "Reconnect to this unit's Wi-Fi, or check BlueSignal Cloud." in page
        assert "r.status === 403" in page or "status === 403" in page
        release.set()
        worker.join(timeout=1)
