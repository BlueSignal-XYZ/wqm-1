"""
Port 80 captive portal.

A phone that joins WQM1-xxxx must get a captive response on port 80, not a
redirect to :8080, and that listener must close once the unit joins the
site network. These tests bind 127.0.0.1 only. Nothing here opens wlan0.
"""

from __future__ import annotations

import http.client
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from utils.captive_portal import (
    PORTAL_URL,
    PROBE_HOSTS,
    PROBE_PATHS,
    READY_PATH,
    STARTING_UP_TEXT,
    UNAVAILABLE_HTML_BUDGET,
    arm_portal,
    backend_ready,
    classify_request,
    disarm_portal,
    dnsmasq_shared_config,
    ensure_dnsmasq_conf,
    portal_should_bind,
    portal_unit_id,
    prefers_html,
    read_mode,
    rewrite_location,
    run_loop,
    unavailable_html,
    unavailable_plain,
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
        self._answer(with_body=True)

    def do_HEAD(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._answer(with_body=False)

    def _answer(self, with_body: bool) -> None:
        type(self).hits.append(self.path)
        if self.path.startswith("/setup"):
            body = b"setup-page"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if with_body:
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


def _request(
    port: int,
    path: str,
    host: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
) -> http.client.HTTPResponse:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    sent = {"Host": host, "Connection": "close"}
    if headers:
        sent.update(headers)
    conn.request(method, path, headers=sent)
    resp = conn.getresponse()
    resp.body = resp.read()  # type: ignore[attr-defined]
    conn.close()
    return resp


def _get(port: int, path: str, host: str) -> http.client.HTTPResponse:
    return _request(port, path, host)


@pytest.fixture
def down_portal():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    closed = sock.getsockname()[1]
    sock.close()
    from utils.captive_portal import make_server

    portal = make_server("127.0.0.1", 0, backend=closed)
    thread = threading.Thread(target=portal.serve_forever, daemon=True)
    thread.start()
    try:
        yield portal
    finally:
        portal.shutdown()
        thread.join(timeout=2)


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

    def test_a_down_service_window_does_not_loop_the_setup_page(self, down_portal):
        resp = _get(down_portal.server_address[1], "/setup/", "192.168.4.1")
        assert resp.status == 503
        assert b"Success" not in resp.body  # type: ignore[attr-defined]
        assert resp.getheader("Location") is None
        assert resp.getheader("Retry-After") == "2"
        assert resp.getheader("Cache-Control") == "no-store"

    def test_location_rewrite_strips_the_service_window_port(self):
        assert rewrite_location("http://192.168.4.1:8080/setup/", 8080) == PORTAL_URL
        assert rewrite_location("http://127.0.0.1:8080/setup/pin", 8080) == (
            "http://192.168.4.1/setup/pin"
        )
        assert rewrite_location("/setup/", 8080) == "/setup/"


class TestDnsmasqConfig:
    def test_dedicated_dnsmasq_keeps_wildcard_and_option_114(self, tmp_path):
        from utils.captive_portal import DNSMASQ_CONF, dnsmasq_ap_config

        shared = dnsmasq_shared_config()
        text = dnsmasq_ap_config()
        assert "address=/#/192.168.4.1\n" in shared
        assert 'dhcp-option=114,"http://192.168.4.1/setup/"\n' in shared
        assert shared in text
        assert "interface=wlan0\n" in text
        assert "bind-interfaces\n" in text
        assert "listen-address=192.168.4.1\n" in text
        assert "dhcp-range=192.168.4.10,192.168.4.254,255.255.255.0,12h\n" in text
        assert "dhcp-option=3,192.168.4.1\n" in text
        assert "dhcp-option=6,192.168.4.1\n" in text
        assert ":8080" not in text
        assert str(DNSMASQ_CONF) == "/run/wqm1/dnsmasq-ap.conf"
        path = tmp_path / "dnsmasq-ap.conf"
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
        monkeypatch.setattr(netctl, "reach_gateway_or_dns", lambda *a, **k: fake.reach)
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


class TestUnavailablePage:
    def test_html_is_branded_and_under_the_size_budget(self):
        body = unavailable_html("WQM-10001")
        text = body.decode()
        assert len(body) < UNAVAILABLE_HTML_BUDGET
        assert len(unavailable_html("W" * 64)) < UNAVAILABLE_HTML_BUDGET
        assert "BlueSignal" in text
        assert "Your WQM-1 is starting up." in text
        assert "This takes about 2–3 minutes. Keep this page open." in text
        assert "WQM-10001" in text
        assert "https://" not in text
        assert "http://" not in text
        assert "<link " not in text
        assert "src=" not in text
        assert "cdn" not in text.lower()

    def test_html_auto_retries_setup_and_polls_ready(self):
        text = unavailable_html().decode()
        assert 'http-equiv="refresh"' in text
        assert 'content="3;url=/setup/"' in text
        assert 'fetch("/ready"' in text
        assert 'location.replace("/setup/")' in text

    def test_plain_text_stays_readable_without_markup(self):
        body = unavailable_plain("WQM-10001")
        assert body.startswith(STARTING_UP_TEXT.encode())
        assert b"Unit: WQM-10001\n" in body
        assert b"<" not in body

    def test_prefers_html_for_browsers_and_plain_for_curl(self):
        assert prefers_html("text/html,application/xhtml+xml") is True
        assert prefers_html("*/*") is True
        assert prefers_html("") is True
        assert prefers_html("text/plain") is False
        assert prefers_html("*/*", "curl/8.6.0") is False
        assert prefers_html("text/html", "curl/8.6.0") is False

    def test_portal_unit_id_omits_the_all_zero_fallback(self, monkeypatch):
        monkeypatch.setattr("utils.captive_portal.get_device_id", lambda: "WQM-10001")
        assert portal_unit_id() == "WQM-10001"
        monkeypatch.setattr(
            "utils.captive_portal.get_device_id",
            lambda: "BS-WQM1-000000000000",
        )
        assert portal_unit_id() == ""

        def _boom() -> str:
            raise OSError("no id")

        monkeypatch.setattr("utils.captive_portal.get_device_id", _boom)
        assert portal_unit_id() == ""

    def test_html_response_from_a_phone_accept_header(self, down_portal, monkeypatch):
        monkeypatch.setattr("utils.captive_portal.portal_unit_id", lambda: "WQM-10001")
        resp = _request(
            down_portal.server_address[1],
            "/setup/",
            "192.168.4.1",
            headers={"Accept": "text/html"},
        )
        assert resp.status == 503
        assert resp.getheader("Retry-After") == "2"
        assert resp.getheader("Cache-Control") == "no-store"
        assert "text/html" in (resp.getheader("Content-Type") or "")
        body = resp.body  # type: ignore[attr-defined]
        assert b"BlueSignal" in body
        assert b"Your WQM-1 is starting up." in body
        assert b"Keep this page open." in body
        assert b"WQM-10001" in body
        assert b'http-equiv="refresh"' in body
        assert b'fetch("/ready"' in body
        assert len(body) < UNAVAILABLE_HTML_BUDGET

    def test_curl_gets_plain_text(self, down_portal, monkeypatch):
        monkeypatch.setattr("utils.captive_portal.portal_unit_id", lambda: "WQM-10001")
        resp = _request(
            down_portal.server_address[1],
            "/setup/",
            "192.168.4.1",
            headers={"User-Agent": "curl/8.6.0", "Accept": "*/*"},
        )
        assert resp.status == 503
        assert resp.getheader("Retry-After") == "2"
        assert resp.getheader("Cache-Control") == "no-store"
        assert "text/plain" in (resp.getheader("Content-Type") or "")
        body = resp.body  # type: ignore[attr-defined]
        assert STARTING_UP_TEXT.encode() in body
        assert b"Unit: WQM-10001" in body
        assert b"<html" not in body

    def test_head_keeps_headers_and_has_no_body(self, down_portal):
        resp = _request(
            down_portal.server_address[1],
            "/setup/",
            "192.168.4.1",
            method="HEAD",
            headers={"Accept": "text/html"},
        )
        assert resp.status == 503
        assert resp.getheader("Retry-After") == "2"
        assert resp.getheader("Cache-Control") == "no-store"
        assert "text/html" in (resp.getheader("Content-Type") or "")
        assert int(resp.getheader("Content-Length") or "0") > 0
        assert resp.body == b""  # type: ignore[attr-defined]

    def test_ready_is_503_while_the_service_window_is_down(self, down_portal):
        resp = _request(down_portal.server_address[1], READY_PATH, "192.168.4.1")
        assert resp.status == 503
        assert resp.getheader("Retry-After") == "2"
        assert resp.getheader("Cache-Control") == "no-store"
        assert resp.body == b""  # type: ignore[attr-defined]
        assert backend_ready(down_portal.backend_port) is False

    def test_ready_is_204_once_the_service_window_answers(self, servers):
        portal, _backend = servers
        resp = _request(portal.server_address[1], READY_PATH, "192.168.4.1")
        assert resp.status == 204
        assert resp.getheader("Cache-Control") == "no-store"
        assert resp.body == b""  # type: ignore[attr-defined]
        assert backend_ready(portal.backend_port) is True

    def test_ready_head_stays_empty(self, down_portal):
        resp = _request(
            down_portal.server_address[1],
            READY_PATH,
            "192.168.4.1",
            method="HEAD",
        )
        assert resp.status == 503
        assert resp.getheader("Content-Length") == "0"
        assert resp.body == b""  # type: ignore[attr-defined]
