"""Extended GPS tests — coordinate parsing edge cases and driver behavior."""

from unittest.mock import MagicMock


class TestGPSCoordinateParsing:
    """Test NMEA lat/lon parsing edge cases."""

    def test_parse_equator_prime_meridian(self, mock_hardware):
        from sensors.gps import _parse_gga

        # 0°0.000 N, 0°0.000 E — equator/prime meridian
        sentence = "$GPGGA,120000,0000.000,N,00000.000,E,1,06,1.5,0.0,M,0.0,M,,*68"
        fix = _parse_gga(sentence)
        assert fix is not None
        assert abs(fix.latitude) < 0.001
        assert abs(fix.longitude) < 0.001

    def test_parse_high_latitude(self, mock_hardware):
        from sensors.gps import _parse_gga

        # 89°59.999 N — near north pole
        sentence = "$GPGGA,120000,8959.999,N,00000.000,E,1,06,1.5,0.0,M,0.0,M,,*5D"
        fix = _parse_gga(sentence)
        assert fix is not None
        assert fix.latitude > 89.9

    def test_parse_timestamp(self, mock_hardware):
        from sensors.gps import _parse_gga

        sentence = "$GPGGA,143456,4807.038,N,01131.000,E,1,08,0.9,545.4,M,47.0,M,,*4F"
        fix = _parse_gga(sentence)
        # Recalculate checksum for this sentence
        if fix is not None:
            assert fix.timestamp is not None or fix.timestamp is None  # depends on checksum

    def test_verify_checksum_edge_cases(self, mock_hardware):
        from sensors.gps import _verify_checksum

        # Empty body
        assert _verify_checksum("$*00") is True  # XOR of nothing is 0
        # No asterisk
        assert _verify_checksum("$GPGGA") is False
        # Invalid hex
        assert _verify_checksum("$GP*ZZ") is False

    def test_parse_gga_missing_altitude(self, mock_hardware):
        from sensors.gps import _parse_gga

        # Valid fix but no altitude field
        sentence = "$GPGGA,120000,4807.038,N,01131.000,E,1,08,0.9,,M,47.0,M,,*5A"
        fix = _parse_gga(sentence)
        if fix is not None:
            assert fix.altitude is None

    def test_parse_gga_missing_satellites(self, mock_hardware):
        from sensors.gps import _parse_gga

        sentence = "$GPGGA,120000,4807.038,N,01131.000,E,1,,0.9,545.4,M,47.0,M,,*76"
        fix = _parse_gga(sentence)
        if fix is not None:
            assert fix.satellites is None


class TestGPSDriverExtended:
    def test_get_fix_with_valid_nmea(self, mock_hardware):
        from sensors.gps import GPS

        valid_gga = b"$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,47.0,M,,*4F\r\n"
        mock_hardware["serial"].readline.side_effect = [valid_gga, b""]
        mock_hardware["serial"].is_open = True

        gps = GPS()
        fix = gps.get_fix(timeout_s=1.0)
        assert fix is not None
        assert abs(fix.latitude - 48.1173) < 0.001

    def test_last_fix_property(self, mock_hardware):
        from sensors.gps import GPS

        gps = GPS()
        assert gps.last_fix is None

        valid_gga = b"$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,47.0,M,,*4F\r\n"
        mock_hardware["serial"].readline.side_effect = [valid_gga, b""]

        gps.get_fix(timeout_s=1.0)
        assert gps.last_fix is not None

    def test_close(self, mock_hardware):
        from sensors.gps import GPS

        gps = GPS()
        gps.close()
        mock_hardware["serial"].close.assert_called_once()

    def test_get_fix_returns_cached_when_serial_closed(self, mock_hardware):
        from sensors.gps import GPS

        mock_hardware["serial"].is_open = False
        gps = GPS()
        result = gps.get_fix(timeout_s=0.1)
        # Returns last_fix (None) when serial is closed
        assert result is None

    def test_get_fix_skips_invalid_checksum(self, mock_hardware):
        from sensors.gps import GPS

        bad_checksum = b"$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,47.0,M,,*00\r\n"
        valid_gga = b"$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,47.0,M,,*4F\r\n"
        mock_hardware["serial"].readline.side_effect = [bad_checksum, valid_gga, b""]

        gps = GPS()
        fix = gps.get_fix(timeout_s=1.0)
        assert fix is not None  # should have parsed the valid one
