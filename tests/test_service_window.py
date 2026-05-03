"""Tests for the service window Flask application."""

import json
import sqlite3

import pytest

from service_window.app import create_app


@pytest.fixture
def db_path(tmp_path):
    """Create a temporary SQLite DB with test data."""
    path = str(tmp_path / "test.db")
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        PRAGMA journal_mode=WAL;
        CREATE TABLE readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            ph REAL, tds_ppm REAL, turbidity_ntu REAL, orp_mv REAL,
            temp_c REAL, lat REAL, lon REAL, alt_m REAL,
            battery_v REAL, relay_state INTEGER DEFAULT 0, synced INTEGER DEFAULT 0
        );
        CREATE TABLE lorawan_session (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            dev_addr BLOB, nwk_skey BLOB, app_skey BLOB,
            fcnt_up INTEGER DEFAULT 0, fcnt_down INTEGER DEFAULT 0,
            joined INTEGER DEFAULT 0, updated_at TEXT
        );
        INSERT INTO lorawan_session (id) VALUES (1);
        INSERT INTO readings (timestamp, ph, tds_ppm, turbidity_ntu, temp_c, relay_state)
        VALUES ('2025-01-15T10:30:00Z', 7.2, 450.0, 12.5, 22.3, 0);
        """
    )
    conn.close()
    return path


@pytest.fixture
def app(db_path, tmp_path):
    """Create test Flask app."""
    config_path = str(tmp_path / "config.yaml")
    cal_path = str(tmp_path / "calibration.yaml")
    return create_app(
        {
            "db_path": db_path,
            "pin": "9999",
            "config_path": config_path,
            "cal_path": cal_path,
            "cmd_sock": str(tmp_path / "cmd.sock"),
        }
    )


@pytest.fixture
def client(app):
    """Flask test client."""
    app.config["TESTING"] = True
    return app.test_client()


@pytest.fixture
def authed_client(client):
    """Flask test client with authenticated session."""
    with client.session_transaction() as sess:
        sess["pin_verified"] = True
    return client


class TestAuth:
    def test_login_page_renders(self, client):
        resp = client.get("/login")
        assert resp.status_code == 200
        assert b"PIN" in resp.data

    def test_login_redirect_on_unauthenticated(self, client):
        resp = client.get("/")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_login_with_correct_pin(self, client):
        resp = client.post("/login", data={"pin": "9999"}, follow_redirects=False)
        assert resp.status_code == 302

    def test_login_with_wrong_pin(self, client):
        resp = client.post("/login", data={"pin": "0000"})
        assert resp.status_code == 200
        assert b"Incorrect" in resp.data

    def test_logout(self, authed_client):
        resp = authed_client.get("/logout", follow_redirects=False)
        assert resp.status_code == 302
        # After logout, dashboard should redirect to login
        resp = authed_client.get("/")
        assert resp.status_code == 302


class TestDashboard:
    def test_dashboard_renders(self, authed_client):
        resp = authed_client.get("/")
        assert resp.status_code == 200
        assert b"Dashboard" in resp.data

    def test_dashboard_shows_reading(self, authed_client):
        resp = authed_client.get("/")
        assert b"7.2" in resp.data or b"7.20" in resp.data


class TestSensors:
    def test_sensors_page(self, authed_client):
        resp = authed_client.get("/sensors/")
        assert resp.status_code == 200

    def test_sensors_json(self, authed_client):
        resp = authed_client.get("/sensors/data.json")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert isinstance(data, list)
        assert len(data) >= 1


class TestCalibration:
    def test_calibration_index(self, authed_client):
        resp = authed_client.get("/calibrate/")
        assert resp.status_code == 200

    def test_ph_calibration_page(self, authed_client):
        resp = authed_client.get("/calibrate/ph")
        assert resp.status_code == 200

    def test_ph_calibration(self, authed_client):
        resp = authed_client.post(
            "/calibrate/ph",
            data={"v_ph4": "1.04", "v_ph7": "1.50"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"calibrated" in resp.data.lower()

    def test_ph_calibration_voltages_too_close(self, authed_client):
        resp = authed_client.post(
            "/calibrate/ph",
            data={"v_ph4": "1.50", "v_ph7": "1.5005"},
            follow_redirects=True,
        )
        assert b"too close" in resp.data.lower()

    def test_ph_calibration_invalid_input(self, authed_client):
        resp = authed_client.post(
            "/calibrate/ph",
            data={"v_ph4": "bad", "v_ph7": "1.5"},
            follow_redirects=True,
        )
        assert b"invalid" in resp.data.lower()

    def test_tds_calibration_page(self, authed_client):
        resp = authed_client.get("/calibrate/tds")
        assert resp.status_code == 200

    def test_tds_calibration_valid(self, authed_client):
        resp = authed_client.post(
            "/calibrate/tds",
            data={"known_ppm": "500", "measured_v": "1.0"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"calibrated" in resp.data.lower()

    def test_tds_calibration_rejects_non_positive_voltage(self, authed_client):
        resp = authed_client.post(
            "/calibrate/tds",
            data={"known_ppm": "500", "measured_v": "0"},
            follow_redirects=True,
        )
        assert b"positive" in resp.data.lower()

    def test_tds_calibration_invalid_input(self, authed_client):
        resp = authed_client.post(
            "/calibrate/tds",
            data={"known_ppm": "bad", "measured_v": "1"},
            follow_redirects=True,
        )
        assert b"invalid" in resp.data.lower()

    def test_turbidity_calibration_page(self, authed_client):
        resp = authed_client.get("/calibrate/turbidity")
        assert resp.status_code == 200

    def test_turbidity_calibration_valid(self, authed_client):
        resp = authed_client.post(
            "/calibrate/turbidity",
            data={"clear_v": "4.1"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"calibrated" in resp.data.lower()

    def test_turbidity_calibration_invalid_input(self, authed_client):
        resp = authed_client.post(
            "/calibrate/turbidity",
            data={"clear_v": "bad"},
            follow_redirects=True,
        )
        assert b"invalid" in resp.data.lower()

    def test_orp_calibration_page(self, authed_client):
        resp = authed_client.get("/calibrate/orp")
        assert resp.status_code == 200

    def test_orp_calibration_valid(self, authed_client):
        resp = authed_client.post(
            "/calibrate/orp",
            data={"known_mv": "220", "measured_mv": "210"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"calibrated" in resp.data.lower()

    def test_orp_calibration_invalid_input(self, authed_client):
        resp = authed_client.post(
            "/calibrate/orp",
            data={"known_mv": "bad", "measured_mv": "210"},
            follow_redirects=True,
        )
        assert b"invalid" in resp.data.lower()


class TestLoRa:
    def test_lora_page(self, authed_client):
        resp = authed_client.get("/lora/")
        assert resp.status_code == 200

    def test_appkey_validation_rejects_short(self, authed_client):
        resp = authed_client.post("/lora/appkey", data={"app_key": "abc"}, follow_redirects=True)
        assert b"32 hex" in resp.data

    def test_appkey_valid_is_saved(self, authed_client, tmp_path):
        good_key = "0123456789abcdef0123456789abcdef"
        resp = authed_client.post("/lora/appkey", data={"app_key": good_key}, follow_redirects=True)
        assert resp.status_code == 200
        assert b"updated" in resp.data.lower() or b"restart" in resp.data.lower()


class TestRelays:
    def test_relays_page(self, authed_client):
        resp = authed_client.get("/relays/")
        assert resp.status_code == 200

    def test_relay_api_validates_channel(self, authed_client):
        resp = authed_client.post(
            "/relays/api/set",
            data=json.dumps({"channel": 5, "state": True}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_relay_api_validates_state_type(self, authed_client):
        resp = authed_client.post(
            "/relays/api/set",
            data=json.dumps({"channel": 1, "state": "on"}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_relay_api_socket_unavailable_returns_502(self, authed_client):
        """Valid command should attempt socket; missing sock returns 502."""
        resp = authed_client.post(
            "/relays/api/set",
            data=json.dumps({"channel": 1, "state": True}),
            content_type="application/json",
        )
        body = json.loads(resp.data)
        assert body.get("ok") is False
        assert resp.status_code == 502

    def test_relay_form_invalid_channel(self, authed_client):
        resp = authed_client.post(
            "/relays/set",
            data={"channel": "9", "state": "on"},
            follow_redirects=True,
        )
        assert b"1-4" in resp.data or b"channel" in resp.data.lower()

    def test_relay_form_bad_payload(self, authed_client):
        resp = authed_client.post(
            "/relays/set",
            data={"channel": "not_a_number", "state": "on"},
            follow_redirects=True,
        )
        assert b"invalid" in resp.data.lower()

    def test_relay_form_valid_reaches_socket(self, authed_client):
        """Valid form submit attempts the socket (which will fail); flash indicates error."""
        resp = authed_client.post(
            "/relays/set",
            data={"channel": "2", "state": "off"},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert b"failed" in resp.data.lower() or b"error" in resp.data.lower()


class TestDiagnostics:
    def test_diagnostics_page(self, authed_client):
        resp = authed_client.get("/diagnostics/")
        assert resp.status_code == 200

    def test_diagnostics_run(self, authed_client):
        """Run diagnostics route — should execute script or show not-found, never crash."""
        resp = authed_client.get("/diagnostics/run")
        assert resp.status_code == 200


class TestProvisioning:
    def test_provision_page(self, authed_client):
        resp = authed_client.get("/provision/")
        assert resp.status_code == 200

    def test_provision_identity(self, authed_client):
        resp = authed_client.get("/provision/identity")
        assert resp.status_code == 200

    def test_provision_report_json(self, authed_client):
        resp = authed_client.get("/provision/report")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert "device_id" in data

    def test_provision_qr(self, authed_client):
        resp = authed_client.get("/provision/qr.svg")
        assert resp.status_code == 200
        assert "image/svg+xml" in resp.content_type

    def test_provision_appkey_rejects_bad_key(self, authed_client):
        resp = authed_client.post(
            "/provision/appkey", data={"app_key": "short"}, follow_redirects=True
        )
        assert b"32 hex" in resp.data

    def test_provision_appkey_accepts_valid_key(self, authed_client):
        good_key = "deadbeefdeadbeefdeadbeefdeadbeef"
        resp = authed_client.post(
            "/provision/appkey", data={"app_key": good_key}, follow_redirects=True
        )
        assert resp.status_code == 200
        assert b"saved" in resp.data.lower() or b"appkey" in resp.data.lower()

    def test_provision_verify(self, authed_client):
        """Verify route runs diagnostics.sh or reports not-found; does not crash."""
        resp = authed_client.get("/provision/verify")
        assert resp.status_code == 200
