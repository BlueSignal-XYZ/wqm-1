"""Captain, 2026-10-24 and 2026-10-25: no PIN anywhere. Setup is the hotspot
only. Owners use BlueSignal Cloud. The LAN page is closed.

These tests pin the review on PR #153 (findings 1–2) and Claude's 2026-10-25
review on PR #157. Each one fails when the matching fix is reverted.

Invariants:

- There is no PIN to set or enter. ``_SETTING_ALIASES`` does not map one.
- Setup pages are reachable only from this unit's own hotspot.
- After setup the home LAN does not get the Service Window.
- The hotspot comes up when ``wlan0`` has no usable station, not "only while
  the unit is offline". Ethernet and LTE are not that check.
- Hold this build off ``BS-WQM1-0000a92e4e7d`` until a later site visit or
  firmware push. No per-unit exemption in code.
"""

from pathlib import Path

import yaml

from service_window.app import create_app
from tests.remote import HOME_NETWORK, HOTSPOT, from_addr
from tests.test_service_window_setup import make_app

# Setup routes a phone on the unit's own hotspot may hit with no PIN.
# /setup/pin is intentionally absent: there is no PIN to set or enter.
_HOTSPOT_SETUP_PATHS = (
    "/setup/",
    "/setup/network",
    "/setup/card-status",
    "/setup/sensors",
    "/setup/done",
    "/setup/identity",
    "/setup/cloud",
    "/setup/join-status",
)

_LAN_CLOSED_PATHS = (
    "/setup/network",
    "/setup/sensors",
    "/setup/done",
    "/",
    "/settings/",
    "/login",
)


def _client(tmp_path, mock_hardware, monkeypatch, *, on_hotspot, setup_completed=True):
    from service_window import captive

    monkeypatch.setattr(captive, "from_hotspot", lambda: on_hotspot)
    app = make_app(tmp_path, mock_hardware, pin="1234", setup_completed=setup_completed)
    client = from_addr(app.test_client(), HOTSPOT if on_hotspot else HOME_NETWORK)
    with client.session_transaction() as sess:
        sess.clear()
    return client, app


def _no_pin_form(response) -> None:
    body = response.data
    assert b'name="pin"' not in body
    assert b"Enter PIN" not in body
    assert b"factory PIN" not in body
    assert "/login" not in (response.headers.get("Location") or "")


class TestHotspotSetupNeedsNoPin:
    def test_hotspot_client_reaches_setup_network_and_other_setup_routes(
        self, tmp_path, mock_hardware, monkeypatch
    ):
        client, _app = _client(tmp_path, mock_hardware, monkeypatch, on_hotspot=True)
        for path in _HOTSPOT_SETUP_PATHS:
            response = client.get(path)
            assert response.status_code in (200, 302), path
            location = response.headers.get("Location") or ""
            assert "/login" not in location, path
            _no_pin_form(response)


class TestLanHasNoLogin:
    def test_lan_client_does_not_reach_setup_and_gets_no_login(
        self, tmp_path, mock_hardware, monkeypatch
    ):
        client, _app = _client(tmp_path, mock_hardware, monkeypatch, on_hotspot=False)
        for path in _LAN_CLOSED_PATHS:
            response = client.get(path)
            assert response.status_code == 403, path
            _no_pin_form(response)

    def test_lan_login_does_not_accept_a_pin(self, tmp_path, mock_hardware, monkeypatch):
        client, _app = _client(tmp_path, mock_hardware, monkeypatch, on_hotspot=False)
        response = client.post("/login", data={"pin": "1234"})
        assert response.status_code == 403
        _no_pin_form(response)
        with client.session_transaction() as sess:
            assert not sess.get("pin_verified")
        saved = yaml.safe_load(Path(_app.config["CONFIG_PATH"]).read_text())
        assert (saved.get("service_window") or {}).get("pin") is None


class TestFactoryPinRefusalIsGone:
    def test_factory_pin_refused_gate_is_removed(self):
        from service_window import auth

        assert not hasattr(auth, "factory_pin_refused")

    def test_login_no_longer_refuses_factory_pin_by_name(
        self, tmp_path, mock_hardware, monkeypatch
    ):
        """The old gate's message must not come back; LAN login is closed."""
        client, _app = _client(tmp_path, mock_hardware, monkeypatch, on_hotspot=False)
        response = client.post("/login", data={"pin": "1234"})
        assert b"factory PIN" not in response.data
        assert b"only works on its own" not in response.data
        assert response.status_code == 403
        with client.session_transaction() as sess:
            assert not sess.get("pin_verified")


class TestPinCodeIsGone:
    def test_setup_pin_route_is_gone(self, tmp_path, mock_hardware, monkeypatch):
        client, _app = _client(tmp_path, mock_hardware, monkeypatch, on_hotspot=True)
        assert client.get("/setup/pin").status_code == 404
        posted = client.post("/setup/pin", data={"pin": "8642", "pin_confirm": "8642"})
        assert posted.status_code == 404

    def test_factory_pin_and_lockout_helpers_are_gone(self):
        from service_window import auth
        from service_window.routes import setup as setup_mod

        assert not hasattr(auth, "_record_failure")
        assert not hasattr(auth, "_is_rate_limited")
        assert not hasattr(auth, "_attempts")
        assert not hasattr(setup_mod, "_FACTORY_PIN")
        assert not hasattr(setup_mod, "pin")

    def test_pin_verified_session_does_not_open_the_lan(self, tmp_path, mock_hardware, monkeypatch):
        client, _app = _client(
            tmp_path, mock_hardware, monkeypatch, on_hotspot=False, setup_completed=True
        )
        with client.session_transaction() as sess:
            sess["pin_verified"] = True
        response = client.get("/settings/")
        assert response.status_code == 403
        _no_pin_form(response)

    def test_create_app_does_not_load_a_pin_key(self, tmp_path, mock_hardware):
        config_path = tmp_path / "config.yaml"
        config_path.write_text('service_window:\n  port: 8080\n  pin: "1234"\n')
        app = create_app(
            {
                "TESTING": True,
                "CONFIG_PATH": str(config_path),
                "SECRET_KEY": "test-secret",
                "DB_PATH": str(tmp_path / "wqm1.db"),
            }
        )
        assert "PIN" not in app.config
        example = Path("config/config.yaml.example").read_text()
        assert "\n  pin:" not in example

    def test_test_open_window_is_ignored_when_not_testing(
        self, tmp_path, mock_hardware, monkeypatch
    ):
        client, app = _client(
            tmp_path, mock_hardware, monkeypatch, on_hotspot=False, setup_completed=True
        )
        app.config["TEST_OPEN_WINDOW"] = True
        app.config["TESTING"] = False
        response = client.get("/settings/")
        assert response.status_code == 403


class TestNoPinLeftovers:
    """Claude review 2026-10-25: leftover PIN text and the alias must go."""

    def test_setting_aliases_has_no_pin(self):
        from service_window.app import _SETTING_ALIASES

        assert "PIN" not in _SETTING_ALIASES
        assert "pin" not in _SETTING_ALIASES
        assert "pin" not in _SETTING_ALIASES.values()

    def test_make_app_does_not_put_pin_in_app_config(self, tmp_path, mock_hardware):
        app = make_app(tmp_path, mock_hardware)
        assert "PIN" not in app.config
        assert "pin" not in app.config

    def test_hotspot_docstring_matches_ensure_reachable(self):
        from service_window.auth import _wifi_setup_on_hotspot

        doc = _wifi_setup_on_hotspot.__doc__ or ""
        assert "only comes up while the unit is offline" not in doc
        assert "wlan0" in doc
        assert "Ethernet" in doc and "LTE" in doc

    def test_installer_manual_does_not_walk_pin_setup(self):
        html = Path("docs/manual/WQM1-IM-100-RevH.html").read_text()
        assert "PIN setup" not in html
        assert "walks PIN" not in html

    def test_manual_errata_does_not_set_a_pin(self):
        text = Path("docs/manual-errata-revD9.md").read_text()
        assert "set a real PIN" not in text

    def test_launch_audit_m7_is_no_pin(self):
        text = Path("docs/launch-audit.md").read_text()
        assert "M7 Service Window PIN" in text
        assert "use more than four on a shared network" not in text
        assert "BS-WQM1-0000a92e4e7d" in text

    def test_rollout_note_holds_live_field_unit(self):
        text = Path("docs/no-pin-rollout.md").read_text()
        assert "BS-WQM1-0000a92e4e7d" in text
        assert "site visit" in text
        assert "firmware push" in text
        assert "no PIN" in text
