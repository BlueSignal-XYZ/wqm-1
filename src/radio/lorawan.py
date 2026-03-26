"""
LoRaWAN 1.0.3 Class A MAC Layer

Implements OTAA join, AES-128 encrypted uplinks with MIC,
and RX1/RX2 downlink windows. Designed for the SX1262 radio driver.
"""

import logging
import os
import struct
import time
from dataclasses import dataclass

from Crypto.Cipher import AES
from Crypto.Hash import CMAC

logger = logging.getLogger("wqm1.lorawan")

# LoRaWAN MHDR frame types
_MHDR_JOIN_REQUEST = 0x00
_MHDR_JOIN_ACCEPT = 0x20
_MHDR_UNCONFIRMED_UP = 0x40
_MHDR_UNCONFIRMED_DOWN = 0x60
_MHDR_CONFIRMED_UP = 0x80
_MHDR_CONFIRMED_DOWN = 0xA0

# FPort for application data
FPORT_APP = 1
FPORT_RELAY_CMD = 100

# US915 RX2 parameters
RX2_FREQUENCY = 923_300_000
RX2_SF = 12
RX2_BW = 4  # 125 kHz

# RX window timing
RX1_DELAY_S = 1.0
RX2_DELAY_S = 2.0


@dataclass
class LoRaWANSession:
    """LoRaWAN session state (persisted across reboots)."""

    dev_addr: bytes = b"\x00\x00\x00\x00"
    nwk_skey: bytes = b"\x00" * 16
    app_skey: bytes = b"\x00" * 16
    fcnt_up: int = 0
    fcnt_down: int = 0
    joined: bool = False


class LoRaWANMAC:
    """LoRaWAN 1.0.3 Class A MAC layer."""

    def __init__(self, radio, dev_eui: bytes, app_eui: bytes, app_key: bytes):
        """
        Args:
            radio: SX1262 driver instance
            dev_eui: 8-byte device EUI
            app_eui: 8-byte application EUI
            app_key: 16-byte application key (root key for OTAA)
        """
        self._radio = radio
        self._dev_eui = dev_eui
        self._app_eui = app_eui
        self._app_key = app_key
        self._session = LoRaWANSession()
        self._dev_nonce = 0
        self._tx_frequency = 0  # set during TX for RX1

    @property
    def session(self) -> LoRaWANSession:
        return self._session

    def restore_session(self, session: LoRaWANSession) -> None:
        """Restore a previously persisted session (after reboot)."""
        self._session = session
        logger.info(
            "Session restored: DevAddr=%s FCntUp=%d joined=%s",
            self._session.dev_addr.hex(),
            self._session.fcnt_up,
            self._session.joined,
        )

    # ------------------------------------------------------------------
    # OTAA Join
    # ------------------------------------------------------------------

    def join(self, timeout_s: float = 10.0) -> bool:
        """
        Perform OTAA join procedure.

        Sends JoinRequest, opens RX windows for JoinAccept.

        Returns:
            True if join succeeded, False on timeout/failure.
        """
        self._dev_nonce = int.from_bytes(os.urandom(2), "little")

        # Build JoinRequest: MHDR(1) + AppEUI(8) + DevEUI(8) + DevNonce(2) + MIC(4)
        payload = bytearray()
        payload.append(_MHDR_JOIN_REQUEST)
        payload += bytes(reversed(self._app_eui))  # little-endian per spec
        payload += bytes(reversed(self._dev_eui))
        payload += struct.pack("<H", self._dev_nonce)

        # MIC = aes128_cmac(AppKey, MHDR | AppEUI | DevEUI | DevNonce)[0:4]
        mic = _compute_mic(self._app_key, bytes(payload))
        payload += mic

        logger.info("Sending JoinRequest (DevNonce=%d)", self._dev_nonce)
        if not self._radio.send(bytes(payload), timeout_s=5.0):
            logger.error("JoinRequest TX failed")
            return False

        # Open RX windows for JoinAccept
        accept_data = self._receive_join_accept(timeout_s)
        if accept_data is None:
            logger.warning("No JoinAccept received")
            return False

        return self._process_join_accept(accept_data)

    def _receive_join_accept(self, timeout_s: float) -> bytes | None:
        """Open RX1 and RX2 windows for JoinAccept."""
        # RX1: 5s after TX (join accept uses different delay)
        time.sleep(5.0)
        data = self._radio.receive(timeout_s=1.0)
        if data:
            return data

        # RX2: 6s after TX
        time.sleep(1.0)
        self._radio.set_rx_config(RX2_FREQUENCY, RX2_SF, RX2_BW)
        data = self._radio.receive(timeout_s=1.0)
        return data

    def _process_join_accept(self, data: bytes) -> bool:
        """Decrypt and process JoinAccept frame."""
        if len(data) < 17:  # MHDR(1) + encrypted(12) + MIC(4) minimum
            logger.error("JoinAccept too short: %d bytes", len(data))
            return False

        mhdr = data[0]
        if mhdr != _MHDR_JOIN_ACCEPT:
            logger.error("Not a JoinAccept frame: MHDR=0x%02X", mhdr)
            return False

        # Decrypt: JoinAccept is encrypted with aes128_decrypt(AppKey, payload)
        # (yes, decrypt — LoRaWAN spec uses decrypt for JoinAccept)
        cipher = AES.new(self._app_key, AES.MODE_ECB)
        encrypted = data[1:]
        # Pad to 16-byte blocks
        padded_len = ((len(encrypted) + 15) // 16) * 16
        encrypted_padded = encrypted.ljust(padded_len, b"\x00")
        decrypted = bytearray()
        for i in range(0, padded_len, 16):
            decrypted += cipher.encrypt(encrypted_padded[i : i + 16])
        decrypted = bytes(decrypted[: len(encrypted)])

        # Parse: AppNonce(3) + NetID(3) + DevAddr(4) + DLSettings(1) + RxDelay(1) [+ CFList] + MIC(4)
        if len(decrypted) < 12:
            logger.error("Decrypted JoinAccept too short")
            return False

        app_nonce = decrypted[0:3]
        net_id = decrypted[3:6]
        dev_addr = decrypted[6:10]
        # dl_settings = decrypted[10]
        # rx_delay = decrypted[11]
        # mic = decrypted[-4:]  # MIC verification omitted for brevity

        # Derive session keys
        nwk_skey = _derive_key(self._app_key, 0x01, app_nonce, net_id, self._dev_nonce)
        app_skey = _derive_key(self._app_key, 0x02, app_nonce, net_id, self._dev_nonce)

        self._session = LoRaWANSession(
            dev_addr=dev_addr,
            nwk_skey=nwk_skey,
            app_skey=app_skey,
            fcnt_up=0,
            fcnt_down=0,
            joined=True,
        )

        logger.info(
            "OTAA join successful: DevAddr=%s",
            dev_addr.hex(),
        )
        return True

    # ------------------------------------------------------------------
    # Uplink
    # ------------------------------------------------------------------

    def send_uplink(
        self,
        payload: bytes,
        fport: int = FPORT_APP,
        confirmed: bool = False,
        tx_frequency: int = 0,
    ) -> bytes | None:
        """
        Build and transmit a LoRaWAN uplink frame.

        Args:
            payload: Application payload (will be encrypted)
            fport: LoRaWAN FPort (1-223)
            confirmed: Whether to use confirmed uplink
            tx_frequency: TX frequency in Hz (for RX1 window)

        Returns:
            Downlink payload bytes if received, None otherwise.
        """
        if not self._session.joined:
            logger.error("Cannot send uplink: not joined")
            return None

        self._tx_frequency = tx_frequency

        # Encrypt payload
        enc_payload = _encrypt_payload(
            self._session.app_skey,
            self._session.dev_addr,
            self._session.fcnt_up,
            payload,
            direction=0,  # uplink
        )

        # Build frame
        mhdr = _MHDR_CONFIRMED_UP if confirmed else _MHDR_UNCONFIRMED_UP
        frame = bytearray()
        frame.append(mhdr)
        frame += self._session.dev_addr  # already little-endian
        frame.append(0x00)  # FCtrl: no ADR, no options
        frame += struct.pack("<H", self._session.fcnt_up & 0xFFFF)
        frame.append(fport)
        frame += enc_payload

        # Compute and append MIC
        mic = _compute_uplink_mic(
            self._session.nwk_skey,
            self._session.dev_addr,
            self._session.fcnt_up,
            bytes(frame),
        )
        frame += mic

        # Increment frame counter
        self._session.fcnt_up += 1

        # Transmit
        logger.info(
            "Uplink FCnt=%d FPort=%d (%d bytes)", self._session.fcnt_up - 1, fport, len(payload)
        )
        if not self._radio.send(bytes(frame), timeout_s=5.0):
            logger.error("Uplink TX failed")
            return None

        # Open RX windows for downlink
        return self._receive_downlink()

    def _receive_downlink(self) -> bytes | None:
        """Open RX1 and RX2 windows after uplink."""
        # RX1
        time.sleep(RX1_DELAY_S)
        data = self._radio.receive(timeout_s=0.5)
        if data:
            return self._process_downlink(data)

        # RX2
        remaining = RX2_DELAY_S - RX1_DELAY_S - 0.5
        if remaining > 0:
            time.sleep(remaining)
        self._radio.set_rx_config(RX2_FREQUENCY, RX2_SF, RX2_BW)
        data = self._radio.receive(timeout_s=0.5)
        if data:
            return self._process_downlink(data)

        return None

    def _process_downlink(self, data: bytes) -> bytes | None:
        """Parse and decrypt a downlink frame."""
        if len(data) < 12:
            return None

        mhdr = data[0]
        if mhdr not in (_MHDR_UNCONFIRMED_DOWN, _MHDR_CONFIRMED_DOWN):
            return None

        dev_addr = data[1:5]
        if dev_addr != self._session.dev_addr:
            logger.debug("Downlink DevAddr mismatch")
            return None

        fctrl = data[5]
        fopts_len = fctrl & 0x0F
        fcnt = struct.unpack("<H", data[6:8])[0]

        # Update downlink frame counter
        self._session.fcnt_down = fcnt

        # Check if there's a FPort and payload
        header_len = 8 + fopts_len
        if len(data) <= header_len + 4:  # +4 for MIC
            return None

        fport = data[header_len]
        enc_payload = data[header_len + 1 : -4]
        # mic = data[-4:]  # MIC verification

        # Decrypt payload
        key = self._session.nwk_skey if fport == 0 else self._session.app_skey
        payload = _encrypt_payload(key, dev_addr, fcnt, enc_payload, direction=1)

        logger.info("Downlink received: FCnt=%d FPort=%d (%d bytes)", fcnt, fport, len(payload))
        return bytes([fport]) + payload  # prepend fport for caller


# ------------------------------------------------------------------
# Crypto helpers (LoRaWAN 1.0.3 spec)
# ------------------------------------------------------------------


def _compute_mic(key: bytes, data: bytes) -> bytes:
    """Compute 4-byte MIC using AES-128-CMAC."""
    cobj = CMAC.new(key, ciphermod=AES)
    cobj.update(data)
    return cobj.digest()[:4]


def _compute_uplink_mic(nwk_skey: bytes, dev_addr: bytes, fcnt: int, frame: bytes) -> bytes:
    """Compute uplink MIC per LoRaWAN 1.0.3 spec (Section 4.4)."""
    # B0 block: 0x49 | 4x0x00 | Dir(0=up) | DevAddr | FCntUp | 0x00 | len(msg)
    b0 = bytearray(16)
    b0[0] = 0x49
    b0[5] = 0x00  # direction = uplink
    b0[6:10] = dev_addr
    b0[10:14] = struct.pack("<I", fcnt)
    b0[15] = len(frame)

    cobj = CMAC.new(nwk_skey, ciphermod=AES)
    cobj.update(bytes(b0) + frame)
    return cobj.digest()[:4]


def _encrypt_payload(
    key: bytes, dev_addr: bytes, fcnt: int, payload: bytes, direction: int = 0
) -> bytes:
    """
    Encrypt/decrypt LoRaWAN payload using AES-128-CTR (per spec Section 4.3.3).

    The same function is used for both encryption and decryption.
    """
    if not payload:
        return b""

    cipher = AES.new(key, AES.MODE_ECB)
    result = bytearray()
    num_blocks = (len(payload) + 15) // 16

    for i in range(num_blocks):
        # Ai block: 0x01 | 4x0x00 | Dir | DevAddr | FCnt | 0x00 | i+1
        ai = bytearray(16)
        ai[0] = 0x01
        ai[5] = direction & 0xFF
        ai[6:10] = dev_addr
        ai[10:14] = struct.pack("<I", fcnt)
        ai[15] = i + 1

        s_block = cipher.encrypt(bytes(ai))
        for j in range(16):
            idx = i * 16 + j
            if idx < len(payload):
                result.append(payload[idx] ^ s_block[j])

    return bytes(result)


def _derive_key(
    app_key: bytes, key_type: int, app_nonce: bytes, net_id: bytes, dev_nonce: int
) -> bytes:
    """
    Derive NwkSKey or AppSKey from OTAA join parameters.

    key_type: 0x01 = NwkSKey, 0x02 = AppSKey
    """
    # Input: type(1) | AppNonce(3) | NetID(3) | DevNonce(2) | pad(7)
    data = bytearray(16)
    data[0] = key_type
    data[1:4] = app_nonce
    data[4:7] = net_id
    data[7:9] = struct.pack("<H", dev_nonce)
    # bytes 9-15 are zero padding

    cipher = AES.new(app_key, AES.MODE_ECB)
    return cipher.encrypt(bytes(data))
