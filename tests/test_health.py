"""Tests for firmware/src/utils/health.py — health telemetry."""

import pytest


class TestHealthReporter:
    def test_initial_report(self, mock_hardware):
        from utils.health import HealthReporter
        hr = HealthReporter("1.0.0")
        report = hr.get_report()
        assert report["firmwareVersion"] == "1.0.0"
        assert report["batteryLevel"] == 0
        assert report["signalStrength"] == -120
        assert report["lastSeen"] == 0

    def test_battery_level_conversion(self, mock_hardware):
        from utils.health import HealthReporter
        hr = HealthReporter()
        hr.update_battery(24.5)  # mid-range in 21-28V system
        assert 0 < hr.get_battery_level() < 100

    def test_battery_level_clamped(self, mock_hardware):
        from utils.health import HealthReporter
        hr = HealthReporter()
        hr.update_battery(30.0)  # above max
        assert hr.get_battery_level() == 100
        hr.update_battery(18.0)  # below min
        assert hr.get_battery_level() == 0

    def test_rssi_update(self, mock_hardware):
        from utils.health import HealthReporter
        hr = HealthReporter()
        hr.update_rssi(-67)
        assert hr.get_signal_strength() == -67

    def test_last_seen_update(self, mock_hardware):
        from utils.health import HealthReporter
        hr = HealthReporter()
        hr.update_last_seen()
        report = hr.get_report()
        assert report["lastSeen"] > 0
