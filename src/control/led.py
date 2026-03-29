"""
Status LED Controller

4 LEDs with dedicated functions:
  LED1 (GPIO24) — heartbeat (1 Hz background blink)
  LED2 (GPIO25) — LoRa TX indicator
  LED3 (GPIO12) — GPS fix indicator
  LED4 (GPIO13) — error indicator
"""

import atexit
import contextlib
import logging
import threading
import time

import RPi.GPIO as GPIO

from utils.config import LED_ERROR, LED_GPS_FIX, LED_HEARTBEAT, LED_LORA_TX, LED_PINS

logger = logging.getLogger("wqm1.leds")


class StatusLEDs:
    """Controls 4 status LEDs on the WQM-1 HAT."""

    def __init__(self):
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        for pin in LED_PINS:
            GPIO.setup(pin, GPIO.OUT, initial=GPIO.LOW)

        self._heartbeat_thread = None
        self._heartbeat_running = False

        atexit.register(self.cleanup)
        logger.info("LED controller initialised")

    def set(self, led_pin: int, state: bool) -> None:
        """Set an LED on or off by GPIO pin number."""
        GPIO.output(led_pin, GPIO.HIGH if state else GPIO.LOW)

    def on(self, led_pin: int) -> None:
        self.set(led_pin, True)

    def off(self, led_pin: int) -> None:
        self.set(led_pin, False)

    def blink(self, led_pin: int, count: int = 3, on_s: float = 0.1, off_s: float = 0.1) -> None:
        """Blink an LED a fixed number of times (blocking)."""
        for i in range(count):
            self.on(led_pin)
            time.sleep(on_s)
            self.off(led_pin)
            if i < count - 1:
                time.sleep(off_s)

    def startup_test(self) -> None:
        """Flash all LEDs in sequence at startup."""
        for pin in LED_PINS:
            self.on(pin)
            time.sleep(0.15)
        time.sleep(0.3)
        for pin in LED_PINS:
            self.off(pin)
        logger.info("LED startup test complete")

    # --- Heartbeat (LED1) ---

    def heartbeat_start(self) -> None:
        """Start 1 Hz heartbeat blink on LED1 in a background thread."""
        if self._heartbeat_running:
            return
        self._heartbeat_running = True
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, daemon=True, name="heartbeat"
        )
        self._heartbeat_thread.start()
        logger.info("Heartbeat started")

    def heartbeat_stop(self) -> None:
        """Stop heartbeat."""
        self._heartbeat_running = False
        if self._heartbeat_thread:
            self._heartbeat_thread.join(timeout=2.0)
            self._heartbeat_thread = None
        self.off(LED_HEARTBEAT)

    def _heartbeat_loop(self) -> None:
        """Background heartbeat: 1 Hz (500 ms on, 500 ms off)."""
        while self._heartbeat_running:
            try:
                GPIO.output(LED_HEARTBEAT, GPIO.HIGH)
                time.sleep(0.5)
                GPIO.output(LED_HEARTBEAT, GPIO.LOW)
                time.sleep(0.5)
            except Exception:
                break

    # --- Convenience for named functions ---

    def lora_tx_on(self) -> None:
        self.on(LED_LORA_TX)

    def lora_tx_off(self) -> None:
        self.off(LED_LORA_TX)

    def gps_fix_on(self) -> None:
        self.on(LED_GPS_FIX)

    def gps_fix_off(self) -> None:
        self.off(LED_GPS_FIX)

    def error_on(self) -> None:
        self.on(LED_ERROR)

    def error_off(self) -> None:
        self.off(LED_ERROR)

    def error_pattern(self, count: int = 5) -> None:
        """Rapid blink on error LED."""
        self.blink(LED_ERROR, count=count, on_s=0.08, off_s=0.08)

    # --- Cleanup ---

    def cleanup(self) -> None:
        """Stop heartbeat and turn all LEDs off."""
        self._heartbeat_running = False
        for pin in LED_PINS:
            with contextlib.suppress(Exception):
                GPIO.output(pin, GPIO.LOW)
        logger.info("LED cleanup complete")
