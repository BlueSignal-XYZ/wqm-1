"""AbleEdge load-control skeleton: schema, mock client, fail-safe, hooks."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from integrations.ableedge.client import (
    CircuitCommandResult,
    HttpAbleEdgeClient,
    parse_meter_reading,
)
from integrations.ableedge.controller import LoadController, build_client
from integrations.ableedge.errors import AbleEdgeAuthError, AbleEdgeUnreachableError
from integrations.ableedge.mock import MockAbleEdgeClient
from integrations.ableedge.schema import (
    DEFAULT_API_BASE,
    LoadControlConfig,
    parse_load_control,
)
from integrations.ableedge.secrets import AbleEdgeSecrets, resolve_secrets


def _cfg(**over) -> LoadControlConfig:
    base = {
        "vendor": "ableedge",
        "site_id": "site-1",
        "device_id": "dev-uuid",
        "circuit_id": "ckt-uuid",
        "fail_safe": "off",
        "poll_s": 30,
        "backend": "mock",
    }
    base.update(over)
    return parse_load_control(base)


class TestParseLoadControl:
    def test_defaults_to_none(self):
        cfg = parse_load_control(None)
        assert cfg.vendor == "none"
        assert cfg.fail_safe == "off"
        assert cfg.circuit_ampacity_a is None
        assert cfg.api_base == DEFAULT_API_BASE

    def test_non_mapping_is_none(self):
        assert parse_load_control("ableedge").vendor == "none"

    def test_locked_vendors_refused(self):
        for vendor in ("span", "lumin", "savant"):
            cfg = parse_load_control({"vendor": vendor, "device_id": "x"})
            assert cfg.vendor == "none"

    def test_unknown_vendor_is_none(self):
        assert parse_load_control({"vendor": "acme"}).vendor == "none"

    def test_valid_vendors(self):
        assert parse_load_control({"vendor": "ableedge"}).vendor == "ableedge"
        assert parse_load_control({"vendor": "relay_only"}).vendor == "relay_only"

    def test_invalid_fail_safe_falls_back_to_off(self):
        assert parse_load_control({"fail_safe": "explode"}).fail_safe == "off"

    def test_ampacity_installer_only_never_guessed(self):
        assert parse_load_control({}).circuit_ampacity_a is None
        assert parse_load_control({"circuit_ampacity_a": 20}).circuit_ampacity_a == 20.0
        assert parse_load_control({"circuit_ampacity_a": 0}).circuit_ampacity_a is None
        assert parse_load_control({"circuit_ampacity_a": -15}).circuit_ampacity_a is None
        assert parse_load_control({"circuit_ampacity_a": "twenty"}).circuit_ampacity_a is None

    def test_inline_secret_values_are_ignored(self):
        cfg = parse_load_control(
            {
                "vendor": "ableedge",
                "credentials": {
                    "client_id": "THIS-IS-A-SECRET",
                    "client_id_env": "MY_CLIENT_ID",
                },
            }
        )
        assert cfg.credentials.client_id_env == "MY_CLIENT_ID"
        assert not hasattr(cfg.credentials, "client_id")

    def test_circuit_id_falls_back_to_device(self):
        cfg = parse_load_control({"device_id": "dev-1"})
        assert cfg.bound_circuit_id == "dev-1"

    def test_poll_s_clamped(self):
        assert parse_load_control({"poll_s": 1}).poll_s == 30
        assert parse_load_control({"poll_s": 45}).poll_s == 45

    def test_fallback_relay_range(self):
        assert parse_load_control({"fallback_relay": 3}).fallback_relay == 3
        assert parse_load_control({"fallback_relay": 9}).fallback_relay is None


class TestSecrets:
    def test_env_wins(self, tmp_path):
        refs = parse_load_control({}).credentials
        secrets = resolve_secrets(
            refs,
            environ={
                refs.client_id_env: "id",
                refs.client_secret_env: "secret",
                refs.subscription_key_env: "key",
            },
            secrets_dir=tmp_path,
        )
        assert secrets.complete
        assert secrets.client_id == "id"

    def test_file_fallback(self, tmp_path):
        (tmp_path / "client_id").write_text("file-id\n")
        (tmp_path / "client_secret").write_text("file-secret\n")
        (tmp_path / "subscription_key").write_text("file-key\n")
        refs = parse_load_control({}).credentials
        secrets = resolve_secrets(refs, environ={}, secrets_dir=tmp_path)
        assert secrets.complete
        assert secrets.client_secret == "file-secret"

    def test_incomplete_without_values(self, tmp_path):
        refs = parse_load_control({}).credentials
        secrets = resolve_secrets(refs, environ={}, secrets_dir=tmp_path)
        assert not secrets.complete


class TestMockClient:
    def test_set_and_status(self):
        client = MockAbleEdgeClient(_cfg())
        assert client.authenticate() is True
        result = client.set_circuit(True, reason="test")
        assert result.ok and result.on and result.via == "ableedge"
        status = client.get_status()
        assert status.on is True
        assert status.position == "close"
        assert status.circuit_id == "ckt-uuid"

    def test_unreachable_raises(self):
        client = MockAbleEdgeClient(_cfg())
        client.unreachable = True
        with pytest.raises(AbleEdgeUnreachableError):
            client.set_circuit(True)

    def test_auth_failure(self):
        client = MockAbleEdgeClient(_cfg())
        client.auth_ok = False
        with pytest.raises(AbleEdgeAuthError):
            client.authenticate()

    def test_power_is_whatever_the_test_set(self):
        client = MockAbleEdgeClient(_cfg())
        client.authenticate()
        from integrations.ableedge.client import PowerReading

        client.power = PowerReading(watts=120.0, volts=120.0, amps=1.0)
        reading = client.get_power()
        assert reading.watts == 120.0
        # Ampacity from config is not copied onto the reading.
        assert _cfg(circuit_ampacity_a=20).circuit_ampacity_a == 20.0
        assert reading.amps == 1.0


class TestFailSafe:
    def _controller(self, fail_safe: str, client: MockAbleEdgeClient | None = None, **over):
        cfg = _cfg(fail_safe=fail_safe, **over)
        mock = client or MockAbleEdgeClient(cfg)
        return LoadController(cfg, client=mock), mock

    def test_unreachable_set_fails_off(self):
        ctl, mock = self._controller("off")
        mock.authenticate()
        mock.set_circuit(True)
        mock.unreachable = True
        result = ctl.set_circuit(True)
        assert result.ok is False
        assert result.via == "fail_safe"
        assert result.fail_safe_applied == "off"
        assert result.on is False
        assert ctl.last_applied is False
        assert ctl.reachable is False

    def test_unreachable_keeps_last(self):
        ctl, mock = self._controller("last")
        mock.authenticate()
        ctl.set_circuit(True)
        mock.unreachable = True
        result = ctl.set_circuit(False)
        assert result.on is True
        assert result.fail_safe_applied == "last"

    def test_unreachable_last_with_no_history_is_off(self):
        ctl, mock = self._controller("last")
        mock.unreachable = True
        result = ctl.set_circuit(True)
        assert result.on is False

    def test_unreachable_can_fail_on(self):
        ctl, mock = self._controller("on")
        mock.unreachable = True
        result = ctl.set_circuit(False)
        assert result.on is True
        assert result.fail_safe_applied == "on"

    def test_fail_safe_latches_once(self):
        relays = MagicMock()
        ctl, mock = self._controller("off", fallback_relay=2)
        ctl._relays = relays
        mock.unreachable = True
        ctl.poll()
        ctl.poll()
        relays.set.assert_called_once_with(2, False)

    def test_reachable_again_clears_latch(self):
        ctl, mock = self._controller("off")
        mock.unreachable = True
        ctl.poll()
        assert ctl._fail_safe_latched is True
        mock.unreachable = False
        mock.authenticate()
        ctl.poll()
        assert ctl.reachable is True
        assert ctl._fail_safe_latched is False

    def test_missing_client_is_fail_safe(self):
        cfg = _cfg()
        ctl = LoadController(cfg, client=None)
        result = ctl.set_circuit(True)
        assert result.via == "fail_safe"
        assert result.on is False

    def test_vendor_none_refuses(self):
        ctl = LoadController(parse_load_control({"vendor": "none"}), client=None)
        result = ctl.set_circuit(True)
        assert result.ok is False
        assert result.via == "none"

    def test_relay_only_uses_fallback_not_ableedge(self):
        relays = MagicMock()
        cfg = parse_load_control({"vendor": "relay_only", "fallback_relay": 3})
        ctl = LoadController(cfg, client=MockAbleEdgeClient(cfg), relays=relays)
        result = ctl.set_circuit(True)
        assert result.ok is True
        assert result.via == "relay_only"
        relays.set.assert_called_once_with(3, True)
        assert ctl.client.set_calls == []

    def test_shutdown_fail_off(self):
        ctl, mock = self._controller("off")
        mock.authenticate()
        ctl.set_circuit(True)
        ctl.shutdown()
        assert ctl.last_applied is False

    def test_get_power_does_not_use_ampacity(self):
        cfg = _cfg(circuit_ampacity_a=30)
        mock = MockAbleEdgeClient(cfg)
        mock.authenticate()
        ctl = LoadController(cfg, client=mock)
        reading = ctl.get_power()
        assert reading.amps is None
        assert reading.watts is None


class TestBuildClient:
    def test_mock_backend_needs_no_secrets(self):
        client = build_client(_cfg(backend="mock"), environ={})
        assert isinstance(client, MockAbleEdgeClient)

    def test_http_without_secrets_is_none(self):
        client = build_client(_cfg(backend="http"), environ={}, secrets=AbleEdgeSecrets("", "", ""))
        assert client is None

    def test_http_without_device_id_is_none(self):
        secrets = AbleEdgeSecrets("id", "secret", "key")
        client = build_client(_cfg(backend="http", device_id=""), secrets=secrets, environ={})
        assert client is None

    def test_env_mock_overrides_http(self):
        client = build_client(_cfg(backend="http"), environ={"ABLEEDGE_BACKEND": "mock"})
        assert isinstance(client, MockAbleEdgeClient)


class TestMeterParse:
    def test_watts_only_when_v_and_i_present(self):
        reading = parse_meter_reading(
            {"data": {"voltageAN": {"val": 120}, "currentA": {"val": 2.5}}}
        )
        assert reading.watts == 300.0
        assert reading.volts == 120.0
        assert reading.amps == 2.5

    def test_no_invented_watts_from_voltage_alone(self):
        reading = parse_meter_reading({"data": {"voltageAN": {"val": 120}}})
        assert reading.watts is None
        assert reading.volts == 120.0

    def test_empty_body(self):
        assert parse_meter_reading({}).watts is None


class _Resp:
    def __init__(self, status, body=None):
        self.status = status
        self._body = b"" if body is None else json.dumps(body).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class TestHttpClient:
    def _client(self):
        cfg = parse_load_control(
            {"vendor": "ableedge", "device_id": "dev-1", "api_base": "https://api.em.eaton.com"}
        )
        secrets = AbleEdgeSecrets("cid", "csec", "subkey")
        return HttpAbleEdgeClient(cfg, secrets)

    def test_authenticate_and_set_circuit_mapping(self, monkeypatch):
        captured = []

        def fake_urlopen(req, timeout=None):
            captured.append(
                {
                    "url": req.full_url,
                    "method": req.get_method(),
                    "headers": {k.lower(): v for k, v in req.header_items()},
                    "body": json.loads(req.data.decode()) if req.data else None,
                }
            )
            if req.full_url.endswith("/serviceAccount/authToken"):
                return _Resp(200, {"data": {"token": "tok", "expiresAt": "2099-01-01T00:00:00Z"}})
            return _Resp(204)

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        client = self._client()
        assert client.authenticate() is True
        result = client.set_circuit(True, reason="awg")
        assert result.ok and result.on
        assert captured[0]["body"] == {"clientId": "cid", "clientSecret": "csec"}
        assert captured[0]["headers"]["em-api-subscription-key"] == "subkey"
        on_cmd = captured[1]["body"]
        assert on_cmd["command"] == "close"
        client.set_circuit(False)
        assert captured[2]["body"]["command"] == "open"

    def test_auth_failure(self, monkeypatch):
        monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _Resp(401, {}))
        client = self._client()
        with pytest.raises(AbleEdgeAuthError):
            client.authenticate()

    def test_get_status_unreachable(self, monkeypatch):
        import urllib.error

        def boom(*_a, **_k):
            raise urllib.error.URLError("down")

        monkeypatch.setattr("urllib.request.urlopen", boom)
        client = self._client()
        client._token = "tok"
        client._token_expires_mono = 1e18
        with pytest.raises(AbleEdgeUnreachableError):
            client.get_status()


class TestConfigManagerLoadControl:
    def test_nested_block_loads(self, tmp_path, mock_hardware):
        from utils.config import ConfigManager

        base = tmp_path / "config.yaml"
        base.write_text("load_control:\n  vendor: ableedge\n  device_id: abc\n  fail_safe: last\n")
        mgr = ConfigManager(str(base), str(tmp_path / "remote.yaml"))
        assert mgr.settings.load_control["vendor"] == "ableedge"
        assert mgr.settings.load_control["device_id"] == "abc"

    def test_remote_overlay_cannot_rebind(self, tmp_path, mock_hardware):
        from utils.config import ConfigManager

        base = tmp_path / "config.yaml"
        base.write_text("load_control:\n  vendor: ableedge\n  device_id: local\n")
        remote = tmp_path / "remote.yaml"
        remote.write_text(
            "version: 1\nvalues:\n  load_control:\n    vendor: none\n    device_id: evil\n"
        )
        mgr = ConfigManager(str(base), str(remote))
        assert mgr.settings.load_control["device_id"] == "local"


class TestAbleEdgePollWorker:
    def test_polls_controller(self, mock_hardware):
        from app.workers import AbleEdgePollWorker

        calls = []
        load = SimpleNamespace(poll_s=12, poll=lambda: calls.append(1))
        worker = AbleEdgePollWorker(load)
        assert worker.interval_s() == 12
        worker.step()
        assert calls == [1]


class TestCommandPathHook:
    """Cloud / socket AWG commands must not go through sensor sampling."""

    def test_handle_cmd_circuit_set(self, firmware_with_load):
        _, app, ctl = firmware_with_load
        result = app._handle_cmd({"action": "circuit_set", "state": True})
        assert result["ok"] is True
        assert result["via"] == "ableedge"
        assert ctl.last_applied is True

    def test_handle_cmd_rejects_non_bool(self, firmware_with_load):
        _, app, _ = firmware_with_load
        result = app._handle_cmd({"action": "awg_set", "state": "on"})
        assert result["ok"] is False

    def test_cloud_awg_command_acks(self, firmware_with_load):
        _, app, ctl = firmware_with_load
        acks = []
        app._cloud = SimpleNamespace(
            ack_command=lambda cid, status, err=None: acks.append((cid, status, err))
        )
        app._apply_cloud_command({"id": "c9", "type": "awg", "state": False})
        assert acks == [("c9", "done", None)]
        assert ctl.last_applied is False

    def test_relay_command_still_independent(self, firmware_with_load):
        _, app, ctl = firmware_with_load
        result = app._handle_cmd({"action": "relay_set", "channel": 1, "state": True})
        assert result["ok"] is True
        assert ctl.client.set_calls == []

    def test_from_settings_mock(self):
        ctl = LoadController.from_settings(
            {"vendor": "ableedge", "device_id": "d", "backend": "mock"},
            environ={"ABLEEDGE_BACKEND": "mock"},
        )
        assert ctl.vendor == "ableedge"
        assert isinstance(ctl.client, MockAbleEdgeClient)


@pytest.fixture
def firmware_with_load(monkeypatch, tmp_path, mock_hardware):
    """Started WQM1App with a mock AbleEdge controller already bound."""
    import main
    from utils.config import ConfigManager

    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "board: rpi-zero-2w\n"
        "load_control:\n"
        "  vendor: ableedge\n"
        "  device_id: dev-uuid\n"
        "  backend: mock\n"
        "  fail_safe: off\n"
    )
    mgr = ConfigManager(str(cfg), str(tmp_path / "config.d" / "remote.yaml"))
    monkeypatch.setattr(main, "get_config_manager", lambda: mgr)
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

    app = main.WQM1App()
    app.start()
    # Force a deterministic mock client (from_settings already used backend=mock).
    assert app._load is not None
    return main, app, app._load


class TestCircuitCommandResultShape:
    def test_fields(self):
        r = CircuitCommandResult(ok=True, on=True, via="ableedge", reachable=True)
        assert r.error is None
        assert r.fail_safe_applied is None
