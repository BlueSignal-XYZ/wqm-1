"""Tests for firmware/drivers/gps.py — NMEA parsing."""

import pytest


class TestNMEAChecksum:
    def test_valid_checksum(self, mock_hardware):
        from sensors.gps import _verify_checksum
        sentence = "$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,47.0,M,,*4F"
        assert _verify_checksum(sentence) is True

    def test_invalid_checksum(self, mock_hardware):
        from sensors.gps import _verify_checksum
        sentence = "$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,47.0,M,,*00"
        assert _verify_checksum(sentence) is False

    def test_no_dollar_sign(self, mock_hardware):
        from sensors.gps import _verify_checksum
        assert _verify_checksum("GPGGA,123519*47") is False


class TestGGAParsing:
    def test_parse_valid_gga(self, mock_hardware):
        from sensors.gps import _parse_gga
        # _parse_gga does not validate checksum, just parses fields
        sentence = "$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,47.0,M,,*4F"
        fix = _parse_gga(sentence)
        assert fix is not None
        assert abs(fix.latitude - 48.1173) < 0.001
        assert abs(fix.longitude - 11.5167) < 0.001
        assert fix.altitude == 545.4
        assert fix.satellites == 8

    def test_parse_gngga(self, mock_hardware):
        from sensors.gps import _parse_gga
        sentence = "$GNGGA,123519,4807.038,N,01131.000,E,1,12,0.8,545.4,M,47.0,M,,*5B"
        fix = _parse_gga(sentence)
        assert fix is not None
        assert fix.satellites == 12

    def test_parse_no_fix(self, mock_hardware):
        from sensors.gps import _parse_gga
        sentence = "$GPGGA,123519,,,,,0,00,,,M,,M,,*6B"
        fix = _parse_gga(sentence)
        assert fix is None

    def test_parse_south_west(self, mock_hardware):
        from sensors.gps import _parse_gga
        sentence = "$GPGGA,123519,3316.000,S,06432.000,W,1,08,0.9,10.0,M,0.0,M,,*44"
        fix = _parse_gga(sentence)
        assert fix is not None
        assert fix.latitude < 0  # South
        assert fix.longitude < 0  # West

    def test_parse_non_gga_returns_none(self, mock_hardware):
        from sensors.gps import _parse_gga
        assert _parse_gga("$GPRMC,123519,A,4807.038,N,01131.000,E,*55") is None

    def test_parse_short_sentence_returns_none(self, mock_hardware):
        from sensors.gps import _parse_gga
        assert _parse_gga("$GPGGA,123519*00") is None


class TestGPSDriver:
    def test_get_fix_returns_none_on_timeout(self, mock_hardware):
        from sensors.gps import GPS
        mock_hardware["serial"].readline.return_value = b""
        gps = GPS()
        fix = gps.get_fix(timeout_s=0.1)
        assert fix is None

    def test_power_cycle_pulses_extint(self, mock_hardware):
        from sensors.gps import GPS
        import RPi.GPIO as GPIO
        gps = GPS()
        GPIO.output.reset_mock()
        gps.power_cycle()
        calls = GPIO.output.call_args_list
        # Should have HIGH then LOW on EXTINT pin (19)
        extint_calls = [c for c in calls if c[0][0] == 19]
        assert len(extint_calls) >= 2
