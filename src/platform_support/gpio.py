"""
One door to the HAT's digital lines, whatever host is underneath.

Every driver names its lines by the HAT's BCM numbers (``RELAY_PINS``,
``LORA_DIO1``, ``FAN_EN`` …) and asks this facade to drive them. The facade
picks the backend from the active board profile:

* ``rpi`` — the Raspberry Pi path, byte-for-byte what the drivers did before
  this module existed: ``RPi.GPIO`` in BCM mode for setup/output/input, and
  ``lgpio`` on gpiochip 0 for edge alerts (RPi.GPIO's ``add_event_detect``
  is broken on kernel 6.6+, which is why the SX1262 and the pulse meter
  already used lgpio there). A field unit sees no change.
* ``gpiochip`` — any Linux host whose header is a plain gpiochip (the
  Orange Pi Zero 3W): ``lgpio`` for everything, with the BCM number
  translated through :mod:`platform_support.hostpins` into the host's
  (chip, line).
* ``none`` — the headers belong to an MCU (Arduino Q family); opening the
  facade raises, and the board gate in main.py never asks.

The facade is deliberately small: outputs, inputs, rising-edge alerts.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from collections.abc import Callable
from typing import Any

from platform_support.hostpins import HostPins

logger = logging.getLogger("wqm1.gpio")

_MISSING = object()

#: lgpio's alert callback signature: (chip, gpio, level, tick).
AlertCallback = Callable[[int, int, int, int], None]


class AlertHandle:
    """A claimed edge alert; ``cancel()`` releases the callback and the line."""

    def __init__(self, cancel: Callable[[], None]) -> None:
        self._cancel = cancel
        self._done = False

    def cancel(self) -> None:
        if self._done:
            return
        self._done = True
        self._cancel()


class HostGpio:
    """Digital IO on the HAT's BCM-numbered nets for the active host."""

    def __init__(
        self,
        pins: HostPins,
        rpi_module: Any = None,
        lgpio_module: Any = None,
    ) -> None:
        self._pins = pins
        self._backend = pins.backend
        # Typed Any on purpose: these are hardware modules (or test fakes)
        # whose absence is checked once, below, at construction.
        self._rpi: Any = rpi_module
        self._lg: Any = lgpio_module
        self._lock = threading.Lock()
        # gpiochip backend: one handle per chip, opened on first use.
        self._handles: dict[int, Any] = {}
        # BCM nets this facade has claimed on the gpiochip backend (freed on close).
        self._claimed: dict[int, tuple[int, int]] = {}
        self._rpi_mode_set = False

        if self._backend == "rpi":
            if self._rpi is None:
                raise RuntimeError("RPi.GPIO not installed — no direct GPIO on this host")
        elif self._backend == "gpiochip":
            if self._lg is None:
                raise RuntimeError("lgpio not installed — no gpiochip access on this host")
        else:
            raise RuntimeError(f"no direct GPIO on this host ({pins.board_id})")

    @property
    def backend(self) -> str:
        return self._backend

    @property
    def pins(self) -> HostPins:
        return self._pins

    # --- RPi.GPIO backend -------------------------------------------------

    def _rpi_ready(self) -> None:
        if not self._rpi_mode_set:
            self._rpi.setmode(self._rpi.BCM)
            self._rpi.setwarnings(False)
            self._rpi_mode_set = True

    # --- lgpio backend ----------------------------------------------------

    def _handle(self, chip: int) -> Any:
        h = self._handles.get(chip)
        if h is None:
            h = self._lg.gpiochip_open(chip)
            self._handles[chip] = h
        return h

    def _chip_line(self, bcm: int) -> tuple[int, int]:
        return self._pins.line(bcm)

    # --- public API --------------------------------------------------------

    def setup_output(self, bcm: int, initial: bool = False) -> None:
        if self._backend == "rpi":
            self._rpi_ready()
            self._rpi.setup(
                bcm, self._rpi.OUT, initial=self._rpi.HIGH if initial else self._rpi.LOW
            )
            return
        chip, line = self._chip_line(bcm)
        with self._lock:
            self._lg.gpio_claim_output(self._handle(chip), line, 1 if initial else 0)
            self._claimed[bcm] = (chip, line)

    def setup_input(self, bcm: int) -> None:
        if self._backend == "rpi":
            self._rpi_ready()
            self._rpi.setup(bcm, self._rpi.IN)
            return
        chip, line = self._chip_line(bcm)
        with self._lock:
            self._lg.gpio_claim_input(self._handle(chip), line)
            self._claimed[bcm] = (chip, line)

    def write(self, bcm: int, state: bool) -> None:
        if self._backend == "rpi":
            self._rpi.output(bcm, self._rpi.HIGH if state else self._rpi.LOW)
            return
        chip, line = self._chip_line(bcm)
        self._lg.gpio_write(self._handle(chip), line, 1 if state else 0)

    def read(self, bcm: int) -> bool:
        if self._backend == "rpi":
            return bool(self._rpi.input(bcm))
        chip, line = self._chip_line(bcm)
        return bool(self._lg.gpio_read(self._handle(chip), line))

    def claim_alert(
        self,
        bcm: int,
        callback: AlertCallback,
        debounce_us: int | None = None,
    ) -> AlertHandle:
        """Rising-edge alert on a net. Both backends use lgpio for this."""
        lg = self._lg
        if lg is None:
            # The rpi backend only needs lgpio for alerts; import it here so a
            # Pi without it still drives relays, LEDs and the fan.
            try:
                import lgpio as lg  # type: ignore[no-redef]
            except ImportError as e:
                raise RuntimeError("lgpio not installed — edge alerts unavailable") from e
            self._lg = lg
        chip, line = (0, bcm) if self._backend == "rpi" else self._chip_line(bcm)
        with self._lock:
            handle = self._handle(chip)
        lg.gpio_claim_alert(handle, line, lg.RISING_EDGE)
        if debounce_us is not None:
            with_debounce = getattr(lg, "gpio_set_debounce_micros", None)
            if with_debounce is not None:
                with_debounce(handle, line, int(debounce_us))
        cb = lg.callback(handle, line, lg.RISING_EDGE, callback)

        def _cancel() -> None:
            with contextlib.suppress(Exception):
                cb.cancel()
            with contextlib.suppress(Exception):
                lg.gpio_free(handle, line)

        return AlertHandle(_cancel)

    def release(self, bcm: int) -> None:
        """Free a claimed line (gpiochip backend); a no-op on RPi.GPIO."""
        if self._backend != "gpiochip":
            return
        with self._lock:
            claimed = self._claimed.pop(bcm, None)
        if claimed is None:
            return
        chip, line = claimed
        with contextlib.suppress(Exception):
            self._lg.gpio_free(self._handles[chip], line)

    def close(self) -> None:
        """Free every line and chip handle this facade holds."""
        if self._backend != "gpiochip":
            return
        with self._lock:
            claimed = list(self._claimed.items())
            self._claimed.clear()
            handles = dict(self._handles)
            self._handles.clear()
        for _bcm, (chip, line) in claimed:
            with contextlib.suppress(Exception):
                self._lg.gpio_free(handles[chip], line)
        for h in handles.values():
            with contextlib.suppress(Exception):
                self._lg.gpiochip_close(h)


def open_gpio(
    rpi_module: Any = _MISSING,
    lgpio_module: Any = _MISSING,
    pins: HostPins | None = None,
) -> HostGpio:
    """
    A :class:`HostGpio` for the active board.

    Drivers pass their own module-level ``RPi.GPIO`` import as ``rpi_module``
    so the existing "RPi.GPIO not installed" guard (and the tests that pin
    it) keep their meaning on the Pi backend. ``lgpio_module`` is injected
    by tests; on hardware it is imported here.
    """
    from platform_support import active_pins

    resolved = pins if pins is not None else active_pins()
    rpi = rpi_module
    if rpi is _MISSING:
        try:
            import RPi.GPIO as rpi  # type: ignore[import-not-found,no-redef]
        except ImportError:
            rpi = None
    lg = lgpio_module
    if lg is _MISSING:
        try:
            import lgpio as lg  # type: ignore[no-redef]
        except ImportError:
            lg = None
    return HostGpio(resolved, rpi_module=rpi, lgpio_module=lg)
