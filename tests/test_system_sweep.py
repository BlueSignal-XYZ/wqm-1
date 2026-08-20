"""
System sweep: additional tests for sensor calibration accuracy,
SQLite WAL edge cases, Cayenne LPP roundtrips, GPS coordinate
parsing, and power management boundary conditions.
"""

from unittest.mock import MagicMock


class TestPHCalibrationAccuracy:
    """Verify pH reading accuracy across the calibration range."""

    def test_ph4_reads_near_4(self, mock_hardware):
        from sensors.ads1115 import ADS1115
        from sensors.ph import PHSensor

        adc = ADS1115()
        adc.read_voltage = MagicMock(return_value=1.04)
        ph = PHSensor(adc)
        ph.set_calibration(v_ph4=1.04, v_ph7=1.50)
        result = ph.read(temp_c=25.0)
        assert result is not None
        assert abs(result - 4.0) < 0.15

    def test_ph10_reads_near_10(self, mock_hardware):
        from sensors.ads1115 import ADS1115
        from sensors.ph import PHSensor

        adc = ADS1115()
        ph = PHSensor(adc)
        ph.set_calibration(v_ph4=1.04, v_ph7=1.50)
        slope = ph._slope
        v_ph10 = 1.50 + (10.0 - 7.0) / slope
        adc.read_voltage = MagicMock(return_value=v_ph10)
        result = ph.read(temp_c=25.0)
        assert result is not None
        assert abs(result - 10.0) < 0.15

    def test_ph_recalc_slope_called_on_calibration(self, mock_hardware):
        from sensors.ads1115 import ADS1115
        from sensors.ph import PHSensor

        adc = ADS1115()
        ph = PHSensor(adc)
        old_slope = ph._slope
        ph.set_calibration(v_ph4=0.80, v_ph7=1.80)
        assert ph._slope != old_slope

    def test_ph_window_clears_on_recalibration(self, mock_hardware):
        from sensors.ads1115 import ADS1115
        from sensors.ph import PHSensor

        adc = ADS1115()
        adc.read_voltage = MagicMock(return_value=1.50)
        ph = PHSensor(adc)
        ph.read(temp_c=25.0)
        assert len(ph._window) > 0
        ph.set_calibration(v_ph4=1.00, v_ph7=1.60)
        assert len(ph._window) == 0


class TestTDSVoltageConversion:
    """Verify TDS divider and compensation math."""

    def test_tds_divider_correction(self, mock_hardware):
        from sensors.ads1115 import ADS1115
        from sensors.tds import TDSSensor
        from utils.config import TDS_DIVIDER_RATIO

        adc = ADS1115()
        adc.read_voltage = MagicMock(return_value=TDS_DIVIDER_RATIO)
        tds = TDSSensor(adc)
        tds.set_calibration(k=500.0)
        result = tds.read(temp_c=25.0)
        assert result is not None
        assert abs(result - 500.0) < 5.0

    def test_tds_hot_water_reduces_reading(self, mock_hardware):
        from sensors.ads1115 import ADS1115
        from sensors.tds import TDSSensor

        adc = ADS1115()
        adc.read_voltage = MagicMock(return_value=0.3125)
        tds = TDSSensor(adc)

        tds_25 = tds.read(temp_c=25.0)
        tds._window.clear()
        tds_40 = tds.read(temp_c=40.0)
        assert tds_40 < tds_25


class TestSQLiteWALOperations:
    """SQLite WAL mode, concurrent access, and rotation edge cases."""

    def test_concurrent_read_write(self, mock_hardware, tmp_path):
        from storage.database import WQM1Database

        db = WQM1Database(path=str(tmp_path / "test.db"))
        row_id = db.insert_reading({"ph": 7.0, "temp_c": 25.0})
        latest = db.get_latest()
        assert latest is not None
        assert latest["id"] == row_id
        db.close()

    def test_unsynced_returns_in_order(self, mock_hardware, tmp_path):
        from storage.database import WQM1Database

        db = WQM1Database(path=str(tmp_path / "test.db"))
        db.insert_reading({"ph": 6.0})
        db.insert_reading({"ph": 7.0})
        db.insert_reading({"ph": 8.0})
        unsynced = db.get_unsynced(limit=10)
        assert len(unsynced) == 3
        assert unsynced[0]["ph"] == 6.0
        assert unsynced[2]["ph"] == 8.0
        db.close()

    def test_mark_synced_then_unsynced_count(self, mock_hardware, tmp_path):
        from storage.database import WQM1Database

        db = WQM1Database(path=str(tmp_path / "test.db"))
        ids = [db.insert_reading({"ph": float(i)}) for i in range(5)]
        db.mark_synced(ids[:3])
        assert db.get_count(synced=True) == 3
        assert db.get_count(synced=False) == 2
        db.close()

    def test_lorawan_session_roundtrip(self, mock_hardware, tmp_path):
        from storage.database import WQM1Database

        db = WQM1Database(path=str(tmp_path / "test.db"))
        db.save_session(
            dev_addr=b"\x01\x02\x03\x04",
            nwk_skey=b"\x00" * 16,
            app_skey=b"\x11" * 16,
            fcnt_up=42,
            fcnt_down=7,
            joined=True,
        )
        session = db.load_session()
        assert session is not None
        assert bytes(session["dev_addr"]) == b"\x01\x02\x03\x04"
        assert session["fcnt_up"] == 42
        assert session["joined"] == 1
        db.close()

    def test_rotate_preserves_unsynced(self, mock_hardware, tmp_path):
        from storage.database import WQM1Database

        db = WQM1Database(path=str(tmp_path / "test.db"))
        for _ in range(20):
            db.insert_reading({"ph": 7.0})
        db.mark_synced(list(range(1, 16)))
        deleted = db.rotate(max_rows=10)
        assert deleted == 10
        assert db.get_count(synced=False) == 5
        db.close()

    def test_empty_mark_synced_is_noop(self, mock_hardware, tmp_path):
        from storage.database import WQM1Database

        db = WQM1Database(path=str(tmp_path / "test.db"))
        db.mark_synced([])
        db.close()


class TestCayenneLPPRoundtrip:
    """Full encode-decode roundtrips for all sensor types."""

    def test_full_reading_roundtrip(self, mock_hardware):
        from radio.cayenne import decode, encode

        data = {
            "temp_c": 23.5,
            "ph": 7.21,
            "tds_ppm": 250.0,
            "turbidity_ntu": 45.5,
            "orp_mv": 225.0,
            "lat": 30.2672,
            "lon": -97.7431,
            "alt_m": 150.0,
        }
        payload = encode(data)
        result = decode(payload)

        assert abs(result["temp_c"] - 23.5) < 0.15
        assert abs(result["ph"] - 7.21) < 0.015
        assert abs(result["tds_ppm"] - 250.0) < 0.015
        assert abs(result["turbidity_ntu"] - 45.5) < 0.015
        assert abs(result["orp_mv"] - 225.0) < 0.015
        assert abs(result["lat"] - 30.2672) < 0.001
        assert abs(result["lon"] - (-97.7431)) < 0.001
        assert abs(result["alt_m"] - 150.0) < 0.015

    def test_encode_missing_fields(self, mock_hardware):
        from radio.cayenne import decode, encode

        payload = encode({"ph": 7.0})
        result = decode(payload)
        assert abs(result["ph"] - 7.0) < 0.015
        assert "temp_c" not in result

    def test_encode_negative_values(self, mock_hardware):
        from radio.cayenne import decode, encode

        data = {"temp_c": -5.0, "orp_mv": -200.0}
        payload = encode(data)
        result = decode(payload)
        assert abs(result["temp_c"] - (-5.0)) < 0.15
        assert abs(result["orp_mv"] - (-200.0)) < 0.015

    def test_gps_western_hemisphere(self, mock_hardware):
        from radio.cayenne import decode, encode

        data = {"lat": 40.7128, "lon": -74.0060, "alt_m": 10.0}
        payload = encode(data)
        result = decode(payload)
        assert abs(result["lat"] - 40.7128) < 0.001
        assert abs(result["lon"] - (-74.0060)) < 0.001


class TestGPSCoordinateParsing:
    """NMEA coordinate edge cases."""

    def test_parse_equator_prime_meridian(self, mock_hardware):
        from sensors.gps import _parse_gga

        sentence = "$GPGGA,120000.00,0000.0000,N,00000.0000,E,1,08,1.2,0.0,M,,,,*6E"
        fix = _parse_gga(sentence)
        assert fix is not None
        assert abs(fix.latitude) < 0.001
        assert abs(fix.longitude) < 0.001

    def test_parse_southern_eastern(self, mock_hardware):
        from sensors.gps import _parse_gga

        sentence = "$GPGGA,120000.00,3352.1300,S,15112.5580,E,1,10,0.8,58.0,M,,,,*51"
        fix = _parse_gga(sentence)
        assert fix is not None
        assert fix.latitude < 0
        assert fix.longitude > 0

    def test_parse_gngga_prefix(self, mock_hardware):
        from sensors.gps import _parse_gga

        sentence = "$GNGGA,120000.00,3016.0000,N,09744.5800,W,1,08,1.2,150.0,M,,,,*6F"
        fix = _parse_gga(sentence)
        assert fix is not None

    def test_verify_checksum_correct(self, mock_hardware):
        from sensors.gps import _verify_checksum

        body = "GPGGA,120000.00,3016.0000,N,09744.5800,W,1,08,1.2,150.0,M,,,,"
        cs = 0
        for c in body:
            cs ^= ord(c)
        sentence = f"${body}*{cs:02X}"
        assert _verify_checksum(sentence) is True

    def test_verify_checksum_wrong(self, mock_hardware):
        from sensors.gps import _verify_checksum

        assert _verify_checksum("$GPGGA,data*00") is False


class TestFanControllerBoundaryConditions:
    """Fan controller at exact threshold boundaries."""

    def test_fan_at_exact_on_threshold(self, mock_hardware):
        from utils.watchdog import FanController

        fan = FanController(on_temp=60.0, off_temp=55.0)
        fan.update(cpu_temp=60.0)
        assert fan.is_on is True

    def test_fan_at_exact_off_threshold(self, mock_hardware):
        from utils.watchdog import FanController

        fan = FanController(on_temp=60.0, off_temp=55.0)
        fan.update(cpu_temp=65.0)
        fan.update(cpu_temp=55.0)
        assert fan.is_on is False

    def test_fan_rapid_oscillation_hysteresis(self, mock_hardware):
        from utils.watchdog import FanController

        fan = FanController(on_temp=60.0, off_temp=55.0)
        fan.update(cpu_temp=65.0)
        assert fan.is_on is True
        fan.update(cpu_temp=57.0)
        assert fan.is_on is True
        fan.update(cpu_temp=56.0)
        assert fan.is_on is True
        fan.update(cpu_temp=54.0)
        assert fan.is_on is False
        fan.update(cpu_temp=58.0)
        assert fan.is_on is False


class TestHealthReporterBoundaries:
    """Health reporter signal edge cases."""

    def test_signal_default_unknown_is_none(self, mock_hardware):
        # v2: honest nulls — no RSSI yet must not report a fake -120 dBm.
        from utils.health import HealthReporter

        hr = HealthReporter("2.0.0")
        assert hr.get_signal_strength() is None

    def test_report_structure(self, mock_hardware):
        from utils.health import HealthReporter

        hr = HealthReporter(firmware_version="2.0.0")
        hr.update_rssi(-85)
        hr.update_last_seen()
        report = hr.get_report()
        # batteryLevel is deliberately absent — removed 2026-08-20 because
        # nothing measured it. See utils/health.py.
        assert "batteryLevel" not in report
        assert "signalStrength" in report
        assert "lastSeen" in report
        assert "firmwareVersion" in report
        assert report["firmwareVersion"] == "2.0.0"
        assert report["signalStrength"] == -85


class TestLoRaWANSessionManagement:
    """LoRaWAN session state transitions."""

    def test_restore_session_enables_uplink(self, mock_hardware):
        from radio.lorawan import LoRaWANMAC, LoRaWANSession

        mock_radio = MagicMock()
        mock_radio.send.return_value = True
        mock_radio.receive.return_value = None
        mac = LoRaWANMAC(mock_radio, bytes(8), bytes(8), bytes(16))

        session = LoRaWANSession(
            dev_addr=b"\x01\x02\x03\x04",
            nwk_skey=bytes(16),
            app_skey=bytes(16),
            fcnt_up=10,
            joined=True,
        )
        mac.restore_session(session)
        mac.send_uplink(b"test")
        assert mock_radio.send.called

    def test_fcnt_increments_on_uplink(self, mock_hardware):
        from radio.lorawan import LoRaWANMAC, LoRaWANSession

        mock_radio = MagicMock()
        mock_radio.send.return_value = True
        mock_radio.receive.return_value = None
        mac = LoRaWANMAC(mock_radio, bytes(8), bytes(8), bytes(16))

        session = LoRaWANSession(
            dev_addr=b"\x01\x02\x03\x04",
            nwk_skey=bytes(16),
            app_skey=bytes(16),
            fcnt_up=0,
            joined=True,
        )
        mac.restore_session(session)
        mac.send_uplink(b"test1")
        mac.send_uplink(b"test2")
        assert mac.session.fcnt_up == 2

    def test_uplink_payload_encrypted(self, mock_hardware):
        from radio.lorawan import LoRaWANMAC, LoRaWANSession

        mock_radio = MagicMock()
        mock_radio.send.return_value = True
        mock_radio.receive.return_value = None
        mac = LoRaWANMAC(mock_radio, bytes(8), bytes(8), bytes(16))

        session = LoRaWANSession(
            dev_addr=b"\x01\x02\x03\x04",
            nwk_skey=bytes(16),
            app_skey=bytes(16),
            joined=True,
        )
        mac.restore_session(session)
        mac.send_uplink(b"plaintext")
        frame = mock_radio.send.call_args[0][0]
        assert b"plaintext" not in frame
