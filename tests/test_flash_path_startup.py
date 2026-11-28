"""A fresh card must reach its first reading without Cloud or pending commands.

Rocky-style install: the unit boots from master's image, sensors come up
(mocked here), and Cloud may have no pending commands or be unreachable.
The command poller from #165 must not crash startup or stall the first
sample. The live unit ``BS-WQM1-0000a92e4e7d`` is not contacted.
"""

from __future__ import annotations

import atexit
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.state import StateStore
from app.workers import CommandWorker, SamplingWorker
from cloud.device_commands import DeviceCommandDispatcher


def _config(tmp_path, **extra: str) -> str:
    lines = [
        "board: rpi-zero-2w",
        f"db_path: {tmp_path / 'wqm1.db'}",
        f"log_path: {tmp_path / 'wqm1.log'}",
        f"cmd_sock: {tmp_path / 'cmd.sock'}",
        "cloud_enabled: true",
        "api_key: bench-device-key",
        "cloud_api_base: https://example.test/app",
        "cloud_ingest_url: https://example.test/ingestReading",
        "cloud_command_url: https://example.test/deviceCommands",
        "sensor_read_s: 60",
        "command_poll_s: 5",
    ]
    lines.extend(f"{key}: {value}" for key, value in extra.items())
    cfg = tmp_path / "config.yaml"
    cfg.write_text("\n".join(lines) + "\n")
    return str(cfg)


def _started_app(monkeypatch, tmp_path, mock_hardware):
    import main
    from utils.config import ConfigManager

    cfg = _config(tmp_path)
    mgr = ConfigManager(cfg, str(tmp_path / "config.d" / "remote.yaml"))
    monkeypatch.setattr(main, "get_config_manager", lambda *_a, **_k: mgr)
    for name in (
        "RelayController",
        "StatusLEDs",
        "FanController",
        "ADS1115",
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
    ):
        monkeypatch.setattr(main, name, MagicMock(name=name))
    main.WQM1Database.return_value.load_session.return_value = None
    monkeypatch.setattr(main.WQM1App, "_start_cmd_listener", lambda self: None)
    app = main.WQM1App(cfg)
    app.start()
    return app


class TestFreshUnitReachesFirstReading:
    def test_start_returns_even_if_pending_commands_would_hang(
        self, monkeypatch, tmp_path, mock_hardware
    ):
        import main

        def hang(*_a, **_k):
            raise AssertionError("start() must not poll Cloud for pending commands")

        monkeypatch.setattr(main.CloudClient, "fetch_pending_commands", hang)
        monkeypatch.setattr(main.CloudClient, "poll", hang)
        started = time.monotonic()
        app = _started_app(monkeypatch, tmp_path, mock_hardware)
        atexit.unregister(app._shutdown)
        assert time.monotonic() - started < 2.0
        assert app._supervisor is not None
        assert app._cloud is not None

    def test_first_sample_stores_a_reading_while_cloud_is_down(
        self, monkeypatch, tmp_path, mock_hardware
    ):
        app = _started_app(monkeypatch, tmp_path, mock_hardware)
        atexit.unregister(app._shutdown)

        class Probe:
            def __init__(self, value):
                self.value = value

            def read(self, **_k):
                return self.value

            def read_temp_c(self):
                return self.value

        db = SimpleNamespace(readings=[])

        def insert_reading(row):
            db.readings.append(row)
            return len(db.readings)

        db.insert_reading = insert_reading
        worker = SamplingWorker(
            app._settings_provider,
            sensors={
                "temperature": Probe(21.5),
                "ph": Probe(7.2),
                "tds": Probe(340.0),
                "turbidity": Probe(3.0),
                "orp": None,
            },
            db=db,
            rules=None,
            relays=SimpleNamespace(get_state_bitmask=lambda: 0),
            leds=None,
            health=SimpleNamespace(update_last_seen=lambda: None),
            state=StateStore(),
        )
        worker.step()
        assert len(db.readings) == 1
        assert db.readings[0]["ph"] == 7.2
        assert db.readings[0]["tds_ppm"] == 340.0

    def test_command_worker_is_wired_with_the_dispatcher(
        self, monkeypatch, tmp_path, mock_hardware
    ):
        app = _started_app(monkeypatch, tmp_path, mock_hardware)
        atexit.unregister(app._shutdown)
        workers = app._build_workers()
        cmd = next(w for w in workers if w.name == "cloud-cmd")
        assert cmd._apply_device_command is not None
        assert isinstance(cmd._apply_device_command.__self__, DeviceCommandDispatcher)
        cache = cmd._apply_device_command.__self__._cache_path
        assert cache.name == "handled-commands.json"
        assert cache.parent == tmp_path


class TestPendingPollerDoesNotBreakTheTick:
    def test_empty_pending_list_applies_nothing(self):
        applied = []
        cloud = SimpleNamespace(
            poll=lambda: {"commands": [], "configVersion": None},
            fetch_pending_commands=lambda: [],
            post_command_result=lambda *a, **k: True,
            send_event=lambda *a, **k: None,
        )
        CommandWorker(
            lambda: SimpleNamespace(command_poll_s=5, cloud_enabled=True),
            cloud,
            StateStore(),
            apply_command=lambda _c: None,
            apply_device_command=applied.append,
        ).step()
        assert applied == []

    def test_fetch_raising_leaves_the_legacy_poll_done(self):
        legacy = []

        def boom():
            raise OSError("cloud unreachable")

        cloud = SimpleNamespace(
            poll=lambda: {"commands": [{"id": "legacy", "type": "restart"}], "configVersion": None},
            fetch_pending_commands=boom,
            post_command_result=lambda *a, **k: True,
            send_event=lambda *a, **k: None,
        )
        worker = CommandWorker(
            lambda: SimpleNamespace(command_poll_s=5, cloud_enabled=True),
            cloud,
            StateStore(),
            apply_command=legacy.append,
            apply_device_command=lambda _c: None,
        )
        worker.step()
        assert legacy == [{"id": "legacy", "type": "restart"}]

    def test_unreachable_cloud_client_returns_empty_pending(self, monkeypatch):
        from cloud.client import CloudClient

        client = CloudClient(
            device_id="BS-WQM1-00aabbccddee",
            ingest_url="https://example.test/ingestReading",
            command_url="https://example.test/deviceCommands",
            api_key="device-key",
            fw_version="2.4.0",
            api_base="https://example.test/app",
        )
        client._sleep = lambda *_: None
        client._max_retries = 1

        def down(req, timeout=None):
            raise OSError("network is unreachable")

        monkeypatch.setattr("urllib.request.urlopen", down)
        assert client.fetch_pending_commands() == []
        assert client.poll() == {
            "commands": [],
            "relaySchedule": None,
            "configVersion": None,
            "otaPending": False,
        }
