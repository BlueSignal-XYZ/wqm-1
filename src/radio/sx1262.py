"""
SX1262 LoRa Radio Driver

Raw SPI driver for the LORA1262-915TCXO module via spidev.
Implements the SX1262 command interface for LoRa packet TX.
"""

import contextlib
import logging
import threading
import time

import RPi.GPIO as GPIO
import spidev

from utils.config import (
    LORA_BANDWIDTH,
    LORA_BUSY,
    LORA_CODING_RATE,
    LORA_CRC_ON,
    LORA_DIO1,
    LORA_FREQUENCY,
    LORA_HP_MAX,
    LORA_PA_DEVICE_SEL,
    LORA_PA_DUTY_CYCLE,
    LORA_PA_LUT,
    LORA_PREAMBLE_LEN,
    LORA_RST,
    LORA_SPREADING_FACTOR,
    LORA_SYNC_WORD,
    LORA_TX_POWER,
    SPI_BUS,
    SPI_DEVICE,
)

logger = logging.getLogger("wqm1.sx1262")

# ---------------------------------------------------------------------------
# SX1262 opcodes
# ---------------------------------------------------------------------------
_CMD_SET_SLEEP = 0x84
_CMD_SET_STANDBY = 0x80
_CMD_SET_TX = 0x83
_CMD_SET_RX = 0x82
_CMD_SET_PACKET_TYPE = 0x8A
_CMD_SET_RF_FREQUENCY = 0x86
_CMD_SET_PA_CONFIG = 0x95
_CMD_SET_TX_PARAMS = 0x8E
_CMD_SET_BUFFER_BASE_ADDR = 0x8F
_CMD_SET_MODULATION_PARAMS = 0x8B
_CMD_SET_PACKET_PARAMS = 0x8C
_CMD_SET_DIO_IRQ_PARAMS = 0x08
_CMD_CLR_IRQ_STATUS = 0x02
_CMD_GET_IRQ_STATUS = 0x12
_CMD_WRITE_BUFFER = 0x0E
_CMD_READ_BUFFER = 0x1E
_CMD_WRITE_REGISTER = 0x0D
_CMD_READ_REGISTER = 0x1D
_CMD_SET_DIO3_AS_TCXO_CTRL = 0x97
_CMD_CALIBRATE = 0x89
_CMD_SET_REGULATOR_MODE = 0x96
_CMD_GET_STATUS = 0xC0
_CMD_GET_RX_BUFFER_STATUS = 0x13
_CMD_GET_RSSI_INST = 0x15

# Packet type
_PACKET_TYPE_LORA = 0x01

# Standby modes
_STDBY_RC = 0x00
_STDBY_XOSC = 0x01

# Regulator mode
_REGULATOR_DC_DC = 0x01

# IRQ masks
_IRQ_TX_DONE = 0x0001
_IRQ_RX_DONE = 0x0002
_IRQ_TIMEOUT = 0x0200
_IRQ_ALL = 0x03FF

# TCXO voltage (1.8V for LORA1262-915TCXO)
_TCXO_VOLTAGE_1_8V = 0x03

# Sync word register address for LoRa
_REG_LORA_SYNC_WORD_MSB = 0x0740
_REG_LORA_SYNC_WORD_LSB = 0x0741

# BW encoding for SX1262
_BW_MAP = {
    7800: 0x00,
    10400: 0x08,
    15600: 0x01,
    20800: 0x09,
    31250: 0x02,
    41700: 0x0A,
    62500: 0x03,
    125000: 0x04,
    250000: 0x05,
    500000: 0x06,
}

# Max SPI clock for SX1262
_SPI_MAX_SPEED = 2_000_000  # 2 MHz (conservative)


class SX1262:
    """SX1262 LoRa transceiver via SPI."""

    def __init__(self):
        self._spi = None
        self._tx_done_event = threading.Event()
        self._last_rssi = -120

        # Setup GPIO
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        GPIO.setup(LORA_RST, GPIO.OUT, initial=GPIO.HIGH)
        GPIO.setup(LORA_BUSY, GPIO.IN)
        GPIO.setup(LORA_DIO1, GPIO.IN)

        # Open SPI
        self._spi = spidev.SpiDev()
        self._spi.open(SPI_BUS, SPI_DEVICE)
        self._spi.max_speed_hz = _SPI_MAX_SPEED
        self._spi.mode = 0  # CPOL=0, CPHA=0
        self._spi.no_cs = False

        logger.info("SX1262 SPI opened")

    def init(self) -> None:
        """Full initialisation sequence for LoRa TX."""
        self._reset()
        self._wait_busy()

        # Set regulator mode to DC-DC (more efficient)
        self._cmd(_CMD_SET_REGULATOR_MODE, [_REGULATOR_DC_DC])
        self._wait_busy()

        # Configure DIO3 as TCXO control (1.8V, 5ms timeout)
        timeout = 320  # 5 ms in 15.625 µs steps
        self._cmd(
            _CMD_SET_DIO3_AS_TCXO_CTRL,
            [
                _TCXO_VOLTAGE_1_8V,
                (timeout >> 16) & 0xFF,
                (timeout >> 8) & 0xFF,
                timeout & 0xFF,
            ],
        )
        self._wait_busy()

        # Calibrate all blocks
        self._cmd(_CMD_CALIBRATE, [0x7F])
        self._wait_busy()

        # Set standby with XOSC
        self._cmd(_CMD_SET_STANDBY, [_STDBY_XOSC])
        self._wait_busy()

        # Set packet type to LoRa
        self._cmd(_CMD_SET_PACKET_TYPE, [_PACKET_TYPE_LORA])
        self._wait_busy()

        # Set RF frequency
        self._set_frequency(LORA_FREQUENCY)

        # Configure PA for +22 dBm
        self._cmd(
            _CMD_SET_PA_CONFIG, [LORA_PA_DUTY_CYCLE, LORA_HP_MAX, LORA_PA_DEVICE_SEL, LORA_PA_LUT]
        )
        self._wait_busy()

        # Set TX power and ramp time
        # Power: 0x16 = 22 dBm, ramp: 0x04 = 200 µs
        self._cmd(_CMD_SET_TX_PARAMS, [LORA_TX_POWER & 0xFF, 0x04])
        self._wait_busy()

        # Set modulation params: SF, BW, CR, LDRO
        ldro = 1 if LORA_SPREADING_FACTOR >= 10 else 0  # auto LDRO for SF>=10
        self._cmd(
            _CMD_SET_MODULATION_PARAMS,
            [
                LORA_SPREADING_FACTOR,
                LORA_BANDWIDTH,
                LORA_CODING_RATE,
                ldro,
            ],
        )
        self._wait_busy()

        # Set sync word (LoRaWAN public: 0x3444)
        self._write_register(_REG_LORA_SYNC_WORD_MSB, (LORA_SYNC_WORD >> 8) & 0xFF)
        self._write_register(_REG_LORA_SYNC_WORD_LSB, LORA_SYNC_WORD & 0xFF)

        # Set buffer base addresses (TX=0, RX=128)
        self._cmd(_CMD_SET_BUFFER_BASE_ADDR, [0x00, 0x80])
        self._wait_busy()

        # Setup DIO1 for TxDone IRQ
        self._cmd(
            _CMD_SET_DIO_IRQ_PARAMS,
            [
                (_IRQ_TX_DONE >> 8) & 0xFF,
                _IRQ_TX_DONE & 0xFF,  # IRQ mask
                (_IRQ_TX_DONE >> 8) & 0xFF,
                _IRQ_TX_DONE & 0xFF,  # DIO1 mask
                0x00,
                0x00,  # DIO2 mask
                0x00,
                0x00,  # DIO3 mask
            ],
        )
        self._wait_busy()

        # Setup DIO1 interrupt callback
        with contextlib.suppress(Exception):
            GPIO.remove_event_detect(LORA_DIO1)
        GPIO.add_event_detect(LORA_DIO1, GPIO.RISING, callback=self._on_dio1)

        logger.info(
            "SX1262 initialised: %d MHz, SF%d, BW%d, CR4/%d, %d dBm",
            LORA_FREQUENCY // 1_000_000,
            LORA_SPREADING_FACTOR,
            125,
            LORA_CODING_RATE + 4,
            LORA_TX_POWER,
        )

    def send(self, data: bytes, timeout_s: float = 5.0) -> bool:
        """
        Transmit a LoRa packet.

        Args:
            data: Payload bytes (max 255)
            timeout_s: Maximum time to wait for TxDone

        Returns:
            True if TxDone received, False on timeout
        """
        if len(data) > 255:
            raise ValueError("Payload exceeds 255 bytes")

        # Set packet params for this payload size
        crc_type = 0x01 if LORA_CRC_ON else 0x00
        self._cmd(
            _CMD_SET_PACKET_PARAMS,
            [
                (LORA_PREAMBLE_LEN >> 8) & 0xFF,
                LORA_PREAMBLE_LEN & 0xFF,
                0x00,  # explicit header
                len(data),  # payload length
                crc_type,
                0x00,  # standard IQ
            ],
        )
        self._wait_busy()

        # Write payload to TX buffer at offset 0
        self._spi.xfer2([_CMD_WRITE_BUFFER, 0x00] + list(data))
        self._wait_busy()

        # Clear IRQ flags
        self._cmd(_CMD_CLR_IRQ_STATUS, [0xFF, 0xFF])
        self._wait_busy()

        # Start TX (timeout 0 = no timeout from radio side, we manage it)
        self._tx_done_event.clear()
        self._cmd(_CMD_SET_TX, [0x00, 0x00, 0x00])

        # Wait for TxDone via DIO1 interrupt
        done = self._tx_done_event.wait(timeout=timeout_s)

        if done:
            logger.info("LoRa TX complete (%d bytes)", len(data))
        else:
            logger.warning("LoRa TX timeout after %.1fs", timeout_s)
            # Force back to standby
            self._cmd(_CMD_SET_STANDBY, [_STDBY_XOSC])
            self._wait_busy()

        return done

    def _on_dio1(self, channel) -> None:
        """DIO1 rising edge callback — TxDone or RxDone."""
        self._tx_done_event.set()

    def _reset(self) -> None:
        """Hardware reset: pull RST low for 1 ms, then release."""
        GPIO.output(LORA_RST, GPIO.LOW)
        time.sleep(0.001)
        GPIO.output(LORA_RST, GPIO.HIGH)
        time.sleep(0.010)  # wait 10 ms after reset

    def _wait_busy(self, timeout_s: float = 1.0) -> None:
        """Wait until BUSY pin goes low."""
        deadline = time.monotonic() + timeout_s
        while GPIO.input(LORA_BUSY):
            if time.monotonic() > deadline:
                logger.warning("SX1262 BUSY timeout")
                return
            time.sleep(0.0001)

    def _cmd(self, opcode: int, params: list = None) -> list:
        """Send SPI command and return response bytes."""
        tx = [opcode] + (params or [])
        self._spi.xfer2(tx)
        self._wait_busy()

    def _write_register(self, address: int, value: int) -> None:
        """Write a single byte to an SX1262 register."""
        self._spi.xfer2(
            [
                _CMD_WRITE_REGISTER,
                (address >> 8) & 0xFF,
                address & 0xFF,
                value,
            ]
        )
        self._wait_busy()

    def _read_register(self, address: int) -> int:
        """Read a single byte from an SX1262 register."""
        resp = self._spi.xfer2(
            [
                _CMD_READ_REGISTER,
                (address >> 8) & 0xFF,
                address & 0xFF,
                0x00,  # status
                0x00,  # data
            ]
        )
        return resp[4]

    def _set_frequency(self, freq_hz: int) -> None:
        """Set RF frequency via SetRfFrequency command."""
        # freq_reg = freq_hz * 2^25 / 32e6
        freq_reg = int(freq_hz * (1 << 25) / 32_000_000)
        self._cmd(
            _CMD_SET_RF_FREQUENCY,
            [
                (freq_reg >> 24) & 0xFF,
                (freq_reg >> 16) & 0xFF,
                (freq_reg >> 8) & 0xFF,
                freq_reg & 0xFF,
            ],
        )
        self._wait_busy()

    def receive(self, timeout_s: float = 1.0) -> bytes | None:
        """
        Set radio to RX mode and wait for a packet.

        Args:
            timeout_s: Maximum time to wait for RxDone.

        Returns:
            Received payload bytes, or None on timeout.
        """
        # Configure DIO1 for RxDone IRQ
        irq_mask = _IRQ_RX_DONE | _IRQ_TIMEOUT
        self._cmd(
            _CMD_SET_DIO_IRQ_PARAMS,
            [
                (irq_mask >> 8) & 0xFF,
                irq_mask & 0xFF,
                (irq_mask >> 8) & 0xFF,
                irq_mask & 0xFF,
                0x00,
                0x00,
                0x00,
                0x00,
            ],
        )
        self._wait_busy()

        # Clear IRQ
        self._cmd(_CMD_CLR_IRQ_STATUS, [0xFF, 0xFF])
        self._wait_busy()

        # Start RX with radio-side timeout
        # Timeout = timeout_s * 64000 (15.625 µs steps)
        rx_timeout = int(timeout_s * 64000)
        self._tx_done_event.clear()
        self._cmd(
            _CMD_SET_RX,
            [
                (rx_timeout >> 16) & 0xFF,
                (rx_timeout >> 8) & 0xFF,
                rx_timeout & 0xFF,
            ],
        )

        # Wait for DIO1 (RxDone or Timeout)
        if not self._tx_done_event.wait(timeout=timeout_s + 0.5):
            self._cmd(_CMD_SET_STANDBY, [_STDBY_XOSC])
            self._wait_busy()
            return None

        # Check IRQ status to distinguish RxDone from Timeout
        irq = self._get_irq_status()
        if not (irq & _IRQ_RX_DONE):
            self._cmd(_CMD_SET_STANDBY, [_STDBY_XOSC])
            self._wait_busy()
            return None

        # Read RX buffer status: [status, payloadLen, bufferOffset]
        resp = self._spi.xfer2([_CMD_GET_RX_BUFFER_STATUS, 0x00, 0x00, 0x00])
        self._wait_busy()
        payload_len = resp[2] if len(resp) > 2 else 0
        buffer_offset = resp[3] if len(resp) > 3 else 0

        if payload_len == 0:
            return None

        # Read buffer
        rx_data = self._spi.xfer2([_CMD_READ_BUFFER, buffer_offset, 0x00] + [0x00] * payload_len)
        self._wait_busy()

        # Store RSSI
        self._last_rssi = self._read_rssi()

        # Back to standby
        self._cmd(_CMD_SET_STANDBY, [_STDBY_XOSC])
        self._wait_busy()

        return bytes(rx_data[3 : 3 + payload_len])

    def get_rssi(self) -> int:
        """Return last packet RSSI in dBm."""
        return self._last_rssi

    def _read_rssi(self) -> int:
        """Read RSSI of last received packet from radio register."""
        resp = self._spi.xfer2([_CMD_GET_RSSI_INST, 0x00, 0x00])
        self._wait_busy()
        # RSSI = -resp[2] / 2
        raw = resp[2] if len(resp) > 2 else 0
        return -(raw // 2)

    def _get_irq_status(self) -> int:
        """Read current IRQ status flags."""
        resp = self._spi.xfer2([_CMD_GET_IRQ_STATUS, 0x00, 0x00, 0x00])
        self._wait_busy()
        return ((resp[2] if len(resp) > 2 else 0) << 8) | (resp[3] if len(resp) > 3 else 0)

    def set_rx_config(self, frequency: int, sf: int, bw: int) -> None:
        """
        Reconfigure radio for RX window (different frequency/SF/BW).

        Args:
            frequency: RX frequency in Hz
            sf: Spreading factor
            bw: Bandwidth index (SX1262 encoding)
        """
        self._set_frequency(frequency)
        ldro = 1 if sf >= 10 else 0
        self._cmd(
            _CMD_SET_MODULATION_PARAMS,
            [sf, bw, LORA_CODING_RATE, ldro],
        )
        self._wait_busy()

    def close(self) -> None:
        """Close SPI and release GPIO."""
        with contextlib.suppress(Exception):
            GPIO.remove_event_detect(LORA_DIO1)
        if self._spi:
            self._spi.close()
            self._spi = None
        logger.info("SX1262 closed")
