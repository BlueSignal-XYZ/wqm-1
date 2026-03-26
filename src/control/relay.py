"""
4-Channel Relay Controller

Controls relays via GPIO (active-high through LTV-354T optocoupler + S8050).
All relays are forced OFF at init and on cleanup via atexit.
"""

import atexit
import logging

import RPi.GPIO as GPIO
from utils.config import RELAY_PINS

logger = logging.getLogger("wqm1.relay")


class RelayController:
    """Controls 4 relays on the WQM-1 HAT."""

    def __init__(self):
        self._pins = RELAY_PINS
        self._state = 0  # 4-bit bitmask (bit 0 = relay 1)

        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        for pin in self._pins:
            GPIO.setup(pin, GPIO.OUT, initial=GPIO.LOW)

        atexit.register(self.cleanup)
        logger.info("Relay controller initialised, all OFF")

    def set(self, channel: int, state: bool) -> None:
        """
        Set a relay on or off.

        Args:
            channel: 1-4
            state: True = energised (NO closed), False = de-energised
        """
        if not 1 <= channel <= 4:
            raise ValueError(f"Relay channel must be 1-4, got {channel}")
        pin = self._pins[channel - 1]
        GPIO.output(pin, GPIO.HIGH if state else GPIO.LOW)
        if state:
            self._state |= 1 << (channel - 1)
        else:
            self._state &= ~(1 << (channel - 1))
        logger.debug("Relay %d %s (GPIO %d)", channel, "ON" if state else "OFF", pin)

    def get(self, channel: int) -> bool:
        """Check if a relay is currently on."""
        if not 1 <= channel <= 4:
            raise ValueError(f"Relay channel must be 1-4, got {channel}")
        return bool(self._state & (1 << (channel - 1)))

    def all_off(self) -> None:
        """Turn all relays off."""
        for pin in self._pins:
            GPIO.output(pin, GPIO.LOW)
        self._state = 0
        logger.info("All relays OFF")

    def get_state_bitmask(self) -> int:
        """Return relay state as 4-bit integer (for DB storage)."""
        return self._state

    def cleanup(self) -> None:
        """Force all relays off and release GPIO."""
        try:
            for pin in self._pins:
                try:
                    GPIO.output(pin, GPIO.LOW)
                except Exception:
                    pass
            self._state = 0
        except Exception:
            pass
        logger.info("Relay cleanup complete")
