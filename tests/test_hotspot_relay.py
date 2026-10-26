"""The phone's captive sheet reaches setup through the port-80 relay.

`utils.captive_portal` listens on 192.168.4.1:80 and passes each request to
the Service Window from 127.0.0.1. Before the relay mark, `from_hotspot()`
saw 127.0.0.1, not the hotspot subnet, so every setup page loaded through
the sheet answered "no local sign-in". The other tests monkeypatch
`from_hotspot`, which is why they never saw it.

The mark also keeps a phone from counting as the bench SSH tunnel, which
arrives from loopback too.
"""

import http.client
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from tests.test_service_window_setup import make_app


@pytest.fixture
def real_hotspot(monkeypatch):
    """Hotspot up, not a virtual unit: `from_hotspot()` runs its real checks."""
    from service_window import captive
    from utils import netsim

    monkeypatch.setattr(captive, "hotspot_up", lambda: True)
    monkeypatch.setattr(netsim, "is_virtual", lambda: False)


def _get(app, path, remote, headers=None):
    with app.test_client() as c:
        return c.get(path, headers=headers or {}, environ_base={"REMOTE_ADDR": remote})


def _post(app, path, remote, headers=None):
    with app.test_client() as c:
        return c.post(path, headers=headers or {}, environ_base={"REMOTE_ADDR": remote})


RELAYED = {"X-WQM1-Relay": "hotspot"}


class TestRelayedSetupRequests:
    def test_relayed_request_reaches_the_setup_page(self, tmp_path, mock_hardware, real_hotspot):
        app = make_app(tmp_path, mock_hardware)
        r = _get(app, "/setup/network", "127.0.0.1", RELAYED)
        assert r.status_code != 403
        assert b"no local sign-in" not in r.data

    def test_direct_hotspot_client_reaches_the_setup_page(
        self, tmp_path, mock_hardware, real_hotspot
    ):
        app = make_app(tmp_path, mock_hardware)
        r = _get(app, "/setup/network", "192.168.4.23")
        assert r.status_code != 403

    def test_loopback_without_the_mark_is_not_the_hotspot(self, tmp_path, mock_hardware):
        from service_window import captive

        app = make_app(tmp_path, mock_hardware)
        with app.test_request_context("/", environ_base={"REMOTE_ADDR": "127.0.0.1"}):
            assert not captive._relayed_from_hotspot()

    def test_the_mark_from_the_home_network_counts_for_nothing(
        self, tmp_path, mock_hardware, real_hotspot
    ):
        app = make_app(tmp_path, mock_hardware)
        assert _get(app, "/setup/network", "10.0.0.5", RELAYED).status_code == 403

    def test_relayed_requests_reach_setup_pages_only(self, tmp_path, mock_hardware, real_hotspot):
        app = make_app(tmp_path, mock_hardware, setup_completed=True)
        assert _get(app, "/settings/", "127.0.0.1", RELAYED).status_code == 403

    def test_nothing_is_relayed_while_the_hotspot_is_down(
        self, tmp_path, mock_hardware, monkeypatch
    ):
        from service_window import captive
        from utils import netsim

        monkeypatch.setattr(captive, "hotspot_up", lambda: False)
        monkeypatch.setattr(netsim, "is_virtual", lambda: False)
        app = make_app(tmp_path, mock_hardware)
        assert _get(app, "/setup/network", "127.0.0.1", RELAYED).status_code == 403

    # Cursor security review on #153 (b35f319): a phone on the hotspot that
    # keeps cellular can be made to POST from a page on another site.

    def test_a_cross_site_post_from_a_hotspot_phone_is_refused(
        self, tmp_path, mock_hardware, real_hotspot
    ):
        app = make_app(tmp_path, mock_hardware)
        form = {"action": "join", "ssid": "Evil", "password": "hunter22"}
        for remote, extra in (("127.0.0.1", RELAYED), ("192.168.4.23", {})):
            for headers in (
                {"Origin": "https://evil.example"},
                {"Sec-Fetch-Site": "cross-site"},
            ):
                with app.test_client() as c:
                    r = c.post(
                        "/setup/network",
                        data=form,
                        headers={"Host": "192.168.4.1", **extra, **headers},
                        environ_base={"REMOTE_ADDR": remote},
                    )
                # Directly from the setup network, 8080 is closed outright
                # (install plan §3); through the relay the gate refuses it.
                assert r.status_code == (404 if remote.startswith("192.168.4.") else 403), (
                    remote,
                    headers,
                )

    def test_the_setup_page_itself_still_posts(self, tmp_path, mock_hardware, real_hotspot):
        app = make_app(tmp_path, mock_hardware)
        same_origin = {
            "Host": "192.168.4.1",
            "Origin": "http://192.168.4.1",
            "Sec-Fetch-Site": "same-origin",
        }
        with app.test_client() as c:
            r = c.post(
                "/setup/network",
                data={"action": "rescan"},
                headers={**RELAYED, **same_origin},
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
            )
        assert r.status_code != 403
        assert b"no local sign-in" not in r.data

    def test_a_hotspot_phone_cannot_replace_the_units_keys(
        self, tmp_path, mock_hardware, real_hotspot
    ):
        """Cursor security review on #153 (42e9d86): the recovery hotspot comes
        back whenever Wi-Fi is lost, so /setup/cloud must not write keys from
        it. Saving a key there is the bench override only."""
        import yaml

        app = make_app(tmp_path, mock_hardware, setup_completed=True)
        form = {"api_key": "k" * 20, "app_key": "a" * 32, "app_eui": "b" * 16}
        before = (tmp_path / "config.yaml").read_text()
        for remote, extra in (("127.0.0.1", RELAYED), ("192.168.4.23", {})):
            with app.test_client() as c:
                r = c.post(
                    "/setup/cloud",
                    data=form,
                    headers={"Host": "192.168.4.1", **extra},
                    environ_base={"REMOTE_ADDR": remote},
                )
            # Directly from the setup network, 8080 is closed outright
            # (install plan §3); through the relay the gate refuses it.
            assert r.status_code == (404 if remote.startswith("192.168.4.") else 403), remote
        assert (tmp_path / "config.yaml").read_text() == before
        assert yaml.safe_load(before).get("api_key") != "k" * 20

    def test_a_hotspot_phone_can_still_continue_past_the_cloud_page(
        self, tmp_path, mock_hardware, real_hotspot
    ):
        app = make_app(tmp_path, mock_hardware)
        with app.test_client() as c:
            r = c.post(
                "/setup/cloud",
                data={"continue": "1"},
                headers={"Host": "192.168.4.1", **RELAYED},
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
            )
        assert r.status_code == 302
        assert r.headers["Location"].endswith("/setup/done")


class TestBenchTunnel:
    """Decision 2 (24 Oct 2026): staff reach the Service Window at the bench
    over ``ssh -L 8080:127.0.0.1:8080 pi@<unit>``. That request arrives from
    loopback with no relay mark."""

    def test_the_tunnel_reaches_owner_pages(self, tmp_path, mock_hardware, real_hotspot):
        app = make_app(tmp_path, mock_hardware, setup_completed=True)
        assert _get(app, "/settings/", "127.0.0.1").status_code == 200
        # These two reach their handler; with no bus they may answer 502.
        for path in ("/calibrate/live.json", "/rs485/"):
            r = _get(app, path, "127.0.0.1")
            assert r.status_code != 403, path
            assert b"no local sign-in" not in r.data, path

    def test_the_tunnel_can_finish_an_unfinished_unit(self, tmp_path, mock_hardware, monkeypatch):
        """An Ethernet or LTE unit never raises its hotspot, so the tunnel is
        the one local way to reach /setup/done."""
        from service_window import captive
        from utils import netsim

        monkeypatch.setattr(captive, "hotspot_up", lambda: False)
        monkeypatch.setattr(netsim, "is_virtual", lambda: False)
        app = make_app(tmp_path, mock_hardware)
        assert _get(app, "/setup/done", "127.0.0.1").status_code != 403

    def test_a_relayed_phone_is_not_the_tunnel(self, tmp_path, mock_hardware, real_hotspot):
        app = make_app(tmp_path, mock_hardware, setup_completed=True)
        assert _get(app, "/settings/", "127.0.0.1", RELAYED).status_code == 403

    def test_any_relay_mark_is_not_the_tunnel(self, tmp_path, mock_hardware, real_hotspot):
        app = make_app(tmp_path, mock_hardware, setup_completed=True)
        r = _get(app, "/settings/", "127.0.0.1", {"X-WQM1-Relay": "anything"})
        assert r.status_code == 403

    def test_the_home_network_is_not_the_tunnel(self, tmp_path, mock_hardware, real_hotspot):
        app = make_app(tmp_path, mock_hardware, setup_completed=True)
        assert _get(app, "/settings/", "10.0.0.5").status_code == 403

    # Cursor security review on #157 (ce248b1): while the tunnel is up, any
    # page in the staff browser can reach 127.0.0.1:8080 from loopback.

    def test_a_cross_site_post_through_the_tunnel_is_refused(
        self, tmp_path, mock_hardware, real_hotspot
    ):
        app = make_app(tmp_path, mock_hardware, setup_completed=True)
        for headers in (
            {"Origin": "https://evil.example"},
            {"Sec-Fetch-Site": "cross-site"},
            {"Origin": "null"},
        ):
            r = _post(app, "/relays/set", "127.0.0.1", headers)
            assert r.status_code == 403, headers
            assert b"no local sign-in" in r.data, headers

    def test_a_rebound_host_name_is_refused(self, tmp_path, mock_hardware, monkeypatch):
        """With the hotspot down (a unit online at its site), a DNS-rebinding
        page reaches the tunnel under its own name."""
        from service_window import captive
        from utils import netsim

        monkeypatch.setattr(captive, "hotspot_up", lambda: False)
        monkeypatch.setattr(netsim, "is_virtual", lambda: False)
        app = make_app(tmp_path, mock_hardware, setup_completed=True)
        r = _get(app, "/settings/", "127.0.0.1", {"Host": "evil.example:8080"})
        assert r.status_code == 403
        assert b"no local sign-in" in r.data
        assert _get(app, "/settings/", "127.0.0.1").status_code == 200

    def test_the_tunnel_tab_and_bench_scripts_still_post(
        self, tmp_path, mock_hardware, real_hotspot
    ):
        app = make_app(tmp_path, mock_hardware, setup_completed=True)
        same_origin = {
            "Host": "127.0.0.1:8080",
            "Origin": "http://127.0.0.1:8080",
            "Sec-Fetch-Site": "same-origin",
        }
        for headers in (same_origin, {"Host": "[::1]:8080"}, {}):
            r = _post(app, "/relays/set", "127.0.0.1", headers)
            assert r.status_code != 403, headers
            assert b"no local sign-in" not in r.data, headers


class _Echo(BaseHTTPRequestHandler):
    seen: dict[str, str] = {}

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
        type(self).seen = {k.lower(): v for k, v in self.headers.items()}
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_a):
        pass


class TestRelayMarksEveryRequest:
    def test_relay_sets_the_mark_and_replaces_a_forged_one(self):
        from utils import captive_portal

        backend = HTTPServer(("127.0.0.1", 0), _Echo)
        portal = captive_portal.make_server("127.0.0.1", 0, backend=backend.server_port)
        threads = [
            threading.Thread(target=s.serve_forever, daemon=True, name=f"wqm1-test-{n}")
            for n, s in (("backend", backend), ("portal", portal))
        ]
        for t in threads:
            t.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", portal.server_port, timeout=5)
            conn.request(
                "GET",
                "/setup/network",
                headers={"Host": "192.168.4.1", "x-wqm1-relay": "forged"},
            )
            resp = conn.getresponse()
            resp.read()
            conn.close()
            assert resp.status == 200
            assert _Echo.seen.get("x-wqm1-relay") == "hotspot"
        finally:
            portal.shutdown()
            backend.shutdown()
            portal.server_close()
            backend.server_close()
