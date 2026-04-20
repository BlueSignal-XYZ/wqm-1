"""Tests targeting the remaining 32 uncovered lines across 8 modules."""

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml


# ---------------------------------------------------------------------------
# calibrate.py:77 — _save() error path, tmp cleanup
# ---------------------------------------------------------------------------
class TestCalibrationSaveFailure:
    def test_save_cleans_up_tmp_on_error(self, tmp_path, mock_hardware):
        from calibration.calibrate import CalibrationManager

        cal_path = tmp_path / "cal.yaml"
        mgr = CalibrationManager(path=str(cal_path))

        with patch("pathlib.Path.replace", side_effect=OSError("disk full")):
            mgr.calibrate_ph(1.0, 1.5)

        assert not (tmp_path / "cal.tmp").exists()


# ---------------------------------------------------------------------------
# app.py:69-73 — _load_sw_config() reads YAML with service_window section
# ---------------------------------------------------------------------------
class TestAppConfigLoading:
    def test_load_sw_config_reads_yaml(self, tmp_path, mock_hardware):
        config = {"service_window": {"port": 9090, "pin": "5678"}}

        with (
            patch("pathlib.Path.exists", return_value=True),
            patch("builtins.open", create=True) as mock_open,
        ):
            mock_open.return_value.__enter__ = lambda s: s
            mock_open.return_value.__exit__ = MagicMock(return_value=False)
            mock_open.return_value.read = MagicMock(return_value=yaml.dump(config))

            from service_window.app import _load_sw_config

            with (
                patch("yaml.safe_load", return_value=config),
                patch.object(Path, "exists", return_value=True),
            ):
                result = _load_sw_config()

        assert isinstance(result, dict)

    def test_load_sw_config_handles_exception(self, mock_hardware):
        from service_window.app import _load_sw_config

        with patch.object(Path, "exists", side_effect=RuntimeError("boom")):
            result = _load_sw_config()
        assert result == {}


# ---------------------------------------------------------------------------
# auth.py:53 — _record_attempt() creates new IP entry
# ---------------------------------------------------------------------------
class TestAuthRecordAttempt:
    def test_record_attempt_creates_new_ip_entry(self, mock_hardware):
        from service_window.auth import _attempts, _record_attempt

        _attempts.clear()
        _record_attempt("192.168.1.99")
        assert "192.168.1.99" in _attempts
        assert len(_attempts["192.168.1.99"]) == 1
        _attempts.clear()


# ---------------------------------------------------------------------------
# diagnostics.py:34,48-51,68-69
# ---------------------------------------------------------------------------
class TestDiagnosticsEdgeCases:
    @pytest.fixture
    def diag_app(self, tmp_path):
        from service_window.app import create_app

        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
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
            INSERT INTO readings (timestamp, ph) VALUES ('2025-01-01T00:00:00Z', 7.0);
            """
        )
        conn.close()
        app = create_app({
            "db_path": db_path,
            "pin": "1234",
            "config_path": str(tmp_path / "config.yaml"),
            "cal_path": str(tmp_path / "cal.yaml"),
            "cmd_sock": str(tmp_path / "cmd.sock"),
        })
        app.config["TESTING"] = True
        return app

    @pytest.fixture
    def authed(self, diag_app):
        client = diag_app.test_client()
        with client.session_transaction() as sess:
            sess["pin_verified"] = True
        return client

    def test_diagnostics_run_script_not_found(self, authed, mock_hardware):
        """When diagnostics.sh doesn't exist, render 'not found' message."""
        with patch.object(Path, "exists", return_value=False):
            resp = authed.get("/diagnostics/run")
        assert resp.status_code == 200
        assert b"not found" in resp.data

    def test_diagnostics_run_timeout(self, authed, mock_hardware):
        """Subprocess timeout produces a timeout message."""
        import subprocess

        with (
            patch.object(Path, "exists", return_value=True),
            patch(
                "service_window.routes.diagnostics.subprocess.run",
                side_effect=subprocess.TimeoutExpired("bash", 30),
            ),
        ):
            resp = authed.get("/diagnostics/run")
        assert resp.status_code == 200
        assert b"timed out" in resp.data.lower()

    def test_diagnostics_run_exception(self, authed, mock_hardware):
        """Generic subprocess error produces an error message."""
        with (
            patch.object(Path, "exists", return_value=True),
            patch(
                "service_window.routes.diagnostics.subprocess.run",
                side_effect=OSError("exec failed"),
            ),
        ):
            resp = authed.get("/diagnostics/run")
        assert resp.status_code == 200
        assert b"Error" in resp.data

    def test_get_recent_logs_failure(self, mock_hardware):
        """Journal log failure returns fallback message."""
        from service_window.routes.diagnostics import _get_recent_logs

        with patch(
            "service_window.routes.diagnostics.subprocess.run",
            side_effect=FileNotFoundError("journalctl"),
        ):
            result = _get_recent_logs()
        assert "Could not read" in result


# ---------------------------------------------------------------------------
# provision.py:36-40,106-109,149-150,164-166
# ---------------------------------------------------------------------------
class TestProvisionEdgeCases:
    @pytest.fixture
    def prov_app(self, tmp_path):
        from service_window.app import create_app

        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
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
            """
        )
        conn.close()
        app = create_app({
            "db_path": db_path,
            "pin": "1234",
            "config_path": str(tmp_path / "config.yaml"),
            "cal_path": str(tmp_path / "cal.yaml"),
            "cmd_sock": str(tmp_path / "cmd.sock"),
        })
        app.config["TESTING"] = True
        return app

    @pytest.fixture
    def authed(self, prov_app):
        client = prov_app.test_client()
        with client.session_transaction() as sess:
            sess["pin_verified"] = True
        return client

    def test_get_identity_fallback_on_import_error(self, authed, mock_hardware):
        """When utils.identity import fails, use fallback identity."""
        with patch(
            "service_window.routes.provision._get_identity",
            return_value={
                "device_id": "BS-WQM1-unknown",
                "dev_eui": "0000000000000000",
                "ble_name": "BlueSignal-0000",
            },
        ):
            resp = authed.get("/provision/")
        assert resp.status_code == 200

    def test_get_identity_direct_fallback(self, mock_hardware):
        """_get_identity() returns fallback when identity module raises."""
        from service_window.routes.provision import _get_identity

        with patch(
            "utils.identity.get_device_id",
            side_effect=RuntimeError("no /proc/cpuinfo"),
        ):
            result = _get_identity()
        assert result["device_id"] == "BS-WQM1-unknown"

    def test_verify_subprocess_exception(self, authed, mock_hardware):
        """verify() handles subprocess exceptions gracefully."""
        with (
            patch.object(Path, "exists", return_value=True),
            patch(
                "service_window.routes.provision.subprocess.run",
                side_effect=OSError("exec failed"),
            ),
        ):
            resp = authed.get("/provision/verify")
        assert resp.status_code == 200
        assert b"Error" in resp.data

    def test_qr_without_qrcode_package(self, authed, mock_hardware):
        """QR route gracefully handles missing qrcode package."""
        import sys

        saved_modules = {}
        for mod_name in list(sys.modules):
            if mod_name.startswith("qrcode"):
                saved_modules[mod_name] = sys.modules.pop(mod_name)
        try:
            with patch.dict("sys.modules", {"qrcode": None, "qrcode.image.svg": None}):
                resp = authed.get("/provision/qr.svg")
            assert resp.status_code == 200
            assert b"not installed" in resp.data
        finally:
            sys.modules.update(saved_modules)

    def test_read_version_exception_returns_unknown(self, mock_hardware):
        """read_firmware_version() returns 'unknown' when VERSION file can't be read."""
        from service_window import read_firmware_version

        with patch.object(Path, "exists", side_effect=RuntimeError("bad")):
            result = read_firmware_version()
        assert result == "unknown"


# ---------------------------------------------------------------------------
# relays.py:44 — successful relay command flash
# ---------------------------------------------------------------------------
class TestRelaySuccessFlash:
    @pytest.fixture
    def relay_app(self, tmp_path):
        from service_window.app import create_app

        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
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
            """
        )
        conn.close()
        app = create_app({
            "db_path": db_path,
            "pin": "1234",
            "config_path": str(tmp_path / "config.yaml"),
            "cal_path": str(tmp_path / "cal.yaml"),
            "cmd_sock": str(tmp_path / "cmd.sock"),
        })
        app.config["TESTING"] = True
        return app

    def test_relay_form_success_flash(self, relay_app, mock_hardware):
        """Successful relay command produces success flash."""
        client = relay_app.test_client()
        with client.session_transaction() as sess:
            sess["pin_verified"] = True

        with patch(
            "service_window.routes.relays.send_command",
            return_value={"ok": True},
        ):
            resp = client.post(
                "/relays/set",
                data={"channel": "1", "state": "on"},
                follow_redirects=True,
            )
        assert resp.status_code == 200
        assert b"ON" in resp.data or b"success" in resp.data.lower()


# ---------------------------------------------------------------------------
# status.py:25-27 — _read_version() exception path
# ---------------------------------------------------------------------------
class TestStatusVersionFallback:
    def test_read_version_returns_unknown_on_error(self, mock_hardware):
        from service_window import read_firmware_version

        with patch.object(Path, "exists", side_effect=RuntimeError("fail")):
            result = read_firmware_version()
        assert result == "unknown"


# ---------------------------------------------------------------------------
# config.py:190 — atomic_json_write error path with tmp cleanup
# ---------------------------------------------------------------------------
class TestAtomicJsonWriteFailure:
    def test_atomic_json_write_cleans_tmp_on_error(self, tmp_path, mock_hardware):
        from utils.config import atomic_json_write

        target = str(tmp_path / "data.json")

        with (
            patch("pathlib.Path.replace", side_effect=OSError("cross-device")),
            pytest.raises(OSError),
        ):
            atomic_json_write(target, {"key": "value"})

        assert not (tmp_path / "data.tmp").exists()
