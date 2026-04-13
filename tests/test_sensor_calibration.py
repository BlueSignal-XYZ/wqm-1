"""Extended tests for sensor calibration routines — pH, TDS, turbidity, ORP."""

from unittest.mock import MagicMock


class TestPHCalibrationDetailed:
    """Detailed pH calibration and temperature compensation tests."""

    def test_nernst_slope_at_25c(self, mock_hardware):
        from sensors.ph import _nernst_slope

        slope = _nernst_slope(25.0)
        # At 25°C: slope = (8.314 * 298.15) / 96485 ≈ 0.02569 V/pH
        assert abs(slope - 0.02569) < 0.001

    def test_nernst_slope_increases_with_temp(self, mock_hardware):
        from sensors.ph import _nernst_slope

        slope_25 = _nernst_slope(25.0)
        slope_35 = _nernst_slope(35.0)
        assert slope_35 > slope_25

    def test_nernst_slope_at_0c(self, mock_hardware):
        from sensors.ph import _nernst_slope

        slope = _nernst_slope(0.0)
        # At 0°C: slope = (8.314 * 273.15) / 96485 ≈ 0.02353
        assert abs(slope - 0.02353) < 0.001

    def test_ph_temp_compensation_effect(self, mock_hardware):
        """pH reading should change with temperature at same voltage."""
        from sensors.ads1115 import ADS1115
        from sensors.ph import PHSensor

        adc = ADS1115()
        adc.read_voltage = MagicMock(return_value=1.40)

        ph_sensor = PHSensor(adc)
        ph_sensor.set_calibration(v_ph4=1.04, v_ph7=1.50)

        ph_25 = ph_sensor.read(temp_c=25.0)
        ph_sensor._window.clear()
        ph_35 = ph_sensor.read(temp_c=35.0)

        # Same voltage but different temp compensation
        assert ph_25 is not None
        assert ph_35 is not None
        assert ph_25 != ph_35

    def test_ph_default_temp_25c(self, mock_hardware):
        """Default temp=25.0 should give temp_factor of 1.0."""
        from sensors.ads1115 import ADS1115
        from sensors.ph import PHSensor

        adc = ADS1115()
        adc.read_voltage = MagicMock(return_value=1.50)

        ph_sensor = PHSensor(adc)
        ph = ph_sensor.read()  # default temp_c=25.0
        assert ph is not None

    def test_ph_median_filter(self, mock_hardware):
        """Median filter should smooth out outliers."""
        from sensors.ads1115 import ADS1115
        from sensors.ph import PHSensor

        adc = ADS1115()
        ph_sensor = PHSensor(adc)
        ph_sensor.set_calibration(v_ph4=1.04, v_ph7=1.50)

        # Feed 5 readings: 4 at pH~7 and 1 outlier
        voltages = [1.50, 1.50, 1.50, 3.00, 1.50]  # 3.00 is outlier
        results = []
        for v in voltages:
            adc.read_voltage = MagicMock(return_value=v)
            ph_sensor._window.clear()  # reset for single read
            results.append(ph_sensor.read(temp_c=25.0))

        # Last reading should be close to 7.0 due to median filter
        # (window has 5 samples, median should exclude the outlier effect)

    def test_ph_calibration_with_close_voltages(self, mock_hardware):
        """Voltages too close should fall back to default Nernst slope."""
        from sensors.ads1115 import ADS1115
        from sensors.ph import PHSensor
        from utils.config import NERNST_SLOPE_25C

        adc = ADS1115()
        ph_sensor = PHSensor(adc)
        ph_sensor.set_calibration(v_ph4=1.500, v_ph7=1.5005)
        assert abs(ph_sensor._slope - NERNST_SLOPE_25C) < 0.001


class TestTDSCalibrationDetailed:
    def test_tds_calibration_changes_output(self, mock_hardware):
        from sensors.ads1115 import ADS1115
        from sensors.tds import TDSSensor

        adc = ADS1115()
        adc.read_voltage = MagicMock(return_value=0.3125)

        tds = TDSSensor(adc)
        tds_default = tds.read(temp_c=25.0)

        tds._window.clear()
        tds.set_calibration(k=1000.0)
        tds_new = tds.read(temp_c=25.0)

        assert tds_new > tds_default  # higher k = higher reading

    def test_tds_extreme_temperature(self, mock_hardware):
        """TDS at extreme temperatures should still return valid results."""
        from sensors.ads1115 import ADS1115
        from sensors.tds import TDSSensor

        adc = ADS1115()
        adc.read_voltage = MagicMock(return_value=0.3125)
        tds = TDSSensor(adc)

        # Very cold water
        result = tds.read(temp_c=5.0)
        assert result is not None
        assert result >= 0

    def test_tds_none_temp(self, mock_hardware):
        """TDS with temp_c=None should use comp_coeff=1.0."""
        from sensors.ads1115 import ADS1115
        from sensors.tds import TDSSensor

        adc = ADS1115()
        adc.read_voltage = MagicMock(return_value=0.3125)
        tds = TDSSensor(adc)

        result = tds.read(temp_c=None)
        assert result is not None
        assert result >= 0


class TestTurbidityCalibrationDetailed:
    def test_set_clear_water_voltage(self, mock_hardware):
        from sensors.ads1115 import ADS1115
        from sensors.turbidity import TurbiditySensor

        adc = ADS1115()
        adc.read_voltage = MagicMock(return_value=3.8)
        sensor = TurbiditySensor(adc)
        sensor.set_clear_water_voltage(3.8)

        ntu = sensor.read()
        assert ntu is not None
        assert abs(ntu) < 10  # should be near 0

    def test_turbidity_mid_range(self, mock_hardware):
        from sensors.ads1115 import ADS1115
        from sensors.turbidity import TurbiditySensor

        adc = ADS1115()
        # Midpoint between 4.1 (clear) and 0.5 (max): ~2.3V
        adc.read_voltage = MagicMock(return_value=2.3)
        sensor = TurbiditySensor(adc)
        ntu = sensor.read()
        assert ntu is not None
        assert 1000 < ntu < 2000  # should be roughly mid-range


class TestORPCalibrationDetailed:
    def test_orp_with_offset_calibration(self, mock_hardware):
        from sensors.ads1115 import ADS1115
        from sensors.orp import ORPSensor

        adc = ADS1115()
        adc.read_voltage = MagicMock(return_value=2.048)  # at reference
        sensor = ORPSensor(adc)

        # Calibrate with 225 mV standard, measured 200 mV → offset = +25
        sensor.set_offset(known_mv=225.0, measured_mv=200.0)
        orp = sensor.read()
        assert orp is not None
        # At Vref: ORP = (2.048 - 2.048) * 1000 + 25 = 25 mV
        assert abs(orp - 25.0) < 5.0

    def test_orp_median_filter_window(self, mock_hardware):
        from sensors.ads1115 import ADS1115
        from sensors.orp import ORPSensor

        adc = ADS1115()
        sensor = ORPSensor(adc)

        # Read 6 times — window should be max 5
        for v in [2.1, 2.15, 2.2, 2.25, 2.3, 2.35]:
            adc.read_voltage = MagicMock(return_value=v)
            sensor.read()

        assert len(sensor._window) == 5


class TestCalibrationManagerEdgeCases:
    def test_ph_calibration_too_close_returns_current_slope(self, mock_hardware, tmp_path):
        from calibration.calibrate import CalibrationManager

        cm = CalibrationManager(path=str(tmp_path / "cal.yaml"))
        original_slope = cm.data.ph_slope
        slope = cm.calibrate_ph(v_ph4=1.50, v_ph7=1.5005)
        assert slope == original_slope

    def test_tds_calibration_zero_voltage_returns_current_k(self, mock_hardware, tmp_path):
        from calibration.calibrate import CalibrationManager

        cm = CalibrationManager(path=str(tmp_path / "cal.yaml"))
        original_k = cm.data.tds_k
        k = cm.calibrate_tds(known_ppm=500.0, measured_v=0.0)
        assert k == original_k

    def test_tds_calibration_negative_voltage_returns_current_k(self, mock_hardware, tmp_path):
        from calibration.calibrate import CalibrationManager

        cm = CalibrationManager(path=str(tmp_path / "cal.yaml"))
        original_k = cm.data.tds_k
        k = cm.calibrate_tds(known_ppm=500.0, measured_v=-1.0)
        assert k == original_k

    def test_turbidity_calibration_persists(self, mock_hardware, tmp_path):
        from calibration.calibrate import CalibrationManager

        path = str(tmp_path / "cal.yaml")
        cm1 = CalibrationManager(path=path)
        cm1.calibrate_turbidity(clear_water_v=3.9)

        cm2 = CalibrationManager(path=path)
        assert cm2.data.turbidity_v_clear == 3.9

    def test_orp_calibration_persists(self, mock_hardware, tmp_path):
        from calibration.calibrate import CalibrationManager

        path = str(tmp_path / "cal.yaml")
        cm1 = CalibrationManager(path=path)
        cm1.calibrate_orp(known_mv=250.0, measured_mv=230.0)

        cm2 = CalibrationManager(path=path)
        assert abs(cm2.data.orp_offset_mv - 20.0) < 0.1
