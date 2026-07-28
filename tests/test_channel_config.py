"""
Channel config rejection tests.

These assert on the *content* of the error, not just that one was raised — an
installer reading "invalid config" learns nothing, while "exceeds the 3.0 A
limit for a NC contact" tells them what to change.
"""

import pytest

from utils.config import (
    CONTACT_NC,
    CONTACT_NO,
    RELAY_MAX_CURRENT_A_NC,
    RELAY_MAX_CURRENT_A_NO,
    load_channel_configs,
    validate_channel_config,
)


def _cfg(**over):
    base = {
        "channel": 1,
        "role": "auxiliary",
        "contact": CONTACT_NO,
        "fail_safe_state": "stop",
        "load_type": "contactor_coil",
        "expected_current_a": 0.5,
    }
    base.update(over)
    return base


class TestFailSafeInvariant:
    """fail_safe_state=run is only physically achievable on an NC contact."""

    def test_rejects_aeration_on_normally_open(self):
        cfg, errors = validate_channel_config(
            _cfg(role="aeration", contact=CONTACT_NO, fail_safe_state="run")
        )
        assert cfg is None
        assert any("life-critical" in e and "NC" in e for e in errors)

    def test_rejects_any_role_wanting_run_failsafe_on_no(self):
        # The general invariant: not just aeration. A life-critical load
        # mislabelled 'auxiliary' must not slip through.
        cfg, errors = validate_channel_config(
            _cfg(role="auxiliary", contact=CONTACT_NO, fail_safe_state="run")
        )
        assert cfg is None
        assert any("requires contact=NC" in e for e in errors)

    def test_accepts_aeration_on_normally_closed(self):
        cfg, errors = validate_channel_config(
            _cfg(role="aeration", contact=CONTACT_NC, fail_safe_state="run")
        )
        assert errors == []
        assert cfg is not None
        assert cfg.fail_safe_state == "run"

    def test_failsafe_is_never_energised(self):
        cfg, _ = validate_channel_config(_cfg(contact=CONTACT_NC, fail_safe_state="run"))
        assert cfg.failsafe_is_energised is False

    def test_circulation_is_treated_as_life_critical(self):
        cfg, errors = validate_channel_config(
            _cfg(role="circulation", contact=CONTACT_NO, fail_safe_state="run")
        )
        assert cfg is None
        assert any("life-critical" in e for e in errors)


class TestPilotDutyLimits:
    def test_rejects_over_5a_on_normally_open(self):
        cfg, errors = validate_channel_config(_cfg(contact=CONTACT_NO, expected_current_a=5.1))
        assert cfg is None
        assert any(f"{RELAY_MAX_CURRENT_A_NO:.1f} A limit" in e for e in errors)

    def test_rejects_over_3a_on_normally_closed(self):
        cfg, errors = validate_channel_config(
            _cfg(contact=CONTACT_NC, fail_safe_state="run", expected_current_a=3.1)
        )
        assert cfg is None
        assert any(f"{RELAY_MAX_CURRENT_A_NC:.1f} A limit" in e for e in errors)

    def test_error_names_the_limit_and_pilot_duty(self):
        _, errors = validate_channel_config(_cfg(expected_current_a=9.0))
        joined = " ".join(errors)
        assert "5.0 A limit" in joined
        assert "pilot-duty" in joined
        assert "contactor coil" in joined

    @pytest.mark.parametrize("amps", [5.0, 4.9, 0.0])
    def test_accepts_at_or_below_no_limit(self, amps):
        cfg, errors = validate_channel_config(_cfg(contact=CONTACT_NO, expected_current_a=amps))
        assert errors == [], errors
        assert cfg is not None

    def test_rejects_line_voltage_load_type(self):
        cfg, errors = validate_channel_config(_cfg(load_type="line_voltage_motor"))
        assert cfg is None
        assert any("pilot duty" in e for e in errors)


class TestEnumAndRangeRejection:
    @pytest.mark.parametrize(
        "over,needle",
        [
            ({"role": "nonsense"}, "unknown role"),
            ({"contact": "SPDT"}, "contact must be NO or NC"),
            ({"fail_safe_state": "maybe"}, "fail_safe_state must be run or stop"),
            ({"channel": 9}, "channel must be 1-4"),
            ({"stale_cycles": 0}, "stale_cycles must be >= 1"),
            ({"min_on_s": -5}, "min_on_s must be >= 0"),
        ],
    )
    def test_rejects(self, over, needle):
        cfg, errors = validate_channel_config(_cfg(**over))
        assert cfg is None
        assert any(needle in e for e in errors), errors


class TestDefaultsAndLoading:
    def test_channel_defaults_to_uncommissioned(self):
        cfg, errors = validate_channel_config(_cfg())
        assert errors == []
        assert cfg.commissioned is False, "channels must boot inert"

    def test_default_stale_cycles_is_three(self):
        cfg, _ = validate_channel_config(_cfg())
        assert cfg.stale_cycles == 3

    def test_invalid_channel_is_dropped_not_partially_applied(self):
        configs, errors = load_channel_configs(
            {
                "channels": [
                    _cfg(channel=1),
                    _cfg(channel=2, role="aeration", contact=CONTACT_NO, fail_safe_state="run"),
                ]
            }
        )
        assert 1 in configs
        assert 2 not in configs, "an unvalidatable channel must stay inert, not half-applied"
        assert errors

    def test_empty_policies_yields_no_channels(self):
        configs, errors = load_channel_configs({})
        assert configs == {}
        assert errors == []
