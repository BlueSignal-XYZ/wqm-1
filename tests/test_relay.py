"""Tests for firmware/drivers/relay.py — relay controller."""

import pytest


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
