"""Tests for LoRaWAN MAC layer — JoinAccept processing, downlink handling, RX windows."""

import struct
from unittest.mock import MagicMock, patch

from Crypto.Cipher import AES
from Crypto.Hash import CMAC


class TestJoinAcceptProcessing:
    """Tests for _process_join_accept and related join flow."""

    def _build_join_accept(self, app_key: bytes, app_nonce: bytes, net_id: bytes,
                           dev_addr: bytes) -> bytes:
        """Build a valid encrypted JoinAccept frame for testing."""
        # Plaintext: AppNonce(3) + NetID(3) + DevAddr(4) + DLSettings(1) + RxDelay(1) + MIC(4)
        plaintext = app_nonce + net_id + dev_addr + b"\x00\x00"  # DLSettings + RxDelay

        # Compute MIC over MHDR + plaintext
        mhdr = 0x20
        mic_input = bytes([mhdr]) + plaintext
        cobj = CMAC.new(app_key, ciphermod=AES)
        cobj.update(mic_input)
        mic = cobj.digest()[:4]
        plaintext_with_mic = plaintext + mic

        # Encrypt: LoRaWAN JoinAccept uses AES ECB encrypt (not decrypt) on plaintext
        cipher = AES.new(app_key, AES.MODE_ECB)
        padded_len = ((len(plaintext_with_mic) + 15) // 16) * 16
        padded = plaintext_with_mic.ljust(padded_len, b"\x00")
        encrypted = bytearray()
        for i in range(0, padded_len, 16):
            encrypted += cipher.decrypt(padded[i:i + 16])
        encrypted = bytes(encrypted[:len(plaintext_with_mic)])

        return bytes([mhdr]) + encrypted

    def test_successful_join_accept(self, mock_hardware):
        """Test full OTAA join with valid JoinAccept response."""
        from radio.lorawan import LoRaWANMAC

        app_key = b"\x2b\x7e\x15\x16\x28\xae\xd2\xa6\xab\xf7\x15\x88\x09\xcf\x4f\x3c"
        app_nonce = b"\x01\x02\x03"
        net_id = b"\x04\x05\x06"
        dev_addr = b"\xaa\xbb\xcc\xdd"

        join_accept = self._build_join_accept(app_key, app_nonce, net_id, dev_addr)

        mock_radio = MagicMock()
        mock_radio.send.return_value = True
        mock_radio.receive.return_value = join_accept

        dev_eui = bytes(8)
        app_eui = bytes(8)
        mac = LoRaWANMAC(mock_radio, dev_eui, app_eui, app_key)

        with patch("radio.lorawan.time"):
            result = mac.join(timeout_s=1.0)

        assert result is True
        assert mac.session.joined is True
        assert mac.session.dev_addr == dev_addr
        assert len(mac.session.nwk_skey) == 16
        assert len(mac.session.app_skey) == 16
        assert mac.session.fcnt_up == 0

    def test_join_accept_too_short(self, mock_hardware):
        """JoinAccept shorter than 17 bytes should be rejected."""
        from radio.lorawan import LoRaWANMAC

        mock_radio = MagicMock()
        mock_radio.send.return_value = True
        mock_radio.receive.return_value = b"\x20" + b"\x00" * 10  # too short

        mac = LoRaWANMAC(mock_radio, bytes(8), bytes(8), bytes(16))
        with patch("radio.lorawan.time"):
            result = mac.join(timeout_s=1.0)
        assert result is False

    def test_join_accept_wrong_mhdr(self, mock_hardware):
        """Frame with wrong MHDR type should be rejected."""
        from radio.lorawan import LoRaWANMAC

        mock_radio = MagicMock()
        mock_radio.send.return_value = True
        mock_radio.receive.return_value = b"\x40" + b"\x00" * 20  # wrong MHDR

        mac = LoRaWANMAC(mock_radio, bytes(8), bytes(8), bytes(16))
        with patch("radio.lorawan.time"):
            result = mac.join(timeout_s=1.0)
        assert result is False

    def test_join_tx_failure(self, mock_hardware):
        """Join should return False if TX fails."""
        from radio.lorawan import LoRaWANMAC

        mock_radio = MagicMock()
        mock_radio.send.return_value = False

        mac = LoRaWANMAC(mock_radio, bytes(8), bytes(8), bytes(16))
        result = mac.join(timeout_s=1.0)
        assert result is False
        assert mac.session.joined is False

    def test_join_no_accept_received(self, mock_hardware):
        """Join should return False if no JoinAccept received in RX windows."""
        from radio.lorawan import LoRaWANMAC

        mock_radio = MagicMock()
        mock_radio.send.return_value = True
        mock_radio.receive.return_value = None

        mac = LoRaWANMAC(mock_radio, bytes(8), bytes(8), bytes(16))
        with patch("radio.lorawan.time"):
            result = mac.join(timeout_s=1.0)
        assert result is False

    def test_join_accept_via_rx2(self, mock_hardware):
        """JoinAccept received in RX2 window should still be processed."""
        from radio.lorawan import LoRaWANMAC

        app_key = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
        join_accept = self._build_join_accept(app_key, b"\x01\x02\x03", b"\x04\x05\x06",
                                               b"\xaa\xbb\xcc\xdd")

        mock_radio = MagicMock()
        mock_radio.send.return_value = True
        # RX1 returns None, RX2 returns the JoinAccept
        mock_radio.receive.side_effect = [None, join_accept]

        mac = LoRaWANMAC(mock_radio, bytes(8), bytes(8), app_key)
        with patch("radio.lorawan.time"):
            result = mac.join(timeout_s=1.0)
        assert result is True
        assert mac.session.joined is True


class TestDownlinkProcessing:
    """Tests for downlink frame parsing and decryption."""

    def _build_downlink(self, dev_addr: bytes, nwk_skey: bytes, app_skey: bytes,
                        fcnt: int, fport: int, payload: bytes) -> bytes:
        """Build a valid encrypted downlink frame."""
        from radio.lorawan import _encrypt_payload

        enc_payload = _encrypt_payload(app_skey, dev_addr, fcnt, payload, direction=1)

        frame = bytearray()
        frame.append(0x60)  # MHDR: unconfirmed downlink
        frame += dev_addr
        frame.append(0x00)  # FCtrl
        frame += struct.pack("<H", fcnt & 0xFFFF)
        frame.append(fport)
        frame += enc_payload

        # Compute MIC (reusing uplink MIC fn with direction trick — not spec-correct
        # but sufficient for testing the parsing path)
        b0 = bytearray(16)
        b0[0] = 0x49
        b0[5] = 0x01  # direction = downlink
        b0[6:10] = dev_addr
        b0[10:14] = struct.pack("<I", fcnt)
        b0[15] = len(frame)
        cobj = CMAC.new(nwk_skey, ciphermod=AES)
        cobj.update(bytes(b0) + bytes(frame))
        mic = cobj.digest()[:4]
        frame += mic

        return bytes(frame)

    def test_downlink_after_uplink(self, mock_hardware):
        """Downlink received after uplink should be decrypted and returned."""
        from radio.lorawan import LoRaWANMAC, LoRaWANSession

        dev_addr = b"\x01\x02\x03\x04"
        nwk_skey = b"\x11" * 16
        app_skey = b"\x22" * 16

        downlink_frame = self._build_downlink(dev_addr, nwk_skey, app_skey,
                                               fcnt=0, fport=1, payload=b"\xAA\xBB")

        mock_radio = MagicMock()
        mock_radio.send.return_value = True
        mock_radio.receive.return_value = downlink_frame

        mac = LoRaWANMAC(mock_radio, bytes(8), bytes(8), bytes(16))
        mac.restore_session(LoRaWANSession(
            dev_addr=dev_addr, nwk_skey=nwk_skey, app_skey=app_skey,
            fcnt_up=0, joined=True,
        ))

        result = mac.send_uplink(b"test", fport=1)
        assert result is not None
        assert result[0] == 1  # fport prepended
        assert result[1:] == b"\xAA\xBB"

    def test_downlink_wrong_dev_addr_ignored(self, mock_hardware):
        """Downlink with wrong DevAddr should be ignored."""
        from radio.lorawan import LoRaWANMAC, LoRaWANSession

        dev_addr = b"\x01\x02\x03\x04"
        wrong_addr = b"\xff\xff\xff\xff"
        nwk_skey = b"\x11" * 16
        app_skey = b"\x22" * 16

        downlink = self._build_downlink(wrong_addr, nwk_skey, app_skey,
                                         fcnt=0, fport=1, payload=b"\xAA")

        mock_radio = MagicMock()
        mock_radio.send.return_value = True
        mock_radio.receive.return_value = downlink

        mac = LoRaWANMAC(mock_radio, bytes(8), bytes(8), bytes(16))
        mac.restore_session(LoRaWANSession(
            dev_addr=dev_addr, nwk_skey=nwk_skey, app_skey=app_skey,
            fcnt_up=0, joined=True,
        ))

        result = mac.send_uplink(b"test", fport=1)
        assert result is None

    def test_downlink_short_frame_ignored(self, mock_hardware):
        """Frames shorter than 12 bytes should be ignored."""
        from radio.lorawan import LoRaWANMAC, LoRaWANSession

        mock_radio = MagicMock()
        mock_radio.send.return_value = True
        mock_radio.receive.return_value = b"\x60" + b"\x00" * 5  # too short

        mac = LoRaWANMAC(mock_radio, bytes(8), bytes(8), bytes(16))
        mac.restore_session(LoRaWANSession(
            dev_addr=b"\x00" * 4, nwk_skey=bytes(16), app_skey=bytes(16),
            fcnt_up=0, joined=True,
        ))

        result = mac.send_uplink(b"test", fport=1)
        assert result is None

    def test_downlink_no_payload(self, mock_hardware):
        """Downlink with no FPort/payload (just header + MIC) returns None."""
        from radio.lorawan import LoRaWANMAC, LoRaWANSession

        dev_addr = b"\x01\x02\x03\x04"
        # Build frame with no payload: MHDR(1) + DevAddr(4) + FCtrl(1) + FCnt(2) + MIC(4) = 12
        frame = bytearray()
        frame.append(0x60)
        frame += dev_addr
        frame.append(0x00)  # FCtrl
        frame += struct.pack("<H", 0)  # FCnt
        frame += b"\x00" * 4  # MIC
        assert len(frame) == 12

        mock_radio = MagicMock()
        mock_radio.send.return_value = True
        mock_radio.receive.return_value = bytes(frame)

        mac = LoRaWANMAC(mock_radio, bytes(8), bytes(8), bytes(16))
        mac.restore_session(LoRaWANSession(
            dev_addr=dev_addr, nwk_skey=bytes(16), app_skey=bytes(16),
            fcnt_up=0, joined=True,
        ))

        result = mac.send_uplink(b"test", fport=1)
        assert result is None

    def test_downlink_rx2_fallback(self, mock_hardware):
        """If RX1 returns nothing, downlink from RX2 should be processed."""
        from radio.lorawan import LoRaWANMAC, LoRaWANSession

        dev_addr = b"\x01\x02\x03\x04"
        nwk_skey = b"\x11" * 16
        app_skey = b"\x22" * 16
        downlink = self._build_downlink(dev_addr, nwk_skey, app_skey,
                                         fcnt=0, fport=100, payload=b"\x01\x01")

        mock_radio = MagicMock()
        mock_radio.send.return_value = True
        # RX1 returns None, RX2 returns downlink
        mock_radio.receive.side_effect = [None, downlink]

        mac = LoRaWANMAC(mock_radio, bytes(8), bytes(8), bytes(16))
        mac.restore_session(LoRaWANSession(
            dev_addr=dev_addr, nwk_skey=nwk_skey, app_skey=app_skey,
            fcnt_up=0, joined=True,
        ))

        result = mac.send_uplink(b"test", fport=1)
        assert result is not None
        assert result[0] == 100  # fport

    def test_downlink_fport_0_uses_nwk_skey(self, mock_hardware):
        """Downlink on FPort 0 should use NwkSKey for decryption."""
        from radio.lorawan import LoRaWANMAC, LoRaWANSession, _encrypt_payload

        dev_addr = b"\x01\x02\x03\x04"
        nwk_skey = b"\x11" * 16
        app_skey = b"\x22" * 16

        # Build downlink on fport=0 (MAC commands use NwkSKey)
        payload = b"\x03\x00"  # LinkADRReq example
        enc_payload = _encrypt_payload(nwk_skey, dev_addr, 0, payload, direction=1)

        frame = bytearray()
        frame.append(0x60)
        frame += dev_addr
        frame.append(0x00)
        frame += struct.pack("<H", 0)
        frame.append(0)  # FPort 0
        frame += enc_payload

        b0 = bytearray(16)
        b0[0] = 0x49
        b0[5] = 0x01
        b0[6:10] = dev_addr
        b0[15] = len(frame)
        cobj = CMAC.new(nwk_skey, ciphermod=AES)
        cobj.update(bytes(b0) + bytes(frame))
        frame += cobj.digest()[:4]

        mock_radio = MagicMock()
        mock_radio.send.return_value = True
        mock_radio.receive.return_value = bytes(frame)

        mac = LoRaWANMAC(mock_radio, bytes(8), bytes(8), bytes(16))
        mac.restore_session(LoRaWANSession(
            dev_addr=dev_addr, nwk_skey=nwk_skey, app_skey=app_skey,
            fcnt_up=0, joined=True,
        ))

        result = mac.send_uplink(b"test", fport=1)
        assert result is not None
        assert result[0] == 0  # fport 0
        assert result[1:] == payload


class TestRXWindows:
    """Tests for RX window timing in _receive_join_accept and _receive_downlink."""

    def test_receive_join_accept_tries_both_windows(self, mock_hardware):
        """Should try RX1 then RX2 with proper radio reconfiguration."""
        from radio.lorawan import LoRaWANMAC

        mock_radio = MagicMock()
        mock_radio.send.return_value = True
        mock_radio.receive.return_value = None

        mac = LoRaWANMAC(mock_radio, bytes(8), bytes(8), bytes(16))
        with patch("radio.lorawan.time"):
            mac.join(timeout_s=1.0)

        # Should have called receive twice (RX1 and RX2)
        assert mock_radio.receive.call_count == 2
        # Should have called set_rx_config for RX2
        assert mock_radio.set_rx_config.called
