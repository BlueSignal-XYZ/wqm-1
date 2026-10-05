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


class TestHostSerialFallbacks:
    """Allwinner hosts print no `Serial` line in cpuinfo; U-Boot's device-tree
    serial-number and the SID eFuse stand in. The Pi path is untouched."""

    @staticmethod
    def _opener(files):
        from io import BytesIO, StringIO

        def fake_open(path, mode="r", *a, **k):
            if path not in files:
                raise FileNotFoundError(path)
            data = files[path]
            return BytesIO(data) if "b" in mode else StringIO(data)

        return fake_open

    def test_device_tree_serial_when_cpuinfo_has_no_serial_line(self, mock_hardware):
        files = {
            "/proc/cpuinfo": "processor\t: 0\nBogoMIPS\t: 48.00\n",
            "/proc/device-tree/serial-number": b"0c8b5e1d7a2f4b60\x00",
        }
        with patch("builtins.open", self._opener(files)):
            from utils.identity import get_host_serial, get_pi_serial

            assert get_pi_serial() == "0c8b5e1d7a2f4b60"
            assert get_host_serial() == "0c8b5e1d7a2f4b60"

    def test_sid_efuse_is_the_last_resort(self, mock_hardware):
        files = {
            "/proc/cpuinfo": "processor\t: 0\n",
            "/sys/bus/nvmem/devices/sunxi-sid0/nvmem": bytes(range(1, 17)),
        }
        with patch("builtins.open", self._opener(files)):
            from utils.identity import get_pi_serial

            assert get_pi_serial() == "0102030405060708"

    def test_cpuinfo_serial_still_wins_on_a_pi(self, mock_hardware):
        files = {
            "/proc/cpuinfo": "Serial\t\t: 10000000abcdef01\n",
            "/proc/device-tree/serial-number": b"ffffffffffffffff\x00",
        }
        with patch("builtins.open", self._opener(files)):
            from utils.identity import get_pi_serial

            assert get_pi_serial() == "10000000abcdef01"

    def test_all_zero_serial_is_not_a_serial(self, mock_hardware):
        files = {
            "/proc/cpuinfo": "Serial\t\t: 0000000000000000\n",
            "/proc/device-tree/serial-number": b"0c8b5e1d7a2f4b60",
        }
        with patch("builtins.open", self._opener(files)):
            from utils.identity import get_pi_serial

            assert get_pi_serial() == "0c8b5e1d7a2f4b60"

    def test_long_device_tree_serial_keeps_its_last_16_digits(self, mock_hardware):
        files = {"/proc/device-tree/serial-number": b"1234567890abcdef0123\x00"}
        with patch("builtins.open", self._opener(files)):
            from utils.identity import get_pi_serial

            assert get_pi_serial() == "567890abcdef0123"
