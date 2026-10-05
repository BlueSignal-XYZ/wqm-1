"""
Board profiles — which host the firmware is running on, and what that host
can physically reach.

The WQM-1 firmware started life on the Raspberry Pi Zero 2 W, where Linux
owns the 40-pin header directly (ADS1115 over kernel I2C, DS18B20 over
w1-gpio, SX1262 over spidev, relays/LEDs/fan over RPi.GPIO). The Arduino
UNO Q — and the upcoming VENTUNO Q — split the machine in two: Debian runs
on the Qualcomm MPU, but the Arduino headers (analog pins, Qwiic I2C, SPI,
GPIO) belong to the STM32 MCU, reachable from Linux only through Arduino's
Bridge RPC. The Qualcomm's own exposed lines are camera/audio-dedicated and
reserved in the device tree — they are not general-purpose Linux GPIO.

So a board profile answers one load-bearing question:
``has_direct_headers`` — can this Linux see the analog/1-Wire/SPI/GPIO
peripherals directly? When it can't, the firmware runs **digital-first**:
RS485 Modbus probes over the USB adapter (pH, EC, TDS, salinity, temp,
chlorine, ORP — the full parameter set), USB GPS, and Wi-Fi cloud sync all
live on the Linux side and need no header at all. Header peripherals
(analog probes, LoRa, relays, LEDs, fan) come later via a Bridge companion
sketch on the MCU — see docs/platforms.md.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("wqm1.board")

DEVICE_TREE_MODEL = "/proc/device-tree/model"


@dataclass(frozen=True)
class BoardProfile:
    id: str
    name: str
    family: str  # "raspberry-pi" | "orange-pi" | "arduino-qualcomm" | "generic"
    #: Linux can reach the analog ADC, 1-Wire, SPI radio, and GPIO
    #: (relays/LEDs/fan/hardware watchdog) directly through the kernel.
    has_direct_headers: bool
    notes: str = ""
    #: How the firmware drives the HAT's lines on this host — see
    #: platform_support/gpio.py. "rpi" is RPi.GPIO (+ lgpio for alerts),
    #: "gpiochip" is lgpio on the kernel's gpiochips with the BCM numbers
    #: translated by platform_support/hostpins.py, "none" means the headers
    #: are not Linux's to drive.
    gpio_backend: str = "none"


PROFILES: dict[str, BoardProfile] = {
    "rpi-zero-2w": BoardProfile(
        id="rpi-zero-2w",
        name="Raspberry Pi Zero 2 W",
        family="raspberry-pi",
        has_direct_headers=True,
        notes="Reference platform. Full analog + LoRa + relay support.",
        gpio_backend="rpi",
    ),
    "orangepi-zero-3w": BoardProfile(
        id="orangepi-zero-3w",
        name="Orange Pi Zero 3W",
        family="orange-pi",
        has_direct_headers=True,
        notes=(
            "Allwinner A733, Raspberry Pi Zero form factor and 40-pin header "
            "layout: power, ground, I2C (TWI0, pins 3/5), SPI (SPI3, pins "
            "19/21/23/24) and UART (UART0, pins 8/10) sit where the Pi puts "
            "them, so the HAT seats unchanged. GPIO lines are Allwinner "
            "pins driven through lgpio; five plain-GPIO header pins are not "
            "in the published pinout and are read off the board at the "
            "bench (scripts/host-pins.py). Full stack, bench verification "
            "pending — see docs/platforms.md."
        ),
        gpio_backend="gpiochip",
    ),
    "arduino-uno-q": BoardProfile(
        id="arduino-uno-q",
        name="Arduino UNO Q",
        family="arduino-qualcomm",
        has_direct_headers=False,
        notes=(
            "Debian on Qualcomm QRB2210; headers belong to the STM32U585. "
            "Digital-first: RS485 probes + USB GPS + Wi-Fi sync. Header "
            "peripherals require the Bridge companion sketch (future)."
        ),
    ),
    "arduino-ventuno-q": BoardProfile(
        id="arduino-ventuno-q",
        name="Arduino VENTUNO Q",
        family="arduino-qualcomm",
        has_direct_headers=False,
        notes=(
            "Same dual-brain layout as the UNO Q (Dragonwing IQ-8275 + "
            "STM32H5). Digital-first until the Bridge companion lands."
        ),
    ),
    "generic-linux": BoardProfile(
        id="generic-linux",
        name="Generic Linux host",
        family="generic",
        has_direct_headers=False,
        notes="Unknown hardware — assume no direct header access.",
    ),
}

# Substring of /proc/device-tree/model -> profile id. First match wins;
# ordered most-specific first. Matched against the lower-cased model AND a
# compacted copy with spaces/hyphens removed, because Orange Pi images have
# printed the same board as "OrangePi Zero3", "Orange Pi Zero 3" and
# "orangepi-zero3" across releases.
_MODEL_MATCHES: tuple[tuple[str, str], ...] = (
    ("orangepizero3w", "orangepi-zero-3w"),
    ("raspberry pi", "rpi-zero-2w"),
    ("ventuno", "arduino-ventuno-q"),
    ("uno q", "arduino-uno-q"),
    ("qrb2210", "arduino-uno-q"),
    ("iq-8275", "arduino-ventuno-q"),
    ("qualcomm", "arduino-uno-q"),
)


def _read_model(model_path: str) -> str:
    try:
        raw = Path(model_path).read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\x00", b"").decode("utf-8", errors="replace").strip()


def detect_board(
    override: str = "auto",
    model_path: str = DEVICE_TREE_MODEL,
) -> BoardProfile:
    """
    Resolve the board profile.

    ``override`` comes from the ``board`` config setting: a known profile id
    pins the profile explicitly; ``auto`` sniffs /proc/device-tree/model.
    Unrecognized models — and hosts with no device tree at all — fall back
    to the Raspberry Pi profile, NOT generic-linux: every unit in the field
    today is a Pi, and an OTA update must never demote a working analog
    deployment to digital-only because a model string changed shape.
    """
    if override and override != "auto":
        profile = PROFILES.get(override)
        if profile is not None:
            return profile
        logger.warning("Unknown board '%s' in config — falling back to auto", override)

    model = _read_model(model_path).lower()
    if model:
        compact = model.replace(" ", "").replace("-", "").replace("_", "")
        for needle, profile_id in _MODEL_MATCHES:
            if needle in model or needle in compact:
                logger.info("Detected board: %s (%r)", profile_id, model)
                return PROFILES[profile_id]
        logger.warning("Unrecognized board model %r — assuming Raspberry Pi", model)
    return PROFILES["rpi-zero-2w"]
