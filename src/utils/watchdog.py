"""
Fan Controller + Hardware Watchdog

Fan: On/off via GPIO21 (BCX56-16 NPN), hysteresis 60/55°C.
Watchdog: the host's hardware watchdog (/dev/watchdog — BCM2835 on the Pi,
          sunxi-wdt on the Orange Pi), must be pet periodically or the
          system reboots.
"""

import atexit
import contextlib
import logging
from typing import IO

from platform_support.gpio import HostGpio, open_gpio
from utils.config import FAN_EN

logger = logging.getLogger("wqm1.fan")

try:
    import RPi.GPIO as GPIO
except ImportError:  # not a Raspberry Pi: the facade drives a gpiochip host
    GPIO = None  # (Orange Pi) or refuses on a headerless one (Arduino Q)


def get_cpu_temp() -> float:
    """Read CPU temperature in °C from sysfs. Returns 25.0 on failure."""
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return int(f.read().strip()) / 1000.0
    except Exception:
        return 25.0


class FanController:
    """On/off fan with temperature hysteresis."""

    def __init__(
        self, on_temp: float = 60.0, off_temp: float = 55.0, io: HostGpio | None = None
    ) -> None:
        self._io = io if io is not None else open_gpio(rpi_module=GPIO)
        self._on_temp = on_temp
        self._off_temp = off_temp
        self._is_on = False

        self._io.setup_output(FAN_EN, initial=False)

        atexit.register(self.cleanup)
        logger.info("Fan controller initialised (on=%.0f°C, off=%.0f°C)", on_temp, off_temp)

    def update(self, cpu_temp: float | None = None) -> bool:
        """
        Update fan state based on temperature. Uses CPU temp if not provided.

        Returns:
            True if fan state changed.
        """
        if cpu_temp is None:
            cpu_temp = get_cpu_temp()

        was_on = self._is_on

        if cpu_temp >= self._on_temp:
            self._set(True)
        elif cpu_temp <= self._off_temp:
            self._set(False)
        # Between thresholds: maintain current state

        if was_on != self._is_on:
            logger.info("Fan %s (CPU %.1f°C)", "ON" if self._is_on else "OFF", cpu_temp)
            return True
        return False

    def _set(self, state: bool) -> None:
        self._io.write(FAN_EN, state)
        self._is_on = state

    @property
    def is_on(self) -> bool:
        return self._is_on

    def cleanup(self) -> None:
        with contextlib.suppress(Exception):
            self._io.write(FAN_EN, False)
        self._is_on = False


class HardwareWatchdog:
    """The host's hardware watchdog (/dev/watchdog). Pet it or the system reboots."""

    def __init__(self) -> None:
        self._fd: IO[bytes] | None = None
        try:
            self._fd = open("/dev/watchdog", "wb", buffering=0)  # noqa: SIM115
            logger.info("Hardware watchdog enabled")
        except Exception as e:
            logger.info("Hardware watchdog not available: %s", e)

    def pet(self) -> None:
        """Write to watchdog to prevent reboot."""
        if self._fd:
            with contextlib.suppress(Exception):
                self._fd.write(b"\x00")

    def close(self) -> None:
        """Disable watchdog by writing 'V' (magic close)."""
        if self._fd:
            with contextlib.suppress(Exception):
                self._fd.write(b"V")
                self._fd.close()
            self._fd = None
