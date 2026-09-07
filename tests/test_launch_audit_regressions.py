"""Regressions for the pre-launch audit (docs/launch-audit.md).

Each test pins one thing the audit found the firmware could not do — join or
receive on a real US915 network, keep the clean end of the turbidity range,
survive an upgrade with its relay rules, or refuse a replayed downlink.
"""

import struct
import sys
from unittest.mock import MagicMock, patch

import pytest
from Crypto.Cipher import AES
from Crypto.Hash import CMAC

# --------------------------------------------------------------------------
# SX1262 driver
# --------------------------------------------------------------------------

_CMD_SET_DIO2_AS_RF_SWITCH_CTRL = 0x9D
_CMD_SET_PACKET_PARAMS = 0x8C
_CMD_SET_MODULATION_PARAMS = 0x8B
_CMD_SET_RF_FREQUENCY = 0x86
_CMD_CALIBRATE_IMAGE = 0x98


def _radio():
    sys.modules.setdefault("lgpio", MagicMock())
    from radio.sx1262 import SX1262

    radio = SX1262()
    radio._wait_busy = lambda timeout_s=1.0: True
    radio._tx_done_event = MagicMock()
    radio._tx_done_event.wait.return_value = True
    sent: list[tuple[int, list[int]]] = []
    radio._cmd = lambda op, args=None: sent.append((op, list(args or [])))
    radio._xfer = lambda data: list(data)
    radio._sent = sent
    return radio


def _freq_hz(args: list[int]) -> int:
    reg = (args[0] << 24) | (args[1] << 16) | (args[2] << 8) | args[3]
    return round(reg * 32_000_000 / (1 << 25))


class TestSx1262RfSwitchAndRx:
    def test_init_enables_dio2_rf_switch_and_image_calibration(self, mock_hardware):
        radio = _radio()
        radio.init()
        ops = [op for op, _ in radio._sent]
        assert _CMD_SET_DIO2_AS_RF_SWITCH_CTRL in ops, (
            "DIO2 must drive the LORA1262 antenna switch or nothing reaches the SMA"
        )
        assert [a for op, a in radio._sent if op == _CMD_SET_DIO2_AS_RF_SWITCH_CTRL] == [[0x01]]
        assert [a for op, a in radio._sent if op == _CMD_CALIBRATE_IMAGE] == [[0xE1, 0xE9]]

    def test_init_raises_when_busy_never_drops(self, mock_hardware):
        radio = _radio()
        radio._wait_busy = lambda timeout_s=1.0: False
        with pytest.raises(RuntimeError, match="BUSY"):
            radio.init()

    def test_receive_uses_inverted_iq_and_no_crc(self, mock_hardware):
        radio = _radio()
        radio._tx_done_event.wait.return_value = False  # window times out
        radio.receive(timeout_s=0.5)
        pkt = [a for op, a in radio._sent if op == _CMD_SET_PACKET_PARAMS]
        assert pkt, "receive() must set its own packet params"
        preamble_hi, preamble_lo, header, length, crc, iq = pkt[-1]
        assert header == 0x00 and length == 0xFF and crc == 0x00 and iq == 0x01

    def test_send_restores_uplink_config_after_rx_window(self, mock_hardware):
        radio = _radio()
        radio.set_tx_config(903_900_000, 9, 0x04)
        radio.set_rx_config(923_300_000, 12, 0x06)  # RX2 retunes the chip
        radio._sent.clear()
        assert radio.send(b"\x01\x02") is True
        freqs = [_freq_hz(a) for op, a in radio._sent if op == _CMD_SET_RF_FREQUENCY]
        mods = [a for op, a in radio._sent if op == _CMD_SET_MODULATION_PARAMS]
        assert freqs and abs(freqs[0] - 903_900_000) < 200
        assert mods and mods[0][0] == 9 and mods[0][1] == 0x04

    def test_ldro_follows_symbol_time_not_sf_alone(self, mock_hardware):
        radio = _radio()
        radio.set_rx_config(923_300_000, 12, 0x06)  # SF12 / 500 kHz: 8.2 ms symbols
        mods = [a for op, a in radio._sent if op == _CMD_SET_MODULATION_PARAMS]
        assert mods[-1] == [12, 0x06, 1, 0], "LDRO must be OFF for SF12/500k (US915 RX2)"
        radio.set_rx_config(903_900_000, 12, 0x04)  # SF12 / 125 kHz: 32.8 ms symbols
        mods = [a for op, a in radio._sent if op == _CMD_SET_MODULATION_PARAMS]
        assert mods[-1] == [12, 0x04, 1, 1]


# --------------------------------------------------------------------------
# ADS1115 per-channel PGA
# --------------------------------------------------------------------------


class TestAds1115TurbidityRange:
    def _config_word(self, mock_hardware) -> int:
        call = mock_hardware["bus"].write_i2c_block_data.call_args
        hi, lo = call[0][2]
        return (hi << 8) | lo

    def test_turbidity_channel_uses_6v144_pga(self, mock_hardware):
        mock_hardware["bus"].read_i2c_block_data.side_effect = [[0x80, 0x00], [0x55, 0x6B]]
        from sensors.ads1115 import ADS1115, channel_full_scale_v

        adc = ADS1115()
        v = adc.read_voltage(1)
        assert (self._config_word(mock_hardware) >> 9) & 0x07 == 0b000  # ±6.144 V
        assert channel_full_scale_v(1) == 6.144
        assert abs(v - 4.10) < 0.002, "4.1 V clear water must be representable, not clipped"

    def test_other_channels_keep_4v096_pga(self, mock_hardware):
        from sensors.ads1115 import ADS1115, channel_full_scale_v

        adc = ADS1115()
        for ch in (0, 2, 3):
            mock_hardware["bus"].read_i2c_block_data.side_effect = [[0x80, 0x00], [0x40, 0x00]]
            v = adc.read_voltage(ch)
            assert (self._config_word(mock_hardware) >> 9) & 0x07 == 0b001
            assert channel_full_scale_v(ch) == 4.096
            assert abs(v - 2.048) < 0.001

    def test_conversion_timeout_raises_instead_of_stale_value(self, mock_hardware):
        mock_hardware["bus"].read_i2c_block_data.side_effect = lambda a, reg, n: [0x00, 0x00]
        from sensors.ads1115 import ADS1115

        adc = ADS1115()
        with pytest.raises(RuntimeError, match="did not complete"):
            adc.read_raw(0)


# --------------------------------------------------------------------------
# LoRaWAN US915 MAC
# --------------------------------------------------------------------------

APP_KEY = b"\x2b\x7e\x15\x16\x28\xae\xd2\xa6\xab\xf7\x15\x88\x09\xcf\x4f\x3c"


def _cmac(key: bytes, data: bytes) -> bytes:
    c = CMAC.new(key, ciphermod=AES)
    c.update(data)
    return c.digest()[:4]


def _join_accept(
    app_key: bytes,
    dev_addr: bytes = b"\xaa\xbb\xcc\xdd",
    dl_settings: int = 0x00,
    rx_delay: int = 0,
    cf_list: bytes = b"",
    corrupt_mic: bool = False,
) -> bytes:
    body = b"\x01\x02\x03" + b"\x04\x05\x06" + dev_addr + bytes([dl_settings, rx_delay]) + cf_list
    mic = _cmac(app_key, bytes([0x20]) + body)
    if corrupt_mic:
        mic = bytes(b ^ 0xFF for b in mic)
    plain = body + mic
    cipher = AES.new(app_key, AES.MODE_ECB)
    padded = plain.ljust(((len(plain) + 15) // 16) * 16, b"\x00")
    enc = b"".join(cipher.decrypt(padded[i : i + 16]) for i in range(0, len(padded), 16))
    return bytes([0x20]) + enc[: len(plain)]


def _downlink(
    dev_addr: bytes,
    nwk_skey: bytes,
    app_skey: bytes,
    fcnt: int,
    fport: int | None,
    payload: bytes,
    fopts: bytes = b"",
    mhdr: int = 0x60,
) -> bytes:
    from radio.lorawan import _encrypt_payload

    frame = bytearray([mhdr]) + dev_addr + bytes([len(fopts)]) + struct.pack("<H", fcnt & 0xFFFF)
    frame += fopts
    if fport is not None:
        key = nwk_skey if fport == 0 else app_skey
        frame += bytes([fport]) + _encrypt_payload(key, dev_addr, fcnt, payload, direction=1)
    b0 = bytearray(16)
    b0[0] = 0x49
    b0[5] = 0x01
    b0[6:10] = dev_addr
    b0[10:14] = struct.pack("<I", fcnt)
    b0[15] = len(frame)
    return bytes(frame) + _cmac(nwk_skey, bytes(b0) + bytes(frame))


def _joined_mac(radio: MagicMock):
    from radio.lorawan import LoRaWANMAC, LoRaWANSession

    mac = LoRaWANMAC(radio, bytes(8), bytes(8), APP_KEY)
    mac.restore_session(
        LoRaWANSession(
            dev_addr=b"\x01\x02\x03\x04",
            nwk_skey=b"\x11" * 16,
            app_skey=b"\x22" * 16,
            fcnt_up=0,
            fcnt_down=0,
            joined=True,
        )
    )
    return mac


class TestUs915ChannelPlan:
    def test_helpers(self, mock_hardware):
        from radio.lorawan import (
            downlink_sf,
            rx1_data_rate,
            rx1_frequency_hz,
            sub_band_channel_mask,
            uplink_frequency_hz,
        )

        assert uplink_frequency_hz(0) == 902_300_000
        assert uplink_frequency_hz(8) == 903_900_000
        assert uplink_frequency_hz(63) == 914_900_000
        assert uplink_frequency_hz(65) == 904_600_000
        assert rx1_frequency_hz(8) == 923_300_000 and rx1_frequency_hz(15) == 927_500_000
        # RP002 US915 RX1DROffset table
        assert [rx1_data_rate(1, o) for o in range(4)] == [11, 10, 9, 8]
        assert [rx1_data_rate(4, o) for o in range(4)] == [13, 13, 12, 11]
        assert downlink_sf(8) == 12 and downlink_sf(13) == 7
        mask = sub_band_channel_mask(2)
        assert [i for i, on in enumerate(mask) if on] == [*range(8, 16), 65]

    def test_join_hops_inside_fsb2_and_opens_rx_on_500k(self, mock_hardware):
        from radio.lorawan import BW_500K, LoRaWANMAC

        radio = MagicMock()
        radio.send.return_value = True
        radio.receive.return_value = None
        mac = LoRaWANMAC(radio, bytes(8), bytes(8), APP_KEY)
        with patch("radio.lorawan.time"):
            assert mac.join() is False
        freq, sf, bw = radio.set_tx_config.call_args[0]
        assert freq in {903_900_000 + 200_000 * i for i in range(8)}, "not a FSB2 channel"
        assert sf == 9 and bw == 0x04
        rx1, rx2 = radio.set_rx_config.call_args_list
        assert rx1[0][0] == 923_300_000 + 600_000 * ((freq - 903_900_000) // 200_000)
        assert rx1[0][1] == 9 and rx1[0][2] == BW_500K  # DR11 = SF9 / 500 kHz
        assert rx2[0] == (923_300_000, 12, BW_500K)

    def test_uplink_hops_and_never_uses_915_000(self, mock_hardware):
        radio = MagicMock()
        radio.send.return_value = True
        radio.receive.return_value = None
        mac = _joined_mac(radio)
        seen = set()
        with patch("radio.lorawan.time"):
            for _ in range(40):
                mac.send_uplink(b"\x01")
                seen.add(radio.set_tx_config.call_args[0][0])
        assert 915_000_000 not in seen
        assert seen <= {903_900_000 + 200_000 * i for i in range(8)}
        assert len(seen) > 1, "uplinks must hop, not sit on one channel"


class TestJoinAcceptVerification:
    def test_bad_mic_is_rejected(self, mock_hardware):
        from radio.lorawan import LoRaWANMAC

        radio = MagicMock()
        radio.send.return_value = True
        radio.receive.return_value = _join_accept(APP_KEY, corrupt_mic=True)
        mac = LoRaWANMAC(radio, bytes(8), bytes(8), APP_KEY)
        with patch("radio.lorawan.time"):
            assert mac.join() is False
        assert mac.session.joined is False

    def test_rx_delay_dl_settings_and_cflist_are_applied(self, mock_hardware):
        from radio.lorawan import LoRaWANMAC

        # CFList type 1: enable channels 0-7 (ChMask0 = 0x00FF), 500 kHz ch 64.
        cf_list = struct.pack("<5H", 0x00FF, 0, 0, 0, 0x0001) + b"\x00" * 5 + b"\x01"
        accept = _join_accept(APP_KEY, dl_settings=0x10, rx_delay=5, cf_list=cf_list)
        radio = MagicMock()
        radio.send.return_value = True
        radio.receive.return_value = accept
        mac = LoRaWANMAC(radio, bytes(8), bytes(8), APP_KEY)
        with patch("radio.lorawan.time"):
            assert mac.join() is True
        assert mac.mac_params["rx1_delay_s"] == 5.0
        assert mac.mac_params["rx1_dr_offset"] == 1
        assert mac.enabled_channels == list(range(8))

    def test_mac_params_round_trip_through_restore(self, mock_hardware):
        from radio.lorawan import LoRaWANMAC, LoRaWANSession

        radio = MagicMock()
        mac = LoRaWANMAC(radio, bytes(8), bytes(8), APP_KEY)
        mac._rx1_delay_s = 5.0
        mac._channel_mask = [i in (16, 17) for i in range(72)]
        params = mac.mac_params
        mac2 = LoRaWANMAC(MagicMock(), bytes(8), bytes(8), APP_KEY)
        mac2.restore_session(LoRaWANSession(joined=True), params)
        assert mac2.mac_params == params
        assert mac2.enabled_channels == [16, 17]


class TestDownlinkVerification:
    def test_mic_mismatch_is_rejected(self, mock_hardware):
        radio = MagicMock()
        radio.send.return_value = True
        frame = _downlink(b"\x01\x02\x03\x04", b"\x99" * 16, b"\x22" * 16, 0, 100, b"\x01\x01")
        radio.receive.return_value = frame  # signed with the WRONG NwkSKey
        mac = _joined_mac(radio)
        with patch("radio.lorawan.time"):
            assert mac.send_uplink(b"\x01") is None
        assert mac.session.fcnt_down == 0

    def test_replayed_downlink_is_rejected_and_counter_advances(self, mock_hardware):
        radio = MagicMock()
        radio.send.return_value = True
        frame = _downlink(b"\x01\x02\x03\x04", b"\x11" * 16, b"\x22" * 16, 7, 100, b"\x01\x01")
        radio.receive.return_value = frame
        mac = _joined_mac(radio)
        with patch("radio.lorawan.time"):
            first = mac.send_uplink(b"\x01")
            assert first == b"\x64\x01\x01"
            assert mac.session.fcnt_down == 8
            replay = mac.send_uplink(b"\x01")
        assert replay is None, "the same relay-ON frame must not actuate twice"
        assert mac.session.fcnt_down == 8

    def test_link_adr_req_is_applied_and_answered_in_next_uplink(self, mock_hardware):
        radio = MagicMock()
        radio.send.return_value = True
        # LinkADRReq: DR2 / TXPower 0, ChMask 0x00FF with ChMaskCntl 0 (channels 0-7)
        fopts = bytes([0x03, 0x20]) + struct.pack("<H", 0x00FF) + bytes([0x00])
        frame = _downlink(b"\x01\x02\x03\x04", b"\x11" * 16, b"\x22" * 16, 0, None, b"", fopts)
        radio.receive.side_effect = [frame, None, None]
        mac = _joined_mac(radio)
        with patch("radio.lorawan.time"):
            mac.send_uplink(b"\x01")
            assert mac.spreading_factor == 8 and mac.enabled_channels == list(range(8))
            mac.send_uplink(b"\x01")
        sent = radio.send.call_args[0][0]
        fctrl = sent[5]
        assert fctrl & 0x0F == 2
        assert sent[8:10] == bytes([0x03, 0x07]), "LinkADRAns must accept all three fields"

    def test_confirmed_downlink_gets_ack(self, mock_hardware):
        radio = MagicMock()
        radio.send.return_value = True
        frame = _downlink(b"\x01\x02\x03\x04", b"\x11" * 16, b"\x22" * 16, 0, 1, b"\x00", mhdr=0xA0)
        radio.receive.side_effect = [frame, None, None]
        mac = _joined_mac(radio)
        with patch("radio.lorawan.time"):
            mac.send_uplink(b"\x01")
            mac.send_uplink(b"\x01")
        assert radio.send.call_args[0][0][5] & 0x20, (
            "FCtrl.ACK must be set after a confirmed downlink"
        )

    def test_payload_over_dr_limit_is_refused(self, mock_hardware):
        radio = MagicMock()
        radio.send.return_value = True
        mac = _joined_mac(radio)
        mac._sf = 10  # DR0: 11-byte limit
        with patch("radio.lorawan.time"):
            assert mac.send_uplink(b"\x00" * 43) is None
        radio.send.assert_not_called()

    def test_fcnt_persisted_before_transmit(self, mock_hardware):
        from radio.lorawan import LoRaWANMAC, LoRaWANSession

        radio = MagicMock()
        radio.receive.return_value = None
        seen: list[int] = []

        def persist():
            seen.append(mac.session.fcnt_up)
            radio.send.assert_not_called()

        mac = LoRaWANMAC(radio, bytes(8), bytes(8), APP_KEY, persist_hook=persist)
        mac.restore_session(LoRaWANSession(dev_addr=b"\x01\x02\x03\x04", joined=True))
        radio.send.return_value = True
        with patch("radio.lorawan.time"):
            mac.send_uplink(b"\x01")
        assert seen == [1]


# --------------------------------------------------------------------------
# Storage: MAC params ride with the session
# --------------------------------------------------------------------------


class TestSessionMacParams:
    def test_round_trip(self, tmp_path, mock_hardware):
        from storage.database import WQM1Database

        db = WQM1Database(str(tmp_path / "t.db"))
        params = {"rx1_delay_s": 5.0, "channel_mask": "00000000000000ff00"}
        db.save_session(b"\x01" * 4, b"\xaa" * 16, b"\xbb" * 16, 3, 1, True, mac_params=params)
        assert db.load_session()["mac_params"] == params
        db.save_session(b"\x01" * 4, b"\xaa" * 16, b"\xbb" * 16, 4, 1, True)
        assert db.load_session()["mac_params"] is None
        db.close()


# --------------------------------------------------------------------------
# Rules: schedule window in local time; policies path survives upgrades
# --------------------------------------------------------------------------


class TestRulesAndPolicies:
    def test_default_clock_is_local_and_tz_aware(self, mock_hardware):
        from control.rules import RulesEngine

        now = RulesEngine()._clock()
        assert now.tzinfo is not None
        assert now.utcoffset() is not None

    def test_policies_are_read_from_etc_first(self, mock_hardware):
        from main import WQM1App

        assert str(WQM1App._POLICIES_PATHS[0]) == "/etc/bluesignal/policies.yaml"


class TestServiceWindowCookies:
    def test_session_cookie_is_samesite_lax(self, tmp_path, mock_hardware):
        from service_window.app import create_app

        app = create_app({"CONFIG_PATH": str(tmp_path / "c.yaml"), "TESTING": True})
        assert app.config["SESSION_COOKIE_SAMESITE"] == "Lax"
        assert app.config["SESSION_COOKIE_HTTPONLY"] is True
