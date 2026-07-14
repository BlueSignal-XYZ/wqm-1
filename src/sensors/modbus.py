"""
Minimal Modbus-RTU master for RS485 sensors (via USB adapter).

Speaks just enough Modbus for the Honde Tech probe family: function codes
03 (read holding registers), 06 (write single register) and 16 (write
multiple registers), CRC-16, and the CDAB float word order the probes use.
Built on the existing pyserial dependency — no new packages.

All bus access goes through :class:`ModbusBus`, which serializes
transactions with a lock so multiple sensor drivers can share one
``/dev/ttyUSB0`` safely from the sampling worker.
"""

import contextlib
import logging
import struct
import threading
import time
from collections.abc import Callable, Iterable
from typing import Any

logger = logging.getLogger("wqm1.modbus")

FC_READ_HOLDING = 0x03
FC_WRITE_SINGLE = 0x06
FC_WRITE_MULTIPLE = 0x10

#: Broadcast address answered by a single connected Honde sensor regardless
#: of its configured address — the response carries the real address.
BROADCAST_QUERY_ADDR = 0xFE

# 3.5 char times at 9600 8N1 is ~4 ms; a slightly larger gap keeps the
# cheap USB adapters honest.
INTER_FRAME_GAP_S = 0.01


class ModbusError(Exception):
    """Base error for Modbus transactions."""


class ModbusTimeout(ModbusError):
    """No (or short) response from the device."""


class ModbusCRCError(ModbusError):
    """Response failed CRC validation."""


class ModbusExceptionResponse(ModbusError):
    """Device returned a Modbus exception frame."""

    def __init__(self, function_code: int, exception_code: int) -> None:
        self.function_code = function_code
        self.exception_code = exception_code
        super().__init__(f"Modbus exception 0x{exception_code:02X} for FC 0x{function_code:02X}")


def crc16(data: bytes) -> int:
    """Standard Modbus CRC-16 (poly 0xA001, init 0xFFFF)."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


def append_crc(frame: bytes) -> bytes:
    """Append CRC-16 (low byte first, per Modbus RTU)."""
    crc = crc16(frame)
    return frame + bytes((crc & 0xFF, crc >> 8))


def check_crc(frame: bytes) -> bool:
    """Validate the trailing CRC of a full RTU frame."""
    if len(frame) < 4:
        return False
    crc = crc16(frame[:-2])
    return frame[-2] == (crc & 0xFF) and frame[-1] == (crc >> 8)


def build_read(address: int, start: int, count: int) -> bytes:
    """FC03 read-holding-registers request."""
    return append_crc(struct.pack(">BBHH", address, FC_READ_HOLDING, start, count))


def build_write_single(address: int, register: int, value: int) -> bytes:
    """FC06 write-single-register request."""
    return append_crc(struct.pack(">BBHH", address, FC_WRITE_SINGLE, register, value & 0xFFFF))


def build_write_multiple(address: int, start: int, values: list[int]) -> bytes:
    """FC16 write-multiple-registers request."""
    payload = struct.pack(">BBHHB", address, FC_WRITE_MULTIPLE, start, len(values), len(values) * 2)
    for v in values:
        payload += struct.pack(">H", v & 0xFFFF)
    return append_crc(payload)


def decode_float_cdab(regs: Iterable[int]) -> float:
    """
    Decode two registers in Honde's CDAB word order into a float.

    On the wire the four float bytes A B C D arrive as words (C D)(A B) —
    the word order is swapped, the bytes within each word are not.
    Example from the datasheet: regs (0x7237, 0x41DB) -> 27.4.
    """
    cd, ab = tuple(regs)
    return struct.unpack(">f", struct.pack(">HH", ab, cd))[0]


def encode_float_cdab(value: float) -> tuple[int, int]:
    """Encode a float into two registers in CDAB word order."""
    ab, cd = struct.unpack(">HH", struct.pack(">f", value))
    return (cd, ab)


def to_int16(reg: int) -> int:
    """Reinterpret an unsigned register as a signed 16-bit value."""
    return reg - 0x10000 if reg >= 0x8000 else reg


class ModbusBus:
    """
    A shared, locked RS485 bus.

    The serial port is opened lazily on first use so constructing the bus
    (e.g. during app wiring or in tests) never touches hardware. A custom
    ``serial_factory`` returning a pyserial-compatible object (``read``,
    ``write``, ``reset_input_buffer``, ``close``) is injected in tests.
    """

    def __init__(
        self,
        port: str,
        baudrate: int = 9600,
        timeout_s: float = 0.5,
        retries: int = 2,
        serial_factory: Callable[[], Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._port = port
        self._baudrate = baudrate
        self._timeout_s = timeout_s
        self._retries = max(1, retries)
        self._serial_factory = serial_factory
        self._sleep = sleep
        self._serial: Any = None
        self._lock = threading.Lock()

    # -- lifecycle -----------------------------------------------------

    def _ensure_open(self) -> Any:
        if self._serial is None:
            if self._serial_factory is not None:
                self._serial = self._serial_factory()
            else:  # pragma: no cover - exercised on hardware only
                import serial

                self._serial = serial.Serial(
                    port=self._port,
                    baudrate=self._baudrate,
                    bytesize=8,
                    parity="N",
                    stopbits=1,
                    timeout=self._timeout_s,
                )
            logger.info("Modbus bus open on %s @ %d baud", self._port, self._baudrate)
        return self._serial

    def close(self) -> None:
        with self._lock:
            if self._serial is not None:
                with contextlib.suppress(Exception):  # best-effort close
                    self._serial.close()
                self._serial = None

    # -- transactions --------------------------------------------------

    def _transact(self, request: bytes, response_len: int) -> bytes:
        """One request/response exchange with retries. Caller holds no lock."""
        last_error: ModbusError = ModbusTimeout("no attempts made")
        with self._lock:
            ser = self._ensure_open()
            for attempt in range(self._retries):
                try:
                    ser.reset_input_buffer()
                    self._sleep(INTER_FRAME_GAP_S)
                    ser.write(request)
                    response = ser.read(response_len)
                    if len(response) >= 5 and response[1] & 0x80:
                        # Exception frame is always 5 bytes: addr, fc|0x80,
                        # code, crc16. Trim in case we over-read.
                        exc = response[:5]
                        if not check_crc(exc):
                            raise ModbusCRCError("bad CRC on exception response")
                        raise ModbusExceptionResponse(exc[1] & 0x7F, exc[2])
                    if len(response) < response_len:
                        raise ModbusTimeout(
                            f"short response ({len(response)}/{response_len} bytes)"
                        )
                    if not check_crc(response):
                        raise ModbusCRCError("bad CRC on response")
                    return response
                except ModbusExceptionResponse:
                    raise  # device answered definitively - retrying won't help
                except ModbusError as e:
                    last_error = e
                    logger.debug("Modbus attempt %d/%d failed: %s", attempt + 1, self._retries, e)
                    self._sleep(INTER_FRAME_GAP_S * 2)
        raise last_error

    def read_registers(self, address: int, start: int, count: int) -> list[int]:
        """FC03: read ``count`` holding registers, returned as unsigned ints."""
        request = build_read(address, start, count)
        response = self._transact(request, response_len=5 + 2 * count)
        byte_count = response[2]
        if byte_count != 2 * count:
            raise ModbusError(f"unexpected byte count {byte_count} (wanted {2 * count})")
        data = response[3 : 3 + byte_count]
        return [struct.unpack(">H", data[i : i + 2])[0] for i in range(0, byte_count, 2)]

    def write_register(self, address: int, register: int, value: int) -> None:
        """FC06: write a single holding register (response echoes the request)."""
        request = build_write_single(address, register, value)
        self._transact(request, response_len=8)

    def write_registers(self, address: int, start: int, values: list[int]) -> None:
        """FC16: write multiple holding registers."""
        request = build_write_multiple(address, start, values)
        self._transact(request, response_len=8)

    # -- discovery / provisioning --------------------------------------

    def probe(self, address: int, register: int = 0x0000) -> bool:
        """True if a device answers an FC03 read at ``address``."""
        try:
            self.read_registers(address, register, 1)
            return True
        except ModbusError:
            return False

    def scan(self, addresses: Iterable[int] = range(1, 17)) -> list[int]:
        """Return the addresses that answer on the bus."""
        return [a for a in addresses if self.probe(a)]

    def query_single_address(self) -> int | None:
        """
        Discover the address of the ONLY sensor on the bus via broadcast
        0xFE. The response frame carries the device's real address. Returns
        None when nothing answers (or more than one device garbles the bus).
        """
        request = build_read(BROADCAST_QUERY_ADDR, 0x0000, 1)
        try:
            with self._lock:
                ser = self._ensure_open()
                ser.reset_input_buffer()
                self._sleep(INTER_FRAME_GAP_S)
                ser.write(request)
                response = ser.read(7)
            if len(response) < 7 or not check_crc(response):
                return None
            return int(response[0])
        except Exception:  # noqa: BLE001 - discovery is best-effort
            return None
