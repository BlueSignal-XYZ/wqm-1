"""
4-Channel Relay Controller

Controls relays via GPIO (active-high through LTV-354T optocoupler + S8050).
All relays are forced OFF at init and on cleanup via atexit.

Timing is enforced HERE, not in whoever asked for the relay. A rule's
``duration_s``, a cloud command's ``durationSeconds`` and a LoRa downlink's
duration all arm the same per-channel timer thread, so an auto-off fires on
time even if the sampling loop that used to sweep it is stalled, in backoff,
or has nothing to sample. ``max_on_s`` (policies.yaml
``limits.max_continuous_on_minutes``) is a hard ceiling on ANY on-period from
ANY source: with it set, no coil can stay energised longer than that without a
fresh request.
"""

import atexit
import contextlib
import logging
import threading

from utils.config import RELAY_PINS

logger = logging.getLogger("wqm1.relay")

try:
    import RPi.GPIO as GPIO
except ImportError:  # non-Pi host (e.g. Arduino UNO Q): relays need the
    GPIO = None  # Bridge companion sketch — the board gate never builds this


class RelayController:
    """Controls 4 relays on the WQM-1 HAT."""

    def __init__(self, max_on_s: float = 0.0) -> None:
        if GPIO is None:
            raise RuntimeError("RPi.GPIO not installed — no direct GPIO on this host")
        self._pins = RELAY_PINS
        self._state = 0  # 4-bit bitmask (bit 0 = relay 1)
        self._lock = threading.RLock()
        self._timers: dict[int, threading.Timer] = {}
        # Hard ceiling on one continuous on-period, seconds. 0 = no ceiling.
        self.max_on_s = float(max_on_s or 0.0)

        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        for pin in self._pins:
            GPIO.setup(pin, GPIO.OUT, initial=GPIO.LOW)

        atexit.register(self.cleanup)
        logger.info("Relay controller initialised, all OFF")

    @staticmethod
    def _check_channel(channel: int) -> None:
        if not 1 <= channel <= 4:
            raise ValueError(f"Relay channel must be 1-4, got {channel}")

    def set(self, channel: int, state: bool) -> None:
        """
        Set a relay on or off.

        Turning a relay OFF cancels any pending auto-off. Turning it ON with
        no duration arms only the ``max_on_s`` ceiling (if one is set); use
        :meth:`arm_auto_off` for a bounded on-period.

        Args:
            channel: 1-4
            state: True = energised (NO closed), False = de-energised
        """
        self._check_channel(channel)
        pin = self._pins[channel - 1]
        with self._lock:
            GPIO.output(pin, GPIO.HIGH if state else GPIO.LOW)
            if state:
                self._state |= 1 << (channel - 1)
            else:
                self._state &= ~(1 << (channel - 1))
            self._cancel_timer(channel)
            if state and self.max_on_s > 0:
                self._arm(channel, self.max_on_s, "maximum continuous on-time")
        logger.debug("Relay %d %s (GPIO %d)", channel, "ON" if state else "OFF", pin)

    def arm_auto_off(self, channel: int, duration_s: float) -> float:
        """
        Turn the relay off after ``duration_s`` seconds (replacing any earlier
        timer for the channel). A no-op when the relay is not on. Returns the
        seconds actually armed — shortened to ``max_on_s`` when that is lower.
        """
        self._check_channel(channel)
        seconds = float(duration_s)
        if seconds <= 0:
            return 0.0
        with self._lock:
            if not self.get(channel):
                return 0.0
            if self.max_on_s > 0:
                seconds = min(seconds, self.max_on_s)
            self._arm(channel, seconds, f"{duration_s:g} s duration")
        return seconds

    def pending_auto_off(self, channel: int) -> bool:
        """True when an auto-off timer is armed for the channel."""
        with self._lock:
            return channel in self._timers

    def _arm(self, channel: int, seconds: float, why: str) -> None:
        self._cancel_timer(channel)
        timer = threading.Timer(seconds, self._auto_off, args=(channel, seconds, why))
        timer.daemon = True
        timer.name = f"wqm1-relay{channel}-autooff"
        self._timers[channel] = timer
        timer.start()

    def _cancel_timer(self, channel: int) -> None:
        timer = self._timers.pop(channel, None)
        if timer is not None:
            timer.cancel()

    def _auto_off(self, channel: int, seconds: float, why: str) -> None:
        with self._lock:
            # A newer timer (or a manual OFF) has already superseded this one.
            if self._timers.get(channel) is not threading.current_thread():
                return
            self._timers.pop(channel, None)
            if not self.get(channel):
                return
            GPIO.output(self._pins[channel - 1], GPIO.LOW)
            self._state &= ~(1 << (channel - 1))
        logger.warning("Relay %d auto-off after %.0f s (%s)", channel, seconds, why)

    def on(self, channel: int) -> None:
        """Turn a relay on (convenience wrapper for set)."""
        self.set(channel, True)

    def off(self, channel: int) -> None:
        """Turn a relay off (convenience wrapper for set)."""
        self.set(channel, False)

    def get(self, channel: int) -> bool:
        """Check if a relay is currently on."""
        self._check_channel(channel)
        return bool(self._state & (1 << (channel - 1)))

    def all_off(self) -> None:
        """Turn all relays off."""
        with self._lock:
            for ch in range(1, 5):
                self._cancel_timer(ch)
            for pin in self._pins:
                GPIO.output(pin, GPIO.LOW)
            self._state = 0
        logger.info("All relays OFF")

    def get_state_bitmask(self) -> int:
        """Return relay state as 4-bit integer (for DB storage)."""
        return self._state

    def cleanup(self) -> None:
        """Force all relays off and release GPIO."""
        with contextlib.suppress(Exception):
            for ch in range(1, 5):
                with contextlib.suppress(Exception):
                    self._cancel_timer(ch)
            for pin in self._pins:
                with contextlib.suppress(Exception):
                    GPIO.output(pin, GPIO.LOW)
            self._state = 0
        logger.info("Relay cleanup complete")
