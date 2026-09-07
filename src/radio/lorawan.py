"""
LoRaWAN 1.0.3 Class A MAC Layer — US915

Implements OTAA join, AES-128 encrypted uplinks with MIC, RX1/RX2 downlink
windows, downlink MIC + frame-counter verification, and the handful of MAC
commands a network server (TTN / ChirpStack) sends a US915 end device.
Designed for the SX1262 radio driver (``radio.sx1262``).

Regional parameters (LoRaWAN Regional Parameters RP002, US902-928):

* 64 uplink channels at 125 kHz: 902.3 + 0.2·n MHz (n = 0..63), DR0-DR3
  (SF10-SF7); 8 uplink channels at 500 kHz: 903.0 + 1.6·n MHz (n = 0..7), DR4.
* 8 downlink channels at 500 kHz: 923.3 + 0.6·n MHz (n = 0..7).
* RX1 is on downlink channel (uplink channel mod 8) at the data rate given by
  the RX1DROffset table; RX2 is 923.3 MHz at DR8 (SF12 / 500 kHz).
* A network server that serves only one "frequency sub-band" (TTN's FSB2 is
  channels 8-15 plus 500 kHz channel 65) tells the device which channels to
  use in the JoinAccept CFList and/or LinkADRReq. Until it does, the device
  uses the configured sub-band (``lora_sub_band``, default 2).

The previous revision of this module transmitted on a single fixed 915.000 MHz
carrier (not a US915 channel), never re-armed the TX parameters after the RX2
window, opened RX windows on the uplink frequency at 125 kHz with standard IQ,
and accepted any JoinAccept or downlink without checking its MIC. None of
those could join or receive on a real network; see docs/launch-audit.md.
"""

import logging
import os
import random
import struct
import time
from dataclasses import dataclass
from typing import Any

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

# --- US915 regional parameters -------------------------------------------
US915_UPLINK_BASE_HZ = 902_300_000
US915_UPLINK_STEP_HZ = 200_000
US915_UPLINK_500K_BASE_HZ = 903_000_000
US915_UPLINK_500K_STEP_HZ = 1_600_000
US915_DOWNLINK_BASE_HZ = 923_300_000
US915_DOWNLINK_STEP_HZ = 600_000
US915_NUM_125K_CHANNELS = 64
US915_NUM_500K_CHANNELS = 8
US915_NUM_CHANNELS = US915_NUM_125K_CHANNELS + US915_NUM_500K_CHANNELS

# RX2 defaults (RP002 US915): 923.3 MHz, DR8 = SF12 / 500 kHz.
RX2_FREQUENCY = 923_300_000
RX2_DR = 8
RX2_SF = 12
# SX1262 bandwidth codes (see radio.sx1262._BW_MAP)
BW_125K = 0x04
BW_500K = 0x06
RX2_BW = BW_500K

# TTN US915 "FSB2" = 125 kHz channels 8-15 + 500 kHz channel 65.
DEFAULT_SUB_BAND = 2

# RX window timing. RECEIVE_DELAY1 defaults to 1 s (a JoinAccept or
# RXTimingSetupReq can change it — TTN v3 uses 5 s); RECEIVE_DELAY2 is always
# RECEIVE_DELAY1 + 1 s. Join windows are fixed at 5 s / 6 s.
DEFAULT_RX1_DELAY_S = 1.0
JOIN_RX1_DELAY_S = 5.0
# How early the receiver is opened before the window, and how long it listens
# for a preamble. 0.5 s covers the longest downlink preamble (SF12 / 500 kHz:
# 8 symbols ≈ 66 ms) plus host scheduling jitter on a Pi Zero 2W.
RX_EARLY_S = 0.02
RX_WINDOW_S = 0.5

# Kept for callers/tests that reference the old names.
RX1_DELAY_S = DEFAULT_RX1_DELAY_S
RX2_DELAY_S = DEFAULT_RX1_DELAY_S + 1.0

# Uplink data rate <-> spreading factor at 125 kHz (DR4 is SF8 / 500 kHz and
# is not used by this device).
_UP_DR_TO_SF = {0: 10, 1: 9, 2: 8, 3: 7}
_SF_TO_UP_DR = {sf: dr for dr, sf in _UP_DR_TO_SF.items()}
# Largest application payload per uplink DR (RP002 US915, dwell-time limited).
MAX_PAYLOAD_BY_DR = {0: 11, 1: 53, 2: 125, 3: 242, 4: 242}

# MAC command identifiers (downlink request -> uplink answer share the CID).
_CID_LINK_CHECK = 0x02
_CID_LINK_ADR = 0x03
_CID_DUTY_CYCLE = 0x04
_CID_RX_PARAM_SETUP = 0x05
_CID_DEV_STATUS = 0x06
_CID_NEW_CHANNEL = 0x07
_CID_RX_TIMING_SETUP = 0x08
_CID_TX_PARAM_SETUP = 0x09
_CID_DL_CHANNEL = 0x0A
# Payload length of each downlink MAC command, so an unknown one stops parsing
# rather than desynchronising the stream.
_MAC_REQ_LEN = {
    _CID_LINK_CHECK: 2,
    _CID_LINK_ADR: 4,
    _CID_DUTY_CYCLE: 1,
    _CID_RX_PARAM_SETUP: 4,
    _CID_DEV_STATUS: 0,
    _CID_NEW_CHANNEL: 5,
    _CID_RX_TIMING_SETUP: 1,
    _CID_TX_PARAM_SETUP: 1,
    _CID_DL_CHANNEL: 4,
}
_MAX_FOPTS_LEN = 15

# Session liveness. Every LINK_CHECK_EVERY_UPLINKS uplinks with no downlink of
# any kind, a LinkCheckReq rides along; after this many go unanswered the
# session is presumed dead on the network side (device re-registered, session
# reset, NS migrated) and the device forgets it and rejoins. At the default
# 300 s cadence: 24 uplinks = 2 h per check, 3 unanswered = ~6-8 h to rejoin.
LINK_CHECK_EVERY_UPLINKS = 24
REJOIN_AFTER_UNANSWERED_LINK_CHECKS = 3


def rx1_data_rate(uplink_dr: int, rx1_dr_offset: int) -> int:
    """US915 RX1 downlink data rate for an uplink DR and RX1DROffset (RP002)."""
    base = {0: 10, 1: 11, 2: 12, 3: 13, 4: 14}.get(uplink_dr, 11)
    return min(13, max(8, base - max(0, min(3, rx1_dr_offset))))


def downlink_sf(dr: int) -> int:
    """Spreading factor of a US915 500 kHz downlink DR (DR8 = SF12 ... DR13 = SF7)."""
    return 20 - max(8, min(13, dr))


def uplink_frequency_hz(channel: int) -> int:
    """Centre frequency of a US915 uplink channel index (0-71)."""
    if channel < US915_NUM_125K_CHANNELS:
        return US915_UPLINK_BASE_HZ + channel * US915_UPLINK_STEP_HZ
    return (
        US915_UPLINK_500K_BASE_HZ + (channel - US915_NUM_125K_CHANNELS) * US915_UPLINK_500K_STEP_HZ
    )


def rx1_frequency_hz(uplink_channel: int) -> int:
    """RX1 downlink frequency for the channel an uplink went out on."""
    return US915_DOWNLINK_BASE_HZ + (uplink_channel % 8) * US915_DOWNLINK_STEP_HZ


def sub_band_channel_mask(sub_band: int) -> list[bool]:
    """Channel mask for one of the eight US915 sub-bands (1-8): its eight
    125 kHz channels plus the matching 500 kHz channel."""
    sub_band = max(1, min(8, int(sub_band)))
    mask = [False] * US915_NUM_CHANNELS
    first = (sub_band - 1) * 8
    for ch in range(first, first + 8):
        mask[ch] = True
    mask[US915_NUM_125K_CHANNELS + sub_band - 1] = True
    return mask


def _now() -> float:
    # float() so a patched-out ``time`` module (tests) still yields a number.
    return float(time.monotonic())


def _sleep_until(t: float) -> None:
    delay = t - _now()
    if delay > 0:
        time.sleep(delay)


@dataclass
class LoRaWANSession:
    """LoRaWAN session state (persisted across reboots).

    ``fcnt_down`` is the NEXT downlink frame counter the device will accept
    (i.e. last verified FCntDown + 1; 0 until a downlink has been received).
    """

    dev_addr: bytes = b"\x00\x00\x00\x00"
    nwk_skey: bytes = b"\x00" * 16
    app_skey: bytes = b"\x00" * 16
    fcnt_up: int = 0
    fcnt_down: int = 0
    joined: bool = False


class LoRaWANMAC:
    """LoRaWAN 1.0.3 Class A MAC layer for US915."""

    def __init__(
        self,
        radio: Any,
        dev_eui: bytes,
        app_eui: bytes,
        app_key: bytes,
        sub_band: int = DEFAULT_SUB_BAND,
        sf: int = 9,
        persist_hook: Any = None,
    ) -> None:
        """
        Args:
            radio: SX1262 driver instance (``send``, ``receive``,
                ``set_tx_config``, ``set_rx_config``)
            dev_eui: 8-byte device EUI (MSB first)
            app_eui: 8-byte application / join EUI (MSB first)
            app_key: 16-byte application key (root key for OTAA)
            sub_band: US915 sub-band (1-8) used until the network server
                sends a channel mask. TTN US915 is FSB2.
            sf: uplink spreading factor at 125 kHz (7-10, i.e. DR3-DR0)
            persist_hook: optional callable invoked right after the uplink
                frame counter is consumed and BEFORE the frame is transmitted,
                so a crash mid-uplink can never replay a frame counter.
        """
        self._radio = radio
        self._dev_eui = dev_eui
        self._app_eui = app_eui
        self._app_key = app_key
        self._session = LoRaWANSession()
        self._dev_nonce = 0
        # Monotonic DevNonce counter (persisted in mac_params). LoRaWAN 1.0.3
        # only asks for a random value, but TTN and ChirpStack refuse a reused
        # one; a counter can never repeat within 65536 joins.
        self._dev_nonce_counter = -1
        self._persist_hook = persist_hook
        self._sub_band = sub_band

        # Regional / MAC state (reset on every successful join; persisted with
        # the session via ``mac_params`` so a reboot keeps the network server's
        # RxDelay and channel plan).
        self._sf = sf if sf in _SF_TO_UP_DR else 9
        self._channel_mask: list[bool] = sub_band_channel_mask(sub_band)
        self._rx1_dr_offset = 0
        self._rx2_dr = RX2_DR
        self._rx2_frequency = RX2_FREQUENCY
        self._rx1_delay_s = DEFAULT_RX1_DELAY_S

        self._last_channel = 0
        self._tx_frequency = 0
        self._pending_fopts = bytearray()  # MAC answers for the next uplink
        self._ack_pending = False  # a confirmed downlink awaits FCtrl.ACK
        self._last_link_check: tuple[int, int] | None = None
        self._uplinks_since_downlink = 0
        self._unanswered_link_checks = 0

    # ------------------------------------------------------------------
    # Session / persisted state
    # ------------------------------------------------------------------

    @property
    def session(self) -> LoRaWANSession:
        return self._session

    @property
    def spreading_factor(self) -> int:
        return self._sf

    @property
    def uplink_dr(self) -> int:
        return _SF_TO_UP_DR[self._sf]

    @property
    def enabled_channels(self) -> list[int]:
        """Enabled 125 kHz uplink channel indices (falls back to the sub-band)."""
        chans = [i for i in range(US915_NUM_125K_CHANNELS) if self._channel_mask[i]]
        if not chans:
            chans = [
                i
                for i in range(US915_NUM_125K_CHANNELS)
                if sub_band_channel_mask(self._sub_band)[i]
            ]
        return chans

    @property
    def mac_params(self) -> dict[str, Any]:
        """Network-assigned MAC parameters to persist alongside the session."""
        bits = 0
        for i, on in enumerate(self._channel_mask):
            if on:
                bits |= 1 << i
        return {
            "sf": self._sf,
            "dev_nonce": self._dev_nonce_counter,
            "rx1_dr_offset": self._rx1_dr_offset,
            "rx2_dr": self._rx2_dr,
            "rx2_frequency": self._rx2_frequency,
            "rx1_delay_s": self._rx1_delay_s,
            "channel_mask": f"{bits:018x}",
        }

    def restore_session(
        self, session: LoRaWANSession, mac_params: dict[str, Any] | None = None
    ) -> None:
        """Restore a previously persisted session (after reboot)."""
        self._session = session
        if mac_params:
            self._apply_mac_params(mac_params)
        logger.info(
            "Session restored: DevAddr=%s FCntUp=%d FCntDown=%d joined=%s rx1_delay=%.0fs",
            self._dev_addr_display(),
            self._session.fcnt_up,
            self._session.fcnt_down,
            self._session.joined,
            self._rx1_delay_s,
        )

    def restore_mac_params(self, params: dict[str, Any] | None) -> None:
        """Restore persisted MAC parameters without a session (an unjoined
        unit still needs its DevNonce counter and last channel plan)."""
        if params:
            self._apply_mac_params(params)

    def forget_session(self) -> None:
        """Drop the session so the radio worker performs a fresh OTAA join."""
        self._session = LoRaWANSession()
        self._reset_mac_params()
        self._uplinks_since_downlink = 0
        self._unanswered_link_checks = 0
        logger.warning("LoRaWAN session forgotten — will rejoin")

    def _persist(self) -> None:
        if self._persist_hook is None:
            return
        try:
            self._persist_hook()
        except Exception as e:  # noqa: BLE001 — persistence must not block the radio
            logger.warning("Session persist failed: %s", e)

    def _next_dev_nonce(self) -> int:
        if self._dev_nonce_counter < 0:
            self._dev_nonce_counter = int.from_bytes(os.urandom(2), "little")
        else:
            self._dev_nonce_counter = (self._dev_nonce_counter + 1) & 0xFFFF
        return self._dev_nonce_counter

    def _apply_mac_params(self, params: dict[str, Any]) -> None:
        try:
            nonce = params.get("dev_nonce")
            if isinstance(nonce, int) and 0 <= nonce <= 0xFFFF:
                self._dev_nonce_counter = nonce
            sf = int(params.get("sf", self._sf))
            if sf in _SF_TO_UP_DR:
                self._sf = sf
            self._rx1_dr_offset = max(0, min(3, int(params.get("rx1_dr_offset", 0))))
            self._rx2_dr = max(8, min(13, int(params.get("rx2_dr", RX2_DR))))
            self._rx2_frequency = int(params.get("rx2_frequency", RX2_FREQUENCY)) or RX2_FREQUENCY
            self._rx1_delay_s = float(params.get("rx1_delay_s", DEFAULT_RX1_DELAY_S))
            if not 1.0 <= self._rx1_delay_s <= 15.0:
                self._rx1_delay_s = DEFAULT_RX1_DELAY_S
            raw_mask = params.get("channel_mask")
            if isinstance(raw_mask, str) and raw_mask:
                bits = int(raw_mask, 16)
                mask = [bool(bits >> i & 1) for i in range(US915_NUM_CHANNELS)]
                if any(mask[:US915_NUM_125K_CHANNELS]):
                    self._channel_mask = mask
        except (TypeError, ValueError) as e:
            logger.warning("Ignoring malformed persisted MAC params: %s", e)

    def _reset_mac_params(self) -> None:
        """Regional defaults — applied on every (re)join per the spec."""
        self._channel_mask = sub_band_channel_mask(self._sub_band)
        self._rx1_dr_offset = 0
        self._rx2_dr = RX2_DR
        self._rx2_frequency = RX2_FREQUENCY
        self._rx1_delay_s = DEFAULT_RX1_DELAY_S
        self._pending_fopts = bytearray()
        self._ack_pending = False

    def _dev_addr_display(self) -> str:
        """DevAddr as the network server console shows it (MSB first)."""
        return bytes(reversed(self._session.dev_addr)).hex().upper()

    # ------------------------------------------------------------------
    # Channel plan
    # ------------------------------------------------------------------

    def _pick_channel(self) -> int:
        """Random enabled 125 kHz uplink channel (frequency hopping)."""
        chans = self.enabled_channels
        # nosec B311 — channel selection is not a security decision
        self._last_channel = random.choice(chans)  # noqa: S311
        return self._last_channel

    def _configure_tx(self, channel: int) -> None:
        self._tx_frequency = uplink_frequency_hz(channel)
        self._radio.set_tx_config(self._tx_frequency, self._sf, BW_125K)

    def _configure_rx1(self, channel: int) -> None:
        sf = downlink_sf(rx1_data_rate(self.uplink_dr, self._rx1_dr_offset))
        self._radio.set_rx_config(rx1_frequency_hz(channel), sf, BW_500K)

    def _configure_rx2(self) -> None:
        self._radio.set_rx_config(self._rx2_frequency, downlink_sf(self._rx2_dr), BW_500K)

    def _receive_windows(self, t_tx: float, rx1_delay_s: float, channel: int) -> bytes | None:
        """Open RX1 then RX2 at absolute offsets from the end of the uplink."""
        self._configure_rx1(channel)
        _sleep_until(t_tx + rx1_delay_s - RX_EARLY_S)
        data = self._radio.receive(timeout_s=RX_WINDOW_S)
        if data:
            return data

        self._configure_rx2()
        _sleep_until(t_tx + rx1_delay_s + 1.0 - RX_EARLY_S)
        return self._radio.receive(timeout_s=RX_WINDOW_S)

    # ------------------------------------------------------------------
    # OTAA Join
    # ------------------------------------------------------------------

    def join(self, timeout_s: float = 10.0) -> bool:
        """
        Perform OTAA join procedure.

        Sends JoinRequest on a random enabled channel, opens RX1 (5 s, on the
        matching downlink channel) and RX2 (6 s, 923.3 MHz SF12/500k) for the
        JoinAccept, verifies its MIC, derives the session keys and applies the
        network's DLSettings / RxDelay / CFList.

        Returns:
            True if join succeeded, False on timeout/failure.
        """
        self._dev_nonce = self._next_dev_nonce()
        self._persist()  # the nonce must never be reused, even across a crash

        # Build JoinRequest: MHDR(1) + AppEUI(8) + DevEUI(8) + DevNonce(2) + MIC(4)
        payload = bytearray()
        payload.append(_MHDR_JOIN_REQUEST)
        payload += bytes(reversed(self._app_eui))  # little-endian per spec
        payload += bytes(reversed(self._dev_eui))
        payload += struct.pack("<H", self._dev_nonce)

        # MIC = aes128_cmac(AppKey, MHDR | AppEUI | DevEUI | DevNonce)[0:4]
        mic = _compute_mic(self._app_key, bytes(payload))
        payload += mic

        channel = self._pick_channel()
        self._configure_tx(channel)
        logger.info(
            "Sending JoinRequest (DevNonce=%d) on channel %d (%.1f MHz, SF%d)",
            self._dev_nonce,
            channel,
            self._tx_frequency / 1e6,
            self._sf,
        )
        if not self._radio.send(bytes(payload), timeout_s=5.0):
            logger.error("JoinRequest TX failed")
            return False
        t_tx = _now()

        accept_data = self._receive_windows(t_tx, JOIN_RX1_DELAY_S, channel)
        if accept_data is None:
            logger.warning("No JoinAccept received")
            return False

        return self._process_join_accept(accept_data)

    def _receive_join_accept(self, _timeout_s: float) -> bytes | None:
        """Open RX1 and RX2 windows for JoinAccept (kept for callers)."""
        return self._receive_windows(_now(), JOIN_RX1_DELAY_S, self._last_channel)

    def _process_join_accept(self, data: bytes) -> bool:
        """Decrypt, verify and apply a JoinAccept frame."""
        if len(data) < 17:  # MHDR(1) + encrypted(12) + MIC(4) minimum
            logger.error("JoinAccept too short: %d bytes", len(data))
            return False

        mhdr = data[0]
        if mhdr != _MHDR_JOIN_ACCEPT:
            logger.error("Not a JoinAccept frame: MHDR=0x%02X", mhdr)
            return False

        # The network server ENCRYPTS the JoinAccept with aes128_decrypt so
        # that the device only needs the encrypt primitive to recover it.
        cipher = AES.new(self._app_key, AES.MODE_ECB)
        encrypted = data[1:]
        padded_len = ((len(encrypted) + 15) // 16) * 16
        encrypted_padded = encrypted.ljust(padded_len, b"\x00")
        decrypted_buf = bytearray()
        for i in range(0, padded_len, 16):
            decrypted_buf += cipher.encrypt(encrypted_padded[i : i + 16])
        decrypted = bytes(decrypted_buf[: len(encrypted)])

        # Parse: AppNonce(3)+NetID(3)+DevAddr(4)+DLSettings(1)+RxDelay(1) [+CFList(16)]+MIC(4)
        if len(decrypted) < 16:
            logger.error("Decrypted JoinAccept too short")
            return False
        body, mic = decrypted[:-4], decrypted[-4:]

        # MIC = aes128_cmac(AppKey, MHDR | AppNonce | NetID | DevAddr | DLSettings
        # | RxDelay | CFList). Without this check any 0x20 frame on the air —
        # another network's JoinAccept, or noise — becomes a "successful" join
        # with garbage keys that then gets persisted.
        if _compute_mic(self._app_key, bytes([mhdr]) + body) != mic:
            logger.error("JoinAccept MIC mismatch — wrong AppKey or not our JoinAccept")
            return False

        app_nonce = body[0:3]
        net_id = body[3:6]
        dev_addr = body[6:10]
        dl_settings = body[10]
        rx_delay = body[11] & 0x0F

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
        self._reset_mac_params()
        self._rx1_dr_offset = (dl_settings >> 4) & 0x07
        self._rx2_dr = max(8, min(13, dl_settings & 0x0F)) if (dl_settings & 0x0F) else RX2_DR
        self._rx1_delay_s = float(rx_delay) if rx_delay else DEFAULT_RX1_DELAY_S

        cf_list = body[12:28] if len(body) >= 28 else b""
        if len(cf_list) == 16 and cf_list[15] == 0x01:
            # CFList type 1 (US915): ChMask0..ChMask4 for channels 0-79.
            masks = struct.unpack("<5H", cf_list[0:10])
            new_mask = [False] * US915_NUM_CHANNELS
            for block, chmask in enumerate(masks):
                for bit in range(16):
                    idx = block * 16 + bit
                    if idx < US915_NUM_CHANNELS:
                        new_mask[idx] = bool(chmask >> bit & 1)
            if any(new_mask[:US915_NUM_125K_CHANNELS]):
                self._channel_mask = new_mask
            else:
                logger.warning("JoinAccept CFList enables no 125 kHz channel — keeping sub-band")

        logger.info(
            "OTAA join successful: DevAddr=%s rx1_delay=%.0fs rx1_dr_offset=%d rx2_dr=%d "
            "channels=%s",
            self._dev_addr_display(),
            self._rx1_delay_s,
            self._rx1_dr_offset,
            self._rx2_dr,
            _summarise_channels(self.enabled_channels),
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
        Build and transmit a LoRaWAN uplink frame, then open RX1/RX2.

        Args:
            payload: Application payload (will be encrypted)
            fport: LoRaWAN FPort (1-223)
            confirmed: Whether to use confirmed uplink
            tx_frequency: ignored — the channel plan decides the frequency
                (kept for call compatibility)

        Returns:
            Downlink payload bytes (FPort prepended) if a verified downlink
            was received, None otherwise.
        """
        if not self._session.joined:
            logger.error("Cannot send uplink: not joined")
            return None

        # Session liveness probe (see LINK_CHECK_EVERY_UPLINKS).
        self._uplinks_since_downlink += 1
        if self._uplinks_since_downlink % LINK_CHECK_EVERY_UPLINKS == 0:
            if self._unanswered_link_checks >= REJOIN_AFTER_UNANSWERED_LINK_CHECKS:
                logger.error(
                    "No downlink for %d uplinks and %d LinkCheckReq unanswered — the network "
                    "no longer knows this session; forgetting it and rejoining",
                    self._uplinks_since_downlink,
                    self._unanswered_link_checks,
                )
                self.forget_session()
                self._persist()
                return None
            self._unanswered_link_checks += 1
            self._queue_answer(bytes([_CID_LINK_CHECK]))

        max_len = MAX_PAYLOAD_BY_DR[self.uplink_dr] - len(self._pending_fopts)
        if len(payload) > max_len:
            logger.error(
                "Uplink payload %d bytes exceeds the %d-byte limit at DR%d (SF%d) — not sent",
                len(payload),
                max_len,
                self.uplink_dr,
                self._sf,
            )
            return None

        fcnt = self._session.fcnt_up
        # Encrypt payload
        enc_payload = _encrypt_payload(
            self._session.app_skey if fport != 0 else self._session.nwk_skey,
            self._session.dev_addr,
            fcnt,
            payload,
            direction=0,  # uplink
        )

        fopts = bytes(self._pending_fopts[:_MAX_FOPTS_LEN])
        fctrl = (0x20 if self._ack_pending else 0x00) | len(fopts)

        # Build frame: MHDR | DevAddr | FCtrl | FCnt | FOpts | FPort | FRMPayload
        mhdr = _MHDR_CONFIRMED_UP if confirmed else _MHDR_UNCONFIRMED_UP
        frame = bytearray()
        frame.append(mhdr)
        frame += self._session.dev_addr  # already little-endian
        frame.append(fctrl)
        frame += struct.pack("<H", fcnt & 0xFFFF)
        frame += fopts
        frame.append(fport)
        frame += enc_payload

        # Compute and append MIC
        mic = _compute_uplink_mic(
            self._session.nwk_skey, self._session.dev_addr, fcnt, bytes(frame)
        )
        frame += mic

        # Consume the frame counter (and persist it) BEFORE the radio has it:
        # a crash between TX and the persist would otherwise replay FCntUp
        # after reboot, and the network server drops replayed counters.
        self._session.fcnt_up += 1
        self._pending_fopts = bytearray()
        self._ack_pending = False
        self._persist()

        channel = self._pick_channel()
        self._configure_tx(channel)
        logger.info(
            "Uplink FCnt=%d FPort=%d (%d bytes%s) on channel %d (%.1f MHz, SF%d)",
            fcnt,
            fport,
            len(payload),
            f", {len(fopts)} B FOpts" if fopts else "",
            channel,
            self._tx_frequency / 1e6,
            self._sf,
        )
        if not self._radio.send(bytes(frame), timeout_s=5.0):
            logger.error("Uplink TX failed")
            return None
        t_tx = _now()

        data = self._receive_windows(t_tx, self._rx1_delay_s, channel)
        if not data:
            return None
        return self._process_downlink(data)

    def _receive_downlink(self) -> bytes | None:
        """Open RX1 and RX2 windows after an uplink (kept for callers)."""
        data = self._receive_windows(_now(), self._rx1_delay_s, self._last_channel)
        return self._process_downlink(data) if data else None

    # ------------------------------------------------------------------
    # Downlink
    # ------------------------------------------------------------------

    def _reconstruct_fcnt_down(self, fcnt16: int) -> int:
        """32-bit FCntDown from the 16 bits on the air (must be >= expected)."""
        expected = self._session.fcnt_down
        candidate = (expected & 0xFFFF0000) | fcnt16
        if candidate < expected:
            candidate += 0x10000
        return candidate

    def _process_downlink(self, data: bytes) -> bytes | None:
        """Verify (DevAddr, MIC, frame counter), parse and decrypt a downlink."""
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
        fcnt16 = struct.unpack("<H", data[6:8])[0]
        fcnt = self._reconstruct_fcnt_down(fcnt16)

        body, mic = data[:-4], data[-4:]
        if _compute_downlink_mic(self._session.nwk_skey, dev_addr, fcnt, body) != mic:
            logger.warning(
                "Downlink FCnt=%d rejected: MIC mismatch (replay, wrong keys, or corruption)",
                fcnt,
            )
            return None

        # Verified — advance the counter so this frame can never be replayed.
        self._session.fcnt_down = fcnt + 1
        self._uplinks_since_downlink = 0
        self._unanswered_link_checks = 0
        if mhdr == _MHDR_CONFIRMED_DOWN:
            self._ack_pending = True

        header_len = 8 + fopts_len
        if fopts_len:
            self._handle_mac_commands(bytes(data[8:header_len]))

        if len(body) <= header_len:
            logger.info("Downlink received: FCnt=%d (MAC only)", fcnt)
            return None

        fport = body[header_len]
        enc_payload = body[header_len + 1 :]

        key = self._session.nwk_skey if fport == 0 else self._session.app_skey
        payload = _encrypt_payload(key, dev_addr, fcnt, enc_payload, direction=1)
        if fport == 0:
            self._handle_mac_commands(payload)

        logger.info("Downlink received: FCnt=%d FPort=%d (%d bytes)", fcnt, fport, len(payload))
        return bytes([fport]) + payload  # prepend fport for caller

    # ------------------------------------------------------------------
    # MAC commands (LoRaWAN 1.0.3 §5)
    # ------------------------------------------------------------------

    def _queue_answer(self, answer: bytes) -> None:
        if len(self._pending_fopts) + len(answer) <= _MAX_FOPTS_LEN:
            self._pending_fopts += answer
        else:
            logger.debug("FOpts full — MAC answer 0x%02X deferred", answer[0])

    def _handle_mac_commands(self, cmds: bytes) -> None:
        i = 0
        while i < len(cmds):
            cid = cmds[i]
            length = _MAC_REQ_LEN.get(cid)
            if length is None:
                logger.warning("Unknown MAC command 0x%02X — ignoring the rest", cid)
                return
            args = cmds[i + 1 : i + 1 + length]
            if len(args) < length:
                return
            i += 1 + length
            try:
                self._handle_mac_command(cid, args)
            except Exception as e:  # noqa: BLE001 — one bad command must not kill the frame
                logger.warning("MAC command 0x%02X failed: %s", cid, e)

    def _handle_mac_command(self, cid: int, args: bytes) -> None:
        if cid == _CID_LINK_CHECK:
            self._last_link_check = (args[0], args[1])
            logger.info("LinkCheckAns: margin=%d dB, gateways=%d", args[0], args[1])
        elif cid == _CID_LINK_ADR:
            self._handle_link_adr(args)
        elif cid == _CID_DUTY_CYCLE:
            # No duty-cycle limits in US915; acknowledge.
            self._queue_answer(bytes([_CID_DUTY_CYCLE]))
        elif cid == _CID_RX_PARAM_SETUP:
            dl_settings = args[0]
            freq = int.from_bytes(args[1:4], "little") * 100
            offset = (dl_settings >> 4) & 0x07
            rx2_dr = dl_settings & 0x0F
            status = 0
            if 0 <= offset <= 3:
                status |= 0x04
            if 8 <= rx2_dr <= 13:
                status |= 0x02
            if 923_300_000 <= freq <= 927_500_000:
                status |= 0x01
            if status == 0x07:
                self._rx1_dr_offset = offset
                self._rx2_dr = rx2_dr
                self._rx2_frequency = freq
                logger.info(
                    "RXParamSetupReq applied: rx1_dr_offset=%d rx2_dr=%d rx2=%.1f MHz",
                    offset,
                    rx2_dr,
                    freq / 1e6,
                )
            else:
                logger.warning("RXParamSetupReq rejected (status 0x%02X)", status)
            self._queue_answer(bytes([_CID_RX_PARAM_SETUP, status]))
        elif cid == _CID_DEV_STATUS:
            # Battery 0 = connected to an external power source (24 V DC in).
            # Margin = SNR of the last received frame, 6-bit two's complement.
            snr = getattr(self._radio, "last_snr", None)
            margin = int(round(float(snr))) if isinstance(snr, int | float) else 0
            margin = max(-32, min(31, margin))
            self._queue_answer(bytes([_CID_DEV_STATUS, 0x00, margin & 0x3F]))
        elif cid == _CID_RX_TIMING_SETUP:
            delay = args[0] & 0x0F
            self._rx1_delay_s = float(delay) if delay else DEFAULT_RX1_DELAY_S
            logger.info("RXTimingSetupReq applied: rx1_delay=%.0fs", self._rx1_delay_s)
            self._queue_answer(bytes([_CID_RX_TIMING_SETUP]))
        elif cid in (_CID_NEW_CHANNEL, _CID_TX_PARAM_SETUP, _CID_DL_CHANNEL):
            # Not applicable to US915 fixed channel plans (RP002): ignored
            # without an answer, as the regional parameters specify.
            logger.debug("MAC command 0x%02X not applicable in US915 — ignored", cid)

    def _handle_link_adr(self, args: bytes) -> None:
        dr = args[0] >> 4
        tx_power = args[0] & 0x0F
        ch_mask = struct.unpack("<H", args[1:3])[0]
        redundancy = args[3]
        ch_mask_cntl = (redundancy >> 4) & 0x07
        status = 0

        new_mask = list(self._channel_mask)
        mask_ok = True
        if ch_mask_cntl <= 4:
            for bit in range(16):
                new_mask[ch_mask_cntl * 16 + bit] = bool(ch_mask >> bit & 1)
        elif ch_mask_cntl in (6, 7):
            for ch in range(US915_NUM_125K_CHANNELS):
                new_mask[ch] = ch_mask_cntl == 6
            for bit in range(US915_NUM_500K_CHANNELS):
                new_mask[US915_NUM_125K_CHANNELS + bit] = bool(ch_mask >> bit & 1)
        else:
            mask_ok = False
        if mask_ok and not any(new_mask[:US915_NUM_125K_CHANNELS]):
            # A plan with no 125 kHz uplink channel is unusable for us
            # (500 kHz DR4 uplinks are not implemented).
            mask_ok = False
        if mask_ok:
            status |= 0x01

        # DR0 (SF10) caps the payload at 11 bytes, below a full reading, and
        # DR4 needs a 500 kHz uplink path this device does not have.
        dr_ok = dr in (1, 2, 3) or dr == 0x0F
        if dr_ok:
            status |= 0x02
        # TX power: the module runs at +22 dBm conducted, inside every US915
        # index; acknowledged but not changed.
        if tx_power <= 10 or tx_power == 0x0F:
            status |= 0x04

        if status == 0x07:
            self._channel_mask = new_mask
            if dr != 0x0F:
                self._sf = _UP_DR_TO_SF[dr]
            logger.info(
                "LinkADRReq applied: DR%s (SF%d) channels=%s",
                dr if dr != 0x0F else "-",
                self._sf,
                _summarise_channels(self.enabled_channels),
            )
        else:
            logger.warning(
                "LinkADRReq rejected (status 0x%02X): dr=%d power=%d cntl=%d mask=0x%04X",
                status,
                dr,
                tx_power,
                ch_mask_cntl,
                ch_mask,
            )
        self._queue_answer(bytes([_CID_LINK_ADR, status]))


def _summarise_channels(channels: list[int]) -> str:
    if not channels:
        return "none"
    if channels == list(range(channels[0], channels[0] + len(channels))):
        return f"{channels[0]}-{channels[-1]}"
    return ",".join(str(c) for c in channels[:12]) + ("…" if len(channels) > 12 else "")


# ------------------------------------------------------------------
# Crypto helpers (LoRaWAN 1.0.3 spec)
# ------------------------------------------------------------------


def _compute_mic(key: bytes, data: bytes) -> bytes:
    """Compute 4-byte MIC using AES-128-CMAC."""
    cobj = CMAC.new(key, ciphermod=AES)
    cobj.update(data)
    return cobj.digest()[:4]


def _compute_frame_mic(
    nwk_skey: bytes, dev_addr: bytes, fcnt: int, frame: bytes, direction: int
) -> bytes:
    """Data-frame MIC per LoRaWAN 1.0.3 §4.4.

    B0 = 0x49 | 4×0x00 | Dir | DevAddr | FCnt32 | 0x00 | len(msg)
    """
    b0 = bytearray(16)
    b0[0] = 0x49
    b0[5] = direction & 0xFF
    b0[6:10] = dev_addr
    b0[10:14] = struct.pack("<I", fcnt & 0xFFFFFFFF)
    b0[15] = len(frame) & 0xFF

    cobj = CMAC.new(nwk_skey, ciphermod=AES)
    cobj.update(bytes(b0) + frame)
    return cobj.digest()[:4]


def _compute_uplink_mic(nwk_skey: bytes, dev_addr: bytes, fcnt: int, frame: bytes) -> bytes:
    """Compute uplink MIC per LoRaWAN 1.0.3 spec (Section 4.4)."""
    return _compute_frame_mic(nwk_skey, dev_addr, fcnt, frame, direction=0)


def _compute_downlink_mic(nwk_skey: bytes, dev_addr: bytes, fcnt: int, frame: bytes) -> bytes:
    """Compute downlink MIC per LoRaWAN 1.0.3 spec (Section 4.4)."""
    return _compute_frame_mic(nwk_skey, dev_addr, fcnt, frame, direction=1)


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
        ai[10:14] = struct.pack("<I", fcnt & 0xFFFFFFFF)
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
