"""Tests for firmware/drivers/ads1115.py — ADS1115 ADC driver."""

import pytest


class TestADS1115Init:
    """Test ADC initialisation."""

    def test_opens_i2c_bus(self, mock_hardware):
        from sensors.ads1115 import ADS1115

        ADS1115(bus=1, address=0x48)
        mock_hardware["smbus2"].SMBus.assert_called_once_with(1)

    def test_verifies_device_present(self, mock_hardware):
        from sensors.ads1115 import ADS1115

        ADS1115()
        mock_hardware["bus"].read_word_data.assert_called_once_with(0x48, 0x01)

    def test_raises_on_bus_failure(self, mock_hardware):
        mock_hardware["smbus2"].SMBus.side_effect = OSError("Bus error")
        from sensors.ads1115 import ADS1115

        with pytest.raises(RuntimeError, match="ADS1115 init failed"):
            ADS1115()


class TestADS1115Read:
    """Test voltage and raw reads."""

    def test_read_voltage_channel_0(self, mock_hardware):
        # Return 16384 raw = 2.048V at ±4.096V PGA
        mock_hardware["bus"].read_i2c_block_data.side_effect = [
            [0x80, 0x00],  # config: OS bit set (done)
            [0x40, 0x00],  # conversion: 16384 = 2.048V
        ]
        from sensors.ads1115 import ADS1115

        adc = ADS1115()
        voltage = adc.read_voltage(0)
        assert abs(voltage - 2.048) < 0.001

    def test_read_raw_signed_negative(self, mock_hardware):
        mock_hardware["bus"].read_i2c_block_data.side_effect = [
            [0x80, 0x00],  # done
            [0xFF, 0x00],  # raw = 0xFF00 = -256 (signed)
        ]
        from sensors.ads1115 import ADS1115

        adc = ADS1115()
        raw = adc.read_raw(0)
        assert raw == -256

    def test_read_voltage_zero(self, mock_hardware):
        mock_hardware["bus"].read_i2c_block_data.side_effect = [
            [0x80, 0x00],
            [0x00, 0x00],
        ]
        from sensors.ads1115 import ADS1115

        adc = ADS1115()
        assert adc.read_voltage(0) == 0.0

    def test_invalid_channel_raises(self, mock_hardware):
        from sensors.ads1115 import ADS1115

        adc = ADS1115()
        with pytest.raises(ValueError):
            adc.read_voltage(5)
        with pytest.raises(ValueError):
            adc.read_voltage(-1)

    def test_read_all_returns_4_channels(self, mock_hardware):
        # Each channel: done + conversion
        mock_hardware["bus"].read_i2c_block_data.side_effect = [
            [0x80, 0x00],
            [0x40, 0x00],  # ch0
            [0x80, 0x00],
            [0x40, 0x00],  # ch1
            [0x80, 0x00],
            [0x40, 0x00],  # ch2
            [0x80, 0x00],
            [0x40, 0x00],  # ch3
        ]
        from sensors.ads1115 import ADS1115

        adc = ADS1115()
        result = adc.read_all()
        assert len(result) == 4
        assert all(ch in result for ch in range(4))

    def test_config_register_sets_correct_channel(self, mock_hardware):
        mock_hardware["bus"].read_i2c_block_data.side_effect = [
            [0x80, 0x00],
            [0x40, 0x00],
        ]
        from sensors.ads1115 import ADS1115

        adc = ADS1115()
        adc.read_voltage(2)  # AIN2
        # Verify write_i2c_block_data was called with channel 2 MUX bits
        call_args = mock_hardware["bus"].write_i2c_block_data.call_args
        config_bytes = call_args[0][2]
        # MUX for AIN2: (0x04 + 2) << 12 = 0x6000, with OS+PGA+MODE = top byte should have 0x60 bits
        mux_bits = (config_bytes[0] >> 4) & 0x07
        assert mux_bits == 6  # 0x04 + 2 = 6


class TestADS1115Close:
    def test_close_releases_bus(self, mock_hardware):
        from sensors.ads1115 import ADS1115

        adc = ADS1115()
        adc.close()
        mock_hardware["bus"].close.assert_called_once()
