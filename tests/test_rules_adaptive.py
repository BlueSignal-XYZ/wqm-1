"""Adaptive rules — relay automation against the site's learned normal.

An adaptive rule fires on deviation from the water profile (median ± k·σ̂ at
the current UTC hour) instead of a fixed setpoint. The invariant under test
throughout: no established baseline, no actuation — a rule pointed at a
parameter that is missing, still learning, or cleared must stay silent.
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock

# 03:00 UTC — bucket 3 in the profile below.
FIXED_NOW = datetime(2026, 8, 19, 3, 30, tzinfo=UTC)


def make_engine(profile=None):
    from control.rules import RulesEngine

    relay = MagicMock()
    engine = RulesEngine(relay, clock=lambda: FIXED_NOW)
    if profile is not None:
        engine.set_baselines(profile)
    return engine, relay


def profile_with(param="ph", m=7.0, mad=0.1, bucket_hour=3):
    """A profile whose hour bucket at `bucket_hour` and overall both carry m/mad."""
    buckets = [None] * 24
    buckets[bucket_hour] = {"m": m, "mad": mad, "n": 40}
    return {
        "params": {param: {"buckets": buckets, "overall": {"m": m, "mad": mad}}},
        "windowDays": 30,
    }


class TestAdaptiveResolution:
    def test_fires_above_learned_band(self, mock_hardware):
        from control.rules import Rule

        # m=7.0, mad=0.1 → σ̂=0.14826; k=2 → threshold ≈ 7.297
        engine, relay = make_engine(profile_with())
        engine.add_rule(Rule(sensor="ph", operator=">", adaptive_k=2.0, relay=1, action="on"))

        assert engine.evaluate({"ph": 7.2}) == []
        assert (1, True) in engine.evaluate({"ph": 7.5})
        relay.set.assert_called_with(1, True)

    def test_below_band_direction_for_less_than(self, mock_hardware):
        from control.rules import Rule

        # "<" resolves m − k·σ̂ ≈ 6.703 — fire when the water drops BELOW its normal.
        engine, _ = make_engine(profile_with())
        engine.add_rule(Rule(sensor="ph", operator="<", adaptive_k=2.0, relay=2, action="on"))

        assert engine.evaluate({"ph": 6.9}) == []
        assert (2, True) in engine.evaluate({"ph": 6.5})

    def test_mad_floor_stops_flatline_hair_trigger(self, mock_hardware):
        from control.rules import Rule

        # mad=0 would make any wiggle cross the band; the per-column floor
        # (0.05 for pH, mirroring the cloud engine) keeps jitter inert.
        engine, _ = make_engine(profile_with(mad=0.0))
        engine.add_rule(Rule(sensor="ph", operator=">", adaptive_k=2.0, relay=1, action="on"))

        assert engine.evaluate({"ph": 7.05}) == []  # within 2×0.05 floor band
        assert (1, True) in engine.evaluate({"ph": 7.2})

    def test_falls_back_to_overall_when_bucket_missing(self, mock_hardware):
        from control.rules import Rule

        # Bucket for the current hour is None (device slept) — overall stats apply.
        engine, _ = make_engine(profile_with(bucket_hour=9))
        engine.add_rule(Rule(sensor="ph", operator=">", adaptive_k=2.0, relay=1, action="on"))

        assert (1, True) in engine.evaluate({"ph": 7.5})

    def test_column_name_maps_to_canonical_param(self, mock_hardware):
        from control.rules import Rule

        # Rules speak reading columns (tds_ppm); the profile speaks canonical
        # names (tds). The engine must bridge them.
        engine, _ = make_engine(profile_with(param="tds", m=200.0, mad=10.0))
        engine.add_rule(Rule(sensor="tds_ppm", operator=">", adaptive_k=2.0, relay=1, action="on"))

        assert (1, True) in engine.evaluate({"tds_ppm": 300.0})
        assert engine.evaluate({"tds_ppm": 210.0}) == []


class TestNoBaselineNoActuation:
    def test_silent_with_no_profile(self, mock_hardware):
        from control.rules import Rule

        engine, relay = make_engine()
        engine.add_rule(Rule(sensor="ph", operator=">", adaptive_k=2.0, relay=1, action="on"))

        assert engine.evaluate({"ph": 12.0}) == []
        relay.set.assert_not_called()

    def test_silent_while_learning(self, mock_hardware):
        from control.rules import Rule

        profile = {"params": {"ph": {"learning": True, "days": 3}}}
        engine, relay = make_engine(profile)
        engine.add_rule(Rule(sensor="ph", operator=">", adaptive_k=2.0, relay=1, action="on"))

        assert engine.evaluate({"ph": 12.0}) == []
        relay.set.assert_not_called()

    def test_clearing_baselines_silences_adaptive_rules(self, mock_hardware):
        from control.rules import Rule

        engine, _ = make_engine(profile_with())
        engine.add_rule(Rule(sensor="ph", operator=">", adaptive_k=2.0, relay=1, action="on"))
        assert (1, True) in engine.evaluate({"ph": 7.5})

        engine.set_baselines(None)
        assert engine.evaluate({"ph": 7.5}) == []

    def test_fixed_rules_unaffected_by_missing_profile(self, mock_hardware):
        from control.rules import Rule

        engine, _ = make_engine()
        engine.add_rule(Rule(sensor="ph", operator=">", threshold=9.0, relay=1, action="on"))

        assert (1, True) in engine.evaluate({"ph": 9.5})


class TestRuleValidation:
    def test_equality_operator_rejected_for_adaptive(self, mock_hardware):
        from control.rules import Rule

        engine, _ = make_engine(profile_with())
        engine.add_rule(Rule(sensor="ph", operator="==", adaptive_k=2.0, relay=1, action="on"))

        assert engine._rules == []

    def test_adaptive_k_out_of_range_rejected(self, mock_hardware):
        from control.rules import Rule

        engine, _ = make_engine(profile_with())
        engine.add_rule(Rule(sensor="ph", operator=">", adaptive_k=0.1, relay=1, action="on"))
        engine.add_rule(Rule(sensor="ph", operator=">", adaptive_k=50, relay=1, action="on"))

        assert engine._rules == []

    def test_rule_with_neither_threshold_nor_k_rejected(self, mock_hardware):
        from control.rules import Rule

        engine, _ = make_engine()
        engine.add_rule(Rule(sensor="ph", operator=">", relay=1, action="on"))

        assert engine._rules == []

    def test_load_rules_accepts_camelcase_adaptive_key(self, mock_hardware):
        # The cloud stores the band width as adaptiveK; the wire format must load.
        engine, _ = make_engine(profile_with())
        engine.load_rules(
            [{"sensor": "ph", "operator": ">", "adaptiveK": 2.0, "relay": 1, "action": "on"}]
        )

        assert len(engine._rules) == 1
        assert (1, True) in engine.evaluate({"ph": 7.5})


class TestSafetyGuardsStillApply:
    def test_suspended_sensor_beats_adaptive_rule(self, mock_hardware):
        from control.rules import Rule

        # Sensor-health suspension must gate adaptive rules exactly like fixed
        # ones — a frozen probe can't actuate off its learned band either.
        engine, _ = make_engine(profile_with())
        engine.add_rule(Rule(sensor="ph", operator=">", adaptive_k=2.0, relay=1, action="on"))
        engine.set_suspended_sensors({"ph"})
        engine.evaluate({"ph": 7.5})  # drains the one-shot fail-safe drop

        assert (1, True) not in engine.evaluate({"ph": 7.5})
