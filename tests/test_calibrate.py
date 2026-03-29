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
