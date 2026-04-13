"""Tests for firmware/sensors/ — pH, TDS, turbidity, ORP."""

from unittest.mock import MagicMock

import pytest


@pytest.fixture
def mock_adc(mock_hardware):
    """Create a mock ADS1115 instance."""
    from sensors.ads1115 import ADS1115

    adc = ADS1115()
    return adc


class TestPHSensor:
    """Test pH voltage-to-pH conversion and calibration."""

    def test_ph7_at_neutral_voltage(self, mock_hardware, mock_adc):
        """pH 7.0 at the calibrated neutral voltage."""
        # Set V_pH7 default (1.50V in settings)
        mock_hardware["bus"].read_i2c_block_data.side_effect = [
            [0x80, 0x00],
            [0x2F, 0x40],  # ~1.50V (raw ≈ 12032)
        ] * 5  # 5 reads for median
        from sensors.ph import PHSensor

        ph_sensor = PHSensor(mock_adc)
        # Override calibration to known values
        ph_sensor._v_ph7 = 1.50
        ph_sensor._v_ph4 = 1.04
        ph_sensor._recalc_slope()

        mock_hardware["bus"].read_i2c_block_data.side_effect = [
            [0x80, 0x00],
            [0x2F, 0x40],
        ]
        # Read voltage will be ~1.498V from the mock
        # We need to control the voltage more precisely
        mock_adc.read_voltage = MagicMock(return_value=1.50)
        ph = ph_sensor.read(temp_c=25.0)
        assert abs(ph - 7.0) < 0.1

    def test_calibration_changes_slope(self, mock_hardware, mock_adc):
        from sensors.ph import PHSensor

        ph_sensor = PHSensor(mock_adc)
        ph_sensor.set_calibration(v_ph4=1.00, v_ph7=1.60)
        # slope should be (7.0 - 4.0) / (1.60 - 1.00) = 5.0
        assert abs(ph_sensor._slope - 5.0) < 0.01

    def test_ph_clamped_to_valid_range(self, mock_hardware, mock_adc):
        from sensors.ph import PHSensor

        mock_adc.read_voltage = MagicMock(return_value=5.0)  # extreme
        ph_sensor = PHSensor(mock_adc)
        ph = ph_sensor.read()
        assert 0.0 <= ph <= 14.0

    def test_returns_none_on_adc_failure(self, mock_hardware, mock_adc):
        from sensors.ph import PHSensor

        mock_adc.read_voltage = MagicMock(side_effect=RuntimeError("bus error"))
        ph_sensor = PHSensor(mock_adc)
        assert ph_sensor.read() is None


class TestTDSSensor:
    """Test TDS conversion with temperature compensation."""

    def test_tds_at_known_voltage(self, mock_hardware, mock_adc):
        from sensors.tds import TDSSensor

        # 0.3125V at ADC → actual = 0.3125 / 0.3125 = 1.0V
        # TDS = 1.0 * 500 = 500 ppm (default k=500)
        mock_adc.read_voltage = MagicMock(return_value=0.3125)
        tds_sensor = TDSSensor(mock_adc)
        tds = tds_sensor.read(temp_c=25.0)
        assert abs(tds - 500.0) < 5.0

    def test_temperature_compensation(self, mock_hardware, mock_adc):
        from sensors.tds import TDSSensor

        mock_adc.read_voltage = MagicMock(return_value=0.3125)
        tds_sensor = TDSSensor(mock_adc)

        tds_25 = tds_sensor.read(temp_c=25.0)
        tds_sensor._window.clear()
        tds_35 = tds_sensor.read(temp_c=35.0)

        # At 35°C, compensation reduces TDS (same voltage, warmer water)
        assert tds_35 < tds_25

    def test_tds_non_negative(self, mock_hardware, mock_adc):
        from sensors.tds import TDSSensor

        mock_adc.read_voltage = MagicMock(return_value=0.0)
        tds_sensor = TDSSensor(mock_adc)
        tds = tds_sensor.read()
        assert tds >= 0.0

    def test_returns_none_on_failure(self, mock_hardware, mock_adc):
        from sensors.tds import TDSSensor

        mock_adc.read_voltage = MagicMock(side_effect=RuntimeError("fail"))
        tds_sensor = TDSSensor(mock_adc)
        assert tds_sensor.read() is None


class TestTurbiditySensor:
    """Test turbidity voltage-to-NTU conversion."""

    def test_clear_water(self, mock_hardware, mock_adc):
        from sensors.turbidity import TurbiditySensor

        mock_adc.read_voltage = MagicMock(return_value=4.1)  # clear water
        sensor = TurbiditySensor(mock_adc)
        ntu = sensor.read()
        assert abs(ntu) < 10.0  # near 0 NTU

    def test_max_turbidity(self, mock_hardware, mock_adc):
        from sensors.turbidity import TurbiditySensor

        mock_adc.read_voltage = MagicMock(return_value=0.5)  # max turbidity
        sensor = TurbiditySensor(mock_adc)
        ntu = sensor.read()
        assert abs(ntu - 3000.0) < 10.0

    def test_clamped_range(self, mock_hardware, mock_adc):
        from sensors.turbidity import TurbiditySensor

        mock_adc.read_voltage = MagicMock(return_value=5.0)  # above clear
        sensor = TurbiditySensor(mock_adc)
        ntu = sensor.read()
        assert ntu == 0.0  # clamped

    def test_returns_none_on_failure(self, mock_hardware, mock_adc):
        from sensors.turbidity import TurbiditySensor

        mock_adc.read_voltage = MagicMock(side_effect=RuntimeError("fail"))
        sensor = TurbiditySensor(mock_adc)
        assert sensor.read() is None


class TestORPSensor:
    """Test ORP millivolt reading."""

    def test_orp_at_reference_voltage(self, mock_hardware, mock_adc):
        from sensors.orp import ORPSensor

        # At Vref (2.048V), ORP should be ~0 mV + offset
        mock_adc.read_voltage = MagicMock(return_value=2.048)
        sensor = ORPSensor(mock_adc)
        orp = sensor.read()
        assert abs(orp) < 5.0  # near 0 mV

    def test_orp_positive(self, mock_hardware, mock_adc):
        from sensors.orp import ORPSensor

        mock_adc.read_voltage = MagicMock(return_value=2.548)  # +500mV
        sensor = ORPSensor(mock_adc)
        orp = sensor.read()
        assert abs(orp - 500.0) < 5.0

    def test_orp_negative(self, mock_hardware, mock_adc):
        from sensors.orp import ORPSensor

        mock_adc.read_voltage = MagicMock(return_value=1.548)  # -500mV
        sensor = ORPSensor(mock_adc)
        orp = sensor.read()
        assert abs(orp - (-500.0)) < 5.0

    def test_offset_calibration(self, mock_hardware, mock_adc):
        from sensors.orp import ORPSensor

        mock_adc.read_voltage = MagicMock(return_value=2.048)
        sensor = ORPSensor(mock_adc)
        sensor.set_offset(known_mv=225.0, measured_mv=200.0)
        assert sensor._offset_mv == 25.0

    def test_returns_none_on_failure(self, mock_hardware, mock_adc):
        from sensors.orp import ORPSensor

        mock_adc.read_voltage = MagicMock(side_effect=RuntimeError("fail"))
        sensor = ORPSensor(mock_adc)
        assert sensor.read() is None


class TestPHCalibrationEdgeCases:
    def test_set_calibration_with_close_voltages_falls_back_to_nernst(
        self, mock_hardware, mock_adc
    ):
        from sensors.ph import PHSensor

        ph = PHSensor(mock_adc)
        ph.set_calibration(v_ph4=1.5000, v_ph7=1.5005)
        # Slope falls back to default Nernst slope at 25°C (~0.05916)
        assert abs(ph._slope - 0.05916) < 1e-6

    def test_temp_compensation_changes_slope(self, mock_hardware, mock_adc):
        from sensors.ph import PHSensor

        mock_adc.read_voltage = MagicMock(return_value=1.30)
        ph = PHSensor(mock_adc)
        ph_25 = ph.read(temp_c=25.0)
        # Drain the median window so next reads are independent
        ph._window.clear()
        ph_50 = ph.read(temp_c=50.0)
        # Different temperature → different (compensated) pH
        assert ph_25 != ph_50

    def test_temp_none_uses_no_compensation(self, mock_hardware, mock_adc):
        from sensors.ph import PHSensor

        mock_adc.read_voltage = MagicMock(return_value=1.50)
        ph = PHSensor(mock_adc)
        # Should not raise when temp_c is None
        result = ph.read(temp_c=None)
        assert result is not None


class TestTDSEdgeCases:
    def test_temp_none_skips_compensation(self, mock_hardware, mock_adc):
        from sensors.tds import TDSSensor

        mock_adc.read_voltage = MagicMock(return_value=0.3125)
        tds = TDSSensor(mock_adc)
        result = tds.read(temp_c=None)
        # Should equal uncompensated reading
        assert result is not None
        assert abs(result - 500.0) < 5.0

    def test_set_calibration_clears_window(self, mock_hardware, mock_adc):
        from sensors.tds import TDSSensor

        mock_adc.read_voltage = MagicMock(return_value=0.3125)
        tds = TDSSensor(mock_adc)
        tds.read()
        assert len(tds._window) > 0
        tds.set_calibration(k=600.0)
        assert tds._window == []
        assert tds._k == 600.0

    def test_extreme_negative_compensation_falls_back(self, mock_hardware, mock_adc):
        """Temperature far below 25°C such that compensation goes <= 0 falls back to 1.0."""
        from sensors.tds import TDSSensor

        mock_adc.read_voltage = MagicMock(return_value=0.5)
        tds = TDSSensor(mock_adc)
        # TDS_TEMP_COEFF = 0.02, compensation = 1 + 0.02*(temp-25)
        # At temp = -25: 1 + 0.02 * (-50) = 0.0 → falls back to 1.0
        result = tds.read(temp_c=-25.0)
        assert result is not None and result > 0


class TestTurbidityEdgeCases:
    def test_set_clear_water_voltage_clears_window(self, mock_hardware, mock_adc):
        from sensors.turbidity import TurbiditySensor

        mock_adc.read_voltage = MagicMock(return_value=4.1)
        sensor = TurbiditySensor(mock_adc)
        sensor.read()
        assert len(sensor._window) > 0
        sensor.set_clear_water_voltage(4.0)
        assert sensor._window == []
        assert sensor._v_clear == 4.0

    def test_invalid_voltage_range_returns_none(self, mock_hardware, mock_adc):
        from sensors.turbidity import TurbiditySensor

        mock_adc.read_voltage = MagicMock(return_value=2.0)
        sensor = TurbiditySensor(mock_adc)
        # If clear-water voltage gets corrupted to <= TURB_V_MAX (0.5), range invalid
        sensor._v_clear = 0.3
        assert sensor.read() is None

    def test_below_max_clamped_to_max(self, mock_hardware, mock_adc):
        from sensors.turbidity import TurbiditySensor

        mock_adc.read_voltage = MagicMock(return_value=0.0)
        sensor = TurbiditySensor(mock_adc)
        ntu = sensor.read()
        assert ntu == 3000.0  # clamped to TURB_NTU_MAX


class TestORPCalibrationEdgeCases:
    def test_offset_persists_across_reads(self, mock_hardware, mock_adc):
        from sensors.orp import ORPSensor

        mock_adc.read_voltage = MagicMock(return_value=2.048)
        sensor = ORPSensor(mock_adc)
        sensor.set_offset(known_mv=100.0, measured_mv=50.0)
        # offset = 50.0 mV
        result = sensor.read()
        assert abs(result - 50.0) < 1.0
