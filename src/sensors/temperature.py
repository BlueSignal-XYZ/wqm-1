"""
DS18B20 1-Wire Temperature Sensor Driver

Uses the w1thermsensor library which reads from the kernel 1-Wire driver
(/sys/bus/w1/devices). Requires dtoverlay=w1-gpio,gpiopin=4 in config.txt.
"""

import logging

logger = logging.getLogger("wqm1.onewire")

try:
    from w1thermsensor import NoSensorFoundError, W1ThermSensor

    _W1_AVAILABLE = True
except ImportError:
    _W1_AVAILABLE = False
    logger.warning("w1thermsensor not installed — DS18B20 unavailable")


class DS18B20:
    """DS18B20 temperature probe on 1-Wire bus."""

    def __init__(self):
        self._sensor = None
        if _W1_AVAILABLE:
            try:
                self._sensor = W1ThermSensor()
                logger.info("DS18B20 found: %s", self._sensor.id)
            except NoSensorFoundError:
                logger.warning("No DS18B20 sensor detected on 1-Wire bus")
            except Exception as e:
                logger.warning("DS18B20 init error: %s", e)

    def available(self) -> bool:
        """Check if a DS18B20 sensor is present."""
        return self._sensor is not None

    def read_temp_c(self) -> float:
        """
        Read temperature in °C.

        Returns:
            Temperature in Celsius, or None if sensor is unavailable.
        """
        if self._sensor is None:
            return None
        try:
            return self._sensor.get_temperature()
        except Exception as e:
            logger.warning("DS18B20 read failed: %s", e)
            return None
