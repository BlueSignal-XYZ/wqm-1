"""Tests for firmware/src/control/rules.py — threshold automation."""

import pytest
from unittest.mock import MagicMock


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
        engine.load_rules([
            {"sensor": "ph", "operator": ">", "threshold": 9.0, "relay": 1, "action": "on"},
            {"sensor": "tds_ppm", "operator": "<", "threshold": 50, "relay": 2, "action": "on"},
        ])
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
