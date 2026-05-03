"""Tests for the StatusLEDs controller (control/led.py)."""

import sys
import threading
import time
from unittest.mock import call, patch

import pytest


@pytest.fixture(autouse=True)
def _mock_gpio():
    """Ensure RPi.GPIO is mocked (conftest handles this)."""
    gpio = sys.modules["RPi.GPIO"]
    gpio.reset_mock()
    yield gpio


class TestStatusLEDsInit:
    def test_init_sets_up_all_four_led_pins(self, _mock_gpio):
        from control.led import StatusLEDs

        leds = StatusLEDs()
        gpio = _mock_gpio
        assert gpio.setmode.called
        setup_calls = gpio.setup.call_args_list
        led_pins = {24, 25, 12, 13}
        setup_pins = {c[0][0] for c in setup_calls}
        assert led_pins.issubset(setup_pins)
        leds.cleanup()

    def test_init_registers_atexit_cleanup(self, _mock_gpio):
        import atexit

        from control.led import StatusLEDs

        with patch.object(atexit, "register") as mock_reg:
            leds = StatusLEDs()
            mock_reg.assert_called_once_with(leds.cleanup)
            leds.cleanup()


class TestLEDOnOff:
    def test_set_on_outputs_high(self, _mock_gpio):
        from control.led import StatusLEDs

        leds = StatusLEDs()
        leds.set(24, True)
        _mock_gpio.output.assert_called_with(24, _mock_gpio.HIGH)
        leds.cleanup()

    def test_set_off_outputs_low(self, _mock_gpio):
        from control.led import StatusLEDs

        leds = StatusLEDs()
        leds.set(25, False)
        _mock_gpio.output.assert_called_with(25, _mock_gpio.LOW)
        leds.cleanup()

    def test_on_convenience(self, _mock_gpio):
        from control.led import StatusLEDs

        leds = StatusLEDs()
        leds.on(12)
        _mock_gpio.output.assert_called_with(12, _mock_gpio.HIGH)
        leds.cleanup()

    def test_off_convenience(self, _mock_gpio):
        from control.led import StatusLEDs

        leds = StatusLEDs()
        leds.off(13)
        _mock_gpio.output.assert_called_with(13, _mock_gpio.LOW)
        leds.cleanup()


class TestBlink:
    def test_blink_toggles_pin(self, _mock_gpio):
        from control.led import StatusLEDs

        leds = StatusLEDs()
        with patch("time.sleep"):
            leds.blink(24, count=2, on_s=0, off_s=0)
        on_calls = [c for c in _mock_gpio.output.call_args_list if c == call(24, _mock_gpio.HIGH)]
        off_calls = [c for c in _mock_gpio.output.call_args_list if c == call(24, _mock_gpio.LOW)]
        assert len(on_calls) == 2
        assert len(off_calls) >= 2
        leds.cleanup()


class TestStartupTest:
    def test_startup_test_lights_all_leds(self, _mock_gpio):
        from control.led import StatusLEDs

        leds = StatusLEDs()
        with patch("time.sleep"):
            leds.startup_test()
        on_calls = [c for c in _mock_gpio.output.call_args_list if c[0][1] == _mock_gpio.HIGH]
        assert len(on_calls) >= 4
        leds.cleanup()


class TestHeartbeat:
    def test_heartbeat_start_stop(self, _mock_gpio):
        from control.led import StatusLEDs

        leds = StatusLEDs()
        original_sleep = time.sleep

        call_count = 0

        def fast_sleep(secs):
            nonlocal call_count
            call_count += 1
            if call_count > 4:
                leds._heartbeat_running = False
            original_sleep(0.001)

        with patch("time.sleep", side_effect=fast_sleep):
            leds.heartbeat_start()
            leds._heartbeat_thread.join(timeout=2.0)

        assert not leds._heartbeat_running or call_count > 4
        leds.cleanup()

    def test_heartbeat_start_idempotent(self, _mock_gpio):
        from control.led import StatusLEDs

        leds = StatusLEDs()
        leds._heartbeat_running = True
        leds.heartbeat_start()
        assert leds._heartbeat_thread is None
        leds._heartbeat_running = False
        leds.cleanup()

    def test_heartbeat_stop_turns_off_led(self, _mock_gpio):
        from control.led import StatusLEDs

        leds = StatusLEDs()
        leds._heartbeat_running = True
        t = threading.Thread(target=lambda: None)
        t.start()
        t.join()
        leds._heartbeat_thread = t
        leds.heartbeat_stop()
        assert not leds._heartbeat_running
        assert leds._heartbeat_thread is None
        leds.cleanup()


class TestConvenienceMethods:
    def test_lora_tx_on_off(self, _mock_gpio):
        from control.led import StatusLEDs

        leds = StatusLEDs()
        leds.lora_tx_on()
        _mock_gpio.output.assert_called_with(25, _mock_gpio.HIGH)
        leds.lora_tx_off()
        _mock_gpio.output.assert_called_with(25, _mock_gpio.LOW)
        leds.cleanup()

    def test_gps_fix_on_off(self, _mock_gpio):
        from control.led import StatusLEDs

        leds = StatusLEDs()
        leds.gps_fix_on()
        _mock_gpio.output.assert_called_with(12, _mock_gpio.HIGH)
        leds.gps_fix_off()
        _mock_gpio.output.assert_called_with(12, _mock_gpio.LOW)
        leds.cleanup()

    def test_error_on_off(self, _mock_gpio):
        from control.led import StatusLEDs

        leds = StatusLEDs()
        leds.error_on()
        _mock_gpio.output.assert_called_with(13, _mock_gpio.HIGH)
        leds.error_off()
        _mock_gpio.output.assert_called_with(13, _mock_gpio.LOW)
        leds.cleanup()

    def test_error_pattern(self, _mock_gpio):
        from control.led import StatusLEDs

        leds = StatusLEDs()
        with patch("time.sleep"):
            leds.error_pattern(3)
        on_calls = [c for c in _mock_gpio.output.call_args_list if c == call(13, _mock_gpio.HIGH)]
        assert len(on_calls) == 3
        leds.cleanup()


class TestCleanup:
    def test_cleanup_turns_all_leds_off(self, _mock_gpio):
        from control.led import StatusLEDs

        leds = StatusLEDs()
        _mock_gpio.output.reset_mock()
        leds.cleanup()
        for pin in [24, 25, 12, 13]:
            assert call(pin, _mock_gpio.LOW) in _mock_gpio.output.call_args_list

    def test_cleanup_handles_gpio_exception(self, _mock_gpio):
        from control.led import StatusLEDs

        leds = StatusLEDs()
        _mock_gpio.output.side_effect = RuntimeError("GPIO error")
        leds.cleanup()
        _mock_gpio.output.side_effect = None
