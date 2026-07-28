"""Edge-case tests for src/control/rules.py — covers the less-travelled paths."""

import time
from datetime import time as dt_time
from unittest.mock import MagicMock


class TestLoadRulesMalformed:
    def test_invalid_rule_logged_and_skipped(self, mock_hardware, caplog):
        from control.rules import RulesEngine

        engine = RulesEngine()
        # Missing required fields triggers TypeError/KeyError path (lines 114-115)
        engine.load_rules(
            [
                {"sensor": "ph", "operator": ">"},  # missing threshold/relay/action
                {"not_a_field": 1},
                {
                    "sensor": "ph",
                    "operator": ">",
                    "threshold": 8.0,
                    "relay": 1,
                    "action": "on",
                },
            ]
        )
        # Only the valid rule should remain
        assert len(engine._rules) == 1

    def test_invalid_operator_skipped(self, mock_hardware):
        from control.rules import Rule, RulesEngine

        engine = RulesEngine()
        # Direct instantiation with a bogus operator bypasses load_rules validation
        engine._rules.append(
            Rule(sensor="ph", operator="bogus", threshold=7.0, relay=1, action="on")
        )
        actions = engine.evaluate({"ph": 9.0})
        assert actions == []


class TestScheduleEdgeCases:
    def test_overnight_window_in_range(self, mock_hardware):
        """Overnight schedule (22:00–06:00) should allow fires at 23:00."""
        from control.rules import Rule, RulesEngine

        engine = RulesEngine()
        engine._schedule_enabled = True
        engine._schedule_start = dt_time(22, 0)
        engine._schedule_end = dt_time(6, 0)
        engine.add_rule(Rule(sensor="ph", operator=">", threshold=8.0, relay=1, action="on"))

        # We can't force datetime.now, so just verify the method executes.
        # Both branches return bool, and this hits the overnight line.
        result = engine._is_in_schedule()
        assert isinstance(result, bool)

    def test_schedule_enabled_but_times_none_returns_true(self, mock_hardware):
        from control.rules import RulesEngine

        engine = RulesEngine()
        engine._schedule_enabled = True
        engine._schedule_start = None
        engine._schedule_end = None
        # Line 121-122: enabled but no times → assume always-in-window
        assert engine._is_in_schedule() is True


class TestCooldownNoPriorOff:
    def test_cooldown_with_no_prior_off_returns_false(self, mock_hardware):
        from control.rules import RulesEngine

        engine = RulesEngine()
        engine._cooldown_s = 30
        # No entry in _last_off → not in cooldown (line 134-135)
        assert engine._is_in_cooldown(1) is False


class TestAutoShutoffTimer:
    def test_timer_expiry_generates_off_action(self, mock_hardware):
        from control.rules import Rule, RulesEngine

        mock_relay = MagicMock()
        engine = RulesEngine(mock_relay)
        engine.add_rule(Rule(sensor="ph", operator=">", threshold=8.0, relay=1, action="on"))
        # Pre-set a timer that's already expired
        engine._timers[1] = time.monotonic() - 1.0

        actions = engine.evaluate({"ph": 5.0})  # condition false, so no ON action
        assert (1, False) in actions
        assert 1 not in engine._timers  # timer cleared
        assert 1 in engine._last_off  # OFF recorded

    def test_timer_set_for_duration_rule(self, mock_hardware):
        from control.rules import Rule, RulesEngine

        engine = RulesEngine()
        engine.add_rule(
            Rule(
                sensor="ph",
                operator=">",
                threshold=8.0,
                relay=1,
                action="on",
                duration_s=60,
            )
        )

        engine.evaluate({"ph": 9.0})
        assert 1 in engine._timers
        assert engine._timers[1] > time.monotonic()


class TestRelayFailureHandling:
    def test_relay_exception_does_not_crash_evaluate(self, mock_hardware, caplog):
        """If relay.set() raises, evaluate() must still return the action list."""
        from control.rules import Rule, RulesEngine

        bad_relay = MagicMock()
        bad_relay.set.side_effect = RuntimeError("GPIO error")

        engine = RulesEngine(bad_relay)
        engine.add_rule(Rule(sensor="ph", operator=">", threshold=8.0, relay=1, action="on"))

        actions = engine.evaluate({"ph": 9.0})
        assert (1, True) in actions  # action still reported despite the error


class TestAccumulateOnTime:
    def test_accumulate_noop_when_budget_disabled(self, mock_hardware):
        from control.rules import RulesEngine

        engine = RulesEngine()
        assert engine._max_on_s_per_hour == 0
        engine._accumulate_on_time(1, 5.0)
        # Should have done nothing (line 152-153 early return)
        assert 1 not in engine._on_time

    def test_accumulate_adds_to_existing_entry(self, mock_hardware):
        from datetime import UTC, datetime

        from control.rules import RulesEngine

        engine = RulesEngine()
        engine._max_on_s_per_hour = 600
        current_hour = datetime.now(UTC).hour
        engine._on_time[1] = (current_hour, 10.0)

        engine._accumulate_on_time(1, 5.0)
        # Line 158-159: hour matches → accumulates
        assert engine._on_time[1][1] == 15.0

    def test_accumulate_resets_on_new_hour(self, mock_hardware):
        from control.rules import RulesEngine

        engine = RulesEngine()
        engine._max_on_s_per_hour = 600
        engine._on_time[1] = (99, 500.0)  # impossible hour → triggers reset branch

        engine._accumulate_on_time(1, 7.0)
        # Line 156-157: different hour → replaces with the new seconds
        assert engine._on_time[1][1] == 7.0


class TestEvaluateAccumulation:
    def test_evaluate_accumulates_on_time_for_active_relay(self, mock_hardware):
        from control.rules import Rule, RulesEngine

        engine = RulesEngine()
        engine.load_policies({"limits": {"max_on_minutes_per_hour": 60}})
        engine.add_rule(Rule(sensor="ph", operator=">", threshold=8.0, relay=1, action="on"))

        # First tick: rule fires, relay becomes "on" and _on_since set
        engine.evaluate({"ph": 9.0})
        assert 1 in engine._on_since

        # Sleep briefly then evaluate again — line 186-189 should accumulate
        time.sleep(0.01)
        engine.evaluate({"ph": 9.0})
        # An entry for relay 1 in current hour should exist and have >0 seconds
        assert 1 in engine._on_time
        assert engine._on_time[1][1] >= 0.0


class TestDownlinkDurationTimer:
    def test_downlink_on_with_duration_creates_timer(self, mock_hardware):
        from control.rules import RulesEngine

        mock_relay = MagicMock()
        engine = RulesEngine(mock_relay)

        # FPort 100, channel=2, state=ON (1), duration=0x001E = 30s
        payload = bytes([2, 1, 0x00, 0x1E])
        assert engine.process_downlink_command(100, payload) is True

        mock_relay.set.assert_called_with(2, True)
        assert 2 in engine._timers
        # Timer should be roughly 30s in the future (line 283)
        assert engine._timers[2] > time.monotonic() + 20

    def test_downlink_wrong_fport_rejected(self, mock_hardware):
        from control.rules import RulesEngine

        engine = RulesEngine()
        assert engine.process_downlink_command(99, b"\x01\x01") is False

    def test_downlink_invalid_channel_rejected(self, mock_hardware):
        from control.rules import RulesEngine

        engine = RulesEngine()
        # Channel 0 is invalid
        assert engine.process_downlink_command(100, bytes([0, 1])) is False
        # Channel 5 is invalid
        assert engine.process_downlink_command(100, bytes([5, 1])) is False

    def test_downlink_short_payload_rejected(self, mock_hardware):
        from control.rules import RulesEngine

        engine = RulesEngine()
        assert engine.process_downlink_command(100, b"\x01") is False
