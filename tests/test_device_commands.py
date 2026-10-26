"""Cloud device-command poller (marketplace #1476, firmware plan item D).

Pins GET .../commands/pending, the ph_calibrate / rs485_config dispatcher,
and POST .../result. Existing deviceCommands poll, ingest, and GET /config
stay on their own paths — this file must not rewrite those tests.
"""

from __future__ import annotations

import inspect
import json
import math
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from sensors.honde import HondeChlorineSensor, HondeMultiSensor, HondeOrpSensor

NOW_MS = 1_700_000_000_000
FUTURE_MS = NOW_MS + 60_000
PAST_MS = NOW_MS - 1


def _cmd(
    *,
    cmd_id: str = "cmd-1",
    cmd_type: str = "ph_calibrate",
    payload: dict | None = None,
    expires_at: int = FUTURE_MS,
    extra: dict | None = None,
) -> dict:
    body = {
        "id": cmd_id,
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
            {"sensor": HondeChlorineSensor.__name__, "address": 1, "enabled": False},
            {"sensor": HondeOrpSensor.__name__, "address": 2, "enabled": False},
            {"sensor": HondeMultiSensor.__name__, "address": 3, "enabled": False},
        ]

    def read_ph_adc_volts(self) -> float:
        if not self.voltages:
            raise RuntimeError("no ADC voltage queued")
        return self.voltages.pop(0)

    def apply_analog_ph(self, v_ph4: float, v_ph7: float) -> dict[str, float]:
        from calibration.calibrate import CalibrationManager

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
    from cloud.device_commands import DeviceCommandDispatcher

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
                sensor=HondeOrpSensor.__name__,
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
            out = _dispatch(_rs485(op="add", sensor=HondeChlorineSensor.__name__, address=address))
            assert out.status == "rejected"


class TestDispatcherHasNoActuationPath:
    def test_module_source_has_no_relay_breaker_or_gpio(self):
        from cloud import device_commands

        src = inspect.getsource(device_commands)
        lowered = src.lower()
        for needle in ("relay", "breaker", "gpio", "awg", "eaton"):
            assert needle not in lowered, f"dispatcher source mentions {needle!r}"

    def test_hardware_protocol_has_no_actuation_methods(self):
        from cloud.device_commands import CommandHardware

        public = {n for n in dir(CommandHardware) if not n.startswith("_")}
        for needle in ("relay", "breaker", "gpio", "awg"):
            assert all(needle not in n.lower() for n in public), public
        assert "read_ph_adc_volts" in public
        params = inspect.signature(CommandHardware.read_ph_adc_volts).parameters
        assert set(params) == {"self"}


class TestPhCalibrateAnalog:
    def test_start_point_finish_uses_calibrate_ph(self):
        hw = FakeHardware(voltages=[1.00, 1.60])
        from cloud.device_commands import DeviceCommandDispatcher

        d = DeviceCommandDispatcher(hardware=hw, now_ms=lambda: NOW_MS)
        assert d.apply(_ph(step="start", probe="analog")).status == "applied"
        p4 = d.apply(_ph(step="point", probe="analog", buffer=4.0))
        assert p4.status == "applied"
        p7 = d.apply(_ph(step="point", probe="analog", buffer=7.0))
        assert p7.status == "applied"
        done = d.apply(_ph(step="finish", probe="analog"))
        assert done.status == "applied"
        assert hw.analog_applied == [(1.00, 1.60)]
        assert done.result is not None
        assert done.result["slope"] == pytest.approx(5.0)
        assert done.result["offset"] == pytest.approx(7.0 - 5.0 * 1.60)
        assert done.result["buffers"] == [4.0, 7.0]
        assert "measuredAt" in done.result
        assert len(done.message) <= 200

    def test_finish_without_both_points_is_rejected(self):
        hw = FakeHardware(voltages=[1.00])
        from cloud.device_commands import DeviceCommandDispatcher

        d = DeviceCommandDispatcher(hardware=hw, now_ms=lambda: NOW_MS)
        d.apply(_ph(step="start", probe="analog"))
        d.apply(_ph(step="point", probe="analog", buffer=4.0))
        out = d.apply(_ph(step="finish", probe="analog"))
        assert out.status == "rejected"
        assert hw.analog_applied == []


class TestPhCalibrateRs485:
    def test_points_call_calibrate_ph_point(self):
        hw = FakeHardware()
        from cloud.device_commands import DeviceCommandDispatcher

        d = DeviceCommandDispatcher(hardware=hw, now_ms=lambda: NOW_MS)
        assert d.apply(_ph(step="start", probe="rs485_multi")).status == "applied"
        for buffer in (4.01, 6.86, 9.18):
            out = d.apply(_ph(step="point", probe="rs485_multi", buffer=buffer))
            assert out.status == "applied", out.message
        assert hw.rs485_points == ["4.01", "6.86", "9.18"]
        done = d.apply(_ph(step="finish", probe="rs485_multi"))
        assert done.status == "applied"
        assert done.result is not None
        assert done.result["buffers"] == [4.01, 6.86, 9.18]
        assert "measuredAt" in done.result


class TestRs485Config:
    def test_add_set_address_enable_disable(self):
        hw = FakeHardware()
        from cloud.device_commands import DeviceCommandDispatcher

        d = DeviceCommandDispatcher(hardware=hw, now_ms=lambda: NOW_MS)
        name = HondeChlorineSensor.__name__
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
        from cloud.device_commands import DeviceCommandDispatcher

        d = DeviceCommandDispatcher(hardware=hw, now_ms=lambda: NOW_MS)
        for cls in (HondeChlorineSensor, HondeOrpSensor, HondeMultiSensor):
            out = d.apply(_rs485(op="add", sensor=cls.__name__, address=11))
            assert out.status == "applied", out.message
        out = d.apply(_rs485(op="add", sensor="HondeWhatever", address=12))
        assert out.status == "rejected"


class TestCloudClientPendingAndResult:
    def _client(self):
        from cloud.client import CloudClient

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
        import urllib.error

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
        from cloud.client import CloudClient

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
        from app.state import StateStore
        from app.workers import CommandWorker
        from cloud.device_commands import DeviceCommandDispatcher

        hw = FakeHardware(voltages=[1.00, 1.60])
        dispatcher = DeviceCommandDispatcher(hardware=hw, now_ms=lambda: NOW_MS)
        posted = []
        legacy = []

        cloud = SimpleNamespace(
            poll=lambda: {
                "commands": [{"id": "legacy", "type": "restart"}],
                "configVersion": 4,
                "otaPending": False,
            },
            fetch_pending_commands=lambda: [
                _ph(step="start", probe="analog"),
            ],
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
        assert args[0] == "cmd-1"
        assert args[1] == "applied"

    def test_missing_v2_methods_leave_legacy_poll_alone(self, mock_hardware):
        from app.state import StateStore
        from app.workers import CommandWorker

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
        from calibration.calibrate import CalibrationManager
        from cloud.device_hardware import Wqm1CommandHardware

        cal = CalibrationManager(path=str(tmp_path / "cal.yaml"))
        ph = MagicMock()
        app = SimpleNamespace(
            _cal=cal,
            _ph=ph,
            _adc=SimpleNamespace(read_voltage=lambda _ch: 1.23),
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

    def test_rs485_list_and_disable_use_local_config(self, tmp_path):
        from cloud.device_hardware import Wqm1CommandHardware
        from utils.config import ConfigManager

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
        chlorine_row = next(r for r in listed if r["sensor"] == HondeChlorineSensor.__name__)
        assert chlorine_row == {
            "sensor": HondeChlorineSensor.__name__,
            "address": 5,
            "enabled": True,
        }
        after = hw.rs485_set_enabled(HondeChlorineSensor.__name__, 5, False)
        assert app._chlorine is None
        row = next(r for r in after if r["sensor"] == HondeChlorineSensor.__name__)
        assert row["enabled"] is False
        assert config.settings.rs485_chlorine_enabled is False


class TestCalibrationDocMovedToCloud:
    def test_page_drops_local_service_window_and_names_cloud(self):
        text = Path("docs/sensor-calibration.md").read_text()
        assert ":8080" not in text
        assert "Service Window" not in text
        assert "BlueSignal Cloud" in text
        assert "4.0" in text and "7.0" in text
        assert "4.01" in text and "6.86" in text and "9.18" in text
        assert "calibrated_at" in text
        assert "converter voltage" in text.lower()
        assert "unverified" in text.lower()
        assert "owner" in text.lower()
