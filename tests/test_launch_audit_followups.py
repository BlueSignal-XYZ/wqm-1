"""Follow-ups from docs/launch-audit.md §2 (H8-H11, M1-M7, M11 and the LOWs).

What each pins: relay on-periods are bounded by the controller itself, a
flat-but-alive sensor no longer drops relays, an uncalibrated pH electrode
publishes a status instead of a number, an OTA can pass on a probe-less unit,
the AWG compressor cannot be short-cycled, an un-gatewayed unit backs off its
joins and never reuses a DevNonce, a dead session is rejoined, an offline
buffer is capped, and a brute-forced PIN locks the address out.
"""

import struct
import sys
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# --------------------------------------------------------------------------
# Relay timers
# --------------------------------------------------------------------------


class TestRelayAutoOff:
    def _rc(self, **kw):
        from control.relay import RelayController

        return RelayController(**kw)

    def _wait_off(self, rc, ch, timeout=2.0):
        deadline = time.monotonic() + timeout
        while rc.get(ch) and time.monotonic() < deadline:
            time.sleep(0.01)
        return not rc.get(ch)

    def test_duration_turns_relay_off_without_any_sweep(self, mock_hardware):
        rc = self._rc()
        rc.set(1, True)
        assert rc.arm_auto_off(1, 0.05) == 0.05
        assert rc.pending_auto_off(1)
        assert self._wait_off(rc, 1)
        mock_hardware["gpio"].output.assert_called_with(17, 0)
        assert not rc.pending_auto_off(1)

    def test_manual_off_cancels_timer_and_rearm_replaces(self, mock_hardware):
        rc = self._rc()
        rc.set(2, True)
        rc.arm_auto_off(2, 10)
        rc.set(2, False)
        assert not rc.pending_auto_off(2)
        rc.set(2, True)
        rc.arm_auto_off(2, 10)
        rc.arm_auto_off(2, 0.05)  # later, shorter request wins
        assert self._wait_off(rc, 2)

    def test_max_on_caps_every_source(self, mock_hardware):
        rc = self._rc(max_on_s=0.05)
        rc.set(3, True)  # no duration at all — cloud / Service Window style
        assert rc.pending_auto_off(3)
        assert self._wait_off(rc, 3)
        rc.set(4, True)
        assert rc.arm_auto_off(4, 60) == 0.05  # shortened to the ceiling
        assert self._wait_off(rc, 4)

    def test_all_off_cancels_timers(self, mock_hardware):
        rc = self._rc()
        rc.set(1, True)
        rc.arm_auto_off(1, 30)
        rc.all_off()
        assert not rc.pending_auto_off(1)
        assert rc.get_state_bitmask() == 0

    def test_rules_hand_duration_to_controller(self, mock_hardware):
        from control.rules import Rule, RulesEngine

        relay = MagicMock()
        engine = RulesEngine(relay)
        engine.add_rule(Rule("ph", ">", 1, "on", threshold=8.5, duration_s=30))
        engine.evaluate({"ph": 9.0})
        relay.set.assert_called_with(1, True)
        relay.arm_auto_off.assert_called_once_with(1, 30)

    def test_downlink_hands_duration_to_controller(self, mock_hardware):
        from control.rules import RulesEngine

        relay = MagicMock()
        engine = RulesEngine(relay)
        assert engine.process_downlink_command(100, bytes([2, 1, 0x00, 0x1E]))
        relay.arm_auto_off.assert_called_once_with(2, 30)

    def test_policies_expose_continuous_ceiling(self, mock_hardware):
        from control.rules import RulesEngine

        engine = RulesEngine()
        engine.load_policies({"limits": {"max_continuous_on_minutes": 2.5}})
        assert engine.max_continuous_on_s == 150


# --------------------------------------------------------------------------
# Flatline is advisory
# --------------------------------------------------------------------------


class TestFlatlineAdvisory:
    def _monitor(self):
        from sensing.monitor import SensorMonitor
        from utils.config import Settings

        t = {"now": 0.0}
        monitor = SensorMonitor(lambda: Settings(), clock=lambda: t["now"])
        return monitor, t

    def test_flat_temperature_reports_but_does_not_suspend(self, mock_hardware):
        monitor, t = self._monitor()
        events = []
        for _ in range(25):
            events.extend(monitor.observe({"temp_c": 21.0625}))
            t["now"] += 60
        assert [e["type"] for e in events] == ["sensor_stuck"]
        assert events[0]["details"]["kind"] == "flat"
        assert monitor.health()["temperature"]["status"] != "ok"
        assert monitor.suspended_sensors() == set()

    def test_no_data_still_suspends(self, mock_hardware):
        monitor, t = self._monitor()
        for _ in range(25):
            monitor.observe({"temp_c": None})
            t["now"] += 60
        assert monitor.suspended_sensors() == {"temperature"}


# --------------------------------------------------------------------------
# pH calibration gate
# --------------------------------------------------------------------------


class TestPhCalibrationGate:
    def test_uncalibrated_probe_publishes_status_not_number(self, mock_hardware):
        from sensors.ph import PHSensor
        from sensors.status import UNCALIBRATED

        adc = MagicMock()
        adc.read_voltage.return_value = 1.5
        ph = PHSensor(adc, calibrated=False)
        result = ph.read_detailed()
        assert result.value is None and result.status == UNCALIBRATED
        assert ph.read() is None
        adc.read_voltage.assert_not_called()

    def test_placeholder_calibration_keeps_gate_closed(self, mock_hardware):
        from sensors.ph import PHSensor

        adc = MagicMock()
        adc.read_voltage.return_value = 1.05  # ≈ pH 6.5 through the LMP91200
        ph = PHSensor(adc)
        ph.set_calibration(1.04, 1.50, calibrated=False)
        assert ph.read() is None
        ph.set_calibration(1.20, 1.02)  # a real measurement opens it
        assert ph.read() == pytest.approx(6.5, abs=0.05)

    def test_manager_reports_factory_defaults_as_uncalibrated(self, tmp_path, mock_hardware):
        from calibration.calibrate import CalibrationManager

        cal = CalibrationManager(str(tmp_path / "cal.yaml"))
        assert cal.is_calibrated("ph") is False
        cal.calibrate_ph(1.20, 1.02)
        assert cal.is_calibrated("ph") is True

    def test_sampling_worker_records_ph_status(self, mock_hardware):
        from app.state import StateStore
        from app.workers import SamplingWorker
        from sensors.ph import PHSensor
        from utils.config import Settings

        ph = PHSensor(MagicMock(), calibrated=False)
        db = MagicMock()
        db.insert_reading.return_value = 1
        temp = MagicMock()
        temp.read_temp_c.return_value = 20.0
        worker = SamplingWorker(
            lambda: Settings(),
            {"ph": ph, "temperature": temp},
            db,
            None,
            None,
            None,
            MagicMock(),
            StateStore(),
        )
        worker.step()
        reading = db.insert_reading.call_args[0][0]
        assert reading["ph"] is None
        assert '"ph": "uncalibrated"' in reading["sensor_status"]


# --------------------------------------------------------------------------
# OTA self-test accepts a liveness beat
# --------------------------------------------------------------------------


class TestOtaLiveness:
    def test_liveness_touch_and_check(self, tmp_path, mock_hardware):
        from app.state import StateStore
        from app.supervisor import Supervisor
        from ota.agent import OTAAgent

        alive = tmp_path / "alive"
        sup = Supervisor([], StateStore(), clock=lambda: 100.0, liveness_path=alive)
        sup.tick()
        assert alive.exists()

        agent = OTAAgent(
            SimpleNamespace(db_path=str(tmp_path / "x.db"), ota_self_test_timeout_s=1),
            "dev",
            state_dir=tmp_path,
        )
        past = (datetime.now(UTC).replace(microsecond=0)).strftime("%Y-%m-%dT%H:%M:%SZ")
        # The beat is at least as new as `past` (same second) — bump it forward.
        import os

        os.utime(alive, (time.time() + 5, time.time() + 5))
        assert agent._has_fresh_liveness(past) is True
        assert agent._has_fresh_liveness("2099-01-01T00:00:00Z") is False

    def test_self_test_passes_on_liveness_without_a_reading(self, tmp_path, mock_hardware):
        from ota.agent import OTAAgent

        agent = OTAAgent(
            SimpleNamespace(db_path=str(tmp_path / "none.db"), ota_self_test_timeout_s=1),
            "dev",
            state_dir=tmp_path,
            clock=lambda: 0.0,
            sleep=lambda s: None,
        )
        agent._service_active = lambda: True  # type: ignore[method-assign]
        agent._apply_time_iso = "2000-01-01T00:00:00Z"
        (tmp_path / "alive").touch()
        assert agent._self_test() == (True, None)


# --------------------------------------------------------------------------
# Smart breaker short-cycle guard
# --------------------------------------------------------------------------


class TestShortCycleGuard:
    def test_on_refused_inside_min_off(self, mock_hardware):
        from integrations.smart_breaker.controller import SmartBreakerController

        settings = SimpleNamespace(
            smart_breaker_vendor="relay_only",
            smart_breaker_interlock_relay=1,
            smart_breaker_min_off_s=180,
            smart_breaker_fail_safe="off",
            smart_breaker_unreachable_grace_s=300,
            smart_breaker_circuit_amps=20,
        )
        t = {"now": 1000.0}
        relays = MagicMock()
        relays.get.return_value = True
        ctl = SmartBreakerController(lambda: settings, None, relays, lambda: t["now"])
        assert ctl.request(True, "test")["ok"]
        assert ctl.request(False, "test")["ok"]
        t["now"] += 60
        refused = ctl.request(True, "cloud")
        assert refused["ok"] is False and "short-cycle" in refused["error"]
        assert refused["retryAfterS"] == 120
        t["now"] += 120
        assert ctl.request(True, "cloud")["ok"]


# --------------------------------------------------------------------------
# LoRaWAN: DevNonce counter, join backoff, dead-session rejoin
# --------------------------------------------------------------------------

APP_KEY = b"\x2b\x7e\x15\x16\x28\xae\xd2\xa6\xab\xf7\x15\x88\x09\xcf\x4f\x3c"


class TestJoinHygiene:
    def test_dev_nonce_increments_and_persists(self, mock_hardware):
        from radio.lorawan import LoRaWANMAC

        radio = MagicMock()
        radio.send.return_value = True
        radio.receive.return_value = None
        persisted = []
        mac = LoRaWANMAC(
            radio, bytes(8), bytes(8), APP_KEY, persist_hook=lambda: persisted.append(1)
        )
        nonces = []
        with patch("radio.lorawan.time"):
            for _ in range(3):
                mac.join()
                nonces.append(struct.unpack("<H", radio.send.call_args[0][0][17:19])[0])
        assert nonces[1] == (nonces[0] + 1) & 0xFFFF and nonces[2] == (nonces[1] + 1) & 0xFFFF
        assert len(persisted) == 3
        # The counter survives a restart through mac_params.
        mac2 = LoRaWANMAC(MagicMock(), bytes(8), bytes(8), APP_KEY)
        mac2.restore_mac_params(mac.mac_params)
        assert mac2.mac_params["dev_nonce"] == nonces[2]

    def test_join_backoff_doubles_to_an_hour(self, mock_hardware):
        from main import JOIN_BACKOFF_MAX_S, _JoiningRadioWorker
        from utils.config import Settings

        lorawan = MagicMock()
        lorawan.session.joined = False
        lorawan.join.return_value = False
        worker = _JoiningRadioWorker(
            lambda: Settings(),
            lorawan,
            None,
            MagicMock(),
            None,
            None,
            MagicMock(),
            MagicMock(),
            lambda: None,
            lambda r: b"",
            1,
        )
        intervals = []
        for _ in range(8):
            worker.step()
            intervals.append(worker.interval_s())
        assert intervals[:3] == [300.0, 300.0, 300.0]
        assert intervals[3:6] == [600.0, 1200.0, 2400.0]
        assert intervals[6] == JOIN_BACKOFF_MAX_S
        lorawan.session.joined = True
        assert worker.interval_s() == 300.0

    def test_dead_session_is_forgotten_after_unanswered_link_checks(self, mock_hardware):
        from radio.lorawan import (
            LINK_CHECK_EVERY_UPLINKS,
            REJOIN_AFTER_UNANSWERED_LINK_CHECKS,
            LoRaWANMAC,
            LoRaWANSession,
        )

        radio = MagicMock()
        radio.send.return_value = True
        radio.receive.return_value = None
        mac = LoRaWANMAC(radio, bytes(8), bytes(8), APP_KEY)
        mac.restore_session(LoRaWANSession(dev_addr=b"\x01\x02\x03\x04", joined=True))
        link_checks = 0
        with patch("radio.lorawan.time"):
            for _ in range(LINK_CHECK_EVERY_UPLINKS * (REJOIN_AFTER_UNANSWERED_LINK_CHECKS + 1)):
                if not mac.session.joined:
                    break
                mac.send_uplink(b"\x01")
                frame = radio.send.call_args[0][0]
                if frame[5] & 0x0F and frame[8] == 0x02:
                    link_checks += 1
        assert link_checks == REJOIN_AFTER_UNANSWERED_LINK_CHECKS
        assert mac.session.joined is False

    def test_forget_session_via_socket_command(self, mock_hardware):
        from main import WQM1App

        app = WQM1App.__new__(WQM1App)
        app._lorawan = MagicMock()
        app._persist_session = MagicMock()
        assert app._handle_cmd({"action": "lora_rejoin"}) == {"ok": True, "rejoining": True}
        app._lorawan.forget_session.assert_called_once()
        app._persist_session.assert_called_once()

    def test_relay_set_with_duration_arms_controller(self, mock_hardware):
        from main import WQM1App

        app = WQM1App.__new__(WQM1App)
        app._relays = MagicMock()
        app._relays.arm_auto_off.return_value = 30.0
        r = app._handle_cmd({"action": "relay_set", "channel": 2, "state": True, "duration_s": 30})
        assert r == {"ok": True, "channel": 2, "state": True, "autoOffS": 30.0}
        app._relays.arm_auto_off.assert_called_once_with(2, 30.0)
        bad = app._handle_cmd(
            {"action": "relay_set", "channel": 2, "state": True, "duration_s": -1}
        )
        assert bad["ok"] is False


# --------------------------------------------------------------------------
# Offline buffer cap
# --------------------------------------------------------------------------


class TestPendingRotation:
    def test_pending_rows_capped_only_when_enabled(self, tmp_path, mock_hardware):
        from storage.database import WQM1Database

        db = WQM1Database(str(tmp_path / "t.db"))
        for i in range(30):
            db.insert_reading({"timestamp": f"2026-01-01T00:00:{i:02d}Z", "ph": 7.0})
        assert db.rotate(max_rows=10) == 0  # pending rows are sacred by default
        assert db.get_count() == 30
        db.rotate_pending = True
        assert db.rotate(max_rows=10) == 20
        assert db.get_count() == 10
        assert db.get_latest()["timestamp"] == "2026-01-01T00:00:29Z"
        db.close()


# --------------------------------------------------------------------------
# PIN lockout
# --------------------------------------------------------------------------


class TestPinLockout:
    def test_hourly_budget_locks_the_address(self, mock_hardware):
        from service_window import auth

        auth._failures.clear()
        auth._lockouts.clear()
        now = 1_000_000.0
        for _ in range(auth._LOCKOUT_AFTER - 1):
            auth._record_failure("10.0.0.9", now)
        assert auth._is_locked_out("10.0.0.9", now) == 0
        auth._record_failure("10.0.0.9", now)
        assert auth._is_locked_out("10.0.0.9", now) == pytest.approx(auth._LOCKOUT_S)
        assert auth._is_locked_out("10.0.0.9", now + auth._LOCKOUT_S + 1) == 0
        auth._clear_failures("10.0.0.9")
        assert "10.0.0.9" not in auth._failures


# --------------------------------------------------------------------------
# Small driver fixes
# --------------------------------------------------------------------------


class TestDriverFixes:
    def test_ds18b20_power_on_value_is_not_a_reading(self, mock_hardware):
        from sensors.temperature import DS18B20

        probe = DS18B20()
        probe._sensor = MagicMock()
        probe._sensor.get_temperature.return_value = 85.0
        assert probe.read_temp_c() is None
        probe._sensor.get_temperature.return_value = 21.5
        assert probe.read_temp_c() == 21.5

    def test_packet_status_rssi_and_snr(self, mock_hardware):
        sys.modules.setdefault("lgpio", MagicMock())
        from radio.sx1262 import SX1262

        radio = SX1262()
        radio._wait_busy = lambda timeout_s=1.0: True
        radio._xfer = lambda data: [0x00, 0x00, 0xA0, 0xF8, 0xA4]  # -80 dBm, -2 dB
        assert radio._read_rssi() == -80
        assert radio.last_snr == -2.0
