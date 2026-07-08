"""Tests for calibration age tracking in src/calibration/calibrate.py."""

from datetime import UTC, datetime, timedelta

import yaml

from calibration.calibrate import CalibrationManager


def make(tmp_path) -> CalibrationManager:
    return CalibrationManager(path=str(tmp_path / "cal.yaml"))


class TestCalibrationAge:
    def test_never_calibrated_age_is_none(self, tmp_path, mock_hardware):
        cm = make(tmp_path)
        for sensor in ("ph", "tds", "turbidity", "orp"):
            assert cm.calibration_age_days(sensor) is None

    def test_set_calibrated_records_and_persists(self, tmp_path, mock_hardware):
        path = str(tmp_path / "cal.yaml")
        cm = CalibrationManager(path=path)
        cm.set_calibrated("ph")
        age = cm.calibration_age_days("ph")
        assert age is not None and age < 0.01
        # Round-trips through the YAML file
        cm2 = CalibrationManager(path=path)
        assert cm2.calibration_age_days("ph") is not None

    def test_age_math(self, tmp_path, mock_hardware):
        cm = make(tmp_path)
        calibrated = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
        cm.set_calibrated("tds", now=calibrated)
        age = cm.calibration_age_days("tds", now=calibrated + timedelta(days=100))
        assert age is not None
        assert abs(age - 100.0) < 1e-6

    def test_future_timestamp_clamps_to_zero(self, tmp_path, mock_hardware):
        cm = make(tmp_path)
        cm.set_calibrated("ph", now=datetime.now(UTC) + timedelta(days=3))
        assert cm.calibration_age_days("ph") == 0.0

    def test_calibrate_methods_stamp_timestamps(self, tmp_path, mock_hardware):
        cm = make(tmp_path)
        cm.calibrate_ph(v_ph4=1.00, v_ph7=1.60)
        cm.calibrate_tds(known_ppm=1000.0, measured_v=2.0)
        cm.calibrate_turbidity(3.95)
        cm.calibrate_orp(known_mv=225.0, measured_mv=200.0)
        for sensor in ("ph", "tds", "turbidity", "orp"):
            age = cm.calibration_age_days(sensor)
            assert age is not None and age < 0.01

    def test_rejected_calibration_does_not_stamp(self, tmp_path, mock_hardware):
        cm = make(tmp_path)
        cm.calibrate_ph(v_ph4=1.500, v_ph7=1.5005)  # ΔV too small — rejected
        assert cm.calibration_age_days("ph") is None
        cm.calibrate_tds(known_ppm=500, measured_v=0.0)  # rejected
        assert cm.calibration_age_days("tds") is None

    def test_platform_offsets_stamp_named_sensors(self, tmp_path, mock_hardware):
        cm = make(tmp_path)
        cm.apply_platform_offsets({"ph": 0.1})
        assert cm.calibration_age_days("ph") is not None
        assert cm.calibration_age_days("tds") is None


class TestOverdueSensors:
    def test_factory_defaults_are_not_overdue(self, tmp_path, mock_hardware):
        """Never-calibrated sensors on pure factory defaults are NOT overdue —
        commissioning (step 6) owns first-calibration nagging."""
        cm = make(tmp_path)
        assert cm.overdue_sensors(90) == []

    def test_fresh_calibration_not_overdue(self, tmp_path, mock_hardware):
        cm = make(tmp_path)
        cm.set_calibrated("ph")
        assert cm.overdue_sensors(90) == []

    def test_old_calibration_is_overdue(self, tmp_path, mock_hardware):
        cm = make(tmp_path)
        old = datetime.now(UTC) - timedelta(days=120)
        cm.set_calibrated("tds", now=old)
        assert cm.overdue_sensors(90) == ["tds"]
        # ... but a bigger allowance clears it
        assert cm.overdue_sensors(365) == []

    def test_boundary_age_is_not_overdue(self, tmp_path, mock_hardware):
        cm = make(tmp_path)
        ref = datetime.now(UTC)
        cm.set_calibrated("ph", now=ref - timedelta(days=90))
        # exactly max_age is not "older than" max_age
        assert cm.overdue_sensors(90, now=ref) == []

    def test_legacy_calibration_without_timestamp_is_overdue(self, tmp_path, mock_hardware):
        """Coefficients differing from factory defaults but with no
        calibrated_at (pre-v2.1 file) — age unknown-but-old, so overdue."""
        path = tmp_path / "cal.yaml"
        path.write_text(yaml.safe_dump({"tds_k": 432.1}))
        cm = CalibrationManager(path=str(path))
        assert cm.overdue_sensors(90) == ["tds"]


class TestBackwardCompatibility:
    def test_old_file_without_calibrated_at_loads_fine(self, tmp_path, mock_hardware):
        path = tmp_path / "cal.yaml"
        path.write_text(yaml.safe_dump({"ph_v_at_4": 1.02, "ph_v_at_7": 1.52, "tds_k": 480.0}))
        cm = CalibrationManager(path=str(path))
        assert cm.data.ph_v_at_4 == 1.02
        assert cm.data.tds_k == 480.0
        assert cm.data.calibrated_at == {}
        assert cm.calibration_age_days("ph") is None

    def test_unparseable_timestamp_treated_as_unknown(self, tmp_path, mock_hardware):
        path = tmp_path / "cal.yaml"
        path.write_text(yaml.safe_dump({"calibrated_at": {"ph": "not-a-date"}}))
        cm = CalibrationManager(path=str(path))
        assert cm.calibration_age_days("ph") is None

    def test_existing_public_api_unchanged(self, tmp_path, mock_hardware):
        cm = make(tmp_path)
        # The original API still behaves exactly as before
        slope = cm.calibrate_ph(v_ph4=1.00, v_ph7=1.60)
        assert abs(slope - 5.0) < 0.01
        assert cm.calibrate_tds(known_ppm=1000.0, measured_v=2.0) == 500.0
        assert cm.calibrate_orp(known_mv=225.0, measured_mv=200.0) == 25.0
        cm.apply_platform_offsets({"ph": 0.1})
        assert cm.get_platform_offset("ph") == 0.1
        assert cm.get_platform_offset("orp") == 0.0
