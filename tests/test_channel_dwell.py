"""
Anti-chatter tests: minimum on-dwell, off-dwell, transition interval, deadband.

Uses an injected clock so dwell is exercised deterministically instead of by
sleeping. The load-bearing assertion in this file is the last class: dwell
protects contactor coils, but it must NEVER delay a fail-safe.
"""

from control.channel import ChannelController
from control.relay import RelayController
from utils.config import (
    CAUSE_STALENESS,
    CAUSE_WATCHDOG,
    CONTACT_NC,
    CONTACT_NO,
    RELAY_PINS,
    ChannelConfig,
)


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def _pump(channel=1, **over):
    kwargs = dict(
        channel=channel,
        role="dosing",
        contact=CONTACT_NO,
        fail_safe_state="stop",
        commissioned=True,
        min_on_s=30,
        min_off_s=60,
        min_interval_s=10,
    )
    kwargs.update(over)
    return ChannelConfig(**kwargs)


def _controller(cfg, clock):
    return ChannelController(RelayController(), {cfg.channel: cfg}, clock=clock)


def pin_of(channel):
    return RELAY_PINS[channel - 1]


class TestMinOnDwell:
    def test_cannot_stop_before_min_on_elapsed(self, gpio_pins):
        clk = FakeClock()
        cc = _controller(_pump(), clk)
        clk.advance(100)  # clear min_off/min_interval before the first start
        assert cc.request(1, run=True, cause="setpoint") is True

        clk.advance(29)
        assert cc.request(1, run=False, cause="setpoint") is False
        assert gpio_pins[pin_of(1)] == 1, "pump should still be running"

    def test_can_stop_once_min_on_elapsed(self, gpio_pins):
        clk = FakeClock()
        cc = _controller(_pump(), clk)
        clk.advance(100)
        cc.request(1, run=True, cause="setpoint")
        clk.advance(31)
        assert cc.request(1, run=False, cause="setpoint") is True
        assert gpio_pins[pin_of(1)] == 0


class TestMinOffDwell:
    def test_cannot_restart_before_min_off_elapsed(self, gpio_pins):
        clk = FakeClock()
        cc = _controller(_pump(), clk)
        clk.advance(100)
        cc.request(1, run=True, cause="setpoint")
        clk.advance(40)
        cc.request(1, run=False, cause="setpoint")

        clk.advance(59)
        assert cc.request(1, run=True, cause="setpoint") is False
        assert gpio_pins[pin_of(1)] == 0

    def test_can_restart_once_min_off_elapsed(self, gpio_pins):
        clk = FakeClock()
        cc = _controller(_pump(), clk)
        clk.advance(100)
        cc.request(1, run=True, cause="setpoint")
        clk.advance(40)
        cc.request(1, run=False, cause="setpoint")
        clk.advance(61)
        assert cc.request(1, run=True, cause="setpoint") is True


class TestMinInterval:
    def test_blocks_transition_inside_the_interval(self):
        clk = FakeClock()
        cc = _controller(_pump(min_on_s=0, min_off_s=0, min_interval_s=45), clk)
        clk.advance(100)
        assert cc.request(1, run=True, cause="setpoint") is True
        clk.advance(44)
        assert cc.request(1, run=False, cause="setpoint") is False
        clk.advance(2)
        assert cc.request(1, run=False, cause="setpoint") is True


class TestNoOpRequests:
    def test_requesting_the_current_state_is_not_a_transition(self):
        clk = FakeClock()
        cc = _controller(_pump(), clk)
        clk.advance(100)
        cc.request(1, run=True, cause="setpoint")
        before = cc.cycles(1)
        assert cc.request(1, run=True, cause="setpoint") is False
        assert cc.cycles(1) == before, "a no-op must not count as contact wear"


class TestDeadband:
    def test_zero_deadband_always_passes(self):
        clk = FakeClock()
        cc = _controller(_pump(deadband=0.0), clk)
        assert cc.passes_deadband(1, value=8.01, threshold=8.0, operator=">") is True

    def test_rising_needs_to_clear_threshold_plus_band(self):
        clk = FakeClock()
        cc = _controller(_pump(deadband=0.3), clk)
        assert cc.passes_deadband(1, value=8.2, threshold=8.0, operator=">") is False
        assert cc.passes_deadband(1, value=8.35, threshold=8.0, operator=">") is True

    def test_falling_needs_to_clear_threshold_minus_band(self):
        clk = FakeClock()
        cc = _controller(_pump(deadband=0.3), clk)
        assert cc.passes_deadband(1, value=7.8, threshold=8.0, operator="<") is False
        assert cc.passes_deadband(1, value=7.6, threshold=8.0, operator="<") is True

    def test_probe_hovering_on_the_setpoint_does_not_chatter(self):
        """The whole point: a noisy probe sitting on the threshold stays put."""
        clk = FakeClock()
        cc = _controller(_pump(deadband=0.25), clk)
        for noise in (7.99, 8.01, 7.98, 8.02, 8.0):
            assert cc.passes_deadband(1, value=noise, threshold=8.0, operator=">") is False


class TestFailSafeOverridesDwell:
    """Anti-chatter protects hardware. It must never protect it into a kill."""

    def test_failsafe_ignores_min_on_dwell(self, gpio_pins):
        clk = FakeClock()
        cc = _controller(_pump(), clk)
        clk.advance(100)
        cc.request(1, run=True, cause="setpoint")
        clk.advance(1)  # deep inside min_on_s

        cc.revert_to_fail_safe(1, cause=CAUSE_WATCHDOG)
        assert gpio_pins[pin_of(1)] == 0, "fail-safe was delayed by a dwell timer"

    def test_failsafe_ignores_min_interval(self, gpio_pins):
        clk = FakeClock()
        cc = _controller(_pump(min_interval_s=3600), clk)
        clk.advance(4000)
        cc.request(1, run=True, cause="setpoint")
        clk.advance(1)
        cc.revert_to_fail_safe(1, cause=CAUSE_STALENESS)
        assert gpio_pins[pin_of(1)] == 0

    def test_staleness_reversion_ignores_dwell_on_nc_channel(self, gpio_pins):
        clk = FakeClock()
        cfg = ChannelConfig(
            channel=3,
            role="aeration",
            contact=CONTACT_NC,
            fail_safe_state="run",
            commissioned=True,
            min_on_s=600,
            min_interval_s=600,
            stale_cycles=1,
        )
        cc = ChannelController(RelayController(), {3: cfg}, clock=clk)
        clk.advance(700)
        cc.request(3, run=False, cause="setpoint")  # aerator stopped, coil energised
        assert gpio_pins[pin_of(3)] == 1

        clk.advance(1)
        cc.note_reading(3, valid=False)
        assert gpio_pins[pin_of(3)] == 0, "aerator must be restored despite the dwell window"
        assert cc.is_running(3) is True
