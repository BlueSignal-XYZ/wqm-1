"""
Irrigation hold — control/irrigation_hold.py and everything it touches.

One relay's COM + NC sit in an irrigation controller's rain-sensor loop. The
engine energises the coil (contact opens, controller holds) when a water
condition trips for N samples and drops it (contact closes, controller runs)
once every condition has been clear for the release period. These tests pin
the logic, the guards that keep every other path off the hold's channel, the
payload key, the simulator fault that drives it in the emulator, and the
Service Window labelling.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from control.irrigation_hold import IrrigationHold, status_line
from utils.config import SETTINGS_SCHEMA, Settings, validate_values

HOLD_KEYS = (
    "irrigation_hold_enabled",
    "irrigation_hold_relay",
    "irrigation_hold_turbidity_ntu",
    "irrigation_hold_tds_ppm",
    "irrigation_hold_ph_min",
    "irrigation_hold_ph_max",
    "irrigation_hold_flow_gpm_max",
    "irrigation_hold_trip_samples",
    "irrigation_hold_release_min",
    "irrigation_hold_on_fault",
)


def _settings(**over):
    s = Settings()
    s.irrigation_hold_enabled = True
    s.irrigation_hold_relay = 3
    s.irrigation_hold_turbidity_ntu = 8.0
    s.irrigation_hold_trip_samples = 2
    s.irrigation_hold_release_min = 10
    for k, v in over.items():
        setattr(s, k, v)
    return s


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _engine(**over):
    relays = MagicMock()
    clock = Clock()
    hold = IrrigationHold(relays, clock=clock)
    hold.configure(_settings(**over))
    relays.reset_mock()  # arming writes one release; tests count from here
    return hold, relays, clock


TRIP = {"turbidity_ntu": 14.2, "tds_ppm": 300.0, "ph": 7.1, "flow_rate_gpm": 0.3}
CLEAR = {"turbidity_ntu": 3.0, "tds_ppm": 300.0, "ph": 7.1, "flow_rate_gpm": 0.3}


# ---------------------------------------------------------------------------
# Engine logic
# ---------------------------------------------------------------------------


class TestTrip:
    def test_holds_after_n_consecutive_samples(self):
        hold, relays, _ = _engine()
        assert hold.evaluate(TRIP, set()).active is False
        relays.set.assert_not_called()
        state = hold.evaluate(TRIP, set())
        assert state.active is True
        relays.set.assert_called_once_with(3, True, unbounded=True)
        assert state.reasons == [{"sensor": "turbidity_ntu", "value": 14.2, "threshold": 8.0}]
        assert state.since is not None

    def test_samples_must_be_consecutive(self):
        hold, relays, _ = _engine()
        hold.evaluate(TRIP, set())
        hold.evaluate(CLEAR, set())
        assert hold.evaluate(TRIP, set()).active is False
        relays.set.assert_not_called()

    def test_relay_written_only_on_transitions(self):
        hold, relays, _ = _engine(irrigation_hold_trip_samples=1)
        for _ in range(5):
            hold.evaluate(TRIP, set())
        assert relays.set.call_count == 1

    def test_ph_min_and_max_directions(self):
        hold, _, _ = _engine(
            irrigation_hold_turbidity_ntu=0.0,
            irrigation_hold_ph_min=6.0,
            irrigation_hold_ph_max=9.0,
            irrigation_hold_trip_samples=1,
        )
        assert hold.evaluate({"ph": 7.0}, set()).active is False
        assert hold.evaluate({"ph": 5.9}, set()).active is True
        hold2, _, _ = _engine(
            irrigation_hold_turbidity_ntu=0.0,
            irrigation_hold_ph_max=9.0,
            irrigation_hold_trip_samples=1,
        )
        assert hold2.evaluate({"ph": 9.0}, set()).active is False  # strictly above
        assert hold2.evaluate({"ph": 9.1}, set()).active is True

    def test_threshold_is_inclusive_for_turbidity_tds_flow(self):
        hold, _, _ = _engine(
            irrigation_hold_turbidity_ntu=0.0,
            irrigation_hold_flow_gpm_max=2.0,
            irrigation_hold_trip_samples=1,
        )
        assert hold.evaluate({"flow_rate_gpm": 2.0}, set()).active is True

    def test_threshold_zero_is_ignored_entirely(self):
        """TDS threshold 0 = off: a missing TDS value is not a fault, and a huge
        TDS value never trips."""
        hold, _, _ = _engine(irrigation_hold_trip_samples=1)
        state = hold.evaluate({"turbidity_ntu": 3.0, "tds_ppm": None}, set())
        assert state.fault is None and state.active is False
        state = hold.evaluate({"turbidity_ntu": 3.0, "tds_ppm": 19000.0}, set())
        assert state.active is False


class TestRelease:
    def test_releases_after_the_delay(self):
        hold, relays, clock = _engine()
        hold.evaluate(TRIP, set())
        hold.evaluate(TRIP, set())
        relays.reset_mock()
        clock.t += 60
        hold.evaluate(CLEAR, set())
        clock.t += 599
        assert hold.evaluate(CLEAR, set()).active is True
        relays.set.assert_not_called()
        clock.t += 1
        state = hold.evaluate(CLEAR, set())
        assert state.active is False and state.since is None and state.reasons == []
        relays.set.assert_called_once_with(3, False)

    def test_hysteresis_lives_in_the_delay(self):
        """A re-trip during the release delay restarts the clear clock."""
        hold, _, clock = _engine()
        hold.evaluate(TRIP, set())
        hold.evaluate(TRIP, set())
        hold.evaluate(CLEAR, set())
        clock.t += 500
        hold.evaluate(TRIP, set())  # back over the line
        clock.t += 60
        hold.evaluate(CLEAR, set())
        clock.t += 500
        assert hold.evaluate(CLEAR, set()).active is True
        clock.t += 100
        assert hold.evaluate(CLEAR, set()).active is False

    def test_release_zero_releases_on_first_clear_sample(self):
        hold, _, _ = _engine(irrigation_hold_release_min=0)
        hold.evaluate(TRIP, set())
        hold.evaluate(TRIP, set())
        assert hold.evaluate(CLEAR, set()).active is False

    def test_reasons_kept_during_release_delay(self):
        hold, _, _ = _engine()
        hold.evaluate(TRIP, set())
        hold.evaluate(TRIP, set())
        state = hold.evaluate(CLEAR, set())
        assert state.active is True
        assert state.reasons[0]["sensor"] == "turbidity_ntu"


class TestFault:
    def test_fault_releases_by_default(self):
        hold, relays, _ = _engine()
        hold.evaluate(TRIP, set())
        hold.evaluate(TRIP, set())
        state = hold.evaluate({"turbidity_ntu": None}, set())
        assert state.active is False
        assert state.fault == "released"
        relays.set.assert_called_with(3, False)

    def test_suspended_probe_is_a_fault(self):
        hold, _, _ = _engine()
        hold.evaluate(TRIP, set())
        hold.evaluate(TRIP, set())
        state = hold.evaluate(TRIP, {"turbidity"})  # frozen value, monitor says no_data
        assert state.active is False
        assert state.fault == "released"

    def test_any_fault_releases_even_if_another_condition_trips(self):
        hold, _, _ = _engine(irrigation_hold_tds_ppm=500.0)
        hold.evaluate(TRIP | {"tds_ppm": 900.0}, set())
        hold.evaluate(TRIP | {"tds_ppm": 900.0}, set())
        state = hold.evaluate({"turbidity_ntu": None, "tds_ppm": 900.0}, set())
        assert state.active is False and state.fault == "released"

    def test_fault_hold_mode_holds(self):
        hold, relays, _ = _engine(irrigation_hold_on_fault="hold")
        assert hold.evaluate({"turbidity_ntu": None}, set()).active is False
        state = hold.evaluate({"turbidity_ntu": None}, set())
        assert state.active is True
        assert state.fault == "held"
        relays.set.assert_called_once_with(3, True, unbounded=True)

    def test_fault_hold_mode_keeps_an_existing_hold(self):
        hold, _, clock = _engine(irrigation_hold_on_fault="hold")
        hold.evaluate(TRIP, set())
        hold.evaluate(TRIP, set())
        clock.t += 3600
        state = hold.evaluate({"turbidity_ntu": None}, {"turbidity"})
        assert state.active is True and state.fault == "held"

    def test_monitor_error_reuses_last_suspension(self):
        """None = no information this cycle — must not read as recovery."""
        hold, _, _ = _engine()
        hold.evaluate(TRIP, {"turbidity"})
        state = hold.evaluate(TRIP, None)
        assert state.fault == "released"
        hold.evaluate(TRIP, set())  # the monitor ran and says: recovered
        assert hold.evaluate(TRIP, set()).active is True


class TestConfigureGuards:
    def test_interlock_conflict_does_not_arm(self, caplog):
        caplog.set_level(logging.ERROR)
        hold, relays, _ = _engine(smart_breaker_interlock_relay=3)
        state = hold.evaluate(TRIP, set())
        hold.evaluate(TRIP, set())
        assert state.error == "relay_conflict"
        assert hold.state().active is False
        assert hold.owns(3) is False
        relays.set.assert_not_called()
        assert any("interlock" in r.message for r in caplog.records)
        assert hold.payload()["error"] == "relay_conflict"

    def test_enabled_with_relay_zero_does_nothing_and_logs_once(self, caplog):
        caplog.set_level(logging.WARNING)
        hold, relays, _ = _engine(irrigation_hold_relay=0)
        for _ in range(3):
            hold.configure(_settings(irrigation_hold_relay=0))
            hold.evaluate(TRIP, set())
        relays.set.assert_not_called()
        assert hold.reserved_channels() == set()
        assert sum("no relay is chosen" in r.message for r in caplog.records) == 1

    def test_changing_relay_releases_old_channel_and_restarts_count(self):
        hold, relays, _ = _engine()
        hold.evaluate(TRIP, set())
        hold.evaluate(TRIP, set())
        relays.reset_mock()
        hold.configure(_settings(irrigation_hold_relay=2))
        # The old channel is released, and the new one is taken over released.
        assert [c.args for c in relays.set.call_args_list] == [(3, False), (2, False)]
        assert hold.evaluate(TRIP, set()).active is False  # count restarted
        assert hold.evaluate(TRIP, set()).active is True
        relays.set.assert_called_with(2, True, unbounded=True)

    def test_disabling_releases(self):
        hold, relays, _ = _engine()
        hold.evaluate(TRIP, set())
        hold.evaluate(TRIP, set())
        hold.configure(_settings(irrigation_hold_enabled=False))
        relays.set.assert_called_with(3, False)
        assert hold.payload() is None
        assert hold.owns(3) is False

    def test_failed_relay_switch_is_retried_next_sample(self, caplog):
        hold, relays, _ = _engine()
        relays.set.side_effect = [OSError("gpio"), None]
        hold.evaluate(TRIP, set())
        state = hold.evaluate(TRIP, set())
        assert state.active is False  # the coil is not open; say so
        assert any("could not switch relay 3" in r.message for r in caplog.records)
        assert hold.evaluate(TRIP, set()).active is True
        assert relays.set.call_count == 2

    def test_failed_release_on_disable_is_retried(self):
        hold, relays, _ = _engine()
        hold.evaluate(TRIP, set())
        hold.evaluate(TRIP, set())
        relays.set.side_effect = [OSError("gpio"), None]
        hold.configure(_settings(irrigation_hold_enabled=False))
        assert hold.state().active is True
        hold.evaluate(CLEAR, set())
        assert hold.state().active is False
        relays.set.assert_called_with(3, False)

    def test_arming_releases_a_coil_left_on_by_something_else(self):
        relays = MagicMock()
        hold = IrrigationHold(relays, clock=Clock())
        hold.configure(_settings(irrigation_hold_enabled=False))
        relays.set.assert_not_called()
        hold.configure(_settings())
        relays.set.assert_called_once_with(3, False)
        hold.configure(_settings(irrigation_hold_turbidity_ntu=9.0))  # not a takeover
        assert relays.set.call_count == 1

    def test_configure_is_idempotent(self, caplog):
        caplog.set_level(logging.INFO)
        hold, _, _ = _engine()
        n = len(caplog.records)
        for _ in range(5):
            hold.configure(_settings())
        assert len(caplog.records) == n

    def test_no_relay_controller_never_claims_the_contact_is_open(self):
        hold = IrrigationHold(None, clock=Clock())
        hold.configure(_settings(irrigation_hold_trip_samples=1))
        assert hold.evaluate(TRIP, set()).active is False


# ---------------------------------------------------------------------------
# Relay controller: max_on exemption
# ---------------------------------------------------------------------------


class TestMaxOnExemption:
    def test_unbounded_skips_the_ceiling(self, mock_hardware):
        from control.relay import RelayController

        rc = RelayController(max_on_s=5.0)
        rc.set(3, True, unbounded=True)
        assert rc.pending_auto_off(3) is False
        rc.set(1, True)
        assert rc.pending_auto_off(1) is True
        rc.all_off()

    def test_engine_energises_unbounded_on_a_real_controller(self, mock_hardware):
        from control.relay import RelayController

        rc = RelayController(max_on_s=5.0)
        hold = IrrigationHold(rc, clock=Clock())
        hold.configure(_settings(irrigation_hold_trip_samples=1))
        hold.evaluate(TRIP, set())
        assert rc.get(3) is True
        assert rc.pending_auto_off(3) is False
        rc.all_off()


# ---------------------------------------------------------------------------
# Rules engine: set aside, fail-safe exemption, LoRa refusal
# ---------------------------------------------------------------------------


class TestRulesEngineReservation:
    def _rules(self, hold):
        from control.rules import RulesEngine

        relay = MagicMock()
        engine = RulesEngine(relay)
        engine.set_reserved_channels_provider(hold.reserved_channels)
        return engine, relay

    def test_rule_on_hold_channel_set_aside_with_warning_at_load(self, caplog):
        caplog.set_level(logging.WARNING)
        hold, _, _ = _engine()
        engine, relay = self._rules(hold)
        engine.load_rules(
            [{"sensor": "ph", "operator": ">", "threshold": 9.0, "relay": 3, "action": "off"}]
        )
        assert any("set aside" in r.message and "relay 3" in r.message for r in caplog.records)
        assert engine.evaluate({"ph": 9.5}) == []
        relay.set.assert_not_called()
        assert len(engine._rules) == 1, "set aside, not deleted"

    def test_rules_resume_when_hold_disabled_without_restart(self):
        hold, _, _ = _engine()
        engine, _ = self._rules(hold)
        engine.load_rules(
            [{"sensor": "ph", "operator": ">", "threshold": 9.0, "relay": 3, "action": "on"}]
        )
        assert engine.evaluate({"ph": 9.5}) == []
        hold.configure(_settings(irrigation_hold_enabled=False))
        assert (3, True) in engine.evaluate({"ph": 9.5})

    def test_rules_on_other_channels_unaffected(self):
        hold, _, _ = _engine()
        engine, _ = self._rules(hold)
        engine.load_rules(
            [{"sensor": "ph", "operator": ">", "threshold": 9.0, "relay": 1, "action": "on"}]
        )
        assert (1, True) in engine.evaluate({"ph": 9.5})

    def test_hold_channel_excluded_from_failsafe_reversion(self):
        from control.rules import Rule, RulesEngine

        hold, _, _ = _engine(irrigation_hold_enabled=False)
        relay = MagicMock()
        engine = RulesEngine(relay)
        engine.set_reserved_channels_provider(hold.reserved_channels)
        engine.add_rule(Rule(sensor="ph", operator=">", threshold=9.0, relay=3, action="on"))
        engine.add_rule(Rule(sensor="ph", operator=">", threshold=9.0, relay=1, action="on"))
        engine.evaluate({"ph": 9.5})
        hold.configure(_settings())  # the hold now owns relay 3
        engine.set_suspended_sensors({"ph"})
        actions = engine.evaluate({"ph": 9.5})
        assert (1, False) in actions
        assert (3, False) not in actions

    def test_rule_timer_on_hold_channel_is_forgotten_not_fired(self):
        import time as _time

        from control.rules import Rule, RulesEngine

        hold, _, _ = _engine(irrigation_hold_enabled=False)
        engine = RulesEngine(MagicMock())
        engine.set_reserved_channels_provider(hold.reserved_channels)
        engine.add_rule(
            Rule(sensor="ph", operator=">", threshold=9.0, relay=3, action="on", duration_s=1)
        )
        engine.evaluate({"ph": 9.5})
        hold.configure(_settings())
        engine._timers[3] = _time.monotonic() - 1  # would expire now
        assert (3, False) not in engine.evaluate({"ph": 7.0})
        assert 3 not in engine._timers

    def test_lora_downlink_refused_on_hold_channel(self):
        hold, _, _ = _engine()
        engine, relay = self._rules(hold)
        assert engine.process_downlink_command(100, bytes([3, 0])) is False
        relay.set.assert_not_called()
        assert engine.process_downlink_command(100, bytes([1, 1])) is True
        relay.set.assert_called_once_with(1, True)


# ---------------------------------------------------------------------------
# main.py: relay_set refusal, status command, hot reload, rule loading
# ---------------------------------------------------------------------------


def _bare_app(hold):
    from main import WQM1App

    app = WQM1App.__new__(WQM1App)
    app._relays = MagicMock()
    app._hold = hold
    return app


class TestCommandRefusal:
    def test_relay_set_refused_on_hold_channel(self):
        hold, _, _ = _engine()
        app = _bare_app(hold)
        for state in (True, False):
            r = app._handle_cmd({"action": "relay_set", "channel": 3, "state": state})
            assert r == {"ok": False, "error": "irrigation hold owns relay 3"}
        app._relays.set.assert_not_called()

    def test_relay_set_allowed_on_other_channels(self):
        hold, _, _ = _engine()
        app = _bare_app(hold)
        assert app._handle_cmd({"action": "relay_set", "channel": 1, "state": True})["ok"]
        app._relays.set.assert_called_once_with(1, True)

    def test_relay_set_allowed_when_hold_disabled(self):
        hold, _, _ = _engine(irrigation_hold_enabled=False)
        app = _bare_app(hold)
        assert app._handle_cmd({"action": "relay_set", "channel": 3, "state": True})["ok"]

    def test_cloud_relay_command_on_hold_channel_acks_error(self):
        hold, _, _ = _engine()
        app = _bare_app(hold)
        app._cloud = MagicMock()
        app._apply_cloud_command({"id": "c1", "type": "relay", "channel": 3, "state": False})
        app._cloud.ack_command.assert_called_once_with(
            "c1", "error", "irrigation hold owns relay 3"
        )
        app._relays.set.assert_not_called()

    def test_status_command(self):
        hold, _, _ = _engine(irrigation_hold_trip_samples=1)
        hold.evaluate(TRIP, set())
        r = _bare_app(hold)._handle_cmd({"action": "irrigation_hold_status"})
        assert r["ok"] is True
        assert r["enabled"] is True and r["active"] is True and r["relay"] == 3
        assert r["releaseMin"] == 10.0


class TestBootWiring:
    def test_boot_sets_aside_rules_and_refuses_relay(self, monkeypatch, tmp_path, mock_hardware):
        from tests.test_smart_breaker_wiring import _boot

        rules = "rules:\n  - {sensor: ph, operator: '>', threshold: 9.0, relay: 2, action: 'on'}\n"
        _, app, _ = _boot(
            monkeypatch,
            tmp_path,
            "board: rpi-zero-2w\n"
            "irrigation_hold_enabled: true\n"
            "irrigation_hold_relay: 2\n"
            "irrigation_hold_turbidity_ntu: 8.0\n" + rules,
        )
        assert app._hold.owns(2)
        assert app._rules._set_aside == {2}
        r = app._handle_cmd({"action": "relay_set", "channel": 2, "state": False})
        assert r["ok"] is False
        worker = app._build_workers()[0]
        assert worker.irrigation_hold is app._hold

    def test_config_reload_reconfigures_hot(self, monkeypatch, tmp_path, mock_hardware):
        from tests.test_smart_breaker_wiring import _boot

        _, app, mgr = _boot(monkeypatch, tmp_path, "board: rpi-zero-2w\n")
        assert app._hold.owns(4) is False
        ok, errors = mgr.apply_remote_config(
            7,
            {
                "irrigation_hold_enabled": True,
                "irrigation_hold_relay": 4,
                "irrigation_hold_tds_ppm": 900.0,
            },
        )
        assert ok, errors
        app._handle_cmd({"action": "config_reload"})
        assert app._hold.owns(4) is True
        assert app._irrigation_hold_payload()["relay"] == 4

    def test_disabled_by_default(self, monkeypatch, tmp_path, mock_hardware):
        from tests.test_smart_breaker_wiring import _boot

        _, app, _ = _boot(monkeypatch, tmp_path, "board: rpi-zero-2w\n")
        assert app._irrigation_hold_payload() is None
        assert app._handle_cmd({"action": "relay_set", "channel": 3, "state": True})["ok"]


# ---------------------------------------------------------------------------
# Payload
# ---------------------------------------------------------------------------


def _client(**kw):
    from cloud.client import CloudClient

    return CloudClient(
        device_id="BS-WQM1-0000a92e4e7d",
        ingest_url="https://example.test/ingest",
        command_url="https://example.test/commands",
        api_key="k" * 32,
        fw_version="9.9.9",
        **kw,
    )


ROW = {
    "id": 1,
    "timestamp": "2026-09-29T12:00:00Z",
    "ph": 7.1,
    "turbidity_ntu": 14.2,
    "relay_state": 4,
    "clock_source": "ntp",
}


class TestPayload:
    def test_key_omitted_and_bytes_identical_when_disabled(self):
        hold, _, _ = _engine(irrigation_hold_enabled=False)
        without = _client().reading_to_json(dict(ROW))
        with_disabled = _client(irrigation_hold_provider=hold.payload).reading_to_json(dict(ROW))
        assert "irrigationHold" not in with_disabled["metadata"]
        assert json.dumps(with_disabled) == json.dumps(without)

    def test_shape_when_enabled(self):
        hold, _, _ = _engine()
        hold.evaluate(TRIP, set())
        hold.evaluate(TRIP, set())
        out = _client(irrigation_hold_provider=hold.payload).reading_to_json(dict(ROW))
        ih = out["metadata"]["irrigationHold"]
        assert set(ih) == {"enabled", "relay", "active", "since", "reasons", "fault", "error"}
        assert ih["enabled"] is True and ih["relay"] == 3 and ih["active"] is True
        assert ih["since"].endswith("Z")
        assert ih["reasons"] == [{"sensor": "turbidity_ntu", "value": 14.2, "threshold": 8.0}]
        assert ih["fault"] is None and ih["error"] is None
        # It rides beside relayState; nothing else moved.
        assert out["metadata"]["relayState"] == 4
        json.dumps(out)  # serialisable

    def test_enabled_but_clear(self):
        hold, _, _ = _engine()
        hold.evaluate(CLEAR, set())
        ih = _client(irrigation_hold_provider=hold.payload).reading_to_json(dict(ROW))["metadata"][
            "irrigationHold"
        ]
        assert ih == {
            "enabled": True,
            "relay": 3,
            "active": False,
            "since": None,
            "reasons": [],
            "fault": None,
            "error": None,
        }

    def test_provider_error_omits_key(self):
        def boom():
            raise RuntimeError("x")

        out = _client(irrigation_hold_provider=boom).reading_to_json(dict(ROW))
        assert "irrigationHold" not in out["metadata"]


# ---------------------------------------------------------------------------
# Settings schema
# ---------------------------------------------------------------------------


class TestSettings:
    def test_every_key_is_remote_and_hot(self):
        for key in HOLD_KEYS:
            spec = SETTINGS_SCHEMA[key]
            assert spec.remote is True, key
            assert spec.hot is True, key

    def test_defaults_are_off(self):
        s = Settings()
        assert s.irrigation_hold_enabled is False
        assert s.irrigation_hold_relay == 0
        assert s.irrigation_hold_trip_samples == 2
        assert s.irrigation_hold_release_min == 10
        assert s.irrigation_hold_on_fault == "release"
        for key in HOLD_KEYS[2:7]:
            assert getattr(s, key) == 0

    @pytest.mark.parametrize(
        "key,lo,hi",
        [
            ("irrigation_hold_relay", 0, 4),
            ("irrigation_hold_turbidity_ntu", 0, 4000),
            ("irrigation_hold_tds_ppm", 0, 20000),
            ("irrigation_hold_ph_min", 0, 14),
            ("irrigation_hold_ph_max", 0, 14),
            ("irrigation_hold_flow_gpm_max", 0, 1000),
            ("irrigation_hold_trip_samples", 1, 10),
            ("irrigation_hold_release_min", 0, 1440),
        ],
    )
    def test_ranges(self, key, lo, hi):
        spec = SETTINGS_SCHEMA[key]
        assert (spec.min, spec.max) == (lo, hi)
        accepted, errors = validate_values({key: hi}, remote=True)
        assert not errors and key in accepted
        _, errors = validate_values({key: hi + 1}, remote=True)
        assert errors
        _, errors = validate_values({key: lo - 1}, remote=True)
        assert errors

    def test_on_fault_choices(self):
        assert validate_values({"irrigation_hold_on_fault": "hold"}, remote=True)[1] == []
        assert validate_values({"irrigation_hold_on_fault": "pause"}, remote=True)[1]

    def test_rules_still_not_remote(self):
        _, errors = validate_values({"rules": []}, remote=True)
        assert errors


# ---------------------------------------------------------------------------
# Sampling worker: evaluated after rules; the suspension recovery fix
# ---------------------------------------------------------------------------


class _Sensor:
    def __init__(self, value):
        self.value = value

    def read(self, temp_c=None):
        return self.value

    def read_temp_c(self):
        return self.value


class _Rules:
    def __init__(self):
        self.suspended_calls: list[set[str]] = []
        self.order: list[str] | None = None

    def set_suspended_sensors(self, s):
        self.suspended_calls.append(set(s))

    def evaluate(self, reading):
        if self.order is not None:
            self.order.append("rules")


def _worker(monitor=None, rules=None, hold=None, settings=None):
    from app.state import StateStore
    from app.workers import SamplingWorker

    class _DB:
        def __init__(self):
            self.readings = []

        def insert_reading(self, r):
            self.readings.append(r)
            return len(self.readings)

    return SamplingWorker(
        lambda: settings or Settings(),
        sensors={"temperature": _Sensor(21.0), "ph": _Sensor(7.1), "turbidity": _Sensor(3.0)},
        db=_DB(),
        rules=rules,
        relays=None,
        leds=None,
        health=SimpleNamespace(update_last_seen=lambda: None),
        state=StateStore(),
        monitor=monitor,
        irrigation_hold=hold,
    )


class _Monitor:
    def __init__(self, sequence):
        self.sequence = list(sequence)
        self.current: set[str] | Exception = set()

    def observe(self, reading):
        self.current = self.sequence.pop(0)
        if isinstance(self.current, Exception):
            raise self.current
        return []

    def suspended_sensors(self):
        return self.current


class TestWorkerRecovery:
    def test_recovered_probe_forwards_empty_set(self):
        """The workers.py fix: suspend then recover must reach the engines as
        an EMPTY set, or the probe stays suspended until restart."""
        rules = _Rules()
        w = _worker(monitor=_Monitor([{"ph"}, set()]), rules=rules)
        w.step()
        w.step()
        assert rules.suspended_calls == [{"ph"}, set()]

    def test_recovery_unsuspends_real_rules_engine(self):
        from control.rules import Rule, RulesEngine

        engine = RulesEngine(MagicMock())
        engine.add_rule(Rule(sensor="ph", operator="<", threshold=8.0, relay=1, action="on"))
        w = _worker(monitor=_Monitor([{"ph"}, set()]), rules=engine)
        w.step()
        assert engine._suspended_columns == {"ph"}
        w.step()
        assert engine._suspended_columns == set()

    def test_monitor_error_forwards_nothing(self):
        rules = _Rules()
        w = _worker(monitor=_Monitor([{"ph"}, RuntimeError("monitor broke")]), rules=rules)
        w.step()
        w.step()
        assert rules.suspended_calls == [{"ph"}], "an exception must not look like recovery"

    def test_hold_sees_recovery_and_error_correctly(self):
        hold = MagicMock()
        w = _worker(monitor=_Monitor([{"turbidity"}, set(), RuntimeError("x")]), hold=hold)
        w.step()
        w.step()
        w.step()
        passed = [c.args[1] for c in hold.evaluate.call_args_list]
        assert passed == [{"turbidity"}, set(), None]

    def test_no_monitor_means_nothing_suspended_for_hold(self):
        hold = MagicMock()
        _worker(hold=hold).step()
        assert hold.evaluate.call_args.args[1] == set()

    def test_hold_configured_hot_and_evaluated_after_rules(self):
        order: list[str] = []
        rules = _Rules()
        rules.order = order

        class _Hold:
            def configure(self, s):
                order.append("configure")
                self.settings = s

            def evaluate(self, reading, suspended):
                order.append("hold")

        settings = _settings()
        hold = _Hold()
        _worker(rules=rules, hold=hold, settings=settings).step()
        assert order == ["rules", "configure", "hold"]
        assert hold.settings is settings

    def test_hold_error_does_not_stop_sampling(self):
        hold = MagicMock()
        hold.evaluate.side_effect = RuntimeError("boom")
        w = _worker(hold=hold)
        w.step()
        assert len(w._db.readings) == 1

    def test_real_engine_driven_by_worker(self):
        relays = MagicMock()
        hold = IrrigationHold(relays, clock=Clock())
        settings = _settings(irrigation_hold_trip_samples=1, irrigation_hold_turbidity_ntu=2.0)
        _worker(hold=hold, settings=settings).step()
        # Configured by the worker (takes the channel over released), then held.
        assert [c for c in relays.set.call_args_list] == [
            ((3, False),),
            ((3, True), {"unbounded": True}),
        ]


# ---------------------------------------------------------------------------
# Simulator: turbidity spike + the hold on the compressed clock
# ---------------------------------------------------------------------------


def _sim_settings(**over):
    s = Settings()
    s.simulate_enabled = True
    s.flow_pulse_enabled = True
    s.sensor_read_s = 60
    for k, v in over.items():
        setattr(s, k, v)
    return s


class TestSimulatorSpike:
    def test_grammar(self):
        from sensors.sim import Fault, parse_faults

        assert parse_faults("turbidity:spike@5") == [Fault("turbidity", "spike", 5)]
        with pytest.raises(ValueError, match="turbidity only"):
            parse_faults("tds:spike@5")
        with pytest.raises(ValueError):
            parse_faults("ph:spike")

    def test_spike_is_bounded_then_normal(self):
        from sensors.sim import SPIKE_CYCLES, SPIKE_NTU, build_simulated_sensors, parse_faults

        sim = build_simulated_sensors(_sim_settings(), parse_faults("turbidity:spike@3"))
        values = []
        for _ in range(3 + SPIKE_CYCLES + 3):
            sim.cycle.tick()
            values.append(sim.turbidity.read_detailed().value)
        assert all(v != SPIKE_NTU for v in values[:2])
        assert values[2 : 2 + SPIKE_CYCLES] == [SPIKE_NTU] * SPIKE_CYCLES
        assert all(v < 60.0 for v in values[2 + SPIKE_CYCLES :])

    def test_spike_does_not_mask_a_later_fault(self):
        from sensors.sim import build_simulated_sensors, parse_faults

        sim = build_simulated_sensors(
            _sim_settings(), parse_faults("turbidity:spike@1,turbidity:read_failed@20")
        )
        for _ in range(20):
            sim.cycle.tick()
        assert sim.turbidity.read_detailed().status == "read_failed"

    def test_virtual_unit_holds_and_releases_on_compressed_clock(self, tmp_path):
        from sensors.sim import SPIKE_CYCLES
        from sensors.sim.unit import VirtualUnit

        u = VirtualUnit(
            1,
            db_path=str(tmp_path / "u.db"),
            cloud_api_base="http://localhost:5001/app",
            ingest_url="http://localhost:5001/ingest",
            api_key="k" * 32,
            settings=_sim_settings(
                irrigation_hold_enabled=True,
                irrigation_hold_relay=3,
                irrigation_hold_turbidity_ntu=80.0,
                irrigation_hold_release_min=5,
            ),
            faults="turbidity:spike@3",
        )
        u.sample(4)  # cycles 3 and 4 are the spike: two tripping samples
        assert u.hold.state().active is True
        assert u.relays.get(3) is True
        # relay_state is sampled before this cycle's evaluation (as for rules),
        # so the row after the engaging one is the first to carry the bit.
        u.sample(1)
        rows = u.db.get_unsynced(10)
        payload = u.cloud.reading_to_json(rows[-1])
        assert payload["metadata"]["irrigationHold"]["active"] is True
        assert payload["metadata"]["relayState"] == 4
        # Spike ends after SPIKE_CYCLES; five simulated minutes later it releases.
        u.sample(SPIKE_CYCLES - 3 + 5)  # cycle 17: first clear was 13
        assert u.hold.state().active is True
        u.sample(1)
        assert u.hold.state().active is False
        assert u.relays.get(3) is False
        u.close()

    def test_virtual_unit_payload_unchanged_when_hold_disabled(self, tmp_path):
        from sensors.sim.unit import VirtualUnit

        u = VirtualUnit(
            1,
            db_path=str(tmp_path / "u.db"),
            cloud_api_base="http://localhost:5001/app",
            ingest_url="http://localhost:5001/ingest",
            api_key="k" * 32,
            settings=_sim_settings(),
            faults="turbidity:spike@1",
        )
        u.sample(3)
        payload = u.cloud.reading_to_json(u.db.get_unsynced(5)[-1])
        assert "irrigationHold" not in payload["metadata"]
        assert payload["metadata"]["relayState"] == 0
        u.close()


# ---------------------------------------------------------------------------
# Service Window
# ---------------------------------------------------------------------------


@pytest.fixture
def sw_client(tmp_path):
    import sqlite3

    from service_window.app import create_app

    db_path = str(tmp_path / "t.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE readings (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT,
            ph REAL, tds_ppm REAL, turbidity_ntu REAL, orp_mv REAL, temp_c REAL,
            lat REAL, lon REAL, alt_m REAL, battery_v REAL,
            relay_state INTEGER DEFAULT 0, synced INTEGER DEFAULT 0);
        CREATE TABLE lorawan_session (id INTEGER PRIMARY KEY, dev_addr BLOB, nwk_skey BLOB,
            app_skey BLOB, fcnt_up INTEGER DEFAULT 0, fcnt_down INTEGER DEFAULT 0,
            joined INTEGER DEFAULT 0, updated_at TEXT);
        """
    )
    conn.close()
    app = create_app(
        {
            "db_path": db_path,
            "pin": "9999",
            "config_path": str(tmp_path / "config.yaml"),
            "cal_path": str(tmp_path / "cal.yaml"),
            "cmd_sock": str(tmp_path / "cmd.sock"),
        }
    )
    app.config["TESTING"] = True
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["pin_verified"] = True
    return c


HOLDING = {
    "ok": True,
    "enabled": True,
    "relay": 3,
    "active": True,
    "since": "2026-09-29T12:00:00Z",
    "reasons": [{"sensor": "turbidity_ntu", "value": 14.0, "threshold": 8.0}],
    "fault": None,
    "error": None,
    "releaseMin": 10.0,
}


class TestServiceWindow:
    def test_relays_page_labels_hold_and_disables_buttons(self, sw_client, monkeypatch):
        import service_window.routes.relays as mod

        monkeypatch.setattr(mod, "send_command", lambda sock, action, **kw: dict(HOLDING))
        html = sw_client.get("/relays/").get_data(as_text=True)
        assert "Relay 3 · Irrigation hold" in html
        assert "holding since 2026-09-29T12:00:00Z (turbidity 14 NTU ≥ 8)" in html
        assert html.count('disabled title="Irrigation hold owns this relay"') == 2
        assert 'setRelay(1, true)">ON' in html  # others untouched

    def test_relays_page_plain_when_firmware_unreachable(self, sw_client):
        html = sw_client.get("/relays/").get_data(as_text=True)
        assert "Irrigation hold" not in html
        assert "disabled" not in html

    def test_status_row_holding(self, sw_client, monkeypatch):
        import service_window.routes.status as mod

        monkeypatch.setattr(mod, "send_command", lambda sock, action, **kw: dict(HOLDING))
        html = sw_client.get("/").get_data(as_text=True)
        assert "Irrigation hold: holding since 2026-09-29T12:00:00Z (turbidity 14 NTU ≥ 8)" in html

    def test_status_row_off(self, sw_client, monkeypatch):
        import service_window.routes.status as mod

        monkeypatch.setattr(
            mod, "send_command", lambda sock, action, **kw: {"ok": True, "enabled": False}
        )
        assert "Irrigation hold: off" in sw_client.get("/").get_data(as_text=True)

    def test_status_row_omitted_when_firmware_unreachable(self, sw_client):
        assert "Irrigation hold" not in sw_client.get("/").get_data(as_text=True)


class TestStatusLine:
    def test_lines(self):
        assert status_line(None) is None
        assert status_line({"ok": False, "error": "x"}) is None
        assert status_line({"ok": True, "enabled": False}) == "off"
        assert status_line({"ok": True, "enabled": True, "relay": 3, "active": False}) == "clear"
        assert (
            status_line(
                {"ok": True, "enabled": True, "relay": 3, "active": False, "fault": "released"}
            )
            == "clear — probe fault, released"
        )
        assert "breaker interlock" in status_line(
            {"ok": True, "enabled": True, "relay": 3, "error": "relay_conflict"}
        )
        assert status_line(dict(HOLDING)).startswith("holding since")

    def test_copy_describes_the_contact_not_the_water(self):
        import re
        from pathlib import Path

        import control.irrigation_hold as mod

        root = Path(mod.__file__).resolve().parents[2]
        texts = [
            Path(mod.__file__).read_text(),
            (root / "src/service_window/templates/relays.html").read_text(),
            (root / "docs/irrigation-hold.md").read_text(),
        ]
        banned = re.compile(
            r"\b(certified|approved|authori[sz]ed|partner|guarantee[sd]?|warrant(y|ies|s|ed)"
            r"|safe|unsafe|potable|penalt\w*|forfeit\w*)\b|licensed by|"
            r"BlueSignal (installs|wires|connects)|we (install|wire)|works with",
            re.IGNORECASE,
        )
        for text in texts:
            assert not banned.search(text), banned.search(text)
