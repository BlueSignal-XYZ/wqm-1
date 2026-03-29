"""Tests for firmware/comms/lora_tx.py — Cayenne LPP encoding."""

import struct


class TestCayenneLPPEncoding:
    """Test Cayenne LPP binary encoding."""

    def test_encode_temperature(self, mock_hardware):
        from radio.cayenne import encode

        payload = encode({"temp_c": 22.5})
        # CH1(1) + TYPE_TEMP(1) + value(2) = 4 bytes
        assert len(payload) == 4
        assert payload[0] == 1  # channel
        assert payload[1] == 0x67  # temperature type
        # 22.5 * 10 = 225 = 0x00E1
        assert struct.unpack(">h", payload[2:4])[0] == 225

    def test_encode_ph(self, mock_hardware):
        from radio.cayenne import encode

        payload = encode({"ph": 7.25})
        assert len(payload) == 4
        assert payload[0] == 2  # channel
        assert payload[1] == 0x02  # analog input
        assert struct.unpack(">h", payload[2:4])[0] == 725

    def test_encode_gps(self, mock_hardware):
        from radio.cayenne import encode

        payload = encode({"lat": 30.267, "lon": -97.743, "alt_m": 150.0})
        # GPS: CH(1) + TYPE(1) + lat(3) + lon(3) + alt(3) = 11 bytes
        assert len(payload) == 11
        assert payload[0] == 6  # channel
        assert payload[1] == 0x88  # GPS type

    def test_encode_full_reading(self, mock_hardware):
        from radio.cayenne import encode

        data = {
            "temp_c": 22.5,
            "ph": 7.2,
            "tds_ppm": 450.0,
            "turbidity_ntu": 120.0,
            "orp_mv": 250.0,
            "lat": 30.267,
            "lon": -97.743,
            "alt_m": 150.0,
        }
        payload = encode(data)
        # 5 * 4 bytes (temp + 4 analog) + 11 bytes (GPS) = 31 bytes
        assert len(payload) == 31

    def test_encode_empty_data(self, mock_hardware):
        from radio.cayenne import encode

        payload = encode({})
        assert payload == b""

    def test_encode_none_values_skipped(self, mock_hardware):
        from radio.cayenne import encode

        payload = encode({"ph": None, "tds_ppm": None})
        assert payload == b""

    def test_encode_partial_gps_skipped(self, mock_hardware):
        from radio.cayenne import encode

        # Only lat, no lon — GPS should be skipped
        payload = encode({"lat": 30.0})
        assert payload == b""

    def test_tds_clamped_to_int16(self, mock_hardware):
        from radio.cayenne import encode

        # TDS 500 ppm * 100 = 50000, exceeds int16 max (32767)
        payload = encode({"tds_ppm": 500.0})
        assert len(payload) == 4
        val = struct.unpack(">h", payload[2:4])[0]
        assert val == 32767  # clamped


class TestInt24:
    def test_positive(self, mock_hardware):
        from radio.cayenne import _int24

        result = _int24(302670)
        assert len(result) == 3
        # Reconstruct
        val = (result[0] << 16) | (result[1] << 8) | result[2]
        assert val == 302670

    def test_negative(self, mock_hardware):
        from radio.cayenne import _int24

        result = _int24(-977430)
        assert len(result) == 3
        # Reconstruct as signed
        val = (result[0] << 16) | (result[1] << 8) | result[2]
        if val >= (1 << 23):
            val -= 1 << 24
        assert val == -977430
