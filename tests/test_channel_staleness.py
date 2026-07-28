"""
Staleness reversion tests.

If the sensor driving a control decision goes quiet, the channel must revert to
its fail-safe rather than keep actuating on the last value it happened to see.
The boundary matters: revert at exactly N cycles, not N-1.
"""

from control.channel import ChannelController
from control.relay import RelayController
from utils.config import CAUSE_STALENESS, CONTACT_NC, CONTACT_NO, RELAY_PINS, ChannelConfig


def _dosing(channel=1, stale_cycles=3, **over):
    kwargs = dict(
        channel=channel,
        role="dosing",
        contact=CONTACT_NO,
        fail_safe_state="stop",
        commissioned=True,
        stale_cycles=stale_cycles,
    )
    kwargs.update(over)
    return ChannelConfig(**kwargs)


def _aeration(channel=3, stale_cycles=3):
    return ChannelConfig(
        channel=channel,
        role="aeration",
        contact=CONTACT_NC,
        fail_safe_state="run",
        commissioned=True,
        stale_cycles=stale_cycles,
    )


def _controller(cfg):
    return ChannelController(RelayController(), {cfg.channel: cfg})


def pin_of(channel):
    return RELAY_PINS[channel - 1]


class TestBoundary:
    def test_does_not_revert_before_n_cycles(self, gpio_pins):
        cc = _controller(_dosing(stale_cycles=3))
        cc.request(1, run=True, cause="setpoint")

        assert cc.note_reading(1, valid=False) is False  # 1
        assert cc.note_reading(1, valid=False) is False  # 2
        assert gpio_pins[pin_of(1)] == 1, "reverted a cycle too early"
        assert cc.is_running(1) is True

    def test_reverts_at_exactly_n_cycles(self, gpio_pins):
        cc = _controller(_dosing(stale_cycles=3))
        cc.request(1, run=True, cause="setpoint")

        cc.note_reading(1, valid=False)
        cc.note_reading(1, valid=False)
        assert cc.note_reading(1, valid=False) is True  # 3rd
        assert gpio_pins[pin_of(1)] == 0
        assert cc.is_running(1) is False
        assert cc.last_cause(1) == CAUSE_STALENESS

    def test_default_is_three_cycles(self, gpio_pins):
        cc = _controller(_dosing())  # no stale_cycles override
        cc.request(1, run=True, cause="setpoint")
        cc.note_reading(1, valid=False)
        cc.note_reading(1, valid=False)
        assert gpio_pins[pin_of(1)] == 1
        cc.note_reading(1, valid=False)
        assert gpio_pins[pin_of(1)] == 0

    def test_single_cycle_config_reverts_immediately(self, gpio_pins):
        cc = _controller(_dosing(stale_cycles=1))
        cc.request(1, run=True, cause="setpoint")
        assert cc.note_reading(1, valid=False) is True
        assert gpio_pins[pin_of(1)] == 0


class TestCounterReset:
    def test_valid_reading_resets_the_counter(self, gpio_pins):
        cc = _controller(_dosing(stale_cycles=3))
        cc.request(1, run=True, cause="setpoint")

        cc.note_reading(1, valid=False)
        cc.note_reading(1, valid=False)
        cc.note_reading(1, valid=True)
        assert cc.stale_count(1) == 0

        # Two more misses must not trip it — the count restarted.
        cc.note_reading(1, valid=False)
        cc.note_reading(1, valid=False)
        assert gpio_pins[pin_of(1)] == 1

    def test_intermittent_probe_still_trips_eventually(self, gpio_pins):
        """Alternating good/bad never accumulates, but a real run of misses does."""
        cc = _controller(_dosing(stale_cycles=3))
        cc.request(1, run=True, cause="setpoint")
        for _ in range(5):
            cc.note_reading(1, valid=False)
            cc.note_reading(1, valid=True)
        assert gpio_pins[pin_of(1)] == 1

        cc.note_reading(1, valid=False)
        cc.note_reading(1, valid=False)
        cc.note_reading(1, valid=False)
        assert gpio_pins[pin_of(1)] == 0


class TestAerationStaleness:
    def test_stale_sensor_leaves_aerator_running(self, gpio_pins):
        """The case that kills fish: sensor dies while the aerator is off."""
        cc = _controller(_aeration(stale_cycles=3))
        cc.request(3, run=False, cause="setpoint")
        assert gpio_pins[pin_of(3)] == 1  # coil energised to hold NC open

        for _ in range(3):
            cc.note_reading(3, valid=False)

        assert gpio_pins[pin_of(3)] == 0, "coil must de-energise"
        assert cc.is_running(3) is True, "NC de-energised = aerator RUNNING"
        assert cc.last_cause(3) == CAUSE_STALENESS


class TestQuiescence:
    def test_continued_misses_do_not_spam_transitions(self, gpio_pins):
        cc = _controller(_dosing(stale_cycles=2))
        cc.request(1, run=True, cause="setpoint")
        cc.note_reading(1, valid=False)
        cc.note_reading(1, valid=False)
        cycles_after_revert = cc.cycles(1)

        for _ in range(10):
            cc.note_reading(1, valid=False)

        assert cc.cycles(1) == cycles_after_revert, "re-reverting must not churn the contact"

    def test_unknown_channel_is_ignored(self):
        cc = _controller(_dosing(channel=1))
        assert cc.note_reading(4, valid=False) is False
