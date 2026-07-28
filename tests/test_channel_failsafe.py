"""
Fail-safe reversion tests.

The property under test throughout: fail-safe is DE-ENERGISED, and on an NC
contact that leaves the load RUNNING. These assert the physical pin level via
the recording GPIO fixture, not just that a method was called.
"""

from control.channel import ChannelController
from control.relay import RelayController
from utils.config import (
    CAUSE_OTA,
    CAUSE_WATCHDOG,
    CONTACT_NC,
    CONTACT_NO,
    RELAY_PINS,
    ChannelConfig,
)


def _aeration_nc(channel=3, **over):
    """Life-critical aeration: NC contact, fail-safe = keep running."""
    kwargs = dict(
        channel=channel,
        role="aeration",
        contact=CONTACT_NC,
        fail_safe_state="run",
        commissioned=True,
    )
    kwargs.update(over)
    return ChannelConfig(**kwargs)


def _dosing_no(channel=1, **over):
    """Dosing pump: NO contact, fail-safe = stop."""
    kwargs = dict(
        channel=channel,
        role="dosing",
        contact=CONTACT_NO,
        fail_safe_state="stop",
        commissioned=True,
    )
    kwargs.update(over)
    return ChannelConfig(**kwargs)


def _controller(configs, clock=None):
    relay = RelayController()
    return ChannelController(relay, {c.channel: c for c in configs}, clock=clock)


def pin_of(channel):
    return RELAY_PINS[channel - 1]


class TestCoilTranslation:
    def test_nc_running_means_de_energised(self, gpio_pins):
        cc = _controller([_aeration_nc()])
        cc.request(3, run=True, cause="setpoint")
        assert cc.is_running(3) is True
        assert gpio_pins[pin_of(3)] == 0, "NC + running must leave the coil de-energised"

    def test_nc_stopping_energises(self, gpio_pins):
        cc = _controller([_aeration_nc()])
        cc.request(3, run=False, cause="setpoint")
        assert gpio_pins[pin_of(3)] == 1, "stopping an NC load requires energising the coil"

    def test_no_running_energises(self, gpio_pins):
        cc = _controller([_dosing_no()])
        cc.request(1, run=True, cause="setpoint")
        assert gpio_pins[pin_of(1)] == 1

    def test_no_stopping_de_energises(self, gpio_pins):
        cc = _controller([_dosing_no()])
        cc.request(1, run=True, cause="setpoint")
        cc.request(1, run=False, cause="setpoint")
        assert gpio_pins[pin_of(1)] == 0


class TestFailSafeReversion:
    def test_aeration_keeps_running_after_reversion(self, gpio_pins):
        cc = _controller([_aeration_nc()])
        cc.request(3, run=False, cause="setpoint")  # aerator deliberately stopped
        assert gpio_pins[pin_of(3)] == 1

        cc.revert_to_fail_safe(3, cause=CAUSE_WATCHDOG)

        assert gpio_pins[pin_of(3)] == 0, "fail-safe must de-energise"
        assert cc.is_running(3) is True, "NC de-energised = aerator RUNNING"

    def test_dosing_stops_after_reversion(self, gpio_pins):
        cc = _controller([_dosing_no()])
        cc.request(1, run=True, cause="setpoint")
        cc.revert_to_fail_safe(1, cause=CAUSE_WATCHDOG)
        assert gpio_pins[pin_of(1)] == 0
        assert cc.is_running(1) is False

    def test_reversion_drives_the_pin_even_when_bookkeeping_already_agrees(self, gpio_pins):
        """A fail-safe must not trust in-memory state — it re-drives the pin."""
        cc = _controller([_aeration_nc()])
        gpio_pins[pin_of(3)] = 1  # simulate a pin desynced from our view
        cc.revert_to_fail_safe(3, cause=CAUSE_WATCHDOG)
        assert gpio_pins[pin_of(3)] == 0

    def test_all_fail_safe_covers_every_channel(self, gpio_pins):
        cc = _controller([_dosing_no(1), _dosing_no(2), _aeration_nc(3), _dosing_no(4)])
        for ch in (1, 2, 4):
            cc.request(ch, run=True, cause="setpoint")
        cc.all_fail_safe(cause=CAUSE_WATCHDOG)
        for ch in (1, 2, 3, 4):
            assert gpio_pins[pin_of(ch)] == 0, f"CH{ch} coil still energised"


class TestOtaAndShutdown:
    def test_ota_reverts_all_channels(self, gpio_pins):
        cc = _controller([_dosing_no(1), _aeration_nc(3)])
        cc.request(1, run=True, cause="setpoint")
        cc.prepare_for_ota()
        assert gpio_pins[pin_of(1)] == 0
        assert gpio_pins[pin_of(3)] == 0
        assert cc.last_cause(1) == CAUSE_OTA

    def test_rollback_reverts_all_channels(self, gpio_pins):
        cc = _controller([_dosing_no(1)])
        cc.request(1, run=True, cause="setpoint")
        cc.prepare_for_ota(reason="rollback")
        assert gpio_pins[pin_of(1)] == 0

    def test_process_exit_de_energises_everything(self, gpio_pins):
        """RelayController.cleanup is the atexit path — a clean crash/stop."""
        relay = RelayController()
        cc = ChannelController(relay, {1: _dosing_no(1)})
        cc.request(1, run=True, cause="setpoint")
        assert gpio_pins[pin_of(1)] == 1
        relay.cleanup()
        assert gpio_pins[pin_of(1)] == 0

    def test_boot_leaves_every_coil_de_energised(self, gpio_pins):
        RelayController()
        for ch in range(1, 5):
            assert gpio_pins[pin_of(ch)] == 0


class TestUncommissioned:
    def test_uncommissioned_channel_never_actuates(self, gpio_pins):
        cc = _controller([_dosing_no(1, commissioned=False)])
        assert cc.request(1, run=True, cause="setpoint") is False
        assert gpio_pins[pin_of(1)] == 0

    def test_unknown_channel_still_de_energises_on_failsafe(self, gpio_pins):
        cc = _controller([])
        cc.revert_to_fail_safe(2, cause=CAUSE_WATCHDOG)
        assert gpio_pins[pin_of(2)] == 0
