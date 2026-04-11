"""Tests for power management — fan controller and CPU temperature."""

from unittest.mock import mock_open, patch


class TestGetCPUTemp:
    def test_read_normal_temp(self, mock_hardware):
        with patch("builtins.open", mock_open(read_data="55000\n")):
            from utils.watchdog import get_cpu_temp

            assert get_cpu_temp() == 55.0

    def test_read_high_temp(self, mock_hardware):
        with patch("builtins.open", mock_open(read_data="72500\n")):
            from utils.watchdog import get_cpu_temp

            assert get_cpu_temp() == 72.5

    def test_fallback_on_error(self, mock_hardware):
        with patch("builtins.open", side_effect=FileNotFoundError):
            from utils.watchdog import get_cpu_temp

            assert get_cpu_temp() == 25.0


class TestFanController:
    def test_fan_off_at_init(self, mock_hardware):
        from utils.watchdog import FanController

        fan = FanController()
        assert fan.is_on is False

    def test_fan_turns_on_above_threshold(self, mock_hardware):
        from utils.watchdog import FanController

        fan = FanController(on_temp=60.0, off_temp=55.0)
        changed = fan.update(cpu_temp=65.0)
        assert changed is True
        assert fan.is_on is True

    def test_fan_stays_off_below_on_threshold(self, mock_hardware):
        from utils.watchdog import FanController

        fan = FanController(on_temp=60.0, off_temp=55.0)
        changed = fan.update(cpu_temp=58.0)
        assert changed is False
        assert fan.is_on is False

    def test_fan_turns_off_below_off_threshold(self, mock_hardware):
        from utils.watchdog import FanController

        fan = FanController(on_temp=60.0, off_temp=55.0)
        fan.update(cpu_temp=65.0)  # turn on
        changed = fan.update(cpu_temp=50.0)  # below off threshold
        assert changed is True
        assert fan.is_on is False

    def test_fan_hysteresis_between_thresholds(self, mock_hardware):
        from utils.watchdog import FanController

        fan = FanController(on_temp=60.0, off_temp=55.0)
        fan.update(cpu_temp=65.0)  # turn on
        assert fan.is_on is True

        # Between thresholds (57°C) — fan should stay on
        changed = fan.update(cpu_temp=57.0)
        assert changed is False
        assert fan.is_on is True

    def test_fan_cleanup_turns_off(self, mock_hardware):
        from utils.watchdog import FanController

        fan = FanController()
        fan.update(cpu_temp=70.0)  # turn on
        fan.cleanup()
        assert fan.is_on is False

    def test_fan_uses_cpu_temp_if_not_provided(self, mock_hardware):
        with patch("utils.watchdog.get_cpu_temp", return_value=70.0):
            from utils.watchdog import FanController

            fan = FanController(on_temp=60.0, off_temp=55.0)
            fan.update()  # should read CPU temp and turn on
            assert fan.is_on is True

    def test_custom_thresholds(self, mock_hardware):
        from utils.watchdog import FanController

        fan = FanController(on_temp=50.0, off_temp=45.0)
        fan.update(cpu_temp=55.0)
        assert fan.is_on is True
        fan.update(cpu_temp=40.0)
        assert fan.is_on is False


class TestHardwareWatchdog:
    def test_init_no_device(self, mock_hardware):
        with patch("builtins.open", side_effect=FileNotFoundError):
            from utils.watchdog import HardwareWatchdog

            wd = HardwareWatchdog()
            assert wd._fd is None

    def test_pet_no_device_no_error(self, mock_hardware):
        with patch("builtins.open", side_effect=FileNotFoundError):
            from utils.watchdog import HardwareWatchdog

            wd = HardwareWatchdog()
            wd.pet()  # should not raise

    def test_close_no_device_no_error(self, mock_hardware):
        with patch("builtins.open", side_effect=FileNotFoundError):
            from utils.watchdog import HardwareWatchdog

            wd = HardwareWatchdog()
            wd.close()  # should not raise
