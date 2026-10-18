"""
Port 80 captive portal.

A phone that joins WQM1-xxxx must get a captive response on port 80, not a
redirect to :8080, and that listener must close once the unit joins the
site network. These tests bind 127.0.0.1 only. Nothing here opens wlan0.
"""

from __future__ import annotations

import http.client
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from utils.captive_portal import (
    PORTAL_URL,
    PROBE_HOSTS,
    PROBE_PATHS,
    arm_portal,
    classify_request,
    disarm_portal,
    dnsmasq_shared_config,
    ensure_dnsmasq_conf,
    portal_should_bind,
    read_mode,
    rewrite_location,
    run_loop,
)

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def virtual_net(tmp_path, monkeypatch):
    path = tmp_path / "netsim.json"
    monkeypatch.setenv("WQM1_VIRTUAL_NET", str(path))
    return path


# One case per vendor probe the sheet depends on. Host plus path, because
# some checks are recognised by the path and some by the name.
PROBE_CASES = [
    ("/hotspot-detect.html", "captive.apple.com"),
    ("/library/test/success.html", "www.apple.com"),
    ("/hotspot-detect.html", "www.appleiphonecell.com"),
    ("/generate_204", "connectivitycheck.gstatic.com"),
    ("/generate_204", "connectivitycheck.android.com"),
    ("/gen_204", "clients3.google.com"),
    ("/generate_204", "play.googleapis.com"),
    ("/connecttest.txt", "www.msftconnecttest.com"),
    ("/ncsi.txt", "www.msftncsi.com"),
    ("/redirect", "www.msftconnecttest.com"),
    ("/canonical.html", "detectportal.firefox.com"),
    ("/success.txt", "detectportal.firefox.com"),
    ("/check_network_status.txt", "connectivitycheck.gstatic.com"),
]


class _Backend(BaseHTTPRequestHandler):
    """Stand-in for the Service Window. Redirects advertise its own port."""

    hits: list[str] = []

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        type(self).hits.append(self.path)
        if self.path.startswith("/setup"):
            body = b"setup-page"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        port = self.server.server_address[1]
        self.send_response(302)
        self.send_header("Location", f"http://127.0.0.1:{port}/setup/")
        self.send_header("Content-Length", "0")
        self.end_headers()


@pytest.fixture
def servers():
    _Backend.hits = []
    backend = HTTPServer(("127.0.0.1", 0), _Backend)
    from utils.captive_portal import make_server

    portal = make_server("127.0.0.1", 0, backend=backend.server_address[1])
    threads = [
        threading.Thread(target=backend.serve_forever, daemon=True),
        threading.Thread(target=portal.serve_forever, daemon=True),
    ]
    for thread in threads:
        thread.start()
    yield portal, backend
    portal.shutdown()
    backend.shutdown()
    for thread in threads:
        thread.join(timeout=2)


def _get(port: int, path: str, host: str) -> http.client.HTTPResponse:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    conn.request("GET", path, headers={"Host": host, "Connection": "close"})
    resp = conn.getresponse()
    resp.body = resp.read()  # type: ignore[attr-defined]
    conn.close()
    return resp


class TestProbeResponses:
    @pytest.mark.parametrize(("path", "host"), PROBE_CASES)
    def test_each_probe_redirects_to_port_80_setup(self, servers, path, host):
        portal, _backend = servers
        resp = _get(portal.server_address[1], path, host)
        assert resp.status == 302
        assert resp.getheader("Location") == PORTAL_URL
        assert ":8080" not in (resp.getheader("Location") or "")
        assert b"Success" not in resp.body  # type: ignore[attr-defined]
        assert b"Microsoft Connect Test" not in resp.body  # type: ignore[attr-defined]
        assert resp.status != 204

    def test_every_probe_path_and_host_is_classified_as_a_redirect(self):
        for path in PROBE_PATHS:
            assert classify_request(path, "192.168.4.1") == "redirect"
        for host in PROBE_HOSTS:
            assert classify_request("/", host) == "redirect"

    def test_redirect_target_is_the_setup_page_on_port_80(self):
        assert PORTAL_URL == "http://192.168.4.1/setup/"
        assert ":8080" not in PORTAL_URL

    def test_a_typed_http_url_redirects_to_the_same_page(self, servers):
        portal, backend = servers
        resp = _get(portal.server_address[1], "/", "example.com")
        assert resp.status == 302
        assert resp.getheader("Location") == PORTAL_URL
        assert _Backend.hits == []

    def test_the_hotspot_address_is_proxied_and_stays_off_8080(self, servers):
        portal, _backend = servers
        page = _get(portal.server_address[1], "/setup/", "192.168.4.1")
        assert page.status == 200
        assert page.body == b"setup-page"  # type: ignore[attr-defined]
        bounced = _get(portal.server_address[1], "/", "192.168.4.1")
        assert bounced.status == 302
        assert bounced.getheader("Location") == "http://192.168.4.1/setup/"
        assert ":8080" not in (bounced.getheader("Location") or "")

    def test_a_down_service_window_does_not_loop_the_setup_page(self):
        import socket

        from utils.captive_portal import make_server

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        closed = sock.getsockname()[1]
        sock.close()
        portal = make_server("127.0.0.1", 0, backend=closed)
        thread = threading.Thread(target=portal.serve_forever, daemon=True)
        thread.start()
        try:
            resp = _get(portal.server_address[1], "/setup/", "192.168.4.1")
        finally:
            portal.shutdown()
            thread.join(timeout=2)
        assert resp.status == 503
        assert b"Success" not in resp.body  # type: ignore[attr-defined]
        assert resp.getheader("Location") is None

    def test_location_rewrite_strips_the_service_window_port(self):
        assert rewrite_location("http://192.168.4.1:8080/setup/", 8080) == PORTAL_URL
        assert rewrite_location("http://127.0.0.1:8080/setup/pin", 8080) == (
            "http://192.168.4.1/setup/pin"
        )
        assert rewrite_location("/setup/", 8080) == "/setup/"


class TestDnsmasqConfig:
    def test_shared_mode_wildcard_and_option_114(self, tmp_path):
        text = dnsmasq_shared_config()
        assert "address=/#/192.168.4.1\n" in text
        assert 'dhcp-option=114,"http://192.168.4.1/setup/"\n' in text
        assert ":8080" not in text
        assert "dnsmasq-shared.d" in str(
            __import__("utils.captive_portal", fromlist=["DNSMASQ_CONF"]).DNSMASQ_CONF
        )
        path = tmp_path / "dnsmasq-shared.d" / "wqm1-captive.conf"
        written = ensure_dnsmasq_conf(path)
        assert written["ok"] is True
        assert path.read_text(encoding="utf-8") == text
        again = ensure_dnsmasq_conf(path)
        assert again["unchanged"] is True

    def test_setup_enables_the_port_80_unit(self):
        setup = (ROOT / "setup.sh").read_text()
        unit = (ROOT / "systemd" / "bluesignal-captive-portal.service").read_text()
        assert "bluesignal-captive-portal.service" in setup
        assert "systemctl enable bluesignal-captive-portal.service" in setup
        assert "AmbientCapabilities=CAP_NET_BIND_SERVICE" in unit
        assert "User=pi" in unit
        assert not any(line.startswith("NoNewPrivileges=") for line in unit.splitlines())
        assert "ExecStart=/usr/bin/python3 -m utils.captive_portal" in unit


class TestTeardown:
    def test_station_mode_does_not_bind_even_if_the_address_is_up(self, tmp_path, monkeypatch):
        mode = tmp_path / "mode"
        monkeypatch.setenv("WQM1_PORTAL_MODE_FILE", str(mode))
        arm_portal()
        assert portal_should_bind(read_mode(), True) is True
        disarm_portal("station")
        assert read_mode() == "down: station"
        assert portal_should_bind(read_mode(), True) is False
        assert portal_should_bind("up", False) is False

    def test_join_disarms_and_a_failed_join_arms_again(self, virtual_net, monkeypatch, tmp_path):
        from utils import netctl, netsim

        mode = tmp_path / "mode"
        monkeypatch.setenv("WQM1_PORTAL_MODE_FILE", str(mode))
        assert netctl.start_ap("WQM1-0001", "cedar-river-42")["ok"]
        assert read_mode() == "up"
        joined = netctl.join_network(
            netsim.DEMO_HOME_SSID,
            netsim.DEMO_HOME_PASSWORD,
            "WQM1-0001",
            "cedar-river-42",
        )
        assert joined["connected"] is True
        assert read_mode() == "down: station"
        assert portal_should_bind(read_mode(), True) is False

        netctl.start_ap("WQM1-0001", "cedar-river-42")
        failed = netctl.join_network("Nowhere", "x", "WQM1-0001", "cedar-river-42")
        assert failed["connected"] is False
        assert failed["ap_restored"] is True
        assert read_mode() == "up"
        assert netsim.ap_active()

    def test_nm_join_disarms_and_a_failed_join_rearms(self, monkeypatch, tmp_path):
        from tests.test_netctl import FakeNm
        from utils import netctl

        mode = tmp_path / "mode"
        monkeypatch.setenv("WQM1_PORTAL_MODE_FILE", str(mode))
        monkeypatch.delenv("WQM1_VIRTUAL_NET", raising=False)
        fake = FakeNm()
        fake.ap = True
        monkeypatch.setattr(netctl, "_run", fake.run)
        arm_portal()
        joined = netctl.join_network(
            "PondHouse",
            "hunter22",
            ap_ssid="WQM1-0001",
            ap_passphrase="river-cedar-42",
            sleep=lambda _s: None,
        )
        assert joined["connected"] is True
        assert read_mode() == "down: station"

        fake.station = False
        fake.ap = True
        fake.join_rc = 4
        failed = netctl.join_network(
            "PondHouse",
            "wrong",
            ap_ssid="WQM1-0001",
            ap_passphrase="river-cedar-42",
            sleep=lambda _s: None,
        )
        assert failed["ap_restored"] is True
        assert read_mode() == "up"

    def test_listener_closes_when_the_station_joins(self, tmp_path, monkeypatch):
        mode = tmp_path / "mode"
        monkeypatch.setenv("WQM1_PORTAL_MODE_FILE", str(mode))
        arm_portal()
        started = threading.Event()
        holder: dict[str, object] = {}

        def on_bind(server: object) -> None:
            holder["server"] = server
            started.set()

        thread = threading.Thread(
            target=run_loop,
            kwargs={
                "bind": ("127.0.0.1", 0),
                "address_up": lambda: True,
                "on_bind": on_bind,
                "poll_s": 0.05,
            },
            daemon=True,
        )
        thread.start()
        assert started.wait(3)
        server = holder["server"]
        port = server.server_address[1]  # type: ignore[attr-defined]
        open_resp = _get(port, "/hotspot-detect.html", "captive.apple.com")
        assert open_resp.status == 302
        assert open_resp.getheader("Location") == PORTAL_URL

        disarm_portal("station")
        deadline = threading.Event()

        def _wait_closed() -> None:
            import time

            end = time.monotonic() + 3
            while time.monotonic() < end:
                try:
                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=0.2)
                    conn.request("GET", "/", headers={"Host": "192.168.4.1"})
                    conn.getresponse().read()
                    conn.close()
                except OSError:
                    deadline.set()
                    return
                time.sleep(0.05)

        _wait_closed()
        assert deadline.is_set()

    def test_flask_probes_use_the_same_port_80_target(self, tmp_path, mock_hardware, monkeypatch):
        from service_window import captive
        from tests.test_service_window_setup import make_app

        monkeypatch.setattr(captive, "hotspot_up", lambda: True)
        app = make_app(tmp_path, mock_hardware)
        rules = {rule.rule for rule in app.url_map.iter_rules()}
        with app.test_client() as client:
            for path in PROBE_PATHS:
                assert path in rules
                response = client.get(path, headers={"Host": "captive.apple.com"})
                assert response.status_code == 302
                assert response.headers["Location"] == PORTAL_URL
                assert b"Success" not in response.data
