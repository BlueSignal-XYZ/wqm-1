"""Tests for firmware/src/control/rules.py — threshold automation."""

import time
from datetime import time as dt_time
from unittest.mock import MagicMock, patch


class TestRulesEngine:
    def test_evaluate_threshold_exceeded(self, mock_hardware):
        from control.rules import Rule, RulesEngine

        mock_relay = MagicMock()
        engine = RulesEngine(mock_relay)
        engine.add_rule(Rule(sensor="ph", operator=">", threshold=9.0, relay=1, action="on"))

        actions = engine.evaluate({"ph": 9.5})
        assert (1, True) in actions
        mock_relay.set.assert_called_with(1, True)

    def test_evaluate_threshold_not_exceeded(self, mock_hardware):
        from control.rules import Rule, RulesEngine

        mock_relay = MagicMock()
        engine = RulesEngine(mock_relay)
        engine.add_rule(Rule(sensor="ph", operator=">", threshold=9.0, relay=1, action="on"))

        actions = engine.evaluate({"ph": 7.0})
        assert len(actions) == 0

    def test_evaluate_less_than(self, mock_hardware):
        from control.rules import Rule, RulesEngine

        engine = RulesEngine()
        engine.add_rule(Rule(sensor="tds_ppm", operator="<", threshold=100.0, relay=2, action="on"))

        actions = engine.evaluate({"tds_ppm": 50.0})
        assert (2, True) in actions

    def test_evaluate_off_action(self, mock_hardware):
        from control.rules import Rule, RulesEngine

        engine = RulesEngine()
        engine.add_rule(Rule(sensor="ph", operator="<=", threshold=6.0, relay=3, action="off"))

        actions = engine.evaluate({"ph": 5.5})
        assert (3, False) in actions

    def test_missing_sensor_skipped(self, mock_hardware):
        from control.rules import Rule, RulesEngine

        engine = RulesEngine()
        engine.add_rule(Rule(sensor="ph", operator=">", threshold=9.0, relay=1, action="on"))

        actions = engine.evaluate({"tds_ppm": 500.0})  # no pH
        assert len(actions) == 0

    def test_load_rules_from_dicts(self, mock_hardware):
        from control.rules import RulesEngine

        engine = RulesEngine()
        engine.load_rules(
            [
                {"sensor": "ph", "operator": ">", "threshold": 9.0, "relay": 1, "action": "on"},
                {"sensor": "tds_ppm", "operator": "<", "threshold": 50, "relay": 2, "action": "on"},
            ]
        )
        assert len(engine._rules) == 2


class TestDownlinkCommand:
    def test_relay_on_command(self, mock_hardware):
        from control.rules import RulesEngine

        mock_relay = MagicMock()
        engine = RulesEngine(mock_relay)

        # FPort=100, channel=1, state=1 (on), duration=0
        result = engine.process_downlink_command(100, bytes([1, 1, 0, 0]))
        assert result is True
        mock_relay.set.assert_called_with(1, True)

    def test_relay_off_command(self, mock_hardware):
        from control.rules import RulesEngine

        mock_relay = MagicMock()
        engine = RulesEngine(mock_relay)

        result = engine.process_downlink_command(100, bytes([2, 0]))
        assert result is True
        mock_relay.set.assert_called_with(2, False)

    def test_wrong_fport_ignored(self, mock_hardware):
        from control.rules import RulesEngine

        engine = RulesEngine()
        assert engine.process_downlink_command(50, bytes([1, 1])) is False

    def test_invalid_channel_rejected(self, mock_hardware):
        from control.rules import RulesEngine

        engine = RulesEngine()
        assert engine.process_downlink_command(100, bytes([5, 1])) is False


class TestScheduleWindow:
    def test_rules_fire_inside_schedule(self, mock_hardware):
        from control.rules import Rule, RulesEngine

        engine = RulesEngine()
        engine.load_policies(
            {
                "schedule": {"enabled": True, "start": "00:00", "end": "23:59"},
            }
        )
        engine.add_rule(Rule(sensor="ph", operator=">", threshold=8.0, relay=1, action="on"))

        actions = engine.evaluate({"ph": 9.0})
        assert (1, True) in actions

    def test_rules_blocked_outside_schedule(self, mock_hardware):
        from control.rules import Rule, RulesEngine

        engine = RulesEngine()
        # Set a window that is definitely not now (1 minute in the past)
        engine._schedule_enabled = True
        engine._schedule_start = dt_time(0, 0)
        engine._schedule_end = dt_time(0, 1)
        engine.add_rule(Rule(sensor="ph", operator=">", threshold=8.0, relay=1, action="on"))

        # Mock datetime.now() to return 12:00
        with patch("control.rules.datetime") as mock_dt:
            mock_dt.now.return_value.time.return_value = dt_time(12, 0)
            mock_dt.now.return_value.hour = 12
            actions = engine.evaluate({"ph": 9.0})
            assert len(actions) == 0

    def test_schedule_disabled_allows_all(self, mock_hardware):
        from control.rules import Rule, RulesEngine

        engine = RulesEngine()
        engine.load_policies(
            {
                "schedule": {"enabled": False},
            }
        )
        engine.add_rule(Rule(sensor="ph", operator=">", threshold=8.0, relay=1, action="on"))

        actions = engine.evaluate({"ph": 9.0})
        assert (1, True) in actions


class TestCooldown:
    def test_cooldown_blocks_reactivation(self, mock_hardware):
        from control.rules import Rule, RulesEngine

        engine = RulesEngine()
        engine.load_policies(
            {
                "limits": {"cooldown_seconds": 120},
            }
        )
        engine.add_rule(Rule(sensor="ph", operator=">", threshold=8.0, relay=1, action="on"))

        # Simulate a recent OFF event
        engine._last_off[1] = time.monotonic()

        # Should be blocked by cooldown
        actions = engine.evaluate({"ph": 9.0})
        on_actions = [(ch, st) for ch, st in actions if st is True and ch == 1]
        assert len(on_actions) == 0

    def test_cooldown_expired_allows_activation(self, mock_hardware):
        from control.rules import Rule, RulesEngine

        engine = RulesEngine()
        engine.load_policies(
            {
                "limits": {"cooldown_seconds": 5},
            }
        )
        engine.add_rule(Rule(sensor="ph", operator=">", threshold=8.0, relay=1, action="on"))

        # Simulate an old OFF event (well past cooldown)
        engine._last_off[1] = time.monotonic() - 100

        actions = engine.evaluate({"ph": 9.0})
        assert (1, True) in actions

    def test_no_cooldown_allows_immediate(self, mock_hardware):
        from control.rules import Rule, RulesEngine

        engine = RulesEngine()
        # No cooldown configured
        engine.add_rule(Rule(sensor="ph", operator=">", threshold=8.0, relay=1, action="on"))

        engine._last_off[1] = time.monotonic()
        actions = engine.evaluate({"ph": 9.0})
        assert (1, True) in actions


class TestMaxOnTime:
    def test_on_time_budget_blocks_when_exceeded(self, mock_hardware):
        from control.rules import Rule, RulesEngine

        engine = RulesEngine()
        engine.load_policies(
            {
                "limits": {"max_on_minutes_per_hour": 10},
            }
        )
        engine.add_rule(Rule(sensor="ph", operator=">", threshold=8.0, relay=1, action="on"))

        # Simulate relay 1 already used 11 minutes this hour
        # UTC, matching RulesEngine._accumulate_on_time. A local-time hour only
        # agrees with it on a UTC machine, so this test passed in CI and failed
        # on any developer box with an offset.
        from datetime import UTC, datetime

        current_hour = datetime.now(UTC).hour
        engine._on_time[1] = (current_hour, 660.0)  # 11 minutes in seconds

        actions = engine.evaluate({"ph": 9.0})
        on_actions = [(ch, st) for ch, st in actions if st is True and ch == 1]
        assert len(on_actions) == 0

    def test_on_time_budget_allows_when_under_limit(self, mock_hardware):
        from control.rules import Rule, RulesEngine

        engine = RulesEngine()
        engine.load_policies(
            {
                "limits": {"max_on_minutes_per_hour": 10},
            }
        )
        engine.add_rule(Rule(sensor="ph", operator=">", threshold=8.0, relay=1, action="on"))

        # Simulate relay 1 used only 2 minutes this hour
        # UTC — see the note in test_on_time_budget_blocks_when_exceeded. This
        # one passed under local time only by accident: a mismatched hour reset
        # the counter to 0.0, which is also under the limit.
        from datetime import UTC, datetime

        current_hour = datetime.now(UTC).hour
        engine._on_time[1] = (current_hour, 120.0)  # 2 minutes

        actions = engine.evaluate({"ph": 9.0})
        assert (1, True) in actions

    def test_on_time_resets_new_hour(self, mock_hardware):
        from control.rules import Rule, RulesEngine

        engine = RulesEngine()
        engine.load_policies(
            {
                "limits": {"max_on_minutes_per_hour": 10},
            }
        )
        engine.add_rule(Rule(sensor="ph", operator=">", threshold=8.0, relay=1, action="on"))

        # Old hour data (different hour than now)
        engine._on_time[1] = (99, 660.0)  # impossible hour, triggers reset

        actions = engine.evaluate({"ph": 9.0})
        assert (1, True) in actions


class TestManualOverride:
    def test_manual_override_blocks_rules(self, mock_hardware):
        from control.rules import Rule, RulesEngine

        engine = RulesEngine()
        engine.load_policies(
            {
                "manual": {"override": True},
            }
        )
        engine.add_rule(Rule(sensor="ph", operator=">", threshold=8.0, relay=1, action="on"))

        actions = engine.evaluate({"ph": 9.0})
        assert len(actions) == 0

    def test_manual_override_off_allows_rules(self, mock_hardware):
        from control.rules import Rule, RulesEngine

        engine = RulesEngine()
        engine.load_policies(
            {
                "manual": {"override": False},
            }
        )
        engine.add_rule(Rule(sensor="ph", operator=">", threshold=8.0, relay=1, action="on"))

        actions = engine.evaluate({"ph": 9.0})
        assert (1, True) in actions


class TestLoadPolicies:
    def test_load_full_policies(self, mock_hardware):
        from control.rules import RulesEngine

        engine = RulesEngine()
        engine.load_policies(
            {
                "limits": {
                    "max_on_minutes_per_hour": 10,
                    "cooldown_seconds": 120,
                },
                "schedule": {
                    "enabled": True,
                    "start": "07:00",
                    "end": "21:00",
                },
                "manual": {"override": False},
            }
        )

        assert engine._cooldown_s == 120
        assert engine._max_on_s_per_hour == 600  # 10 * 60
        assert engine._schedule_enabled is True
        assert engine._schedule_start == dt_time(7, 0)
        assert engine._schedule_end == dt_time(21, 0)
        assert engine._manual_override is False

    def test_load_empty_policies(self, mock_hardware):
        from control.rules import RulesEngine

        engine = RulesEngine()
        engine.load_policies({})

        assert engine._cooldown_s == 0
        assert engine._max_on_s_per_hour == 0
        assert engine._schedule_enabled is False

    def test_load_partial_policies(self, mock_hardware):
        from control.rules import RulesEngine

        engine = RulesEngine()
        engine.load_policies(
            {
                "limits": {"cooldown_seconds": 60},
            }
        )

        assert engine._cooldown_s == 60
        assert engine._max_on_s_per_hour == 0


class TestMultipleRules:
    def test_multiple_rules_same_relay(self, mock_hardware):
        from control.rules import Rule, RulesEngine

        engine = RulesEngine()
        engine.add_rule(
            Rule(
                sensor="ph",
                operator=">",
                threshold=8.5,
                relay=1,
                action="on",
                duration_s=30,
            )
        )
        engine.add_rule(Rule(sensor="temp_c", operator=">", threshold=45.0, relay=1, action="off"))

        # Both conditions met: high pH and high temp
        actions = engine.evaluate({"ph": 9.0, "temp_c": 50.0})
        # Should have both ON and OFF for relay 1
        assert (1, True) in actions
        assert (1, False) in actions

    def test_rules_across_all_sensors(self, mock_hardware):
        from control.rules import Rule, RulesEngine

        engine = RulesEngine()
        engine.add_rule(Rule(sensor="ph", operator=">", threshold=8.5, relay=1, action="on"))
        engine.add_rule(Rule(sensor="tds_ppm", operator=">", threshold=2000, relay=4, action="on"))
        engine.add_rule(Rule(sensor="orp_mv", operator="<", threshold=200, relay=3, action="on"))
        engine.add_rule(
            Rule(
                sensor="turbidity_ntu",
                operator=">",
                threshold=500,
                relay=3,
                action="on",
            )
        )
        engine.add_rule(Rule(sensor="temp_c", operator=">", threshold=35, relay=4, action="on"))

        reading = {
            "ph": 9.0,
            "tds_ppm": 2500,
            "orp_mv": 150,
            "turbidity_ntu": 600,
            "temp_c": 37,
        }

        actions = engine.evaluate(reading)
        assert (1, True) in actions  # pH high
        assert (4, True) in actions  # TDS high and temp high
        assert (3, True) in actions  # ORP low and turbidity high
