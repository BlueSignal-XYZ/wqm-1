"""A missing board must not take the unit down, and must still be diagnosable.

The field failure was ADS1115 init raising out of start() (OSError errno 5
from read_word_data), systemd restarting the process, and the setup hotspot
never appearing. These tests pin the three repairs: the process stays up,
the hotspot path still runs, and the FAT status file names the fault
without copying a secret onto the card.
"""

from __future__ import annotations

import importlib.util
import logging
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_HW_EXCEPT_ADC = [
    "RelayController",
    "StatusLEDs",
    "FanController",
    "DS18B20",
    "PHSensor",
    "TDSSensor",
    "TurbiditySensor",
    "ORPSensor",
    "SX1262",
    "LoRaWANMAC",
    "GPS",
    "WQM1Database",
    "CalibrationManager",
    "HealthReporter",
    "HardwareWatchdog",
]

_ROOT = Path(__file__).resolve().parents[1]


def _config(tmp_path: Path) -> str:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "board: rpi-zero-w\n"
        f"db_path: {tmp_path / 'wqm1.db'}\n"
        f"log_path: {tmp_path / 'wqm1.log'}\n"
        f"cmd_sock: {tmp_path / 'cmd.sock'}\n"
    )
    return str(cfg)


def _prepare(monkeypatch, tmp_path, mock_hardware, *, temperature_error: bool = False):
    """Real ADS1115, every other hardware class mocked, I2C read fails."""
    import main
    from utils.config import ConfigManager

    cfg = _config(tmp_path)
    mgr = ConfigManager(cfg, str(tmp_path / "config.d" / "remote.yaml"))
    monkeypatch.setattr(main, "get_config_manager", lambda *_a, **_k: mgr)
    for name in _HW_EXCEPT_ADC:
        monkeypatch.setattr(main, name, MagicMock(name=name))
    main.WQM1Database.return_value.load_session.return_value = None
    monkeypatch.setattr(main.WQM1App, "_start_cmd_listener", lambda self: None)
    monkeypatch.setattr(main.WQM1App, "run", lambda self: None)
    if temperature_error:
        main.DS18B20.side_effect = OSError(5, "Input/output error")
    bus = mock_hardware["bus"]
    bus.read_word_data.side_effect = OSError(5, "Input/output error")
    status = tmp_path / "wqm1-status.txt"
    monkeypatch.setenv("WQM1_STATUS_FILE", str(status))
    return main, cfg, status


def _error_lines(caplog, text: str) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.ERROR and text in r.getMessage()
    ]


class TestAds1115InitIsNotFatal:
    def test_oserror_at_init_does_not_exit_and_is_logged_once(
        self, monkeypatch, tmp_path, mock_hardware, caplog
    ):
        import atexit

        main, cfg, status = _prepare(monkeypatch, tmp_path, mock_hardware)
        monkeypatch.setattr(sys, "argv", ["wqm1", "--config", cfg])
        root = logging.getLogger("wqm1")
        old_level, old_handlers = root.level, list(root.handlers)
        with caplog.at_level(logging.DEBUG):
            try:
                main.main()
            except SystemExit as exc:  # pragma: no cover - the assertion is the failure
                pytest.fail(f"main() exited on an ADS1115 init error: {exc.code}")
            finally:
                for handler in root.handlers:
                    if handler not in old_handlers:
                        handler.close()
                root.handlers[:] = old_handlers
                root.setLevel(old_level)
        # main() does not hand the app back. Build the same way and inspect it.
        # The log-once count is for this process, not the one main() just ran.
        caplog.clear()
        app = main.WQM1App(cfg)
        with caplog.at_level(logging.DEBUG):
            app.start()
            atexit.unregister(app._shutdown)

            assert app._supervisor is not None
            assert app._adc is None
            assert app._adc_error and "ADS1115 init failed" in app._adc_error
            assert {"ph", "tds", "turbidity"} <= app._faulted_channels
            assert "orp" not in app._faulted_channels
            assert app.hardware_status()["adc"] == "faulted"

            # Two more attempts, both still failing, must not add another error line.
            app._adc_next_retry = 0
            app._retry_deferred_sensors()
            app._adc_next_retry = 0
            app._retry_deferred_sensors()
            assert len(_error_lines(caplog, "ADS1115 init failed")) == 1

            # The converter answers on the next retry: channels come back, once.
            bus = mock_hardware["bus"]
            bus.read_word_data.side_effect = None
            bus.read_word_data.return_value = 0x8583
            app._adc_next_retry = 0
            app._retry_deferred_sensors()
            assert app._adc is not None
            assert "ph" not in app._faulted_channels
            assert any("ADS1115 is responding" in r.getMessage() for r in caplog.records)

        parsed_text = status.read_text(encoding="utf-8")
        assert "adc: ok" in parsed_text
        assert "fatal: none" in parsed_text
        assert "firmware:" in parsed_text

    def test_a_failed_temperature_probe_does_not_exit(
        self, monkeypatch, tmp_path, mock_hardware, caplog
    ):
        import atexit

        main, cfg, _status = _prepare(monkeypatch, tmp_path, mock_hardware, temperature_error=True)
        app = main.WQM1App(cfg)
        with caplog.at_level(logging.ERROR):
            app.start()
        atexit.unregister(app._shutdown)
        assert app._supervisor is not None
        assert "temperature" in app._faulted_channels
        assert len(_error_lines(caplog, "temperature init failed")) == 1


class TestHotspotDespiteTheBoard:
    def test_ap_fallback_still_raises_the_hotspot(self, monkeypatch, tmp_path, mock_hardware):
        import atexit

        from utils import netctl

        spec_nm = importlib.util.spec_from_file_location(
            "wqm1_test_netctl_fake", _ROOT / "tests" / "test_netctl.py"
        )
        assert spec_nm is not None and spec_nm.loader is not None
        netctl_tests = importlib.util.module_from_spec(spec_nm)
        spec_nm.loader.exec_module(netctl_tests)
        fake_cls = netctl_tests.FakeNm

        main, cfg, status = _prepare(monkeypatch, tmp_path, mock_hardware)
        app = main.WQM1App(cfg)
        app.start()
        atexit.unregister(app._shutdown)
        assert app._adc is None
        assert app._supervisor is not None

        spec = importlib.util.spec_from_file_location(
            "wqm1_ap_fallback_under_test", _ROOT / "scripts" / "wqm1-ap-fallback.py"
        )
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        fake = fake_cls()
        monkeypatch.setattr(netctl, "_run", fake.run)
        monkeypatch.setattr(mod, "resolve_credentials", lambda: ("WQM1-TEST", "river-cedar-42"))
        assert mod.main() == 0
        assert fake.ap is True
        hotspot = [c for c in fake.calls if len(c) > 3 and c[1:3] == ["connection", "up"]]
        assert hotspot, "ensure_reachable never activated the setup AP"
        # The passphrase is an argument of the hotspot command and must not
        # land in the status file the AP script writes beside it.
        text = status.read_text(encoding="utf-8")
        assert "hotspot: up" in text
        assert "adc: faulted" in text
        assert "river-cedar-42" not in text
        assert "passphrase" not in text.lower()


class TestBootStatusFile:
    def test_snapshot_has_the_diagnostic_lines_and_no_secrets(self, monkeypatch, tmp_path):
        from diagnostics.boot_status import parse_status, write_boot_status
        from utils.config import FIRMWARE_VERSION

        path = tmp_path / "wqm1-status.txt"
        monkeypatch.setenv("WQM1_STATUS_FILE", str(path))
        first = write_boot_status(
            board="Raspberry Pi Zero W (rpi-zero-w)",
            i2c="bus not enumerated",
            wlan0="down",
            hotspot="up",
            adc="faulted",
            power="ok",
            adc_error="ADS1115 init failed: [Errno 5] Input/output error",
            faulted=["ph", "tds", "turbidity"],
            fatal="none",
        )
        first_parsed = parse_status(first)
        assert first_parsed["firmware"] == FIRMWARE_VERSION
        assert first_parsed["board"] == "Raspberry Pi Zero W (rpi-zero-w)"
        assert first_parsed["i2c"] == "bus not enumerated"
        assert first_parsed["wlan0"] == "down"
        assert first_parsed["hotspot"] == "up"
        assert first_parsed["commission"] == "FAIL: adc faulted"
        assert first_parsed["adc"] == "faulted"
        assert "Input/output error" in first_parsed["adc_error"]
        assert first_parsed["faulted"] == "ph, tds, turbidity"
        assert first_parsed["fatal"] == "none"
        assert "updated" in first_parsed
        # A later write that only knows about a secret-shaped fatal line must
        # redact the whole line and must not wipe the hotspot or the fault.
        # i2c and wlan0 are re-read from the machine; this host has neither.
        text = write_boot_status(
            adc_error="api_key: supersecret",
            fatal="passphrase=river-cedar-42",
        )
        parsed = parse_status(text)
        assert parsed["firmware"] == FIRMWARE_VERSION
        assert parsed["board"] == "Raspberry Pi Zero W (rpi-zero-w)"
        assert parsed["hotspot"] == "up"
        assert parsed["adc"] == "faulted"
        assert parsed["adc_error"] == "[redacted]"
        assert parsed["faulted"] == "ph, tds, turbidity"
        assert parsed["fatal"] == "[redacted]"
        assert "updated" in parsed
        assert "supersecret" not in text
        assert "river-cedar-42" not in text
        assert "BEGIN" not in text

        leaked = write_boot_status(fatal="-----BEGIN RSA PRIVATE KEY-----\nabc")
        assert "abc" not in leaked
        assert "PRIVATE KEY" not in leaked

    def test_a_failed_probe_is_unknown_not_down(self, monkeypatch, tmp_path):
        from diagnostics.boot_status import read_hotspot_state

        # The suite's autouse netctl fake returns rc 127 (nmcli blocked).
        assert read_hotspot_state() == "unknown"


class TestBoardFaultInTheServiceWindow:
    def test_dashboard_and_provisioning_name_the_fault(self, monkeypatch, tmp_path):
        from service_window.app import create_app

        db = tmp_path / "test.db"
        conn = sqlite3.connect(db)
        conn.executescript(
            """
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
        status = tmp_path / "wqm1-status.txt"
        status.write_text(
            "WQM-1 boot status\n"
            "firmware: 2.4.0\n"
            "board: Raspberry Pi Zero W (rpi-zero-w)\n"
            "i2c: bus not enumerated\n"
            "wlan0: down\n"
            "hotspot: up\n"
            "adc: faulted\n"
            "adc_error: ADS1115 init failed: [Errno 5] Input/output error\n"
            "faulted: ph, tds, turbidity\n"
            "fatal: none\n"
            "updated: 2026-10-18T00:00:00Z\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("WQM1_STATUS_FILE", str(status))
        app = create_app(
            {
                "db_path": str(db),
                "pin": "9999",
                "config_path": str(tmp_path / "config.yaml"),
                "cal_path": str(tmp_path / "calibration.yaml"),
                "cmd_sock": str(tmp_path / "missing.sock"),
            }
        )
        app.config["TESTING"] = True
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["pin_verified"] = True
        for path in ("/", "/provision/"):
            resp = client.get(path)
            assert resp.status_code == 200, path
            body = resp.data.decode()
            assert "sensor board did not start" in body, path
            assert "reseat the board" in body, path
            assert "Input/output error" in body, path
