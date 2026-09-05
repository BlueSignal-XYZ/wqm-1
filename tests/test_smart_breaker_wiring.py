"""
Smart breaker wiring — config schema, main.py command paths, and the worker.

The AWG circuit gets exactly one entry point into the firmware (`awg_set`),
reached from the Service Window socket and from the cloud command queue, so
interlock + fail-safe bookkeeping see every request. These tests pin that.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from integrations.smart_breaker import SmartBreakerWorker
from integrations.smart_breaker.fake import FakeSmartBreaker
from utils import config as cfg
from utils.config import Settings, validate_values

# ---------------------------------------------------------------------------
# Config schema
# ---------------------------------------------------------------------------


class TestConfigSchema:
    def test_defaults_are_off_and_fail_off(self):
        s = Settings()
        assert s.smart_breaker_vendor == "none"
        assert s.smart_breaker_fail_safe == "off"
        assert s.smart_breaker_circuit_amps == 0  # never guessed
        assert s.smart_breaker_interlock_relay == 0
        assert s.smart_breaker_client_secret == ""

    def test_every_setting_has_a_schema_entry(self):
        for name in vars(Settings()):
            if name.startswith("smart_breaker_"):
                assert name in cfg.SETTINGS_SCHEMA, name

    @pytest.mark.parametrize("vendor", ["none", "relay_only", "ableedge"])
    def test_vendor_choices_accepted(self, vendor):
        accepted, errors = validate_values({"smart_breaker_vendor": vendor})
        assert errors == [] and accepted == {"smart_breaker_vendor": vendor}

    @pytest.mark.parametrize("vendor", ["span", "franklinwh", "ABLEEDGE", ""])
    def test_unknown_vendor_rejected(self, vendor):
        _, errors = validate_values({"smart_breaker_vendor": vendor})
        assert errors and "must be one of" in errors[0]

    @pytest.mark.parametrize("mode", ["off", "last", "on"])
    def test_fail_safe_choices(self, mode):
        assert validate_values({"smart_breaker_fail_safe": mode})[1] == []

    def test_fail_safe_bad_value_rejected(self):
        assert validate_values({"smart_breaker_fail_safe": "maybe"})[1]

    def test_unquoted_yaml_off_gets_a_hint_and_keeps_the_safe_default(self, tmp_path):
        """`smart_breaker_fail_safe: on` unquoted is YAML `true`; the key is
        rejected with a quoting hint and the shipped `off` stays in force."""
        _, errors = validate_values({"smart_breaker_fail_safe": True})
        assert errors == ['smart_breaker_fail_safe must be a string (quote it: "on" / "off")']
        p = tmp_path / "config.yaml"
        p.write_text("smart_breaker_fail_safe: on\n")
        mgr = cfg.ConfigManager(str(p), str(tmp_path / "remote.yaml"))
        assert mgr.settings.smart_breaker_fail_safe == "off"

    def test_interlock_relay_bounds(self):
        assert validate_values({"smart_breaker_interlock_relay": 4})[1] == []
        assert validate_values({"smart_breaker_interlock_relay": 5})[1]
        assert validate_values({"smart_breaker_interlock_relay": -1})[1]

    def test_poll_floor_respects_vendor_rate_limits(self):
        assert validate_values({"smart_breaker_poll_s": 14})[1]
        assert validate_values({"smart_breaker_poll_s": 15})[1] == []

    def test_credentials_and_urls_never_remote(self):
        for key in (
            "smart_breaker_client_id",
            "smart_breaker_client_secret",
            "smart_breaker_subscription_key",
            "smart_breaker_api_base",
            "smart_breaker_token_url",
            "smart_breaker_auth_mode",
        ):
            _, errors = validate_values({key: "x"}, remote=True)
            assert errors == [f"key not remotely configurable: {key}"], key

    def test_binding_and_policy_are_remote_configurable(self):
        values = {
            "smart_breaker_vendor": "ableedge",
            "smart_breaker_device_id": "uuid",
            "smart_breaker_site_id": "uuid",
            "smart_breaker_circuit_amps": 20,
            "smart_breaker_interlock_relay": 3,
            "smart_breaker_poll_s": 60,
            "smart_breaker_fail_safe": "last",
            "smart_breaker_unreachable_grace_s": 120,
        }
        accepted, errors = validate_values(values, remote=True)
        assert errors == []
        assert accepted == values

    def test_policy_keys_are_hot_binding_keys_need_restart(self):
        hot = cfg.hot_keys(
            {"smart_breaker_poll_s": 1, "smart_breaker_fail_safe": "x", "smart_breaker_vendor": "y"}
        )
        assert hot == {"smart_breaker_poll_s", "smart_breaker_fail_safe"}
        assert cfg.restart_keys({"smart_breaker_device_id": "x"}) == {"smart_breaker_device_id"}

    def test_loads_from_yaml(self, tmp_path):
        p = tmp_path / "config.yaml"
        p.write_text(
            "smart_breaker_vendor: ableedge\n"
            "smart_breaker_device_id: dev-uuid\n"
            "smart_breaker_circuit_amps: 20\n"
            "smart_breaker_client_secret: s3\n"
        )
        mgr = cfg.ConfigManager(str(p), str(tmp_path / "remote.yaml"))
        assert mgr.settings.smart_breaker_vendor == "ableedge"
        assert mgr.settings.smart_breaker_device_id == "dev-uuid"
        assert mgr.settings.smart_breaker_circuit_amps == 20
        assert mgr.settings.smart_breaker_client_secret == "s3"

    def test_example_config_validates(self):
        import yaml

        raw = yaml.safe_load(
            (cfg.Path(__file__).parent.parent / "config" / "config.yaml.example").read_text()
        )
        sb = {k: v for k, v in raw.items() if k.startswith("smart_breaker_")}
        assert sb["smart_breaker_vendor"] == "none"
        assert sb["smart_breaker_client_secret"] == ""
        _, errors = validate_values(sb)
        assert errors == []


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


class TestWorker:
    def test_polls_controller_at_configured_cadence(self):
        ctl = MagicMock()
        settings = SimpleNamespace(smart_breaker_poll_s=45)
        w = SmartBreakerWorker(lambda: settings, ctl)
        assert w.name == "smart-breaker"
        assert w.error_bucket == "cloud"
        assert w.interval_s() == 45.0
        w.step()
        ctl.poll.assert_called_once_with()

    def test_cadence_is_hot(self):
        settings = SimpleNamespace(smart_breaker_poll_s=60)
        w = SmartBreakerWorker(lambda: settings, MagicMock())
        settings.smart_breaker_poll_s = 15
        assert w.interval_s() == 15.0


# ---------------------------------------------------------------------------
# main.py command paths
# ---------------------------------------------------------------------------

_HW_CLASSES = [
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
]


def _boot(monkeypatch, tmp_path, config_text: str):
    import main
    from utils.config import ConfigManager

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(config_text)
    mgr = ConfigManager(str(cfg_path), str(tmp_path / "config.d" / "remote.yaml"))
    monkeypatch.setattr(main, "get_config_manager", lambda: mgr)
    for name in _HW_CLASSES:
        monkeypatch.setattr(main, name, MagicMock(name=name))
    main.WQM1Database.return_value.load_session.return_value = None
    monkeypatch.setattr(main.WQM1App, "_start_cmd_listener", lambda self: None)
    monkeypatch.setattr(main, "WATER_PROFILE_CACHE", tmp_path / "profile.json")
    app = main.WQM1App()
    app.start()
    app._relays.reset_mock()
    return main, app, mgr


@pytest.fixture
def unconfigured(monkeypatch, tmp_path, mock_hardware):
    return _boot(monkeypatch, tmp_path, "board: rpi-zero-2w\n")


@pytest.fixture
def ableedge(monkeypatch, tmp_path, mock_hardware):
    """Firmware booted with an AbleEdge binding, the real controller, and the
    vendor client swapped for FakeSmartBreaker."""
    main, app, mgr = _boot(
        monkeypatch,
        tmp_path,
        "board: rpi-zero-2w\n"
        "smart_breaker_vendor: ableedge\n"
        "smart_breaker_device_id: dev-uuid\n"
        "smart_breaker_interlock_relay: 3\n"
        "smart_breaker_circuit_amps: 20\n"
        "smart_breaker_client_id: cid\n"
        "smart_breaker_client_secret: sec\n"
        "smart_breaker_subscription_key: key\n",
    )
    fake = FakeSmartBreaker(device_id="dev-uuid")
    app._smart_breaker._client = fake
    app._cloud = MagicMock()
    return main, app, fake


class TestUnconfigured:
    def test_awg_set_says_not_configured(self, unconfigured):
        _, app, _ = unconfigured
        assert app._smart_breaker is None
        r = app._handle_cmd({"action": "awg_set", "state": True})
        assert r["ok"] is False
        assert "not configured" in r["error"]
        app._relays.set.assert_not_called()

    def test_awg_status_reports_unconfigured(self, unconfigured):
        _, app, _ = unconfigured
        assert app._handle_cmd({"action": "awg_status"}) == {
            "ok": True,
            "configured": False,
            "vendor": "none",
        }

    def test_no_smart_breaker_worker(self, unconfigured):
        _, app, _ = unconfigured
        assert not any(w.name == "smart-breaker" for w in app._build_workers())

    def test_cloud_awg_command_acks_error(self, unconfigured):
        _, app, _ = unconfigured
        app._cloud = MagicMock()
        app._apply_cloud_command({"id": "c1", "type": "awg", "state": True})
        app._cloud.ack_command.assert_called_once()
        args = app._cloud.ack_command.call_args.args
        assert args[0] == "c1" and args[1] == "error"


class TestAbleEdgeWiring:
    def test_controller_built_from_config(self, ableedge):
        _, app, _ = ableedge
        assert app._smart_breaker is not None
        assert app._smart_breaker.vendor == "ableedge"
        assert app._smart_breaker.interlock_relay == 3

    def test_worker_is_added(self, ableedge):
        _, app, _ = ableedge
        names = [w.name for w in app._build_workers()]
        assert "smart-breaker" in names

    def test_service_window_awg_set_on(self, ableedge):
        _, app, fake = ableedge
        r = app._handle_cmd({"action": "awg_set", "state": True, "reason": "bench"})
        assert r["ok"] is True
        assert r["breaker"] == "confirmed"
        assert r["source"] == "service_window"
        assert fake.is_on is True
        app._relays.set.assert_called_once_with(3, True)

    def test_awg_set_rejects_non_boolean(self, ableedge):
        _, app, fake = ableedge
        assert app._handle_cmd({"action": "awg_set", "state": "on"})["ok"] is False
        assert fake.calls == []

    def test_awg_status_snapshot(self, ableedge):
        _, app, _ = ableedge
        r = app._handle_cmd({"action": "awg_status"})
        assert r["ok"] is True and r["configured"] is True
        assert r["vendor"] == "ableedge"
        assert r["deviceId"] == "dev-uuid"
        assert r["circuitAmps"] == 20
        assert r["failSafe"] == "off"

    def test_cloud_awg_on_acks_done(self, ableedge):
        _, app, fake = ableedge
        app._apply_cloud_command({"id": "c1", "type": "awg", "state": True, "reason": "user"})
        assert fake.is_on is True
        assert fake.calls[-1] == ("set_circuit", (True, "user"))
        app._cloud.ack_command.assert_called_once_with("c1", "done")

    def test_cloud_awg_off_default_reason_names_command(self, ableedge):
        _, app, fake = ableedge
        fake.is_on = True
        app._apply_cloud_command({"id": "c9", "type": "awg", "state": False})
        assert fake.is_on is False
        assert fake.calls[-1][1] == (False, "cloud command c9")
        # OFF: interlock dropped before the breaker was asked.
        app._relays.set.assert_called_once_with(3, False)

    def test_cloud_awg_failure_acks_error_with_reason(self, ableedge):
        _, app, fake = ableedge
        fake.unreachable = True
        app._apply_cloud_command({"id": "c2", "type": "awg", "state": True})
        args = app._cloud.ack_command.call_args.args
        assert args[0] == "c2" and args[1] == "error"
        assert "Unreachable" in args[2]
        app._relays.set.assert_not_called()

    def test_cloud_awg_duration_schedules_off(self, ableedge, monkeypatch):
        main, app, fake = ableedge
        timers = []

        class FakeTimer:
            def __init__(self, interval, fn):
                timers.append((interval, fn))

            def start(self):
                pass

        monkeypatch.setattr(main.threading, "Timer", FakeTimer)
        app._apply_cloud_command({"id": "c3", "type": "awg", "state": True, "durationSeconds": 90})
        assert fake.is_on is True
        assert len(timers) == 1 and timers[0][0] == 90.0
        timers[0][1]()  # fire
        assert fake.is_on is False
        assert "duration elapsed" in fake.calls[-1][1][1]

    def test_cloud_awg_duration_not_scheduled_on_failure(self, ableedge, monkeypatch):
        main, app, fake = ableedge
        fake.unreachable = True
        timer = MagicMock()
        monkeypatch.setattr(main.threading, "Timer", timer)
        app._apply_cloud_command({"id": "c4", "type": "awg", "state": True, "durationSeconds": 90})
        timer.assert_not_called()

    def test_relay_command_path_is_untouched(self, ableedge):
        """The G5Q relays keep their own direct command; AWG does not hijack it."""
        _, app, fake = ableedge
        r = app._handle_cmd({"action": "relay_set", "channel": 1, "state": True})
        assert r == {"ok": True, "channel": 1, "state": True}
        assert fake.calls == []

    def test_shutdown_stops_controller_and_drops_relays(self, ableedge):
        _, app, fake = ableedge
        app._handle_cmd({"action": "awg_set", "state": True})
        fake.calls.clear()
        app._shutdown()
        app._relays.all_off.assert_called_once_with()
        assert fake.calls == []  # no breaker command on the way down


class TestRelayOnlyWiring:
    def test_relay_only_maps_awg_to_channel(self, monkeypatch, tmp_path, mock_hardware):
        _, app, _ = _boot(
            monkeypatch,
            tmp_path,
            "board: rpi-zero-2w\n"
            "smart_breaker_vendor: relay_only\n"
            "smart_breaker_interlock_relay: 4\n",
        )
        assert app._smart_breaker.vendor == "relay_only"
        assert not any(w.name == "smart-breaker" for w in app._build_workers())
        r = app._handle_cmd({"action": "awg_set", "state": True})
        assert r["ok"] is True and r["breaker"] == "n/a"
        app._relays.set.assert_called_once_with(4, True)
