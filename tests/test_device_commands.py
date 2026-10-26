"""Cloud device-command poller (marketplace #1476, firmware plan item D).

Pins GET .../commands/pending, the ph_calibrate / rs485_config dispatcher,
and POST .../result. Existing deviceCommands poll, ingest, and GET /config
stay on their own paths — this file must not rewrite those tests.
"""

from __future__ import annotations

import inspect
import json
import math
import urllib.error
from datetime import datetime
from itertools import count
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.state import StateStore
from app.workers import CommandWorker
from calibration.calibrate import CalibrationManager
from cloud.client import RESULT_POST_DROP, CloudClient
from cloud.device_commands import (
    ALLOWED_SENSORS,
    ANALOG_BUFFERS,
    ANALOG_POINT_MAX_SPREAD_V,
    ANALOG_POINT_READS,
    HANDLED_COMMANDS_MAX,
    RS485_BUFFER_LABELS,
    WIRE_SENSORS,
    CommandHardware,
    DeviceCommandDispatcher,
)
from cloud.device_hardware import Wqm1CommandHardware
from sensors.honde import HondeChlorineSensor, HondeMultiSensor, HondeOrpSensor
from utils.config import ADC_CH_PH, ConfigManager

NOW_MS = 1_700_000_000_000
FUTURE_MS = NOW_MS + 60_000
PAST_MS = NOW_MS - 1
_CMD_IDS = count(1)


def _cmd(
    *,
    cmd_id: str | None = None,
    cmd_type: str = "ph_calibrate",
    payload: dict | None = None,
    expires_at: int = FUTURE_MS,
    extra: dict | None = None,
) -> dict:
    body = {
        "id": cmd_id if cmd_id is not None else f"cmd-{next(_CMD_IDS)}",
        "type": cmd_type,
        "payload": payload if payload is not None else {},
        "expiresAt": expires_at,
    }
    if extra:
        body.update(extra)
    return body


def _ph(*, step: str, probe: str, buffer: float | None = None, extra: dict | None = None) -> dict:
    payload: dict = {"step": step, "probe": probe}
    if buffer is not None:
        payload["buffer"] = buffer
    if extra:
        payload.update(extra)
    return _cmd(payload=payload)


def _rs485(*, op: str, sensor: str, extra_payload: dict | None = None, **fields) -> dict:
    payload = {"op": op, "sensor": sensor, **fields}
    if extra_payload:
        payload.update(extra_payload)
    return _cmd(cmd_type="rs485_config", payload=payload)


class FakeHardware:
    """Records calls. No relay, breaker, or GPIO methods exist on purpose."""

    def __init__(self, voltages: list[float] | None = None) -> None:
        self.voltages = list(voltages or [1.04, 1.50])
        self.analog_applied: list[tuple[float, float]] = []
        self.rs485_points: list[str] = []
        self.ops: list[tuple] = []
        self.sensors = [
            {"sensor": name, "address": addr, "enabled": False}
            for addr, name in enumerate(WIRE_SENSORS, start=1)
        ]

    def read_ph_adc_volts(self) -> float:
        if not self.voltages:
            raise RuntimeError("no ADC voltage queued")
        return self.voltages.pop(0)

    def apply_analog_ph(self, v_ph4: float, v_ph7: float) -> dict[str, float]:
        # The live path must go through calibrate_ph, not a copy of the math.
        cm = CalibrationManager(path="/tmp/device-commands-unused.yaml")
        slope = cm.calibrate_ph(v_ph4, v_ph7)
        self.analog_applied.append((v_ph4, v_ph7))
        offset = 7.0 - slope * v_ph7
        return {"slope": slope, "offset": offset}

    def calibrate_rs485_ph_point(self, buffer: str) -> None:
        if buffer not in HondeMultiSensor.PH_CAL_POINTS:
            raise ValueError(f"unknown pH buffer: {buffer!r}")
        self.rs485_points.append(buffer)

    def rs485_add(self, sensor: str, address: int) -> list[dict]:
        self.ops.append(("add", sensor, address))
        self._upsert(sensor, address=address, enabled=True)
        return list(self.sensors)

    def rs485_set_address(self, sensor: str, address: int, new_address: int) -> list[dict]:
        self.ops.append(("set_address", sensor, address, new_address))
        self._upsert(sensor, address=new_address)
        return list(self.sensors)

    def rs485_set_enabled(self, sensor: str, address: int | None, enabled: bool) -> list[dict]:
        self.ops.append(("set_enabled", sensor, address, enabled))
        self._upsert(sensor, address=address, enabled=enabled)
        return list(self.sensors)

    def rs485_list(self) -> list[dict]:
        return list(self.sensors)

    def _upsert(
        self, sensor: str, *, address: int | None = None, enabled: bool | None = None
    ) -> None:
        for row in self.sensors:
            if row["sensor"] == sensor:
                if address is not None:
                    row["address"] = address
                if enabled is not None:
                    row["enabled"] = enabled
                return
        self.sensors.append(
            {
                "sensor": sensor,
                "address": address or 1,
                "enabled": bool(enabled),
            }
        )


def _dispatch(cmd: dict, hw: FakeHardware | None = None, now_ms: int = NOW_MS):
    return DeviceCommandDispatcher(hardware=hw or FakeHardware(), now_ms=lambda: now_ms).apply(cmd)


class TestDispatcherRejects:
    def test_unknown_type(self):
        out = _dispatch(_cmd(cmd_type="identify", payload={"step": "start"}))
        assert out.status == "rejected"
        assert "unknown type" in out.message.lower()
        assert len(out.message) <= 200

    def test_legacy_relay_type_is_unknown(self):
        out = _dispatch(_cmd(cmd_type="relay", payload={"channel": 1, "state": True}))
        assert out.status == "rejected"
        assert "unknown type" in out.message.lower()

    def test_awg_and_circuit_types_are_unknown(self):
        for cmd_type in ("awg", "circuit"):
            out = _dispatch(_cmd(cmd_type=cmd_type, payload={"state": True}))
            assert out.status == "rejected"
            assert "unknown type" in out.message.lower()

    def test_extra_command_field(self):
        out = _dispatch(_cmd(payload={"step": "start", "probe": "analog"}, extra={"channel": 1}))
        assert out.status == "rejected"
        assert "unexpected" in out.message.lower()

    def test_extra_payload_field(self):
        out = _dispatch(_ph(step="start", probe="analog", extra={"gpio": 17}))
        assert out.status == "rejected"
        assert "unexpected" in out.message.lower()

    def test_expired(self):
        out = _dispatch(
            _cmd(
                payload={"step": "start", "probe": "analog"},
                expires_at=PAST_MS,
            )
        )
        assert out.status == "rejected"
        assert "expired" in out.message.lower()

    def test_non_finite_buffer(self):
        for value in (math.nan, math.inf, -math.inf):
            out = _dispatch(_ph(step="point", probe="analog", buffer=value))
            assert out.status == "rejected"
            assert "non-finite" in out.message.lower()

    def test_non_finite_address(self):
        out = _dispatch(
            _rs485(
                op="add",
                sensor="RD-ORP-WE-01",
                address=math.inf,
            )
        )
        assert out.status == "rejected"
        assert "non-finite" in out.message.lower()

    def test_bool_is_not_a_number(self):
        out = _dispatch(_ph(step="point", probe="analog", buffer=True))  # type: ignore[arg-type]
        assert out.status == "rejected"

    def test_analog_rejects_rs485_buffers_and_ten(self):
        hw = FakeHardware()
        for buffer in (4.01, 6.86, 9.18, 10.0):
            out = _dispatch(_ph(step="point", probe="analog", buffer=buffer), hw)
            assert out.status == "rejected"
            assert "buffer" in out.message.lower()
        assert hw.rs485_points == []
        assert hw.analog_applied == []

    def test_rs485_rejects_analog_buffers_and_ten(self):
        hw = FakeHardware()
        for buffer in (4.0, 7.0, 10.0):
            out = _dispatch(_ph(step="point", probe="rs485_multi", buffer=buffer), hw)
            assert out.status == "rejected"
            assert "buffer" in out.message.lower()
        assert hw.rs485_points == []

    def test_unknown_probe(self):
        out = _dispatch(_ph(step="start", probe="digital_orp"))
        assert out.status == "rejected"

    def test_unknown_rs485_model(self):
        out = _dispatch(_rs485(op="add", sensor="ModbusFlowMeter", address=4))
        assert out.status == "rejected"
        assert "sensor" in out.message.lower()

    def test_address_bounds(self):
        for address in (0, 248):
            out = _dispatch(_rs485(op="add", sensor="residual_chlorine", address=address))
            assert out.status == "rejected"


class TestDispatcherHasNoActuationPath:
    def test_module_source_has_no_relay_breaker_or_gpio(self):
        from cloud import device_commands

        src = inspect.getsource(device_commands)
        lowered = src.lower()
        for needle in ("relay", "breaker", "gpio", "awg", "eaton"):
            assert needle not in lowered, f"dispatcher source mentions {needle!r}"

    def test_hardware_protocol_has_no_actuation_methods(self):
        public = {n for n in dir(CommandHardware) if not n.startswith("_")}
        for needle in ("relay", "breaker", "gpio", "awg"):
            assert all(needle not in n.lower() for n in public), public
        assert "read_ph_adc_volts" in public
        params = inspect.signature(CommandHardware.read_ph_adc_volts).parameters
        assert set(params) == {"self"}


class TestPhCalibrateAnalog:
    def test_start_point_finish_uses_calibrate_ph(self):
        hw = FakeHardware(voltages=[1.00] * ANALOG_POINT_READS + [1.60] * ANALOG_POINT_READS)
        d = DeviceCommandDispatcher(hardware=hw, now_ms=lambda: NOW_MS)
        start = d.apply(_ph(step="start", probe="analog"))
        assert start.status == "applied"
        assert start.result is not None
        assert "slope" not in start.result
        assert "offset" not in start.result
        p4 = d.apply(_ph(step="point", probe="analog", buffer=4.0))
        assert p4.status == "applied"
        assert p4.result is not None
        assert "slope" not in p4.result
        assert "offset" not in p4.result
        p7 = d.apply(_ph(step="point", probe="analog", buffer=7.0))
        assert p7.status == "applied"
        assert "slope" not in (p7.result or {})
        done = d.apply(_ph(step="finish", probe="analog"))
        assert done.status == "applied"
        assert hw.analog_applied == [(1.00, 1.60)]
        assert done.result is not None
        assert done.result["slope"] == pytest.approx(5.0)
        assert done.result["offset"] == pytest.approx(7.0 - 5.0 * 1.60)
        assert done.result["buffers"] == [4.0, 7.0]
        datetime.strptime(done.result["measuredAt"], "%Y-%m-%dT%H:%M:%SZ")
        assert len(done.message) <= 200

        reuse = d.apply(_ph(step="finish", probe="analog"))
        assert reuse.status == "rejected"
        assert "start" in reuse.message.lower()
        assert d._analog_volts == {}
        assert hw.analog_applied == [(1.00, 1.60)]

    def test_finish_without_both_points_is_rejected(self):
        hw = FakeHardware(voltages=[1.00] * ANALOG_POINT_READS)
        d = DeviceCommandDispatcher(hardware=hw, now_ms=lambda: NOW_MS)
        d.apply(_ph(step="start", probe="analog"))
        d.apply(_ph(step="point", probe="analog", buffer=4.0))
        out = d.apply(_ph(step="finish", probe="analog"))
        assert out.status == "rejected"
        assert hw.analog_applied == []


class TestPhCalibrateRs485:
    def test_points_call_calibrate_ph_point(self):
        hw = FakeHardware()
        d = DeviceCommandDispatcher(hardware=hw, now_ms=lambda: NOW_MS)
        start = d.apply(_ph(step="start", probe="rs485_multi"))
        assert start.status == "applied"
        assert start.result is not None
        assert "slope" not in start.result
        assert "offset" not in start.result
        for buffer in (4.01, 6.86, 9.18):
            out = d.apply(_ph(step="point", probe="rs485_multi", buffer=buffer))
            assert out.status == "applied", out.message
            assert "slope" not in (out.result or {})
        assert hw.rs485_points == ["4.01", "6.86", "9.18"]
        done = d.apply(_ph(step="finish", probe="rs485_multi"))
        assert done.status == "applied"
        assert done.result is not None
        assert done.result["buffers"] == [4.01, 6.86, 9.18]
        assert done.result["slope"] is None
        assert done.result["offset"] is None
        datetime.strptime(done.result["measuredAt"], "%Y-%m-%dT%H:%M:%SZ")

    def test_finish_without_points_is_rejected(self):
        d = DeviceCommandDispatcher(hardware=FakeHardware(), now_ms=lambda: NOW_MS)
        assert d.apply(_ph(step="start", probe="rs485_multi")).status == "applied"
        out = d.apply(_ph(step="finish", probe="rs485_multi"))
        assert out.status == "rejected"
        assert "point" in out.message.lower()


class TestRs485Config:
    def test_add_set_address_enable_disable(self):
        hw = FakeHardware()
        d = DeviceCommandDispatcher(hardware=hw, now_ms=lambda: NOW_MS)
        name = "residual_chlorine"
        added = d.apply(_rs485(op="add", sensor=name, address=5))
        assert added.status == "applied"
        assert added.result == {"sensors": hw.rs485_list()}
        assert ("add", name, 5) in hw.ops

        moved = d.apply(_rs485(op="set_address", sensor=name, address=5, newAddress=7))
        assert moved.status == "applied"
        assert any(row["address"] == 7 and row["sensor"] == name for row in moved.result["sensors"])

        enabled = d.apply(_rs485(op="enable", sensor=name, address=7, enabled=True))
        assert enabled.status == "applied"
        disabled = d.apply(_rs485(op="disable", sensor=name, address=7, enabled=False))
        assert disabled.status == "applied"
        row = next(r for r in disabled.result["sensors"] if r["sensor"] == name)
        assert row["enabled"] is False

    def test_only_the_three_allowed_models(self):
        hw = FakeHardware()
        d = DeviceCommandDispatcher(hardware=hw, now_ms=lambda: NOW_MS)
        for name in WIRE_SENSORS:
            out = d.apply(_rs485(op="add", sensor=name, address=11))
            assert out.status == "applied", out.message
        out = d.apply(_rs485(op="add", sensor="HondeWhatever", address=12))
        assert out.status == "rejected"
        assert out.message == "sensor not allowed"


class TestCloudClientPendingAndResult:
    def _client(self):
        c = CloudClient(
            device_id="BS-WQM1-00aabbccddee",
            ingest_url="https://example.test/ingestReading",
            command_url="https://example.test/deviceCommands",
            api_key="device-key",
            fw_version="2.4.0",
            api_base="https://example.test/app",
        )
        c._sleep = lambda *_: None
        return c

    def test_fetch_pending_is_get_with_device_key(self, monkeypatch):
        c = self._client()
        captured = []

        class _Resp:
            status = 200

            def read(self):
                return json.dumps(
                    {
                        "commands": [
                            {
                                "id": "c1",
                                "type": "ph_calibrate",
                                "payload": {"step": "start", "probe": "analog"},
                                "expiresAt": FUTURE_MS,
                            }
                        ]
                    }
                ).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            captured.append(
                {
                    "url": req.full_url,
                    "method": req.get_method(),
                    "headers": {k.lower(): v for k, v in req.header_items()},
                    "body": req.data,
                }
            )
            return _Resp()

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        cmds = c.fetch_pending_commands()
        assert cmds[0]["id"] == "c1"
        assert captured[0]["method"] == "GET"
        assert captured[0]["url"].endswith("/v2/devices/BS-WQM1-00aabbccddee/commands/pending")
        assert captured[0]["headers"]["x-api-key"] == "device-key"
        assert captured[0]["body"] is None

    def test_fetch_pending_empty_on_failure(self, monkeypatch):
        c = self._client()
        c._max_retries = 1

        def boom(req, timeout=None):
            raise urllib.error.URLError("down")

        monkeypatch.setattr("urllib.request.urlopen", boom)
        assert c.fetch_pending_commands() == []

    def test_post_result_shape_and_message_cap(self, monkeypatch):
        c = self._client()
        captured = []

        class _Resp:
            status = 200

            def read(self):
                return b"{}"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            captured.append(
                {
                    "url": req.full_url,
                    "method": req.get_method(),
                    "body": json.loads(req.data.decode()),
                }
            )
            return _Resp()

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        c.post_command_result(
            "c9",
            "rejected",
            result=None,
            message="x" * 500,
        )
        assert captured[0]["method"] == "POST"
        assert captured[0]["url"].endswith("/v2/devices/BS-WQM1-00aabbccddee/commands/c9/result")
        assert captured[0]["body"]["status"] == "rejected"
        assert captured[0]["body"]["message"] == "x" * 200
        assert "result" not in captured[0]["body"]

    def test_existing_poll_and_ingest_urls_unchanged(self, monkeypatch):
        c = self._client()
        captured = []

        class _Resp:
            status = 200

            def read(self):
                return json.dumps({"commands": [], "configVersion": 3}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            captured.append({"url": req.full_url, "method": req.get_method(), "body": req.data})
            return _Resp()

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        poll = c.poll()
        assert poll["configVersion"] == 3
        assert captured[0]["url"] == "https://example.test/deviceCommands"
        assert json.loads(captured[0]["body"].decode()) == {
            "deviceId": "BS-WQM1-00aabbccddee",
            "action": "poll",
        }

    def test_no_api_base_skips_pending(self):
        c = CloudClient(
            device_id="BS-WQM1-00aabbccddee",
            ingest_url="https://example.test/ingestReading",
            command_url="https://example.test/deviceCommands",
            api_key="device-key",
            fw_version="2.4.0",
            api_base="",
        )
        assert c.fetch_pending_commands() == []
        c.post_command_result("c1", "applied")  # must not raise


class TestCommandWorkerPendingTick:
    def test_same_tick_keeps_legacy_poll_and_posts_v2_result(self, mock_hardware):
        hw = FakeHardware(voltages=[1.00] * ANALOG_POINT_READS + [1.60] * ANALOG_POINT_READS)
        dispatcher = DeviceCommandDispatcher(hardware=hw, now_ms=lambda: NOW_MS)
        posted = []
        legacy = []
        start = _ph(step="start", probe="analog")

        cloud = SimpleNamespace(
            poll=lambda: {
                "commands": [{"id": "legacy", "type": "restart"}],
                "configVersion": 4,
                "otaPending": False,
            },
            fetch_pending_commands=lambda: [start],
            post_command_result=lambda *a, **k: posted.append((a, k)),
            send_event=lambda *a, **k: None,
        )
        versions = []
        worker = CommandWorker(
            lambda: SimpleNamespace(command_poll_s=5, cloud_enabled=True),
            cloud,
            StateStore(),
            apply_command=legacy.append,
            apply_device_command=dispatcher.apply,
            on_config_version=versions.append,
        )
        worker.step()
        assert legacy == [{"id": "legacy", "type": "restart"}]
        assert versions == [4]
        assert posted, "v2 result must be posted"
        args, kwargs = posted[0]
        assert args[0] == start["id"]
        assert args[1] == "applied"

    def test_missing_v2_methods_leave_legacy_poll_alone(self, mock_hardware):
        legacy = []
        cloud = SimpleNamespace(
            poll=lambda: {"commands": [{"id": "x", "type": "ota_check"}], "configVersion": None},
            send_event=lambda *a, **k: None,
        )
        worker = CommandWorker(
            lambda: SimpleNamespace(command_poll_s=5),
            cloud,
            StateStore(),
            apply_command=legacy.append,
        )
        worker.step()
        assert legacy == [{"id": "x", "type": "ota_check"}]


class TestAnalogCalibratePhIsTheRealFunction:
    def test_hardware_apply_calls_calibrate_ph(self, tmp_path):
        cal = CalibrationManager(path=str(tmp_path / "cal.yaml"))
        ph = MagicMock()
        channels: list[int] = []
        app = SimpleNamespace(
            _cal=cal,
            _ph=ph,
            _adc=SimpleNamespace(read_voltage=lambda ch: channels.append(ch) or 1.23),
            _adc_error=None,
            _config=SimpleNamespace(_base_path=tmp_path / "config.yaml"),
            _settings=SimpleNamespace(
                rs485_chlorine_enabled=False,
                rs485_chlorine_addr=1,
                rs485_orp_enabled=False,
                rs485_orp_addr=2,
                rs485_multi_enabled=False,
                rs485_multi_addr=3,
                rs485_port="/dev/ttyUSB0",
                rs485_baud=9600,
            ),
            _modbus_bus=None,
            _chlorine=None,
            _orp=None,
            _multi=None,
            _sensor_map=None,
        )
        (tmp_path / "config.yaml").write_text("{}\n")
        hw = Wqm1CommandHardware(app)
        applied = hw.apply_analog_ph(1.00, 1.60)
        assert applied["slope"] == pytest.approx(5.0)
        assert cal.data.ph_v_at_4 == 1.00
        assert cal.data.ph_v_at_7 == 1.60
        ph.set_calibration.assert_called_once()
        assert hw.read_ph_adc_volts() == 1.23
        assert channels == [ADC_CH_PH]

    def test_rs485_list_and_disable_use_local_config(self, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text("rs485_chlorine_enabled: true\nrs485_chlorine_addr: 5\n")
        config = ConfigManager(str(cfg_path), str(tmp_path / "remote.yaml"))
        chlorine = MagicMock()
        app = SimpleNamespace(
            _cal=None,
            _ph=None,
            _adc=None,
            _adc_error=None,
            _config=config,
            _settings=config.settings,
            _modbus_bus=None,
            _chlorine=chlorine,
            _orp=None,
            _multi=None,
            _sensor_map=None,
        )
        hw = Wqm1CommandHardware(app)
        listed = hw.rs485_list()
        chlorine_row = next(r for r in listed if r["sensor"] == "residual_chlorine")
        assert chlorine_row == {
            "sensor": "residual_chlorine",
            "address": 5,
            "enabled": True,
        }
        after = hw.rs485_set_enabled("residual_chlorine", 5, False)
        assert app._chlorine is None
        row = next(r for r in after if r["sensor"] == "residual_chlorine")
        assert row["enabled"] is False
        assert config.settings.rs485_chlorine_enabled is False


def _real_app(tmp_path, yaml_text: str = ""):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml_text or "rs485_port: /dev/ttyTEST\n")
    config = ConfigManager(str(cfg_path), str(tmp_path / "remote.yaml"))
    sync = MagicMock()
    app = SimpleNamespace(
        _cal=None,
        _ph=None,
        _adc=None,
        _adc_error=None,
        _config=config,
        _modbus_bus=MagicMock(),
        _chlorine=None,
        _orp=None,
        _multi=None,
        _sync_sampler=sync,
    )
    return app, config, sync


class TestSetAddressUsesCommandAddress:
    def test_mismatch_does_not_write_the_live_probe_or_config(self, tmp_path):
        app, config, sync = _real_app(
            tmp_path, "rs485_chlorine_enabled: true\nrs485_chlorine_addr: 1\n"
        )
        bus = MagicMock()
        probe = HondeChlorineSensor(bus, address=1)
        app._modbus_bus = bus
        app._chlorine = probe
        hw = Wqm1CommandHardware(app)
        d = DeviceCommandDispatcher(hardware=hw, now_ms=lambda: NOW_MS)

        out = d.apply(
            _rs485(
                op="set_address",
                sensor="residual_chlorine",
                address=5,
                newAddress=7,
            )
        )
        assert out.status == "rejected"
        assert out.message == "no probe at that address"

        bus.write_register.assert_not_called()
        assert probe.address == 1
        assert config.settings.rs485_chlorine_addr == 1
        sync.assert_not_called()

    def test_matching_address_readdresses_that_probe_and_syncs(self, tmp_path):
        app, config, sync = _real_app(
            tmp_path, "rs485_chlorine_enabled: true\nrs485_chlorine_addr: 5\n"
        )
        bus = MagicMock()
        probe = HondeChlorineSensor(bus, address=5)
        app._modbus_bus = bus
        app._chlorine = probe
        hw = Wqm1CommandHardware(app)

        rows = hw.rs485_set_address("residual_chlorine", 5, 7)
        bus.write_register.assert_called_once_with(5, HondeChlorineSensor.REG_ADDRESS, 7)
        assert probe.address == 7
        assert config.settings.rs485_chlorine_addr == 7
        sync.assert_called()
        row = next(r for r in rows if r["sensor"] == "residual_chlorine")
        assert row["address"] == 7


class TestAnalogPointMedian:
    def test_stable_samples_store_the_median(self):
        # First sample is not the median, so a single-shot read would store 1.01.
        samples = [1.01, 1.00, 1.00, 1.00, 1.00]
        assert len(samples) == ANALOG_POINT_READS
        hw = FakeHardware(voltages=list(samples))
        d = DeviceCommandDispatcher(hardware=hw, now_ms=lambda: NOW_MS)
        d.apply(_ph(step="start", probe="analog"))
        out = d.apply(_ph(step="point", probe="analog", buffer=4.0))
        assert out.status == "applied"
        assert d._analog_volts[4.0] == pytest.approx(1.00)

    def test_noisy_samples_are_rejected(self):
        samples = [1.00, 1.00, 1.00, 1.00, 1.00 + ANALOG_POINT_MAX_SPREAD_V + 0.01]
        assert len(samples) == ANALOG_POINT_READS
        hw = FakeHardware(voltages=list(samples))
        d = DeviceCommandDispatcher(hardware=hw, now_ms=lambda: NOW_MS)
        d.apply(_ph(step="start", probe="analog"))
        out = d.apply(_ph(step="point", probe="analog", buffer=4.0))
        assert out.status == "rejected"
        assert "stable" in out.message.lower() or "spread" in out.message.lower()
        assert 4.0 not in d._analog_volts


class TestHandledCommandIds:
    def test_same_id_is_not_reapplied(self):
        hw = FakeHardware(voltages=[1.00] * ANALOG_POINT_READS * 2)
        d = DeviceCommandDispatcher(hardware=hw, now_ms=lambda: NOW_MS)
        d.apply(_ph(step="start", probe="analog"))
        cmd = _ph(step="point", probe="analog", buffer=4.0)
        first = d.apply(cmd)
        remaining_after_first = len(hw.voltages)
        second = d.apply(cmd)
        assert first.status == "applied"
        assert second.status == "applied"
        assert second.result == first.result
        assert len(hw.voltages) == remaining_after_first


class TestRealRs485Adapter:
    def test_add_creates_driver_writes_config_and_syncs(self, tmp_path):
        app, config, sync = _real_app(tmp_path)
        hw = Wqm1CommandHardware(app)
        rows = hw.rs485_add("RD-ORP-WE-01", 6)
        assert isinstance(app._orp, HondeOrpSensor)
        assert app._orp.address == 6
        assert config.settings.rs485_orp_enabled is True
        assert config.settings.rs485_orp_addr == 6
        sync.assert_called()
        row = next(r for r in rows if r["sensor"] == "RD-ORP-WE-01")
        assert row == {"sensor": "RD-ORP-WE-01", "address": 6, "enabled": True}

    def test_enable_builds_the_driver_and_syncs(self, tmp_path):
        app, config, sync = _real_app(tmp_path)
        hw = Wqm1CommandHardware(app)
        hw.rs485_set_enabled("RD-PETSTS-01", 8, True)
        assert isinstance(app._multi, HondeMultiSensor)
        assert app._multi.address == 8
        assert config.settings.rs485_multi_enabled is True
        assert config.settings.rs485_multi_addr == 8
        sync.assert_called()

    def test_disable_clears_driver_and_syncs(self, tmp_path):
        app, config, sync = _real_app(
            tmp_path, "rs485_chlorine_enabled: true\nrs485_chlorine_addr: 5\n"
        )
        app._chlorine = HondeChlorineSensor(app._modbus_bus, address=5)
        hw = Wqm1CommandHardware(app)
        hw.rs485_set_enabled("residual_chlorine", 5, False)
        assert app._chlorine is None
        assert config.settings.rs485_chlorine_enabled is False
        sync.assert_called()


class TestPendingWorkerFailures:
    def test_exception_inside_apply_posts_rejected(self, mock_hardware):
        posted = []

        def apply(_cmd):
            raise RuntimeError("boom")

        cloud = SimpleNamespace(
            poll=lambda: {"commands": [], "configVersion": None},
            fetch_pending_commands=lambda: [_ph(step="start", probe="analog")],
            post_command_result=lambda *a, **k: posted.append((a, k)) or True,
            send_event=lambda *a, **k: None,
        )
        CommandWorker(
            lambda: SimpleNamespace(command_poll_s=5, cloud_enabled=True),
            cloud,
            StateStore(),
            apply_command=lambda _c: None,
            apply_device_command=apply,
        ).step()
        assert posted
        assert posted[0][0][1] == "rejected"
        assert "boom" in posted[0][0][3]

    def test_failed_result_post_retries_without_reapplying(self, mock_hardware):
        hw = FakeHardware(voltages=[1.00] * ANALOG_POINT_READS * 3)
        dispatcher = DeviceCommandDispatcher(hardware=hw, now_ms=lambda: NOW_MS)
        posts = []
        point = _ph(step="point", probe="analog", buffer=4.0)
        fetches: list[int] = []

        def fetch():
            fetches.append(1)
            return [point] if len(fetches) == 1 else []

        def post(*a, **k):
            posts.append(a)
            return False

        cloud = SimpleNamespace(
            poll=lambda: {"commands": [], "configVersion": None},
            fetch_pending_commands=fetch,
            post_command_result=post,
            send_event=lambda *a, **k: None,
        )
        worker = CommandWorker(
            lambda: SimpleNamespace(command_poll_s=5, cloud_enabled=True),
            cloud,
            StateStore(),
            apply_command=lambda _c: None,
            apply_device_command=dispatcher.apply,
        )
        dispatcher.apply(_ph(step="start", probe="analog"))
        worker.step()
        remaining = len(hw.voltages)
        worker.step()
        assert len(posts) == 2
        assert posts[0][0] == point["id"]
        assert posts[1][0] == point["id"]
        assert len(hw.voltages) == remaining


class TestResultHttpStatus:
    def test_409_is_already_recorded(self, monkeypatch):
        c = CloudClient(
            device_id="BS-WQM1-00aabbccddee",
            ingest_url="https://example.test/ingestReading",
            command_url="https://example.test/deviceCommands",
            api_key="device-key",
            fw_version="2.4.0",
            api_base="https://example.test/app",
        )
        c._sleep = lambda *_: None
        c._max_retries = 1

        def conflict(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 409, "Conflict", {}, None)

        monkeypatch.setattr("urllib.request.urlopen", conflict)
        assert c.post_command_result("c1", "applied") is True

    def test_404_and_422_are_terminal(self, monkeypatch):
        c = CloudClient(
            device_id="BS-WQM1-00aabbccddee",
            ingest_url="https://example.test/ingestReading",
            command_url="https://example.test/deviceCommands",
            api_key="device-key",
            fw_version="2.4.0",
            api_base="https://example.test/app",
        )
        c._sleep = lambda *_: None
        c._max_retries = 1

        def not_found(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)

        monkeypatch.setattr("urllib.request.urlopen", not_found)
        assert c.post_command_result("missing", "applied") == RESULT_POST_DROP

        def unprocessable(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 422, "Unprocessable", {}, None)

        monkeypatch.setattr("urllib.request.urlopen", unprocessable)
        assert c.post_command_result("bad", "applied") == RESULT_POST_DROP

    def test_401_and_403_are_retried(self, monkeypatch):
        c = CloudClient(
            device_id="BS-WQM1-00aabbccddee",
            ingest_url="https://example.test/ingestReading",
            command_url="https://example.test/deviceCommands",
            api_key="device-key",
            fw_version="2.4.0",
            api_base="https://example.test/app",
        )
        c._sleep = lambda *_: None
        c._max_retries = 1

        def unauthorized(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

        monkeypatch.setattr("urllib.request.urlopen", unauthorized)
        assert c.post_command_result("rotating", "applied") is False

        c._api_key = "device-key"

        def forbidden(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, None)

        monkeypatch.setattr("urllib.request.urlopen", forbidden)
        assert c.post_command_result("forbidden", "applied") is False


class TestTerminalResultNotRetried:
    def test_404_is_dropped_and_not_reposted(self, mock_hardware):
        hw = FakeHardware(voltages=[1.00] * ANALOG_POINT_READS * 3)
        dispatcher = DeviceCommandDispatcher(hardware=hw, now_ms=lambda: NOW_MS)
        posts = []
        point = _ph(step="point", probe="analog", buffer=4.0)

        def post(*a, **k):
            posts.append(a)
            return RESULT_POST_DROP

        cloud = SimpleNamespace(
            poll=lambda: {"commands": [], "configVersion": None},
            fetch_pending_commands=lambda: [point] if not posts else [],
            post_command_result=post,
            send_event=lambda *a, **k: None,
        )
        worker = CommandWorker(
            lambda: SimpleNamespace(command_poll_s=5, cloud_enabled=True),
            cloud,
            StateStore(),
            apply_command=lambda _c: None,
            apply_device_command=dispatcher.apply,
        )
        dispatcher.apply(_ph(step="start", probe="analog"))
        worker.step()
        worker.step()
        assert len(posts) == 1
        assert point["id"] not in worker._unposted_device_results


class TestHandledIdExpiryWindow:
    def test_unexpired_id_is_not_evicted_by_later_commands(self):
        hw = FakeHardware(voltages=[1.00] * ANALOG_POINT_READS * 2)
        d = DeviceCommandDispatcher(hardware=hw, now_ms=lambda: NOW_MS)
        d.apply(_ph(step="start", probe="analog"))
        cmd = _ph(step="point", probe="analog", buffer=4.0)
        first = d.apply(cmd)
        assert first.status == "applied"
        remaining = len(hw.voltages)
        for _ in range(HANDLED_COMMANDS_MAX):
            assert d.apply(_ph(step="start", probe="analog")).status == "applied"
        second = d.apply(cmd)
        assert second.status == "applied"
        assert second.result == first.result
        assert len(hw.voltages) == remaining


class TestRs485ConfigWrittenAfterHardware:
    def test_add_does_not_write_yaml_when_bus_open_fails(self, tmp_path, monkeypatch):
        app, config, sync = _real_app(tmp_path)
        app._modbus_bus = None
        writes: list[dict] = []

        def boom(*_a, **_k):
            raise OSError("no such port")

        def record_write(path, updates):
            writes.append(dict(updates))
            raise AssertionError("config must not be written when the bus fails")

        monkeypatch.setattr("cloud.device_hardware.ModbusBus", boom)
        monkeypatch.setattr("cloud.device_hardware.update_config", record_write)
        hw = Wqm1CommandHardware(app)
        with pytest.raises(OSError, match="no such port"):
            hw.rs485_add("residual_chlorine", 5)
        assert writes == []
        assert app._chlorine is None
        assert app._modbus_bus is None
        assert config.settings.rs485_chlorine_enabled is False
        assert config.settings.rs485_chlorine_addr == 1
        assert "rs485_chlorine_enabled: true" not in Path(config._base_path).read_text()
        sync.assert_not_called()

    def test_enable_does_not_write_yaml_when_bus_open_fails(self, tmp_path, monkeypatch):
        app, config, sync = _real_app(tmp_path)
        app._modbus_bus = None
        writes: list[dict] = []

        def boom(*_a, **_k):
            raise OSError("port busy")

        def record_write(path, updates):
            writes.append(dict(updates))
            raise AssertionError("config must not be written when the bus fails")

        monkeypatch.setattr("cloud.device_hardware.ModbusBus", boom)
        monkeypatch.setattr("cloud.device_hardware.update_config", record_write)
        hw = Wqm1CommandHardware(app)
        with pytest.raises(OSError, match="port busy"):
            hw.rs485_set_enabled("RD-PETSTS-01", 8, True)
        assert writes == []
        assert app._multi is None
        assert app._modbus_bus is None
        assert config.settings.rs485_multi_enabled is False
        assert config.settings.rs485_multi_addr == 3
        assert "rs485_multi_enabled: true" not in Path(config._base_path).read_text()
        sync.assert_not_called()


class TestSetAddressConfigWriteFailure:
    def test_yaml_failure_after_readdress_names_the_new_address(self, tmp_path, monkeypatch):
        app, config, sync = _real_app(
            tmp_path, "rs485_chlorine_enabled: true\nrs485_chlorine_addr: 5\n"
        )
        bus = MagicMock()
        probe = HondeChlorineSensor(bus, address=5)
        app._modbus_bus = bus
        app._chlorine = probe
        hw = Wqm1CommandHardware(app)
        writes = []

        def boom(path, updates):
            writes.append(updates)
            raise OSError("disk full")

        monkeypatch.setattr("cloud.device_hardware.update_config", boom)
        with pytest.raises(RuntimeError, match="7") as err:
            hw.rs485_set_address("residual_chlorine", 5, 7)
        assert probe.address == 7
        assert config.settings.rs485_chlorine_addr == 5
        assert len(writes) == 2
        assert "7" in str(err.value)
        sync.assert_not_called()


class TestMissingCommandId:
    def test_missing_id_is_rejected_and_does_not_start_a_session(self):
        hw = FakeHardware(voltages=[1.00] * ANALOG_POINT_READS)
        d = DeviceCommandDispatcher(hardware=hw, now_ms=lambda: NOW_MS)
        body = _ph(step="start", probe="analog")
        del body["id"]
        out = d.apply(body)
        assert out.status == "rejected"
        assert "id" in out.message.lower()
        point = d.apply(_ph(step="point", probe="analog", buffer=4.0))
        assert point.status == "rejected"
        assert hw.voltages == [1.00] * ANALOG_POINT_READS


class TestWireSensorNames:
    def test_server_names_are_accepted_and_class_names_are_not(self):
        hw = FakeHardware()
        d = DeviceCommandDispatcher(hardware=hw, now_ms=lambda: NOW_MS)
        for name in WIRE_SENSORS:
            out = d.apply(_rs485(op="add", sensor=name, address=11))
            assert out.status == "applied", out.message
            assert out.result is not None
            assert any(row["sensor"] == name for row in out.result["sensors"])
        out = d.apply(_rs485(op="add", sensor=HondeChlorineSensor.__name__, address=12))
        assert out.status == "rejected"
        assert out.message == "sensor not allowed"


class TestSetAddressLeavesDisabledDetached:
    def test_disabled_sensor_is_not_attached(self, tmp_path):
        app, config, sync = _real_app(tmp_path)
        hw = Wqm1CommandHardware(app)
        with pytest.raises(RuntimeError, match="no probe at that address"):
            hw.rs485_set_address("residual_chlorine", 5, 7)
        assert app._chlorine is None
        assert config.settings.rs485_chlorine_addr == 1
        sync.assert_not_called()


class TestReloadDoesNotUndoRs485Writes:
    def test_remote_overlay_cannot_clobber_command_writes(self, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text("rs485_port: /dev/ttyTEST\n")
        (tmp_path / "remote.yaml").write_text(
            "version: 1\nvalues:\n  rs485_orp_enabled: false\n  rs485_orp_addr: 2\n"
        )
        config = ConfigManager(str(cfg_path), str(tmp_path / "remote.yaml"))
        assert config.settings.rs485_orp_enabled is False
        assert config.settings.rs485_orp_addr == 2
        sync = MagicMock()
        app = SimpleNamespace(
            _cal=None,
            _ph=None,
            _adc=None,
            _adc_error=None,
            _config=config,
            _modbus_bus=MagicMock(),
            _chlorine=None,
            _orp=None,
            _multi=None,
            _sync_sampler=sync,
        )
        hw = Wqm1CommandHardware(app)
        hw.rs485_add("RD-ORP-WE-01", 6)
        assert config.settings.rs485_orp_enabled is True
        assert config.settings.rs485_orp_addr == 6
        assert isinstance(app._orp, HondeOrpSensor)


class TestHandledIdsPersist:
    def test_new_dispatcher_does_not_reapply_a_stored_id(self, tmp_path):
        cache = tmp_path / "handled-commands.json"
        hw = FakeHardware(voltages=[1.00] * ANALOG_POINT_READS * 3)
        d1 = DeviceCommandDispatcher(hardware=hw, now_ms=lambda: NOW_MS, cache_path=cache)
        d1.apply(_ph(step="start", probe="analog"))
        cmd = _ph(step="point", probe="analog", buffer=4.0)
        first = d1.apply(cmd)
        assert first.status == "applied"
        remaining = len(hw.voltages)
        d2 = DeviceCommandDispatcher(hardware=hw, now_ms=lambda: NOW_MS, cache_path=cache)
        second = d2.apply(cmd)
        assert second.status == "applied"
        assert second.result == first.result
        assert len(hw.voltages) == remaining


class TestCalibrationDocsImportValues:
    def test_docs_name_imported_buffers_and_models(self):
        pages = (
            Path("docs/device-commands.md").read_text()
            + Path("docs/sensor-calibration.md").read_text()
        )
        for value in ANALOG_BUFFERS:
            assert f"{value:.1f}" in pages
        for label in RS485_BUFFER_LABELS.values():
            assert label in pages
        for name in ALLOWED_SENSORS:
            assert name in pages
        assert str(ANALOG_POINT_READS) in pages
        assert f"{ANALOG_POINT_MAX_SPREAD_V}" in pages
        assert ADC_CH_PH == 2  # analog point still reads the pH converter channel
