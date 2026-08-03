"""Tests for firmware/drivers/relay.py — relay controller."""

from unittest.mock import MagicMock

import pytest

from utils.config import RELAY_PINS


class TestRelayInit:
    def test_all_relays_off_at_init(self, mock_hardware):
        from control.relay import RelayController

        rc = RelayController()
        # Should call GPIO.setup for each pin with initial=LOW
        assert mock_hardware["gpio"].setup.call_count == 4
        assert rc.get_state_bitmask() == 0


class TestRelayControl:
    def test_set_relay_on(self, mock_hardware):
        from control.relay import RelayController

        rc = RelayController()
        rc.set(1, True)
        mock_hardware["gpio"].output.assert_called_with(17, 1)  # GPIO17, HIGH
        assert rc.get(1) is True

    def test_set_relay_off(self, mock_hardware):
        from control.relay import RelayController

        rc = RelayController()
        rc.set(1, True)
        rc.set(1, False)
        assert rc.get(1) is False

    def test_invalid_channel_raises(self, mock_hardware):
        from control.relay import RelayController

        rc = RelayController()
        with pytest.raises(ValueError):
            rc.set(0, True)
        with pytest.raises(ValueError):
            rc.set(5, True)

    def test_all_off(self, mock_hardware):
        from control.relay import RelayController

        rc = RelayController()
        rc.set(1, True)
        rc.set(3, True)
        rc.all_off()
        assert rc.get_state_bitmask() == 0

    def test_state_bitmask(self, mock_hardware):
        from control.relay import RelayController

        rc = RelayController()
        rc.set(1, True)  # bit 0
        rc.set(3, True)  # bit 2
        assert rc.get_state_bitmask() == 0b0101  # 5

    def test_cleanup_turns_all_off(self, mock_hardware):
        from control.relay import RelayController

        rc = RelayController()
        rc.set(2, True)
        rc.cleanup()
        assert rc.get_state_bitmask() == 0


class TestFailsafeAtExit:
    """The last line of defence before a host reboot: whatever brings the
    interpreter down, the coils end LOW."""

    def test_cleanup_is_registered_at_init(self, monkeypatch, mock_hardware):
        import control.relay as mod

        fake_atexit = MagicMock()
        monkeypatch.setattr(mod, "atexit", fake_atexit)

        rc = mod.RelayController()

        fake_atexit.register.assert_called_once_with(rc.cleanup)

    def test_registered_handler_drives_every_pin_low(self, monkeypatch, mock_hardware):
        import control.relay as mod

        fake_atexit = MagicMock()
        monkeypatch.setattr(mod, "atexit", fake_atexit)
        rc = mod.RelayController()
        rc.set(2, True)
        rc.set(4, True)
        mock_hardware["gpio"].output.reset_mock()

        # Exactly what the interpreter would call on the way out.
        fake_atexit.register.call_args[0][0]()

        low_writes = {call.args for call in mock_hardware["gpio"].output.call_args_list}
        assert low_writes == {(pin, mock_hardware["gpio"].LOW) for pin in RELAY_PINS}
        assert rc.get_state_bitmask() == 0

    def test_cleanup_is_idempotent(self, mock_hardware):
        """`_shutdown` drops the relays too, then atexit runs cleanup — the
        second pass must be harmless."""
        from control.relay import RelayController

        rc = RelayController()
        rc.set(1, True)
        rc.all_off()
        rc.cleanup()
        rc.cleanup()
        assert rc.get_state_bitmask() == 0

    def test_cleanup_survives_gpio_errors(self, mock_hardware):
        """A failing pin must not stop the remaining coils being dropped."""
        from control.relay import RelayController

        rc = RelayController()
        rc.set(1, True)
        mock_hardware["gpio"].output.reset_mock()
        mock_hardware["gpio"].output.side_effect = [RuntimeError("pin busy"), None, None, None]

        rc.cleanup()  # must not raise

        assert mock_hardware["gpio"].output.call_count == len(RELAY_PINS)
        assert rc.get_state_bitmask() == 0
