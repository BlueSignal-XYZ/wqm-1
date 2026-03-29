"""Tests for firmware/src/radio/lorawan.py — LoRaWAN 1.0.3 MAC layer."""

from unittest.mock import MagicMock


class TestKeyDerivation:
    def test_derive_nwk_skey(self, mock_hardware):
        from radio.lorawan import _derive_key

        app_key = bytes(16)  # all zeros for test
        app_nonce = b"\x01\x02\x03"
        net_id = b"\x04\x05\x06"
        dev_nonce = 0x0708

        nwk_skey = _derive_key(app_key, 0x01, app_nonce, net_id, dev_nonce)
        assert len(nwk_skey) == 16
        assert isinstance(nwk_skey, bytes)

    def test_derive_app_skey_differs(self, mock_hardware):
        from radio.lorawan import _derive_key

        app_key = bytes(16)
        app_nonce = b"\x01\x02\x03"
        net_id = b"\x04\x05\x06"
        dev_nonce = 42

        nwk_skey = _derive_key(app_key, 0x01, app_nonce, net_id, dev_nonce)
        app_skey = _derive_key(app_key, 0x02, app_nonce, net_id, dev_nonce)
        assert nwk_skey != app_skey


class TestPayloadEncryption:
    def test_encrypt_decrypt_roundtrip(self, mock_hardware):
        from radio.lorawan import _encrypt_payload

        key = b"\x2b\x7e\x15\x16\x28\xae\xd2\xa6\xab\xf7\x15\x88\x09\xcf\x4f\x3c"
        dev_addr = b"\x01\x02\x03\x04"
        fcnt = 1
        plaintext = b"Hello LoRaWAN"

        encrypted = _encrypt_payload(key, dev_addr, fcnt, plaintext, direction=0)
        assert encrypted != plaintext
        assert len(encrypted) == len(plaintext)

        # Same function decrypts (XOR-based)
        decrypted = _encrypt_payload(key, dev_addr, fcnt, encrypted, direction=0)
        assert decrypted == plaintext

    def test_empty_payload(self, mock_hardware):
        from radio.lorawan import _encrypt_payload

        key = bytes(16)
        result = _encrypt_payload(key, b"\x00" * 4, 0, b"", direction=0)
        assert result == b""


class TestMIC:
    def test_mic_is_4_bytes(self, mock_hardware):
        from radio.lorawan import _compute_mic

        key = bytes(16)
        data = b"test data"
        mic = _compute_mic(key, data)
        assert len(mic) == 4

    def test_mic_deterministic(self, mock_hardware):
        from radio.lorawan import _compute_mic

        key = bytes(16)
        data = b"test data"
        assert _compute_mic(key, data) == _compute_mic(key, data)

    def test_mic_changes_with_data(self, mock_hardware):
        from radio.lorawan import _compute_mic

        key = bytes(16)
        mic1 = _compute_mic(key, b"data1")
        mic2 = _compute_mic(key, b"data2")
        assert mic1 != mic2

    def test_uplink_mic_format(self, mock_hardware):
        from radio.lorawan import _compute_uplink_mic

        nwk_skey = bytes(16)
        dev_addr = b"\x01\x02\x03\x04"
        frame = b"\x40\x01\x02\x03\x04\x00\x00\x00\x01\x00"  # sample frame
        mic = _compute_uplink_mic(nwk_skey, dev_addr, 0, frame)
        assert len(mic) == 4


class TestJoinRequest:
    def test_join_request_format(self, mock_hardware):
        """Test that join() builds a properly formatted JoinRequest."""
        from radio.lorawan import LoRaWANMAC

        mock_radio = MagicMock()
        mock_radio.send.return_value = True
        mock_radio.receive.return_value = None  # no JoinAccept

        dev_eui = bytes.fromhex("0018B20012345678")
        app_eui = bytes.fromhex("0000000000000000")
        app_key = bytes(16)

        mac = LoRaWANMAC(mock_radio, dev_eui, app_eui, app_key)
        mac.join(timeout_s=0.1)  # will fail (no JoinAccept) but TX should happen

        # Verify send was called with a JoinRequest frame
        assert mock_radio.send.called
        frame = mock_radio.send.call_args[0][0]
        assert frame[0] == 0x00  # MHDR = JoinRequest
        assert len(frame) == 23  # MHDR(1) + AppEUI(8) + DevEUI(8) + DevNonce(2) + MIC(4)


class TestSession:
    def test_session_defaults(self, mock_hardware):
        from radio.lorawan import LoRaWANSession

        s = LoRaWANSession()
        assert s.joined is False
        assert s.fcnt_up == 0

    def test_uplink_requires_join(self, mock_hardware):
        from radio.lorawan import LoRaWANMAC

        mock_radio = MagicMock()
        mac = LoRaWANMAC(mock_radio, bytes(8), bytes(8), bytes(16))
        result = mac.send_uplink(b"test")
        assert result is None  # not joined
