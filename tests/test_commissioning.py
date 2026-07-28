"""
Commissioning tests.

Two properties matter here:

  1. A channel that has not been commissioned never actuates from the control
     loop, a manual request, or a cloud command.
  2. The one path that CAN move an uncommissioned channel — the test-fire —
     requires an explicit typed operator confirmation, is logged, and leaves
     the channel back at its fail-safe.
"""

from control.channel import ChannelController
from control.relay import RelayController
from storage.database import WQM1Database
from utils.config import (
    CAUSE_COMMISSIONING_TEST,
    CONTACT_NC,
    CONTACT_NO,
    RELAY_PINS,
    ChannelConfig,
)


def _cfg(channel=1, commissioned=False, **over):
    kwargs = dict(
        channel=channel,
        role="dosing",
        contact=CONTACT_NO,
        fail_safe_state="stop",
        commissioned=commissioned,
    )
    kwargs.update(over)
    return ChannelConfig(**kwargs)


def _controller(cfg, db=None):
    return ChannelController(RelayController(), {cfg.channel: cfg}, db=db)


def pin_of(channel):
    return RELAY_PINS[channel - 1]


class TestInertUntilCommissioned:
    def test_control_loop_cannot_actuate(self, gpio_pins):
        cc = _controller(_cfg(commissioned=False))
        assert cc.request(1, run=True, cause="setpoint") is False
        assert gpio_pins[pin_of(1)] == 0

    def test_manual_request_cannot_actuate(self, gpio_pins):
        cc = _controller(_cfg(commissioned=False))
        assert cc.request(1, run=True, cause="manual") is False
        assert gpio_pins[pin_of(1)] == 0

    def test_commissioning_unlocks_actuation(self, gpio_pins):
        cc = _controller(_cfg(commissioned=True))
        assert cc.request(1, run=True, cause="setpoint") is True
        assert gpio_pins[pin_of(1)] == 1

    def test_decommissioning_reverts_to_failsafe(self, gpio_pins):
        cc = _controller(_cfg(commissioned=True))
        cc.request(1, run=True, cause="setpoint")
        assert gpio_pins[pin_of(1)] == 1

        cc.set_configs({1: _cfg(commissioned=False)})
        assert gpio_pins[pin_of(1)] == 0, "a decommissioned channel must not stay energised"

    def test_removing_a_channel_reverts_it(self, gpio_pins):
        cc = _controller(_cfg(commissioned=True))
        cc.request(1, run=True, cause="setpoint")
        cc.set_configs({})
        assert gpio_pins[pin_of(1)] == 0


class TestTestFire:
    def test_drives_load_away_from_failsafe(self, gpio_pins):
        cc = _controller(_cfg(commissioned=False))
        assert cc.force_test_fire(1, cause=CAUSE_COMMISSIONING_TEST) is True
        assert gpio_pins[pin_of(1)] == 1, "installer must be able to see the load move"
        assert cc.last_cause(1) == CAUSE_COMMISSIONING_TEST

    def test_returns_to_failsafe_afterwards(self, gpio_pins):
        cc = _controller(_cfg(commissioned=False))
        cc.force_test_fire(1, cause=CAUSE_COMMISSIONING_TEST)
        cc.revert_to_fail_safe(1, cause=CAUSE_COMMISSIONING_TEST)
        assert gpio_pins[pin_of(1)] == 0

    def test_nc_channel_test_fire_stops_the_load(self, gpio_pins):
        """On an NC aeration channel, 'away from fail-safe' means stopping it."""
        cc = _controller(
            _cfg(channel=3, role="aeration", contact=CONTACT_NC, fail_safe_state="run")
        )
        cc.force_test_fire(3, cause=CAUSE_COMMISSIONING_TEST)
        assert gpio_pins[pin_of(3)] == 1, "coil energises to open the NC contact"
        assert cc.is_running(3) is False

        cc.revert_to_fail_safe(3, cause=CAUSE_COMMISSIONING_TEST)
        assert gpio_pins[pin_of(3)] == 0
        assert cc.is_running(3) is True, "aerator must be running again"

    def test_refuses_without_a_validated_config(self, gpio_pins):
        cc = ChannelController(RelayController(), {})
        assert cc.force_test_fire(2, cause=CAUSE_COMMISSIONING_TEST) is False
        assert gpio_pins[pin_of(2)] == 0


class TestAuditTrail:
    def test_test_fire_is_logged_with_its_cause(self, tmp_path):
        db = WQM1Database(str(tmp_path / "wqm1.db"))
        cc = _controller(_cfg(commissioned=False), db=db)
        cc.force_test_fire(1, cause=CAUSE_COMMISSIONING_TEST)

        rows = db.get_relay_transitions()
        assert rows, "a test-fire must leave an audit record"
        assert rows[0]["cause"] == CAUSE_COMMISSIONING_TEST
        assert rows[0]["channel"] == 1

    def test_every_cause_is_recorded_distinctly(self, tmp_path):
        db = WQM1Database(str(tmp_path / "wqm1.db"))
        cc = _controller(_cfg(commissioned=True), db=db)
        cc.request(1, run=True, cause="setpoint")
        cc.revert_to_fail_safe(1, cause="staleness")
        cc.request(1, run=True, cause="manual")
        cc.revert_to_fail_safe(1, cause="ota")

        causes = [r["cause"] for r in db.get_relay_transitions()]
        assert set(causes) == {"setpoint", "staleness", "manual", "ota"}

    def test_cycle_counter_survives_restart(self, tmp_path):
        path = str(tmp_path / "wqm1.db")
        db = WQM1Database(path)
        cc = _controller(_cfg(commissioned=True), db=db)
        cc.request(1, run=True, cause="setpoint")
        cc.request(1, run=False, cause="setpoint")
        first = cc.cycles(1)
        assert first >= 2

        # New process, same database — contact wear is a property of the relay.
        cc2 = ChannelController(RelayController(), {1: _cfg(commissioned=True)}, db=db)
        assert cc2.cycles(1) == first

    def test_audit_failure_never_blocks_actuation(self, gpio_pins):
        """Losing the log is strictly less bad than refusing to move a relay."""

        class BrokenDB:
            def get_relay_cycles(self):
                return {}

            def log_relay_transition(self, **_kwargs):
                raise RuntimeError("disk full")

        cc = _controller(_cfg(commissioned=True), db=BrokenDB())
        assert cc.request(1, run=True, cause="setpoint") is True
        assert gpio_pins[pin_of(1)] == 1


class TestSnapshot:
    def test_snapshot_reports_state_and_wear(self):
        cc = ChannelController(
            RelayController(),
            {1: _cfg(1, commissioned=True), 3: _cfg(3, commissioned=False)},
        )
        cc.request(1, run=True, cause="setpoint")
        snap = cc.snapshot()
        assert snap["state_bitmask"] & 0b0001
        assert snap["commissioned"] == {1: True, 3: False}
        assert snap["cycles"][1] >= 1
