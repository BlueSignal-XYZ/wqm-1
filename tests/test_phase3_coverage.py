"""Tests targeting remaining coverage gaps in non-omitted source modules."""

import struct
from unittest.mock import MagicMock, patch


class TestServiceWindowConfigLoading:
    """Cover app.py _load_sw_config YAML parsing branch (lines 69-70)."""

    def test_load_sw_config_with_existing_config(self, tmp_path):
        import yaml

        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({"service_window": {"port": 9090, "pin": "5678"}}))

        from service_window.app import _load_sw_config

        with patch("pathlib.Path", wraps=type(config_file)) as MockPath:
            MockPath.return_value = config_file
            result = _load_sw_config()

        assert result.get("port") == 9090
        assert result.get("pin") == "5678"

    def test_load_sw_config_with_empty_yaml(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("")

        from service_window.app import _load_sw_config

        with patch("pathlib.Path", wraps=type(config_file)) as MockPath:
            MockPath.return_value = config_file
            result = _load_sw_config()

        assert result == {}

    def test_load_sw_config_with_no_service_window_key(self, tmp_path):
        import yaml

        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({"other_key": "value"}))

        from service_window.app import _load_sw_config

        with patch("pathlib.Path", wraps=type(config_file)) as MockPath:
            MockPath.return_value = config_file
            result = _load_sw_config()

        assert result == {}


class TestHeartbeatException:
    """Cover led.py _heartbeat_loop exception branch (lines 97-98)."""

    def test_heartbeat_loop_exits_on_gpio_exception(self, mock_hardware):
        from control.led import StatusLEDs

        leds = StatusLEDs()
        leds._heartbeat_running = True

        call_count = 0
        original_output = mock_hardware["gpio"].output

        def output_side_effect(pin, state):
            nonlocal call_count
            call_count += 1
            if call_count >= 3:
                raise RuntimeError("GPIO error")

        mock_hardware["gpio"].output = MagicMock(side_effect=output_side_effect)

        with patch("control.led.time.sleep", return_value=None):
            leds._heartbeat_loop()

        assert call_count >= 3
        mock_hardware["gpio"].output = original_output


class TestLoRaWANEdgeCases:
    """Cover lorawan.py uncovered lines 165-166 and 288."""

    def test_process_join_accept_too_short(self):
        from radio.lorawan import LoRaWANMAC

        radio = MagicMock()
        mac = LoRaWANMAC(radio, b"\x00" * 8, b"\x00" * 8, b"\x00" * 16)

        # Frame shorter than minimum 17 bytes
        result = mac._process_join_accept(b"\x20" + b"\x00" * 10)
        assert result is False

    def test_process_join_accept_wrong_mhdr(self):
        from radio.lorawan import LoRaWANMAC

        radio = MagicMock()
        mac = LoRaWANMAC(radio, b"\x00" * 8, b"\x00" * 8, b"\x00" * 16)

        # Valid length but wrong MHDR (not JoinAccept)
        result = mac._process_join_accept(b"\x40" + b"\x00" * 20)
        assert result is False

    def test_process_downlink_unknown_mhdr(self):
        from radio.lorawan import LoRaWANMAC, LoRaWANSession

        radio = MagicMock()
        mac = LoRaWANMAC(radio, b"\x00" * 8, b"\x00" * 8, b"\x00" * 16)
        mac._session = LoRaWANSession(
            dev_addr=b"\x01\x02\x03\x04",
            nwk_skey=b"\x00" * 16,
            app_skey=b"\x00" * 16,
            joined=True,
        )

        # MHDR = 0x40 (unconfirmed uplink) — not a downlink type
        frame = bytes([0x40]) + b"\x00" * 15
        result = mac._process_downlink(frame)
        assert result is None

    def test_process_downlink_short_frame(self):
        from radio.lorawan import LoRaWANMAC, LoRaWANSession

        radio = MagicMock()
        mac = LoRaWANMAC(radio, b"\x00" * 8, b"\x00" * 8, b"\x00" * 16)
        mac._session = LoRaWANSession(joined=True)

        result = mac._process_downlink(b"\x60" + b"\x00" * 5)
        assert result is None

    def test_process_downlink_dev_addr_mismatch(self):
        from radio.lorawan import LoRaWANMAC, LoRaWANSession

        radio = MagicMock()
        mac = LoRaWANMAC(radio, b"\x00" * 8, b"\x00" * 8, b"\x00" * 16)
        mac._session = LoRaWANSession(
            dev_addr=b"\x01\x02\x03\x04",
            joined=True,
        )

        # Valid downlink MHDR (0x60) but wrong dev_addr
        frame = bytes([0x60]) + b"\xff\xff\xff\xff" + b"\x00" * 12
        result = mac._process_downlink(frame)
        assert result is None

    def test_process_downlink_no_payload(self):
        from radio.lorawan import LoRaWANMAC, LoRaWANSession

        radio = MagicMock()
        mac = LoRaWANMAC(radio, b"\x00" * 8, b"\x00" * 8, b"\x00" * 16)
        dev_addr = b"\x01\x02\x03\x04"
        mac._session = LoRaWANSession(
            dev_addr=dev_addr,
            nwk_skey=b"\x00" * 16,
            app_skey=b"\x00" * 16,
            joined=True,
        )

        # Downlink frame with no payload: MHDR(1) + DevAddr(4) + FCtrl(1) + FCnt(2) + MIC(4) = 12
        frame = bytearray()
        frame.append(0x60)  # MHDR: unconfirmed downlink
        frame += dev_addr
        frame.append(0x00)  # FCtrl
        frame += struct.pack("<H", 0)  # FCnt
        frame += b"\x00" * 4  # MIC
        result = mac._process_downlink(bytes(frame))
        assert result is None

    def test_send_uplink_not_joined(self):
        from radio.lorawan import LoRaWANMAC

        radio = MagicMock()
        mac = LoRaWANMAC(radio, b"\x00" * 8, b"\x00" * 8, b"\x00" * 16)
        result = mac.send_uplink(b"\x01\x02\x03")
        assert result is None


class TestCayenneBoundaryValues:
    """Test Cayenne LPP encoding with extreme boundary values."""

    def test_encode_max_temperature(self):
        from radio.cayenne import decode, encode

        data = {"temp_c": 3276.7}
        payload = encode(data)
        decoded = decode(payload)
        assert decoded["temp_c"] == 3276.7

    def test_encode_min_temperature(self):
        from radio.cayenne import decode, encode

        data = {"temp_c": -3276.8}
        payload = encode(data)
        decoded = decode(payload)
        assert decoded["temp_c"] == -3276.8

    def test_encode_clamps_oversized_analog(self):
        from radio.cayenne import decode, encode

        data = {"ph": 400.0}
        payload = encode(data)
        decoded = decode(payload)
        assert decoded["ph"] == 327.67

    def test_encode_negative_gps_coordinates(self):
        from radio.cayenne import decode, encode

        data = {"lat": -33.8688, "lon": 151.2093, "alt_m": 58.0}
        payload = encode(data)
        decoded = decode(payload)
        assert abs(decoded["lat"] - (-33.8688)) < 0.001
        assert abs(decoded["lon"] - 151.2093) < 0.001
        assert abs(decoded["alt_m"] - 58.0) < 0.02

    def test_decode_unknown_type_stops_parsing(self):
        from radio.cayenne import decode

        # Channel 1, type 0xFF (unknown) — parser should stop
        payload = bytes([0x01, 0xFF, 0x00, 0x00])
        result = decode(payload)
        assert result == {}

    def test_decode_truncated_payload(self):
        from radio.cayenne import decode

        # Channel + type but not enough data bytes
        payload = bytes([0x01, 0x67, 0x00])  # temperature needs 2 bytes, only 1
        result = decode(payload)
        assert result == {}


class TestDatabaseSessionEdgeCases:
    """Additional database coverage for session management."""

    def test_increment_fcnt_returns_incremented_value(self, tmp_path):
        from storage.database import WQM1Database

        db = WQM1Database(str(tmp_path / "test.db"))
        db.save_session(
            dev_addr=b"\x01\x02\x03\x04",
            nwk_skey=b"\x00" * 16,
            app_skey=b"\x00" * 16,
            fcnt_up=5,
            fcnt_down=0,
            joined=True,
        )
        result = db.increment_fcnt()
        assert result == 6
        result = db.increment_fcnt()
        assert result == 7
        db.close()

    def test_rotate_deletes_oldest_synced_rows(self, tmp_path):
        from storage.database import WQM1Database

        db = WQM1Database(str(tmp_path / "test.db"))
        for i in range(10):
            db.insert_reading({"ph": 7.0 + i * 0.1})

        # Mark first 5 as synced
        db.mark_synced([1, 2, 3, 4, 5])

        deleted = db.rotate(max_rows=7)
        assert deleted == 3
        assert db.get_count() == 7
        db.close()


class TestPHSensorCompensation:
    """Additional pH sensor temperature compensation tests."""

    def test_nernst_slope_varies_with_temperature(self):
        from sensors.ph import _nernst_slope

        slope_25 = _nernst_slope(25.0)
        slope_0 = _nernst_slope(0.0)
        slope_50 = _nernst_slope(50.0)

        assert slope_0 < slope_25 < slope_50

    def test_ph_read_with_extreme_cold_temperature(self, mock_hardware):
        from sensors.ads1115 import ADS1115
        from sensors.ph import PHSensor

        # read_voltage is stubbed directly rather than driven through the I2C
        # register mock: the side_effect list above was misaligned, so the
        # config word (0x8583) was consumed as the conversion result and the
        # sensor actually saw -3.920 V. The old clamp laundered that into a
        # valid-looking pH, which is why this passed. This test is about
        # temperature compensation, not ADS1115 register decoding.
        adc = ADS1115()
        adc.read_voltage = MagicMock(return_value=1.536)  # mid-scale, valid
        ph = PHSensor(adc)
        ph.set_calibration(1.04, 1.50)

        result = ph.read(temp_c=2.0)
        assert result is not None
        assert 0.0 <= result <= 14.0


class TestTDSSensorEdgeCases:
    """TDS sensor edge case tests."""

    def test_tds_with_very_cold_temperature(self, mock_hardware):
        from sensors.ads1115 import ADS1115
        from sensors.tds import TDSSensor

        # Stubbed for the same reason as the pH case above — the register
        # mock delivered -3.920 V, not the intended 0x1000 conversion.
        adc = ADS1115()
        adc.read_voltage = MagicMock(return_value=0.512)  # valid mid-scale
        tds = TDSSensor(adc)

        result = tds.read(temp_c=-10.0)
        assert result is not None
        assert result >= 0.0

    def test_tds_with_none_temperature(self, mock_hardware):
        from sensors.ads1115 import ADS1115
        from sensors.tds import TDSSensor

        adc = ADS1115()
        adc.read_voltage = MagicMock(return_value=0.512)  # valid mid-scale
        tds = TDSSensor(adc)

        result = tds.read(temp_c=None)
        assert result is not None


class TestTurbidityEdgeCases:
    """Turbidity sensor edge case tests."""

    def test_turbidity_at_clear_water_voltage(self, mock_hardware):
        from sensors.ads1115 import ADS1115
        from sensors.turbidity import TurbiditySensor

        # ADS1115.__init__ uses read_word_data (not read_i2c_block_data)
        # read_raw calls: 1) OS poll via read_i2c_block_data, 2) conversion result
        raw = 32767  # max positive signed 16-bit → ~4.096V
        high = (raw >> 8) & 0xFF
        low = raw & 0xFF

        adc = ADS1115()

        mock_hardware["bus"].read_i2c_block_data.side_effect = [
            [0x85, 0x83],  # OS poll (bit 7 set = conversion done)
            [high, low],  # conversion result: ~4.096V
        ]

        turb = TurbiditySensor(adc)
        turb._v_clear = 4.096  # match voltage from ADC

        result = turb.read()
        assert result is not None
        assert result <= 0.2

    def test_turbidity_invalid_voltage_range(self, mock_hardware):
        from sensors.ads1115 import ADS1115
        from sensors.turbidity import TurbiditySensor

        mock_hardware["bus"].read_i2c_block_data.side_effect = [
            [0x85, 0x83],
            [0x85, 0x83],
            [0x00, 0x00],
        ]
        adc = ADS1115()
        turb = TurbiditySensor(adc)
        turb._v_clear = 0.3  # set below TURB_V_MAX (0.5)

        result = turb.read()
        assert result is None


class TestORPSensorCalibration:
    """ORP sensor calibration tests."""

    def test_orp_offset_calibration(self, mock_hardware):
        from sensors.ads1115 import ADS1115
        from sensors.orp import ORPSensor

        mock_hardware["bus"].read_i2c_block_data.side_effect = [
            [0x85, 0x83],
        ]
        adc = ADS1115()
        orp = ORPSensor(adc)

        orp.set_offset(known_mv=225.0, measured_mv=200.0)
        assert orp._offset_mv == 25.0
        assert orp._window == []
