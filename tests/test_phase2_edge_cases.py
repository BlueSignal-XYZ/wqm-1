"""
Edge-case tests for critical firmware paths.

Covers: SPI guard after close, timezone-aware rules scheduling,
Cayenne LPP int24 boundaries, database WAL verification,
sensor temperature compensation extremes, and GPS checksum edge cases.
"""

from datetime import UTC, datetime
from datetime import time as dt_time
from unittest.mock import MagicMock

import pytest


class TestSX1262XferGuard:
    """SPI transfer raises after radio is closed."""

    def test_xfer_raises_after_close(self, mock_hardware):
        import sys

        sys.modules.setdefault("lgpio", MagicMock())
        from radio.sx1262 import SX1262

        radio = SX1262()
        radio._spi = None
        with pytest.raises(RuntimeError, match="SPI bus not available"):
            radio._xfer([0x00])

    def test_xfer_works_when_spi_open(self, mock_hardware):
        import sys

        sys.modules.setdefault("lgpio", MagicMock())
        from radio.sx1262 import SX1262

        radio = SX1262()
        result = radio._xfer([0xC0])
        assert isinstance(result, list)


class TestRulesTimezoneAware:
    """Rules engine uses UTC-aware datetimes."""

    def test_default_clock_is_utc_aware(self, mock_hardware):
        from control.rules import RulesEngine

        # The engine's default clock is what makes the schedule check UTC —
        # assert it directly rather than inferring it from a window match.
        engine = RulesEngine()
        assert engine._clock().tzinfo is UTC

    def test_schedule_check_uses_utc(self, mock_hardware, fixed_clock):
        from control.rules import RulesEngine

        # Previously ran against wall time with a 00:00–23:59 window, which
        # excludes the 23:59 minute — the assertion was false once a day.
        engine = RulesEngine(clock=fixed_clock(12, 0))
        engine._schedule_enabled = True
        engine._schedule_start = dt_time(0, 0)
        engine._schedule_end = dt_time(23, 59)
        assert engine._is_in_schedule() is True

    def test_on_time_budget_uses_utc_hour(self, mock_hardware):
        from control.rules import RulesEngine

        engine = RulesEngine()
        engine._max_on_s_per_hour = 600
        utc_hour = datetime.now(UTC).hour
        assert engine._check_on_time_budget(1) is True
        engine._on_time[1] = (utc_hour, 601.0)
        assert engine._check_on_time_budget(1) is False

    def test_accumulate_on_time_resets_on_new_hour(self, mock_hardware):
        from control.rules import RulesEngine

        engine = RulesEngine()
        engine._max_on_s_per_hour = 600
        engine._on_time[1] = (99, 100.0)
        engine._accumulate_on_time(1, 10.0)
        utc_hour = datetime.now(UTC).hour
        assert engine._on_time[1] == (utc_hour, 10.0)

    def test_overnight_schedule_window(self, mock_hardware, fixed_clock):
        from control.rules import RulesEngine

        def engine_at(hour: int, minute: int = 0):
            engine = RulesEngine(clock=fixed_clock(hour, minute))
            engine._schedule_enabled = True
            engine._schedule_start = dt_time(22, 0)
            engine._schedule_end = dt_time(6, 0)
            return engine

        # An overnight window wraps midnight: in range on both sides of it,
        # out of range in between. This used to re-derive the expectation from
        # the wall clock, so it agreed with the implementation by construction.
        assert engine_at(23, 0)._is_in_schedule() is True
        assert engine_at(3, 0)._is_in_schedule() is True
        assert engine_at(12, 0)._is_in_schedule() is False


class TestCayenneInt24Boundaries:
    """Cayenne LPP int24 encode/decode at boundaries."""

    def test_int24_max_positive(self, mock_hardware):
        from radio.cayenne import _from_int24, _int24

        val = (1 << 23) - 1
        encoded = _int24(val)
        assert len(encoded) == 3
        assert _from_int24(encoded) == val

    def test_int24_max_negative(self, mock_hardware):
        from radio.cayenne import _from_int24, _int24

        val = -(1 << 23)
        encoded = _int24(val)
        assert len(encoded) == 3
        assert _from_int24(encoded) == val

    def test_int24_zero(self, mock_hardware):
        from radio.cayenne import _from_int24, _int24

        encoded = _int24(0)
        assert encoded == b"\x00\x00\x00"
        assert _from_int24(encoded) == 0

    def test_int24_minus_one(self, mock_hardware):
        from radio.cayenne import _from_int24, _int24

        encoded = _int24(-1)
        assert encoded == b"\xff\xff\xff"
        assert _from_int24(encoded) == -1

    def test_encode_gps_southern_hemisphere(self, mock_hardware):
        from radio.cayenne import decode, encode

        payload = encode({"lat": -33.8688, "lon": 151.2093, "alt_m": 58.0})
        result = decode(payload)
        assert abs(result["lat"] - (-33.8688)) < 0.001
        assert abs(result["lon"] - 151.2093) < 0.001

    def test_decode_truncated_gps_payload(self, mock_hardware):
        from radio.cayenne import CH_GPS, LPP_GPS, decode

        truncated = bytes([CH_GPS, LPP_GPS, 0x00, 0x01, 0x02])
        result = decode(truncated)
        assert "lat" not in result

    def test_decode_truncated_analog_payload(self, mock_hardware):
        from radio.cayenne import CH_PH, LPP_ANALOG_INPUT, decode

        truncated = bytes([CH_PH, LPP_ANALOG_INPUT, 0x01])
        result = decode(truncated)
        assert "ph" not in result


class TestDatabaseWALMode:
    """Database uses WAL journal mode correctly."""

    def test_wal_mode_enabled(self, mock_hardware, tmp_path):
        from storage.database import WQM1Database

        db = WQM1Database(path=str(tmp_path / "test.db"))
        cur = db._conn.execute("PRAGMA journal_mode")
        mode = cur.fetchone()[0]
        assert mode == "wal"
        db.close()

    def test_busy_timeout_set(self, mock_hardware, tmp_path):
        from storage.database import WQM1Database

        db = WQM1Database(path=str(tmp_path / "test.db"))
        cur = db._conn.execute("PRAGMA busy_timeout")
        timeout = cur.fetchone()[0]
        assert timeout == 5000
        db.close()

    def test_rotate_deletes_only_synced_rows(self, mock_hardware, tmp_path):
        from storage.database import WQM1Database

        db = WQM1Database(path=str(tmp_path / "test.db"))
        for _ in range(10):
            db.insert_reading({"ph": 7.0})
        db.mark_synced([1, 2, 3, 4, 5])
        deleted = db.rotate(max_rows=7)
        assert deleted == 3
        assert db.get_count() == 7
        unsynced = db.get_count(synced=False)
        assert unsynced == 5
        db.close()

    def test_rotate_no_op_when_under_limit(self, mock_hardware, tmp_path):
        from storage.database import WQM1Database

        db = WQM1Database(path=str(tmp_path / "test.db"))
        db.insert_reading({"ph": 7.0})
        deleted = db.rotate(max_rows=100)
        assert deleted == 0
        db.close()

    def test_increment_fcnt_returns_incremented_value(self, mock_hardware, tmp_path):
        from storage.database import WQM1Database

        db = WQM1Database(path=str(tmp_path / "test.db"))
        v1 = db.increment_fcnt()
        v2 = db.increment_fcnt()
        assert v2 == v1 + 1
        db.close()


class TestSensorTemperatureCompensation:
    """Sensor temperature compensation at extreme ranges."""

    def test_ph_at_zero_celsius(self, mock_hardware):
        from sensors.ads1115 import ADS1115
        from sensors.ph import PHSensor

        adc = ADS1115()
        mock_hardware["bus"].read_i2c_block_data.side_effect = [
            [0x85, 0x83],
            [0x30, 0x00],
        ]
        ph = PHSensor(adc)
        ph.set_calibration(1.04, 1.50)
        result = ph.read(temp_c=0.0)
        assert result is not None
        assert 0.0 <= result <= 14.0

    def test_ph_at_50_celsius(self, mock_hardware):
        from sensors.ads1115 import ADS1115
        from sensors.ph import PHSensor

        adc = ADS1115()
        mock_hardware["bus"].read_i2c_block_data.side_effect = [
            [0x85, 0x83],
            [0x30, 0x00],
        ]
        ph = PHSensor(adc)
        ph.set_calibration(1.04, 1.50)
        result = ph.read(temp_c=50.0)
        assert result is not None
        assert 0.0 <= result <= 14.0

    def test_ph_none_temperature_defaults_to_25(self, mock_hardware):
        from sensors.ads1115 import ADS1115
        from sensors.ph import PHSensor

        adc = ADS1115()
        mock_hardware["bus"].read_i2c_block_data.side_effect = [
            [0x85, 0x83],
            [0x30, 0x00],
        ]
        ph = PHSensor(adc)
        result_none = ph.read(temp_c=None)
        assert result_none is not None

    def test_tds_negative_compensation_clamp(self, mock_hardware):
        from sensors.ads1115 import ADS1115
        from sensors.tds import TDSSensor

        adc = ADS1115()
        mock_hardware["bus"].read_i2c_block_data.side_effect = [
            [0x85, 0x83],
            [0x10, 0x00],
        ]
        tds = TDSSensor(adc)
        result = tds.read(temp_c=-100.0)
        assert result is not None
        assert result >= 0.0

    def test_turbidity_invalid_voltage_range(self, mock_hardware):
        from sensors.ads1115 import ADS1115
        from sensors.turbidity import TurbiditySensor

        adc = ADS1115()
        mock_hardware["bus"].read_i2c_block_data.side_effect = [
            [0x85, 0x83],
            [0x10, 0x00],
        ]
        turb = TurbiditySensor(adc)
        turb._v_clear = 0.1
        result = turb.read()
        assert result is None


class TestGPSChecksumEdgeCases:
    """GPS NMEA parsing edge cases."""

    def test_verify_checksum_no_dollar_sign(self, mock_hardware):
        from sensors.gps import _verify_checksum

        assert _verify_checksum("GPGGA,invalid") is False

    def test_verify_checksum_no_asterisk(self, mock_hardware):
        from sensors.gps import _verify_checksum

        assert _verify_checksum("$GPGGA,no,checksum") is False

    def test_verify_checksum_bad_hex(self, mock_hardware):
        from sensors.gps import _verify_checksum

        assert _verify_checksum("$GPGGA,data*ZZ") is False

    def test_parse_gga_too_few_fields(self, mock_hardware):
        from sensors.gps import _parse_gga

        assert _parse_gga("$GPGGA,1,2,3*00") is None

    def test_parse_gga_fix_quality_zero(self, mock_hardware):
        from sensors.gps import _parse_gga

        sentence = "$GPGGA,120000.00,3016.0000,N,09744.5800,W,0,08,1.2,150.0,M,,,,*XX"
        assert _parse_gga(sentence) is None

    def test_parse_gga_non_gga_sentence(self, mock_hardware):
        from sensors.gps import _parse_gga

        assert _parse_gga("$GPRMC,120000.00,A,3016.0000,N,09744.5800,W,0.0,0.0,210426,,*XX") is None


class TestLoRaWANCrypto:
    """LoRaWAN cryptographic operations."""

    def test_compute_mic_returns_4_bytes(self, mock_hardware):
        from radio.lorawan import _compute_mic

        key = bytes(16)
        data = b"test data"
        mic = _compute_mic(key, data)
        assert len(mic) == 4

    def test_encrypt_decrypt_roundtrip(self, mock_hardware):
        from radio.lorawan import _encrypt_payload

        key = bytes.fromhex("2B7E151628AED2A6ABF7158809CF4F3C")
        dev_addr = b"\x01\x02\x03\x04"
        plaintext = b"Hello LoRaWAN!"
        encrypted = _encrypt_payload(key, dev_addr, 42, plaintext, direction=0)
        decrypted = _encrypt_payload(key, dev_addr, 42, encrypted, direction=0)
        assert decrypted == plaintext

    def test_encrypt_empty_payload(self, mock_hardware):
        from radio.lorawan import _encrypt_payload

        result = _encrypt_payload(bytes(16), b"\x00" * 4, 0, b"", direction=0)
        assert result == b""

    def test_derive_key_produces_16_bytes(self, mock_hardware):
        from radio.lorawan import _derive_key

        key = _derive_key(bytes(16), 0x01, bytes(3), bytes(3), 0)
        assert len(key) == 16

    def test_derive_nwkskey_differs_from_appskey(self, mock_hardware):
        from radio.lorawan import _derive_key

        app_key = bytes(16)
        app_nonce = bytes(3)
        net_id = bytes(3)
        nwk = _derive_key(app_key, 0x01, app_nonce, net_id, 0)
        app = _derive_key(app_key, 0x02, app_nonce, net_id, 0)
        assert nwk != app

    def test_uplink_mic_includes_b0_block(self, mock_hardware):
        from radio.lorawan import _compute_uplink_mic

        key = bytes(16)
        dev_addr = b"\x01\x02\x03\x04"
        mic1 = _compute_uplink_mic(key, dev_addr, 0, b"\x40\x01\x02\x03\x04\x00\x00\x00\x01\xff")
        mic2 = _compute_uplink_mic(key, dev_addr, 1, b"\x40\x01\x02\x03\x04\x00\x00\x00\x01\xff")
        assert mic1 != mic2


class TestHealthReporterEdgeCases:
    """Health reporter boundary conditions."""

