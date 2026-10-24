"""Captain, 2026-10-24: no PIN anywhere. Setup is the hotspot only.

These tests pin the review on PR #153 (findings 1–2). Each one fails when
``src/service_window/auth.py`` is reverted to that PR's head: the factory-PIN
gate comes back, LAN clients are sent to /login, and setup routes other than
the Wi-Fi page need a PIN once setup is finished.
"""

from pathlib import Path

import yaml

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
    client = app.test_client()
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
