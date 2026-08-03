"""
Service Window — the host reboot control on the Settings page.

The recovery path for a unit that answers on :8080 but whose sshd is dead. It
must be hard to fire by accident (typed confirmation, checked server-side) and
honest about what happened when it does fire.
"""

import sqlite3

import pytest

from service_window.app import create_app
from service_window.routes import settings as settings_routes


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            ph REAL, tds_ppm REAL, turbidity_ntu REAL, orp_mv REAL,
            temp_c REAL, lat REAL, lon REAL, alt_m REAL,
            battery_v REAL, relay_state INTEGER DEFAULT 0, synced INTEGER DEFAULT 0
        );
        """
    )
    conn.close()
    return path


@pytest.fixture
def app(db_path, tmp_path):
    return create_app(
        {
            "db_path": db_path,
            "pin": "9999",
            "config_path": str(tmp_path / "config.yaml"),
            "cal_path": str(tmp_path / "calibration.yaml"),
            "cmd_sock": str(tmp_path / "cmd.sock"),
        }
    )


@pytest.fixture
def authed_client(app):
    app.config["TESTING"] = True
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["pin_verified"] = True
    return client


@pytest.fixture
def commands(monkeypatch):
    """Capture what the page sends down the firmware command socket."""
    sent: list[tuple] = []
    reply: dict = {"ok": True, "rebooting": True, "relaysSafe": True}

    def fake_send(sock_path, action, **kwargs):
        sent.append((action, kwargs))
        return reply

    monkeypatch.setattr(settings_routes, "send_command", fake_send)
    return {"sent": sent, "reply": reply}


def reboot(client, typed: str | None = "REBOOT"):
    data = {"reboot": "1"}
    if typed is not None:
        data["reboot_confirm"] = typed
    return client.post("/settings/", data=data, follow_redirects=True)


class TestRebootControl:
    def test_settings_page_offers_the_reboot(self, authed_client):
        resp = authed_client.get("/settings/")
        assert resp.status_code == 200
        assert b"Reboot unit" in resp.data
        assert b"REBOOT" in resp.data

    def test_page_says_what_a_reboot_costs(self, authed_client):
        """An installer needs to know monitoring stops before they press it."""
        body = authed_client.get("/settings/").data.lower()
        assert b"relays" in body
        assert b"ssh" in body

    def test_login_required(self, app):
        app.config["TESTING"] = True
        resp = reboot(app.test_client())
        assert b"PIN" in resp.data


class TestTypedConfirmation:
    def test_missing_confirmation_does_not_reboot(self, authed_client, commands):
        resp = reboot(authed_client, typed=None)

        assert commands["sent"] == []
        assert b"nothing was rebooted" in resp.data

    def test_wrong_word_does_not_reboot(self, authed_client, commands):
        resp = reboot(authed_client, typed="yes")

        assert commands["sent"] == []
        assert b"Type REBOOT" in resp.data

    def test_near_miss_does_not_reboot(self, authed_client, commands):
        reboot(authed_client, typed="REBOOOT")
        assert commands["sent"] == []

    def test_typed_word_reboots(self, authed_client, commands):
        resp = reboot(authed_client, typed="REBOOT")

        assert commands["sent"] == [("reboot", {})]
        assert b"rebooting" in resp.data.lower()

    def test_lowercase_and_whitespace_accepted(self, authed_client, commands):
        """Typed one-handed on a phone at a wet install — the word is the
        deliberate act, not the shift key."""
        reboot(authed_client, typed="  reboot ")
        assert commands["sent"] == [("reboot", {})]

    def test_confirmation_is_checked_server_side(self, authed_client, commands):
        """No JavaScript involved: a raw POST without the field is refused the
        same way the form is."""
        authed_client.post("/settings/", data={"reboot": "1"})
        assert commands["sent"] == []


class TestRebootOutcome:
    def test_success_reports_relays_off(self, authed_client, commands):
        resp = reboot(authed_client)

        body = resp.data.lower()
        assert b"relays are off" in body
        assert b"minute" in body

    def test_unsafe_relays_are_surfaced_not_hidden(self, authed_client, monkeypatch):
        monkeypatch.setattr(
            settings_routes,
            "send_command",
            lambda *a, **k: {"ok": True, "rebooting": True, "relaysSafe": False},
        )

        resp = reboot(authed_client)

        assert b"could not be confirmed off" in resp.data
        assert b"flash error" in resp.data

    def test_firmware_unreachable_says_the_unit_is_still_running(self, authed_client, monkeypatch):
        """The real socket is absent in tests, which is exactly the case where
        the operator must not be told a reboot is under way."""
        resp = reboot(authed_client)

        body = resp.data
        assert b"Could not reboot" in body
        assert b"still running" in body

    def test_reboot_does_not_touch_the_config(self, authed_client, tmp_path, commands):
        config = tmp_path / "config.yaml"
        config.write_text("sensor_read_s: 60\n")

        reboot(authed_client)

        assert config.read_text() == "sensor_read_s: 60\n"


class TestRestartStillSeparate:
    def test_restart_button_needs_no_confirmation(self, authed_client, commands):
        """Bouncing the service is cheap and stays a one-click action."""
        authed_client.post("/settings/", data={"restart": "1"}, follow_redirects=True)

        assert commands["sent"] == [("restart", {})]
