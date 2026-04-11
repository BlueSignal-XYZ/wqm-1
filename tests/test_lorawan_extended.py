"""Extended LoRaWAN tests — uplink frame building, session restore, downlink parsing."""

import struct
from unittest.mock import MagicMock


class TestUplinkFrame:
    def test_uplink_increments_fcnt(self, mock_hardware):
        from radio.lorawan import LoRaWANMAC, LoRaWANSession

        mock_radio = MagicMock()
        mock_radio.send.return_value = True
        mock_radio.receive.return_value = None

        mac = LoRaWANMAC(mock_radio, bytes(8), bytes(8), bytes(16))
        mac.restore_session(
            LoRaWANSession(
                dev_addr=b"\x01\x02\x03\x04",
                nwk_skey=bytes(16),
                app_skey=bytes(16),
                fcnt_up=0,
                joined=True,
            )
        )

        mac.send_uplink(b"test", fport=1)
        assert mac.session.fcnt_up == 1

        mac.send_uplink(b"test2", fport=1)
        assert mac.session.fcnt_up == 2

    def test_uplink_frame_structure(self, mock_hardware):
        """Verify the uplink frame has correct MHDR, DevAddr, FCtrl, FCnt, FPort."""
        from radio.lorawan import LoRaWANMAC, LoRaWANSession

        mock_radio = MagicMock()
        mock_radio.send.return_value = True
        mock_radio.receive.return_value = None

        dev_addr = b"\xAA\xBB\xCC\xDD"
        mac = LoRaWANMAC(mock_radio, bytes(8), bytes(8), bytes(16))
        mac.restore_session(
            LoRaWANSession(
                dev_addr=dev_addr,
                nwk_skey=bytes(16),
                app_skey=bytes(16),
                fcnt_up=5,
                joined=True,
            )
        )

        mac.send_uplink(b"\x01\x02\x03", fport=1)

        frame = mock_radio.send.call_args[0][0]
        assert frame[0] == 0x40  # MHDR: unconfirmed uplink
        assert frame[1:5] == dev_addr
        assert frame[5] == 0x00  # FCtrl: no ADR, no options
        assert struct.unpack("<H", frame[6:8])[0] == 5  # FCnt
        assert frame[8] == 1  # FPort

    def test_confirmed_uplink_mhdr(self, mock_hardware):
        from radio.lorawan import LoRaWANMAC, LoRaWANSession

        mock_radio = MagicMock()
        mock_radio.send.return_value = True
        mock_radio.receive.return_value = None

        mac = LoRaWANMAC(mock_radio, bytes(8), bytes(8), bytes(16))
        mac.restore_session(
            LoRaWANSession(
                dev_addr=b"\x01" * 4,
                nwk_skey=bytes(16),
                app_skey=bytes(16),
                joined=True,
            )
        )

        mac.send_uplink(b"test", fport=1, confirmed=True)
        frame = mock_radio.send.call_args[0][0]
        assert frame[0] == 0x80  # MHDR: confirmed uplink

    def test_uplink_tx_failure_returns_none(self, mock_hardware):
        from radio.lorawan import LoRaWANMAC, LoRaWANSession

        mock_radio = MagicMock()
        mock_radio.send.return_value = False  # TX failure

        mac = LoRaWANMAC(mock_radio, bytes(8), bytes(8), bytes(16))
        mac.restore_session(
            LoRaWANSession(
                dev_addr=b"\x01" * 4,
                nwk_skey=bytes(16),
                app_skey=bytes(16),
                joined=True,
            )
        )

        result = mac.send_uplink(b"test", fport=1)
        assert result is None


class TestSessionRestore:
    def test_restore_session_sets_state(self, mock_hardware):
        from radio.lorawan import LoRaWANMAC, LoRaWANSession

        mock_radio = MagicMock()
        mac = LoRaWANMAC(mock_radio, bytes(8), bytes(8), bytes(16))

        session = LoRaWANSession(
            dev_addr=b"\xDE\xAD\xBE\xEF",
            nwk_skey=b"\x11" * 16,
            app_skey=b"\x22" * 16,
            fcnt_up=100,
            fcnt_down=50,
            joined=True,
        )
        mac.restore_session(session)

        assert mac.session.dev_addr == b"\xDE\xAD\xBE\xEF"
        assert mac.session.fcnt_up == 100
        assert mac.session.joined is True


class TestPayloadEncryptionExtended:
    def test_multi_block_encrypt(self, mock_hardware):
        """Test encryption of payload larger than 16 bytes (multi-block)."""
        from radio.lorawan import _encrypt_payload

        key = b"\x2b\x7e\x15\x16\x28\xae\xd2\xa6\xab\xf7\x15\x88\x09\xcf\x4f\x3c"
        dev_addr = b"\x01\x02\x03\x04"
        fcnt = 1
        plaintext = b"A" * 32  # 2 AES blocks

        encrypted = _encrypt_payload(key, dev_addr, fcnt, plaintext, direction=0)
        assert len(encrypted) == 32
        assert encrypted != plaintext

        decrypted = _encrypt_payload(key, dev_addr, fcnt, encrypted, direction=0)
        assert decrypted == plaintext

    def test_direction_matters(self, mock_hardware):
        """Uplink and downlink encryption should differ for same payload."""
        from radio.lorawan import _encrypt_payload

        key = bytes(16)
        dev_addr = b"\x01\x02\x03\x04"
        plaintext = b"test"

        enc_up = _encrypt_payload(key, dev_addr, 0, plaintext, direction=0)
        enc_down = _encrypt_payload(key, dev_addr, 0, plaintext, direction=1)
        assert enc_up != enc_down


class TestMICExtended:
    def test_uplink_mic_changes_with_fcnt(self, mock_hardware):
        from radio.lorawan import _compute_uplink_mic

        key = bytes(16)
        dev_addr = b"\x01\x02\x03\x04"
        frame = b"\x40\x01\x02\x03\x04\x00\x00\x00\x01\x00"

        mic1 = _compute_uplink_mic(key, dev_addr, 0, frame)
        mic2 = _compute_uplink_mic(key, dev_addr, 1, frame)
        assert mic1 != mic2

    def test_uplink_mic_changes_with_key(self, mock_hardware):
        from radio.lorawan import _compute_uplink_mic

        dev_addr = b"\x01\x02\x03\x04"
        frame = b"\x40\x01\x02\x03\x04\x00\x00\x00\x01\x00"

        mic1 = _compute_uplink_mic(b"\x00" * 16, dev_addr, 0, frame)
        mic2 = _compute_uplink_mic(b"\xFF" * 16, dev_addr, 0, frame)
        assert mic1 != mic2
