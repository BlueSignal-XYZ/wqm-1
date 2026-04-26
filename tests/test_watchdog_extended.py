"""Extended tests for watchdog module (utils/watchdog.py)."""

from unittest.mock import mock_open, patch


class TestGetCpuTemp:
    def test_reads_sysfs_temperature(self):
        from utils.watchdog import get_cpu_temp

        m = mock_open(read_data="45000")
        with patch("builtins.open", m):
            assert get_cpu_temp() == 45.0

    def test_returns_25_on_failure(self):
        from utils.watchdog import get_cpu_temp

        with patch("builtins.open", side_effect=FileNotFoundError):
            assert get_cpu_temp() == 25.0

    def test_reads_fractional_temperature(self):
        from utils.watchdog import get_cpu_temp

        m = mock_open(read_data="52300")
        with patch("builtins.open", m):
            assert get_cpu_temp() == 52.3


class TestHardwareWatchdog:
    def test_init_opens_dev_watchdog(self):
        from utils.watchdog import HardwareWatchdog

        m = mock_open()
        with patch("builtins.open", m):
            wd = HardwareWatchdog()
            m.assert_called_once_with("/dev/watchdog", "wb", buffering=0)
            assert wd._fd is not None

    def test_init_handles_missing_device(self):
        from utils.watchdog import HardwareWatchdog

        with patch("builtins.open", side_effect=FileNotFoundError):
            wd = HardwareWatchdog()
            assert wd._fd is None

    def test_pet_writes_to_watchdog(self):
        from utils.watchdog import HardwareWatchdog

        m = mock_open()
        with patch("builtins.open", m):
            wd = HardwareWatchdog()
            wd.pet()
            wd._fd.write.assert_called_with(b"\x00")

    def test_pet_does_nothing_when_no_fd(self):
        from utils.watchdog import HardwareWatchdog

        with patch("builtins.open", side_effect=FileNotFoundError):
            wd = HardwareWatchdog()
            wd.pet()

    def test_pet_suppresses_write_error(self):
        from utils.watchdog import HardwareWatchdog

        m = mock_open()
        with patch("builtins.open", m):
            wd = HardwareWatchdog()
            wd._fd.write.side_effect = OSError("write failed")
            wd.pet()

    def test_close_writes_magic_close_character(self):
        from utils.watchdog import HardwareWatchdog

        m = mock_open()
        with patch("builtins.open", m):
            wd = HardwareWatchdog()
            fd = wd._fd
            wd.close()
            fd.write.assert_called_with(b"V")
            assert wd._fd is None

    def test_close_does_nothing_when_no_fd(self):
        from utils.watchdog import HardwareWatchdog

        with patch("builtins.open", side_effect=FileNotFoundError):
            wd = HardwareWatchdog()
            wd.close()

    def test_close_suppresses_error(self):
        from utils.watchdog import HardwareWatchdog

        m = mock_open()
        with patch("builtins.open", m):
            wd = HardwareWatchdog()
            wd._fd.write.side_effect = OSError("close failed")
            wd.close()
            assert wd._fd is None


class TestFanControllerExtended:
    def test_update_without_arg_uses_cpu_temp(self):
        from utils.watchdog import FanController

        fc = FanController(on_temp=60.0, off_temp=55.0)
        with patch("utils.watchdog.get_cpu_temp", return_value=65.0):
            changed = fc.update()
            assert changed is True
            assert fc.is_on is True

    def test_hysteresis_holds_state_between_thresholds(self):
        from utils.watchdog import FanController

        fc = FanController(on_temp=60.0, off_temp=55.0)
        fc.update(cpu_temp=65.0)
        assert fc.is_on is True
        changed = fc.update(cpu_temp=57.0)
        assert changed is False
        assert fc.is_on is True

    def test_turns_off_below_off_threshold(self):
        from utils.watchdog import FanController

        fc = FanController(on_temp=60.0, off_temp=55.0)
        fc.update(cpu_temp=65.0)
        changed = fc.update(cpu_temp=50.0)
        assert changed is True
        assert fc.is_on is False

    def test_no_change_returns_false(self):
        from utils.watchdog import FanController

        fc = FanController(on_temp=60.0, off_temp=55.0)
        changed = fc.update(cpu_temp=50.0)
        assert changed is False

    def test_cleanup_sets_gpio_low(self):
        from utils.watchdog import FanController

        fc = FanController()
        fc.update(cpu_temp=70.0)
        fc.cleanup()
        assert fc.is_on is False
