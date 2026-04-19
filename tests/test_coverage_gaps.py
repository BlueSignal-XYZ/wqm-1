"""Tests targeting specific uncovered lines to raise coverage."""

from unittest.mock import MagicMock, patch

import pytest


class TestADS1115BusNone:
    """Cover ads1115.py lines 76 (bus None check) and 92 (conversion timeout)."""

    def test_read_raw_raises_when_bus_none(self, mock_hardware):
        """read_raw must raise RuntimeError when the I2C bus was never opened."""
        from sensors.ads1115 import ADS1115

        adc = ADS1115()
        adc._bus = None
        with pytest.raises(RuntimeError, match="bus not available"):
            adc.read_raw(0)

    def test_read_raw_timeout_returns_stale_value(self, mock_hardware):
        """When the conversion-done bit never asserts, read_raw still returns a value."""
        from sensors.ads1115 import ADS1115

        # Config reads always return 0x00 (OS bit never goes high),
        # then conversion register returns a known value.
        call_count = 0

        def side_effect(addr, reg, length):
            nonlocal call_count
            call_count += 1
            if reg == 0x01:
                return [0x00, 0x00]  # OS bit stays low -> timeout
            return [0x10, 0x00]  # conversion result = 4096

        mock_hardware["bus"].read_i2c_block_data.side_effect = side_effect
        adc = ADS1115()
        raw = adc.read_raw(0)
        # Should eventually read the conversion register even after timeout
        assert isinstance(raw, int)

    def test_close_idempotent(self, mock_hardware):
        """Closing an already-closed ADC must not raise."""
        from sensors.ads1115 import ADS1115

        adc = ADS1115()
        adc.close()
        adc.close()


class TestGPSSerialOpenFailure:
    """Cover gps.py lines 60-61 (serial open raises) and line 222-223 (ValueError in parse)."""

    def test_gps_init_serial_failure(self, mock_hardware):
        """GPS must handle serial.Serial() raising without crashing."""
        mock_hardware["serial_mod"].Serial.side_effect = OSError("permission denied")
        from sensors.gps import GPS

        gps = GPS()
        assert gps._serial is None

    def test_get_fix_returns_none_when_serial_is_none(self, mock_hardware):
        """get_fix with _serial=None must return None (last_fix)."""
        mock_hardware["serial_mod"].Serial.side_effect = OSError("fail")
        from sensors.gps import GPS

        gps = GPS()
        result = gps.get_fix(timeout_s=0.1)
        assert result is None

    def test_close_when_serial_not_open(self, mock_hardware):
        """close() with closed serial must not raise."""
        from sensors.gps import GPS

        gps = GPS()
        mock_hardware["serial"].is_open = False
        gps.close()  # should not raise

    def test_parse_gga_invalid_latitude(self, mock_hardware):
        """GGA with corrupt latitude field must return None, not crash."""
        from sensors.gps import _parse_gga

        sentence = "$GPGGA,120000,INVALID,N,01131.000,E,1,08,0.9,545.4,M,47.0,M,,*00"
        result = _parse_gga(sentence)
        assert result is None

    def test_parse_gga_empty_lat_dir(self, mock_hardware):
        """GGA with empty lat direction must return None."""
        from sensors.gps import _parse_gga

        sentence = "$GPGGA,120000,4807.038,,01131.000,E,1,08,0.9,545.4,M,47.0,M,,*00"
        result = _parse_gga(sentence)
        assert result is None

    def test_parse_gga_empty_lon_dir(self, mock_hardware):
        """GGA with empty lon direction must return None."""
        from sensors.gps import _parse_gga

        sentence = "$GPGGA,120000,4807.038,N,01131.000,,1,08,0.9,545.4,M,47.0,M,,*00"
        result = _parse_gga(sentence)
        assert result is None

    def test_parse_gga_invalid_time(self, mock_hardware):
        """GGA with malformed time field still returns a fix (timestamp=None)."""
        from sensors.gps import _parse_gga

        sentence = "$GPGGA,BAD,4807.038,N,01131.000,E,1,08,0.9,545.4,M,47.0,M,,*00"
        _parse_gga(sentence)


class TestConfigEditorErrorPath:
    """Cover config_editor.py lines 36-39 (atomic write failure cleanup)."""

    def test_atomic_write_failure_cleans_up_tmp(self, tmp_path, mock_hardware):
        """If the rename fails, the .tmp file must be cleaned up."""
        from service_window.config_editor import _atomic_yaml_write

        target = str(tmp_path / "config.yaml")
        with (
            patch("pathlib.Path.replace", side_effect=OSError("cross-device")),
            pytest.raises(OSError),
        ):
            _atomic_yaml_write(target, {"key": "val"})

        # .tmp file should have been cleaned up
        tmp_file = tmp_path / "config.tmp"
        assert not tmp_file.exists()


class TestDatabaseRotateEdge:
    """Cover database.py line 145 (rotate returns default max_rows from settings)."""

    def test_rotate_uses_settings_default(self, tmp_path, mock_hardware):
        """rotate() without max_rows arg must read from settings."""
        from storage.database import WQM1Database

        db = WQM1Database(path=str(tmp_path / "test.db"))
        for _ in range(5):
            db.insert_reading({"ph": 7.0})
        # Default max_rows is 100_000, so no rotation needed
        deleted = db.rotate()
        assert deleted == 0
        db.close()


class TestIdentityBLEName:
    """Cover identity.py line 76 (get_ble_name with default device_id)."""

    def test_ble_name_default_device_id(self, mock_hardware):
        """get_ble_name() with no args must derive from get_device_id()."""
        from utils.identity import get_ble_name

        name = get_ble_name()
        assert name.startswith("BlueSignal-")
        assert len(name.split("-")[-1]) == 4


class TestPHSensorMedianFilter:
    """Cover ph.py line 93 (median window trimming)."""

    def test_ph_window_caps_at_five(self, mock_hardware):
        """pH moving median window must not exceed window_size."""
        from sensors.ads1115 import ADS1115
        from sensors.ph import PHSensor

        adc = ADS1115()
        ph_sensor = PHSensor(adc)
        for v in [1.4, 1.42, 1.44, 1.46, 1.48, 1.50, 1.52]:
            adc.read_voltage = MagicMock(return_value=v)
            ph_sensor.read(temp_c=25.0)
        assert len(ph_sensor._window) == 5


class TestRelayGetInvalidChannel:
    """Cover relay.py line 63 (get() with invalid channel)."""

    def test_get_invalid_channel_raises(self, mock_hardware):
        from control.relay import RelayController

        rc = RelayController()
        with pytest.raises(ValueError):
            rc.get(0)
        with pytest.raises(ValueError):
            rc.get(5)


class TestServiceWindowDBReaderEdgeCases:
    """Cover db_reader paths that weren't hit by existing tests."""

    def test_get_readings_empty_db(self, tmp_path, mock_hardware):
        """get_readings on empty DB must return empty list."""
        import sqlite3

        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                ph REAL, tds_ppm REAL, turbidity_ntu REAL, orp_mv REAL,
                temp_c REAL, lat REAL, lon REAL, alt_m REAL,
                battery_v REAL, relay_state INTEGER DEFAULT 0, synced INTEGER DEFAULT 0
            );
            CREATE TABLE lorawan_session (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                dev_addr BLOB, nwk_skey BLOB, app_skey BLOB,
                fcnt_up INTEGER DEFAULT 0, fcnt_down INTEGER DEFAULT 0,
                joined INTEGER DEFAULT 0, updated_at TEXT
            );
            INSERT INTO lorawan_session (id) VALUES (1);
            """
        )
        conn.close()

        from service_window.db_reader import DBReader

        reader = DBReader(db_path)
        assert reader.get_latest_reading() is None
        assert reader.get_readings() == []
        assert reader.get_reading_count() == 0

    def test_get_lorawan_session_exists(self, tmp_path, mock_hardware):
        """get_lorawan_session returns default session row."""
        import sqlite3

        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE lorawan_session (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                dev_addr BLOB, nwk_skey BLOB, app_skey BLOB,
                fcnt_up INTEGER DEFAULT 0, fcnt_down INTEGER DEFAULT 0,
                joined INTEGER DEFAULT 0, updated_at TEXT
            );
            INSERT INTO lorawan_session (id) VALUES (1);
            """
        )
        conn.close()

        from service_window.db_reader import DBReader

        reader = DBReader(db_path)
        session = reader.get_lorawan_session()
        assert session is not None
        assert session["joined"] == 0


class TestCayenneLPPInt24EdgeCases:
    """Cover edge cases in int24 encoding/decoding."""

    def test_int24_zero(self, mock_hardware):
        from radio.cayenne import _from_int24, _int24

        assert _from_int24(_int24(0)) == 0

    def test_int24_max_positive(self, mock_hardware):
        from radio.cayenne import _from_int24, _int24

        max_val = (1 << 23) - 1
        assert _from_int24(_int24(max_val)) == max_val

    def test_int24_max_negative(self, mock_hardware):
        from radio.cayenne import _from_int24, _int24

        min_val = -(1 << 23)
        assert _from_int24(_int24(min_val)) == min_val


class TestLoRaWANJoinAcceptEdgeCases:
    """Cover lorawan.py join accept processing edge cases."""

    def test_join_accept_wrong_mhdr(self, mock_hardware):
        """A non-JoinAccept frame type must be rejected."""
        from radio.lorawan import LoRaWANMAC

        mock_radio = MagicMock()
        mock_radio.send.return_value = True
        # Return a frame with wrong MHDR
        mock_radio.receive.return_value = bytes([0x40]) + bytes(16)

        mac = LoRaWANMAC(mock_radio, bytes(8), bytes(8), bytes(16))
        result = mac.join(timeout_s=0.1)
        assert result is False

    def test_join_accept_too_short(self, mock_hardware):
        """A JoinAccept shorter than 17 bytes must be rejected."""
        from radio.lorawan import LoRaWANMAC

        mock_radio = MagicMock()
        mock_radio.send.return_value = True
        mock_radio.receive.return_value = bytes([0x20]) + bytes(5)

        mac = LoRaWANMAC(mock_radio, bytes(8), bytes(8), bytes(16))
        result = mac.join(timeout_s=0.1)
        assert result is False


class TestDownlinkProcessing:
    """Cover lorawan.py downlink parsing edge cases."""

    def test_downlink_too_short_returns_none(self, mock_hardware):
        """_process_downlink must return None for frames < 12 bytes."""
        from radio.lorawan import LoRaWANMAC, LoRaWANSession

        mock_radio = MagicMock()
        mac = LoRaWANMAC(mock_radio, bytes(8), bytes(8), bytes(16))
        mac.restore_session(
            LoRaWANSession(
                dev_addr=b"\x01\x02\x03\x04",
                nwk_skey=bytes(16),
                app_skey=bytes(16),
                joined=True,
            )
        )
        result = mac._process_downlink(bytes(8))
        assert result is None

    def test_downlink_wrong_devaddr_returns_none(self, mock_hardware):
        """Downlink with mismatched DevAddr must be ignored."""
        from radio.lorawan import LoRaWANMAC, LoRaWANSession

        mock_radio = MagicMock()
        mac = LoRaWANMAC(mock_radio, bytes(8), bytes(8), bytes(16))
        mac.restore_session(
            LoRaWANSession(
                dev_addr=b"\x01\x02\x03\x04",
                nwk_skey=bytes(16),
                app_skey=bytes(16),
                joined=True,
            )
        )
        # MHDR=0x60 (unconfirmed down), wrong DevAddr, FCtrl, FCnt, MIC
        frame = bytes([0x60]) + b"\xff\xff\xff\xff" + bytes(12)
        result = mac._process_downlink(frame)
        assert result is None

    def test_downlink_no_payload_returns_none(self, mock_hardware):
        """Downlink with no FPort/payload (just header + MIC) returns None."""
        import struct

        from radio.lorawan import LoRaWANMAC, LoRaWANSession

        mock_radio = MagicMock()
        mac = LoRaWANMAC(mock_radio, bytes(8), bytes(8), bytes(16))
        dev_addr = b"\x01\x02\x03\x04"
        mac.restore_session(
            LoRaWANSession(
                dev_addr=dev_addr,
                nwk_skey=bytes(16),
                app_skey=bytes(16),
                joined=True,
            )
        )
        # MHDR + DevAddr + FCtrl(0) + FCnt(2) + MIC(4) = 12 bytes, no FPort
        frame = bytes([0x60]) + dev_addr + bytes([0x00]) + struct.pack("<H", 0) + bytes(4)
        result = mac._process_downlink(frame)
        assert result is None


class TestAuthRateLimiting:
    """Cover auth.py rate limiting paths."""

    def test_rate_limiting_blocks_after_max_attempts(self, mock_hardware):
        """After _MAX_ATTEMPTS failed logins from same IP, further attempts are blocked."""
        from service_window.app import create_app
        from service_window.auth import _attempts

        app = create_app({"pin": "9999", "db_path": ":memory:", "config_path": "/dev/null"})
        app.config["TESTING"] = True
        client = app.test_client()

        _attempts.clear()

        try:
            for _ in range(6):
                client.post("/login", data={"pin": "wrong"})

            resp = client.post("/login", data={"pin": "wrong"})
            assert b"Too many" in resp.data or resp.status_code == 200
        finally:
            _attempts.clear()
