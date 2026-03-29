"""Tests for firmware/src/utils/identity.py — device identity generation."""

from unittest.mock import mock_open, patch


class TestPiSerial:
    def test_parse_serial_from_cpuinfo(self, mock_hardware):
        cpuinfo = "processor\t: 0\nmodel name\t: ARMv7\nSerial\t\t: 10000000abcdef01\n"
        with patch("builtins.open", mock_open(read_data=cpuinfo)):
            from utils.identity import get_pi_serial

            serial = get_pi_serial()
            assert serial == "10000000abcdef01"

    def test_fallback_on_missing_file(self, mock_hardware):
        with patch("builtins.open", side_effect=FileNotFoundError):
            from utils.identity import get_pi_serial

            serial = get_pi_serial()
            assert serial == "0000000000000000"


class TestDeviceId:
    def test_format(self, mock_hardware):
        from utils.identity import get_device_id

        device_id = get_device_id("10000000abcdef01")
        assert device_id == "BS-WQM1-0000abcdef01"

    def test_uses_last_12_chars(self, mock_hardware):
        from utils.identity import get_device_id

        device_id = get_device_id("1234567890abcdef")
        assert device_id == "BS-WQM1-567890abcdef"


class TestDevEUI:
    def test_format(self, mock_hardware):
        from utils.identity import get_dev_eui

        eui = get_dev_eui("10000000abcdef01")
        assert eui == bytes.fromhex("0018B200abcdef01")
        assert len(eui) == 8

    def test_oui_prefix(self, mock_hardware):
        from utils.identity import get_dev_eui

        eui = get_dev_eui("0000000012345678")
        assert eui[:4] == bytes.fromhex("0018B200")


class TestAppEUI:
    def test_constant(self, mock_hardware):
        from utils.identity import APP_EUI

        assert bytes.fromhex("0000000000000000") == APP_EUI
        assert len(APP_EUI) == 8


class TestBLEName:
    def test_format(self, mock_hardware):
        from utils.identity import get_ble_name

        name = get_ble_name("BS-WQM1-0000abcdef01")
        assert name == "BlueSignal-ef01"
        assert name.startswith("BlueSignal-")
