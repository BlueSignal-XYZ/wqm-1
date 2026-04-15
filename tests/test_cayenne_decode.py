"""Tests for Cayenne LPP decode and encode/decode roundtrip."""


class TestCayenneLPPDecode:
    """Test Cayenne LPP binary decoding."""

    def test_decode_temperature(self, mock_hardware):
        from radio.cayenne import decode, encode

        payload = encode({"temp_c": 22.5})
        result = decode(payload)
        assert abs(result["temp_c"] - 22.5) < 0.1

    def test_decode_ph(self, mock_hardware):
        from radio.cayenne import decode, encode

        payload = encode({"ph": 7.25})
        result = decode(payload)
        assert abs(result["ph"] - 7.25) < 0.01

    def test_decode_tds(self, mock_hardware):
        from radio.cayenne import decode, encode

        # Use a value that fits in int16 (max 327.67 with 0.01 resolution)
        payload = encode({"tds_ppm": 150.0})
        result = decode(payload)
        assert abs(result["tds_ppm"] - 150.0) < 0.01

    def test_decode_turbidity(self, mock_hardware):
        from radio.cayenne import decode, encode

        payload = encode({"turbidity_ntu": 120.0})
        result = decode(payload)
        assert abs(result["turbidity_ntu"] - 120.0) < 0.01

    def test_decode_orp(self, mock_hardware):
        from radio.cayenne import decode, encode

        payload = encode({"orp_mv": 2.50})
        result = decode(payload)
        assert abs(result["orp_mv"] - 2.50) < 0.01

    def test_decode_gps(self, mock_hardware):
        from radio.cayenne import decode, encode

        payload = encode({"lat": 30.267, "lon": -97.743, "alt_m": 150.0})
        result = decode(payload)
        assert abs(result["lat"] - 30.267) < 0.001
        assert abs(result["lon"] - (-97.743)) < 0.001
        assert abs(result["alt_m"] - 150.0) < 0.1

    def test_decode_full_reading_roundtrip(self, mock_hardware):
        from radio.cayenne import decode, encode

        original = {
            "temp_c": 22.5,
            "ph": 7.20,
            "tds_ppm": 100.0,
            "turbidity_ntu": 120.0,
            "orp_mv": 2.50,
            "lat": 30.267,
            "lon": -97.743,
            "alt_m": 150.0,
        }
        payload = encode(original)
        decoded = decode(payload)

        assert abs(decoded["temp_c"] - 22.5) < 0.1
        assert abs(decoded["ph"] - 7.20) < 0.01
        assert abs(decoded["tds_ppm"] - 100.0) < 0.01
        assert abs(decoded["turbidity_ntu"] - 120.0) < 0.01
        assert abs(decoded["orp_mv"] - 2.50) < 0.01
        assert abs(decoded["lat"] - 30.267) < 0.001
        assert abs(decoded["lon"] - (-97.743)) < 0.001

    def test_decode_empty_payload(self, mock_hardware):
        from radio.cayenne import decode

        assert decode(b"") == {}

    def test_decode_negative_temperature(self, mock_hardware):
        from radio.cayenne import decode, encode

        payload = encode({"temp_c": -5.3})
        result = decode(payload)
        assert abs(result["temp_c"] - (-5.3)) < 0.1

    def test_decode_unknown_type_stops_parsing(self, mock_hardware):
        from radio.cayenne import decode

        # Channel 99, unknown type 0xFF, then valid data that should not be parsed
        payload = bytes([99, 0xFF, 0x00, 0x00])
        result = decode(payload)
        assert result == {}

    def test_decode_negative_gps(self, mock_hardware):
        from radio.cayenne import decode, encode

        payload = encode({"lat": -33.8688, "lon": 151.2093, "alt_m": 5.0})
        result = decode(payload)
        assert result["lat"] < 0
        assert result["lon"] > 0


class TestCayenneLPPEncodeClamp:
    """Test that out-of-range values are clamped, not allowed to overflow struct."""

    def test_extreme_ph_does_not_crash(self, mock_hardware):
        # pH 14 → 1400, well within int16 (±32767), no clamp needed
        # but the guard should still allow unusual values without raising
        from radio.cayenne import decode, encode

        payload = encode({"ph": 14.0})
        assert len(payload) == 4  # 1 channel + 1 type + 2 bytes
        assert decode(payload)["ph"] == 14.0

    def test_extreme_temperature_is_clamped(self, mock_hardware):
        # 4000°C → 40000 (would overflow int16). Clamp should keep it packable.
        from radio.cayenne import encode

        payload = encode({"temp_c": 4000.0})
        # Must not raise; length still 4 bytes for the temp field
        assert len(payload) == 4

    def test_extreme_negative_temperature_is_clamped(self, mock_hardware):
        from radio.cayenne import encode

        payload = encode({"temp_c": -5000.0})
        assert len(payload) == 4

    def test_large_tds_is_clamped(self, mock_hardware):
        from radio.cayenne import encode

        # 1,000,000 ppm × 100 resolution overflows int16 but must encode
        payload = encode({"tds_ppm": 1_000_000.0})
        assert len(payload) == 4

    def test_encode_skips_none_values(self, mock_hardware):
        from radio.cayenne import encode

        payload = encode({"temp_c": None, "ph": 7.0, "tds_ppm": None})
        # Only pH field; no temp or TDS entries
        assert len(payload) == 4

    def test_encode_empty_dict_returns_empty_bytes(self, mock_hardware):
        from radio.cayenne import encode

        assert encode({}) == b""

    def test_encode_missing_altitude_defaults_to_zero(self, mock_hardware):
        from radio.cayenne import decode, encode

        payload = encode({"lat": 1.0, "lon": 2.0})  # no alt_m
        result = decode(payload)
        assert abs(result["alt_m"]) < 0.01

    def test_encode_gps_requires_both_lat_and_lon(self, mock_hardware):
        from radio.cayenne import encode

        # lat without lon should skip GPS entirely
        payload = encode({"lat": 30.0})
        assert len(payload) == 0
        payload = encode({"lon": -100.0})
        assert len(payload) == 0
