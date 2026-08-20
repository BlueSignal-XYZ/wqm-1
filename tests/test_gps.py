"""Tests for firmware/drivers/gps.py — NMEA parsing."""


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
        import RPi.GPIO as GPIO

        from sensors.gps import GPS

        gps = GPS()
        GPIO.output.reset_mock()
        gps.power_cycle()
        calls = GPIO.output.call_args_list
        # Should have HIGH then LOW on EXTINT pin (19)
        extint_calls = [c for c in calls if c[0][0] == 19]
        assert len(extint_calls) >= 2


class TestNoFixSaysWhy:
    """A GPS that never works must state which failure it is.

    Every path out of get_fix used to return None silently, so a unit whose
    UART never opened, one reading pure noise from a baud mismatch, and one
    with a receiver that simply had not locked all looked identical: a power
    cycle every gps_fix_s and not a word of explanation.
    """

    def _gps(self, lines, is_open=True):
        from sensors.gps import GPS

        g = GPS.__new__(GPS)
        g._port_name = "/dev/serial0"
        g._baud = 38400
        g._last_fix = None
        g._last_no_fix_log = 0.0
        g._last_no_fix_detail = ""
        import threading

        g._lock = threading.Lock()

        class FakeSerial:
            def __init__(self, payload, opened):
                self._payload = list(payload)
                self.is_open = opened

            def reset_input_buffer(self):
                pass

            def readline(self):
                return self._payload.pop(0) if self._payload else b""

        g._serial = FakeSerial(lines, is_open)
        return g

    def test_unopened_uart_is_reported(self, caplog):
        g = self._gps([], is_open=False)
        with caplog.at_level("WARNING"):
            assert g.get_fix(timeout_s=0.05) is None
        assert "UART is not open" in caplog.text

    def test_baud_mismatch_is_named_as_such(self, caplog):
        # Bytes that will never pass an NMEA checksum — what noise looks like.
        with caplog.at_level("WARNING"):
            g = self._gps([b"\xff\xfe garbage\n"] * 3)
            assert g.get_fix(timeout_s=0.2) is None
        assert "failed checksum" in caplog.text
        assert "38400" in caplog.text

    def test_receiver_talking_but_not_locked_is_distinguished(self, caplog):
        # Valid GGA, checksum correct, fix quality 0.
        sentence = "$GNGGA,001339.00,,,,,0,00,99.99,,,,,,"
        body = sentence[1:]
        chk = 0
        for c in body:
            chk ^= ord(c)
        line = f"{sentence}*{chk:02X}\r\n".encode()
        with caplog.at_level("WARNING"):
            g = self._gps([line] * 3)
            assert g.get_fix(timeout_s=0.2) is None
        assert "quality 0" in caplog.text

    def test_silence_on_the_uart_is_distinguished(self, caplog):
        with caplog.at_level("WARNING"):
            g = self._gps([])
            assert g.get_fix(timeout_s=0.05) is None
        assert "no bytes on the UART" in caplog.text
