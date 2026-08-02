"""Tests for src/sensors/honde.py — Honde RS485 probe drivers."""

import struct

import pytest

from sensors.honde import HondeChlorineSensor, HondeMultiSensor, HondeOrpSensor
from sensors.modbus import ModbusBus, append_crc, encode_float_cdab


class FakeSerial:
    def __init__(self) -> None:
        self.responses: list[bytes] = []
        self.writes: list[bytes] = []

    def reset_input_buffer(self) -> None:
        pass

    def write(self, data: bytes) -> int:
        self.writes.append(bytes(data))
        return len(data)

    def read(self, n: int) -> bytes:
        if not self.responses:
            return b""
        return self.responses.pop(0)[:n]

    def close(self) -> None:
        pass


def make_bus(fake: FakeSerial) -> ModbusBus:
    return ModbusBus("/dev/ttyTEST", retries=1, serial_factory=lambda: fake, sleep=lambda _s: None)


def fc03_response(address: int, regs: list[int]) -> bytes:
    body = struct.pack(">BBB", address, 0x03, len(regs) * 2)
    for r in regs:
        body += struct.pack(">H", r)
    return append_crc(body)


def fc06_echo(address: int, register: int, value: int) -> bytes:
    return append_crc(struct.pack(">BBHH", address, 0x06, register, value))


def fc16_echo(address: int, start: int, count: int) -> bytes:
    return append_crc(struct.pack(">BBHH", address, 0x10, start, count))


class TestChlorine:
    def test_read_value(self):
        fake = FakeSerial()
        fake.responses.append(fc03_response(1, list(encode_float_cdab(0.35))))
        sensor = HondeChlorineSensor(make_bus(fake), address=1)
        assert sensor.read() == pytest.approx(0.35, abs=0.001)
        # Value lives at register 0x0001, two registers (float)
        assert fake.writes[0][:6] == bytes.fromhex("010300010002")

    def test_read_returns_none_on_silence(self):
        sensor = HondeChlorineSensor(make_bus(FakeSerial()))
        assert sensor.read() is None

    def test_read_rejects_out_of_range(self):
        fake = FakeSerial()
        fake.responses.append(fc03_response(1, list(encode_float_cdab(999.0))))
        sensor = HondeChlorineSensor(make_bus(fake))
        assert sensor.read() is None

    def test_read_rejects_nan(self):
        fake = FakeSerial()
        fake.responses.append(fc03_response(1, [0x0001, 0x7FC0]))  # NaN in CDAB
        sensor = HondeChlorineSensor(make_bus(fake))
        assert sensor.read() is None

    def test_read_warning_normal(self):
        fake = FakeSerial()
        fake.responses.append(fc03_response(1, [0x0000]))
        assert HondeChlorineSensor(make_bus(fake)).read_warning() == 0

    def test_calibrate_zero_writes_magic(self):
        fake = FakeSerial()
        fake.responses.append(fc06_echo(1, 0x003E, 0x00FF))
        HondeChlorineSensor(make_bus(fake)).calibrate_zero()
        assert fake.writes[0] == bytes.fromhex("0106003E00FFA846")  # datasheet frame

    def test_calibrate_slope_writes_reference_then_confirm(self):
        fake = FakeSerial()
        fake.responses.append(fc16_echo(1, 0x0024, 2))
        fake.responses.append(fc06_echo(1, 0x003F, 0x00FF))
        HondeChlorineSensor(make_bus(fake)).calibrate_slope(1.0)
        assert fake.writes[0] == bytes.fromhex("011000240002040000" + "3F80" + "E014")
        assert fake.writes[1] == bytes.fromhex("0106003F00FFF986")

    def test_calibrate_slope_rejects_bad_reference(self):
        sensor = HondeChlorineSensor(make_bus(FakeSerial()))
        with pytest.raises(ValueError):
            sensor.calibrate_slope(0.0)
        with pytest.raises(ValueError):
            sensor.calibrate_slope(50.0)

    def test_set_address_uses_chlorine_register(self):
        fake = FakeSerial()
        fake.responses.append(fc06_echo(1, 0x0019, 2))
        sensor = HondeChlorineSensor(make_bus(fake), address=1)
        sensor.set_address(2)
        assert fake.writes[0] == bytes.fromhex("010600190002D9CC")  # datasheet frame
        assert sensor.address == 2

    def test_set_address_validates(self):
        sensor = HondeChlorineSensor(make_bus(FakeSerial()))
        with pytest.raises(ValueError):
            sensor.set_address(0)
        with pytest.raises(ValueError):
            sensor.set_address(248)


class TestOrp:
    def test_read_positive(self):
        fake = FakeSerial()
        fake.responses.append(fc03_response(1, [0x0172]))  # 370 mV
        assert HondeOrpSensor(make_bus(fake)).read() == 370.0

    def test_read_negative(self):
        fake = FakeSerial()
        fake.responses.append(fc03_response(1, [0xFF38]))  # -200 mV
        assert HondeOrpSensor(make_bus(fake)).read() == -200.0

    def test_read_none_on_silence(self):
        assert HondeOrpSensor(make_bus(FakeSerial())).read() is None

    def test_read_rejects_out_of_range(self):
        fake = FakeSerial()
        fake.responses.append(fc03_response(1, [0xF000]))  # -4096 mV, impossible
        assert HondeOrpSensor(make_bus(fake)).read() is None

    def test_set_address_uses_family_register(self):
        fake = FakeSerial()
        fake.responses.append(fc06_echo(1, 0x0050, 2))
        sensor = HondeOrpSensor(make_bus(fake), address=1)
        sensor.set_address(2)
        assert fake.writes[0] == bytes.fromhex("010600500002081A")  # datasheet frame


class TestMulti:
    def test_read_all_datasheet_example(self):
        # Datasheet: 02AE 0064 00FA 0032 0032 -> pH 6.86, EC 100, 25.0°C, TDS 50, 0.5 ppt
        fake = FakeSerial()
        fake.responses.append(fc03_response(1, [0x02AE, 0x0064, 0x00FA, 0x0032, 0x0032]))
        values = HondeMultiSensor(make_bus(fake)).read_all()
        assert values == {
            "ph": 6.86,
            "conductivity_uscm": 100.0,
            "temp_c": 25.0,
            "tds_ppm": 50.0,
            "salinity_ppt": 0.5,
        }
        assert fake.writes[0] == bytes.fromhex("01030000000585C9")  # datasheet frame

    def test_read_all_none_on_silence(self):
        assert HondeMultiSensor(make_bus(FakeSerial())).read_all() is None

    def test_ph_calibration_points(self):
        for buffer, frame in (
            ("9.18", "0106005A000A29DE"),
            ("6.86", "0106005B000BB9DE"),
            ("4.01", "0106005C000C49DD"),
        ):
            fake = FakeSerial()
            fake.responses.append(bytes.fromhex(frame))
            HondeMultiSensor(make_bus(fake)).calibrate_ph_point(buffer)
            assert fake.writes[0] == bytes.fromhex(frame)  # datasheet frames

    def test_ph_calibration_rejects_unknown_buffer(self):
        with pytest.raises(ValueError):
            HondeMultiSensor(make_bus(FakeSerial())).calibrate_ph_point("7.00")

    def test_set_ph_offset_datasheet_frame(self):
        fake = FakeSerial()
        fake.responses.append(fc06_echo(1, 0x000B, 0x0064))
        HondeMultiSensor(make_bus(fake)).set_ph_offset(1.0)
        assert fake.writes[0] == bytes.fromhex("0106000B0064F9E3")

    def test_set_ec_slope_datasheet_frame(self):
        fake = FakeSerial()
        fake.responses.append(fc06_echo(1, 0x000A, 0x04B0))
        HondeMultiSensor(make_bus(fake)).set_ec_slope(1.2)
        assert fake.writes[0] == bytes.fromhex("0106000A04B0AABC")

    def test_slope_and_offset_validation(self):
        sensor = HondeMultiSensor(make_bus(FakeSerial()))
        with pytest.raises(ValueError):
            sensor.set_ec_slope(0.0)
        with pytest.raises(ValueError):
            sensor.set_ph_offset(20.0)

    def test_negative_temperature_decodes(self):
        fake = FakeSerial()
        fake.responses.append(fc03_response(1, [700, 500, 0xFFF6, 100, 10]))  # -1.0°C
        values = HondeMultiSensor(make_bus(fake)).read_all()
        assert values is not None
        assert values["temp_c"] == -1.0
