"""Extended relay controller tests — on/off convenience methods."""

import pytest


class TestRelayConvenienceMethods:
    def test_on_sets_relay(self, mock_hardware):
        from control.relay import RelayController

        rc = RelayController()
        rc.on(1)
        assert rc.get(1) is True
        mock_hardware["gpio"].output.assert_called_with(17, 1)

    def test_off_clears_relay(self, mock_hardware):
        from control.relay import RelayController

        rc = RelayController()
        rc.on(2)
        rc.off(2)
        assert rc.get(2) is False

    def test_on_invalid_channel(self, mock_hardware):
        from control.relay import RelayController

        rc = RelayController()
        with pytest.raises(ValueError):
            rc.on(0)
        with pytest.raises(ValueError):
            rc.on(5)

    def test_off_invalid_channel(self, mock_hardware):
        from control.relay import RelayController

        rc = RelayController()
        with pytest.raises(ValueError):
            rc.off(0)

    def test_on_off_sequence(self, mock_hardware):
        from control.relay import RelayController

        rc = RelayController()
        rc.on(1)
        rc.on(3)
        assert rc.get_state_bitmask() == 0b0101
        rc.off(1)
        assert rc.get_state_bitmask() == 0b0100
        rc.off(3)
        assert rc.get_state_bitmask() == 0b0000
