"""Tests for firmware/src/calibration/calibrate.py — calibration manager."""


class TestCalibrationManager:
    def test_default_calibration(self, tmp_path, mock_hardware):
        from calibration.calibrate import CalibrationManager

        cm = CalibrationManager(path=str(tmp_path / "cal.yaml"))
        assert cm.data.ph_v_at_7 == 1.50
        assert cm.data.tds_k == 500.0

    def test_ph_calibration(self, tmp_path, mock_hardware):
        from calibration.calibrate import CalibrationManager

        cm = CalibrationManager(path=str(tmp_path / "cal.yaml"))
        slope = cm.calibrate_ph(v_ph4=1.00, v_ph7=1.60)
        assert abs(slope - 5.0) < 0.01
        assert cm.data.ph_v_at_4 == 1.00
        assert cm.data.ph_v_at_7 == 1.60
        # Verify persistence
        assert (tmp_path / "cal.yaml").exists()

    def test_tds_calibration(self, tmp_path, mock_hardware):
        from calibration.calibrate import CalibrationManager

        cm = CalibrationManager(path=str(tmp_path / "cal.yaml"))
        k = cm.calibrate_tds(known_ppm=1000.0, measured_v=2.0)
        assert k == 500.0
        assert cm.data.tds_k == 500.0

    def test_orp_calibration(self, tmp_path, mock_hardware):
        from calibration.calibrate import CalibrationManager

        cm = CalibrationManager(path=str(tmp_path / "cal.yaml"))
        offset = cm.calibrate_orp(known_mv=225.0, measured_mv=200.0)
        assert offset == 25.0

    def test_platform_offsets(self, tmp_path, mock_hardware):
        from calibration.calibrate import CalibrationManager

        cm = CalibrationManager(path=str(tmp_path / "cal.yaml"))
        cm.apply_platform_offsets({"ph": 0.1, "tds": -5.0})
        assert cm.get_platform_offset("ph") == 0.1
        assert cm.get_platform_offset("tds") == -5.0
        assert cm.get_platform_offset("orp") == 0.0  # not set

    def test_persistence_roundtrip(self, tmp_path, mock_hardware):
        from calibration.calibrate import CalibrationManager

        path = str(tmp_path / "cal.yaml")
        cm1 = CalibrationManager(path=path)
        cm1.calibrate_ph(v_ph4=0.95, v_ph7=1.55)

        # Load from same file
        cm2 = CalibrationManager(path=path)
        assert cm2.data.ph_v_at_4 == 0.95
        assert cm2.data.ph_v_at_7 == 1.55

    def test_ph_calibration_rejects_close_voltages(self, tmp_path, mock_hardware):
        """Guard against /0: slope must not change when ΔV < 0.001."""
        from calibration.calibrate import CalibrationManager

        cm = CalibrationManager(path=str(tmp_path / "cal.yaml"))
        original = cm.data.ph_slope
        returned = cm.calibrate_ph(v_ph4=1.500, v_ph7=1.5005)
        assert returned == original
        assert cm.data.ph_slope == original

    def test_tds_calibration_rejects_non_positive_voltage(self, tmp_path, mock_hardware):
        from calibration.calibrate import CalibrationManager

        cm = CalibrationManager(path=str(tmp_path / "cal.yaml"))
        original = cm.data.tds_k
        assert cm.calibrate_tds(known_ppm=500, measured_v=0.0) == original
        assert cm.calibrate_tds(known_ppm=500, measured_v=-1.0) == original
        assert cm.data.tds_k == original

    def test_turbidity_calibration(self, tmp_path, mock_hardware):
        from calibration.calibrate import CalibrationManager

        cm = CalibrationManager(path=str(tmp_path / "cal.yaml"))
        cm.calibrate_turbidity(3.95)
        assert cm.data.turbidity_v_clear == 3.95

    def test_load_with_malformed_yaml_falls_back(self, tmp_path, mock_hardware):
        """Corrupt calibration file should not crash startup; defaults remain."""
        from calibration.calibrate import CalibrationManager

        path = tmp_path / "cal.yaml"
        path.write_text("::: not yaml :::")
        cm = CalibrationManager(path=str(path))
        # Defaults preserved
        assert cm.data.ph_v_at_7 == 1.50
        assert cm.data.tds_k == 500.0

    def test_load_ignores_unknown_keys(self, tmp_path, mock_hardware):
        from calibration.calibrate import CalibrationManager

        bad = tmp_path / "cal.yaml"
        bad.write_text("ph_v_at_7: 1.7\nunknown_key: foo\n")
        cm = CalibrationManager(path=str(bad))
        assert cm.data.ph_v_at_7 == 1.7
        assert not hasattr(cm.data, "unknown_key")
