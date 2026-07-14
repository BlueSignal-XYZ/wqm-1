"""Tests for src/sensors/modbus.py — frames, CRC, decoding, bus transactions.

Golden frames (request bytes incl. CRC) are transcribed verbatim from the
Honde Tech datasheets, so these tests validate our CRC-16 and frame builder
against the vendor's own examples.
"""

import struct

import pytest

from sensors.modbus import (
    ModbusBus,
    ModbusCRCError,
    ModbusExceptionResponse,
    ModbusTimeout,
    append_crc,
    build_read,
    build_write_multiple,
    build_write_single,
    check_crc,
    decode_float_cdab,
    encode_float_cdab,
    to_int16,
)


class FakeSerial:
    """Scripted serial port: queue responses, record writes."""

    def __init__(self) -> None:
        self.responses: list[bytes] = []
        self.writes: list[bytes] = []
        self.closed = False

    def reset_input_buffer(self) -> None:
        pass

    def write(self, data: bytes) -> int:
        self.writes.append(bytes(data))
        return len(data)

    def read(self, n: int) -> bytes:
        if not self.responses:
            return b""
        chunk = self.responses.pop(0)
        return chunk[:n]

    def close(self) -> None:
        self.closed = True


def make_bus(fake: FakeSerial, retries: int = 1) -> ModbusBus:
    return ModbusBus(
        "/dev/ttyTEST", retries=retries, serial_factory=lambda: fake, sleep=lambda _s: None
    )


class TestGoldenFrames:
    """Request frames exactly as printed in the Honde datasheets."""

    def test_orp_read_value(self):
        assert build_read(0x01, 0x0000, 1) == bytes.fromhex("010300000001840A")

    def test_5in1_read_all(self):
        assert build_read(0x01, 0x0000, 5) == bytes.fromhex("01030000000585C9")

    def test_chlorine_read_value(self):
        assert build_read(0x01, 0x0001, 2) == bytes.fromhex("01030001000295CB")

    def test_chlorine_read_warning(self):
        assert build_read(0x01, 0x0007, 1) == bytes.fromhex("0103000700 01 35CB".replace(" ", ""))

    def test_chlorine_read_ad(self):
        assert build_read(0x01, 0x0066, 1) == bytes.fromhex("010300660001 6415".replace(" ", ""))

    def test_readdress_orp_family(self):
        # ORP / 5-in-1: register 0x0050, address 1 -> 2
        assert build_write_single(0x01, 0x0050, 0x0002) == bytes.fromhex("010600500002081A")

    def test_readdress_chlorine_family(self):
        # Chlorine: register 0x0019, address 1 -> 2
        assert build_write_single(0x01, 0x0019, 0x0002) == bytes.fromhex("010600190002D9CC")

    def test_chlorine_zero_confirm(self):
        assert build_write_single(0x01, 0x003E, 0x00FF) == bytes.fromhex("0106003E00FFA846")

    def test_chlorine_slope_confirm(self):
        assert build_write_single(0x01, 0x003F, 0x00FF) == bytes.fromhex("0106003F00FFF986")

    def test_chlorine_write_cal_reference_float(self):
        # Write 1.0 mg/L (float CDAB) to register 0x0024 via FC16
        frame = build_write_multiple(0x01, 0x0024, list(encode_float_cdab(1.0)))
        assert frame == bytes.fromhex("011000240002040000 3F80 E014".replace(" ", ""))

    def test_5in1_ph_cal_918(self):
        assert build_write_single(0x01, 0x005A, 0x000A) == bytes.fromhex("0106005A000A29DE")

    def test_5in1_ph_cal_686(self):
        assert build_write_single(0x01, 0x005B, 0x000B) == bytes.fromhex("0106005B000BB9DE")

    def test_5in1_ph_cal_401(self):
        assert build_write_single(0x01, 0x005C, 0x000C) == bytes.fromhex("0106005C000C49DD")

    def test_5in1_ec_slope(self):
        # slope 1.2 -> 1200 -> 0x04B0
        assert build_write_single(0x01, 0x000A, 0x04B0) == bytes.fromhex("0106000A04B0AABC")

    def test_5in1_ph_offset(self):
        # offset 1.00 -> 100 -> 0x0064
        assert build_write_single(0x01, 0x000B, 0x0064) == bytes.fromhex("0106000B0064F9E3")


class TestCrcAndDecode:
    def test_check_crc_round_trip(self):
        frame = append_crc(b"\x01\x03\x02\x00\x64")
        assert check_crc(frame)
        assert not check_crc(frame[:-1] + bytes([frame[-1] ^ 0xFF]))
        assert not check_crc(b"\x01\x03")

    def test_decode_float_cdab_datasheet_example(self):
        # Datasheet: bytes 72 37 41 DB on the wire -> "27.4" (0x41DB7237 is
        # exactly 27.4308; the manual prints it rounded to one decimal).
        assert decode_float_cdab((0x7237, 0x41DB)) == pytest.approx(27.43, abs=0.01)

    def test_encode_decode_float_cdab_round_trip(self):
        for value in (0.0, 1.0, 0.35, 19.99, 27.4):
            assert decode_float_cdab(encode_float_cdab(value)) == pytest.approx(value, rel=1e-6)

    def test_encode_float_cdab_one(self):
        # 1.0 = 0x3F800000 -> CDAB words (0x0000, 0x3F80)
        assert encode_float_cdab(1.0) == (0x0000, 0x3F80)

    def test_to_int16(self):
        assert to_int16(0x0064) == 100
        assert to_int16(0xFFFF) == -1
        assert to_int16(0xF830) == -2000


def fc03_response(address: int, regs: list[int]) -> bytes:
    body = struct.pack(">BBB", address, 0x03, len(regs) * 2)
    for r in regs:
        body += struct.pack(">H", r)
    return append_crc(body)


class TestBusTransactions:
    def test_read_registers(self):
        fake = FakeSerial()
        fake.responses.append(fc03_response(1, [0x02AE, 0x0064, 0x00FA, 0x0032, 0x0032]))
        bus = make_bus(fake)
        regs = bus.read_registers(1, 0x0000, 5)
        assert regs == [0x02AE, 0x0064, 0x00FA, 0x0032, 0x0032]
        assert fake.writes == [bytes.fromhex("01030000000585C9")]

    def test_write_register_echo(self):
        fake = FakeSerial()
        fake.responses.append(bytes.fromhex("010600500002081A"))
        bus = make_bus(fake)
        bus.write_register(1, 0x0050, 2)  # no exception = success

    def test_short_response_raises_timeout_after_retries(self):
        fake = FakeSerial()
        bus = make_bus(fake, retries=2)
        with pytest.raises(ModbusTimeout):
            bus.read_registers(1, 0, 1)
        assert len(fake.writes) == 2  # retried

    def test_bad_crc_raises(self):
        fake = FakeSerial()
        good = fc03_response(1, [0x0064])
        fake.responses.append(good[:-1] + bytes([good[-1] ^ 0xFF]))
        bus = make_bus(fake)
        with pytest.raises(ModbusCRCError):
            bus.read_registers(1, 0, 1)

    def test_retry_recovers_after_one_bad_frame(self):
        fake = FakeSerial()
        fake.responses.append(b"")  # timeout first
        fake.responses.append(fc03_response(1, [0x0064]))
        bus = make_bus(fake, retries=2)
        assert bus.read_registers(1, 0, 1) == [0x0064]

    def test_exception_response_raises_and_does_not_retry(self):
        fake = FakeSerial()
        fake.responses.append(append_crc(b"\x01\x83\x02"))  # illegal data address
        bus = make_bus(fake, retries=3)
        with pytest.raises(ModbusExceptionResponse) as exc:
            bus.read_registers(1, 0x9999, 1)
        assert exc.value.exception_code == 0x02
        assert len(fake.writes) == 1  # definitive answer - no retry

    def test_scan_finds_responding_addresses(self):
        fake = FakeSerial()
        # Address 1 answers, 2 silent, 3 answers
        fake.responses.append(fc03_response(1, [0x0001]))
        fake.responses.append(b"")
        fake.responses.append(fc03_response(3, [0x0001]))
        bus = make_bus(fake)
        assert bus.scan(addresses=[1, 2, 3]) == [1, 3]

    def test_query_single_address_broadcast(self):
        fake = FakeSerial()
        # Device configured as address 7 answers the 0xFE broadcast with its own address
        fake.responses.append(fc03_response(7, [0x0001]))
        bus = make_bus(fake)
        assert bus.query_single_address() == 7
        assert fake.writes[0][:2] == b"\xfe\x03"

    def test_query_single_address_silence_returns_none(self):
        fake = FakeSerial()
        bus = make_bus(fake)
        assert bus.query_single_address() is None

    def test_close_is_idempotent(self):
        fake = FakeSerial()
        bus = make_bus(fake)
        fake.responses.append(fc03_response(1, [1]))
        bus.read_registers(1, 0, 1)
        bus.close()
        bus.close()
        assert fake.closed
