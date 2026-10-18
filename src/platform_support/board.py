"""
Board profiles — which host the firmware is running on, and what that host
can physically reach.

The WQM-1 firmware started life on the Raspberry Pi Zero 2 W, where Linux
owns the 40-pin header directly (ADS1115 over kernel I2C, DS18B20 over
w1-gpio, SX1262 over spidev, relays/LEDs/fan over RPi.GPIO). Zero W, Pi 3
and Pi 4 share that header and that GPIO block, so they use the same
``rpi`` backend; only the reported profile id changes. The Arduino UNO Q —
and the upcoming VENTUNO Q — split the machine in two: Debian runs on the
Qualcomm MPU, but the Arduino headers (analog pins, Qwiic I2C, SPI, GPIO)
belong to the STM32 MCU, reachable from Linux only through Arduino's Bridge
RPC. The Qualcomm's own exposed lines are camera/audio-dedicated and
reserved in the device tree — they are not general-purpose Linux GPIO.

So a board profile answers one load-bearing question:
``has_direct_headers`` — can this Linux see the analog/1-Wire/SPI/GPIO
peripherals directly? When it can't, the firmware runs **digital-first**:
RS485 Modbus probes over the USB adapter (pH, EC, TDS, salinity, temp,
chlorine, ORP — the full parameter set), USB GPS, and Wi-Fi cloud sync all
live on the Linux side and need no header at all. Header peripherals
(analog probes, LoRa, relays, LEDs, fan) come later via a Bridge companion
sketch on the MCU — see docs/platforms.md. The Raspberry Pi 5 is named so
it is not reported as a Zero 2 W, and it is not given the header stack:
its GPIO is the RP1, which this firmware does not drive.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("wqm1.board")

DEVICE_TREE_MODEL = "/proc/device-tree/model"
CPUINFO_PATH = "/proc/cpuinfo"

# New-style Raspberry Pi revision: bit 23 set, model type in bits 4..11.
# https://www.raspberrypi.com/documentation/computers/raspberry-pi.html#new-style-revision-codes
# Unknown types (original Zero, Pi 2, Compute Modules) return None and the
# caller keeps the Zero 2 W fallback — see detect_board.
_REVISION_TYPE_TO_PROFILE: dict[int, str] = {
    0x0C: "rpi-zero-w",
    0x12: "rpi-zero-2w",
    0x08: "rpi-3",  # 3B
    0x0D: "rpi-3",  # 3B+
    0x0E: "rpi-3",  # 3A+
    0x11: "rpi-4",  # 4B
    0x13: "rpi-4",  # 400 — same BCM2711 header as the 4B
    0x17: "rpi-5",
    0x19: "rpi-5",  # 500
}


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
    #: False for a board we can name but must not drive. Pi 5 is detected
    #: so a mis-flashed card reports itself honestly; the header stack stays off.
    supported: bool = True


def _pi(
    profile_id: str,
    name: str,
    notes: str,
    *,
    supported: bool = True,
) -> BoardProfile:
    return BoardProfile(
        id=profile_id,
        name=name,
        family="raspberry-pi",
        has_direct_headers=supported,
        notes=notes,
        gpio_backend="rpi" if supported else "none",
        supported=supported,
    )


_PI_HEADER = (
    "40-pin header, BCM numbering, RPi.GPIO. Same install and the same "
    "overlays as the Zero 2 W. See docs/raspberry-pi.md."
)

PROFILES: dict[str, BoardProfile] = {
    "rpi-zero-w": _pi(
        "rpi-zero-w",
        "Raspberry Pi Zero W",
        "ARMv6. Jacques's current standard host. " + _PI_HEADER,
    ),
    "rpi-zero-2w": _pi(
        "rpi-zero-2w",
        "Raspberry Pi Zero 2 W",
        "Reference platform. Full analog + LoRa + relay support. " + _PI_HEADER,
    ),
    "rpi-3": _pi(
        "rpi-3",
        "Raspberry Pi 3",
        "3B, 3B+ and 3A+. Onboard radio is wlan0 via brcmfmac, same hotspot path. " + _PI_HEADER,
    ),
    "rpi-4": _pi(
        "rpi-4",
        "Raspberry Pi 4",
        "4B, and the 400 which has the same header GPIO. The armhf image boots "
        "a 64-bit kernel with a 32-bit userland. " + _PI_HEADER,
    ),
    "rpi-5": _pi(
        "rpi-5",
        "Raspberry Pi 5",
        "Unsupported. The BCM2712 cannot run a 32-bit kernel, and the 40-pin "
        "header is the RP1, which RPi.GPIO does not drive. No workaround is "
        "installed; the header stack stays off. See docs/raspberry-pi.md.",
        supported=False,
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

# Profile ids whose 40-pin header is the Pi's: gpiochip0 line N is BCM N.
RPI_DIRECT_HEADER_IDS: frozenset[str] = frozenset(
    profile_id
    for profile_id, profile in PROFILES.items()
    if profile.family == "raspberry-pi" and profile.has_direct_headers
)

# Substring of /proc/device-tree/model -> profile id, for hosts that are not
# a Raspberry Pi. First match wins. Orange Pi images have printed the same
# board as "OrangePi Zero3", "Orange Pi Zero 3" and "orangepi-zero3" across
# releases, so that match is on the compacted string.
_OTHER_MODEL_MATCHES: tuple[tuple[str, str], ...] = (
    ("orangepizero3w", "orangepi-zero-3w"),
    ("ventuno", "arduino-ventuno-q"),
    ("uno q", "arduino-uno-q"),
    ("qrb2210", "arduino-uno-q"),
    ("iq-8275", "arduino-ventuno-q"),
    ("qualcomm", "arduino-uno-q"),
)


def _compact(model: str) -> str:
    return model.replace(" ", "").replace("-", "").replace("_", "")


def _raspberry_pi_profile(compact: str) -> str:
    """Map a compacted, lower-cased 'raspberrypi…' model onto a profile id.

    A Pi we do not have a specific row for (Pi 2, a Compute Module, a model
    string that only says "Raspberry Pi") returns ``rpi-zero-2w``. That is
    the long-standing fallback: an OTA must not demote a working analog unit
    because a model string changed shape. Pi 5 is specific, and unsupported.
    """
    # 500 before 5, 400 before 4: the shorter token is a prefix of the longer.
    if re.search(r"raspberrypi500(?!\d)", compact) or re.search(r"raspberrypi5(?!\d)", compact):
        return "rpi-5"
    if "zero2" in compact:
        return "rpi-zero-2w"
    if "zerow" in compact:
        return "rpi-zero-w"
    if re.search(r"raspberrypi3(?!\d)", compact):
        return "rpi-3"
    if re.search(r"raspberrypi400(?!\d)", compact) or re.search(r"raspberrypi4(?!\d)", compact):
        return "rpi-4"
    return "rpi-zero-2w"


def _profile_from_model(model: str) -> str | None:
    """Profile id for a lower-cased model string, or None if the family is unknown."""
    if not model:
        return None
    compact = _compact(model)
    for needle, profile_id in _OTHER_MODEL_MATCHES:
        if needle in model or needle in compact:
            return profile_id
    if "raspberrypi" in compact or "raspberry pi" in model:
        return _raspberry_pi_profile(compact)
    return None


def _profile_from_revision(revision: str) -> str | None:
    text = revision.strip().lower()
    if text.startswith("0x"):
        text = text[2:]
    try:
        value = int(text, 16)
    except ValueError:
        return None
    if value & (1 << 23) == 0:
        return None
    return _REVISION_TYPE_TO_PROFILE.get((value >> 4) & 0xFF)


def _read_model(model_path: str) -> str | None:
    """Device-tree model, NUL stripped. None if the file cannot be read; '' if it is empty."""
    try:
        raw = Path(model_path).read_bytes()
    except OSError:
        return None
    return raw.replace(b"\x00", b"").decode("utf-8", errors="replace").strip()


def _read_cpuinfo(cpuinfo_path: str) -> tuple[str, str]:
    """(board model, revision) from /proc/cpuinfo.

    The board line is the Pi's ``Model`` field (``Raspberry Pi …``). A CPUID
    ``model : 173`` line is not a board and is ignored. Either string is
    empty when that field is absent or the file cannot be read.
    """
    try:
        text = Path(cpuinfo_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "", ""
    board = ""
    revision = ""
    for line in text.splitlines():
        key, sep, val = line.partition(":")
        if not sep:
            continue
        key_l = key.strip().lower()
        val = val.strip()
        if key_l == "model" and "raspberry" in val.lower():
            board = val
        elif key_l == "revision" and val:
            revision = val
    return board, revision


def _with_support_warning(profile: BoardProfile) -> BoardProfile:
    if not profile.supported:
        logger.warning(
            "Board %s (%s) is not supported — header stack stays off",
            profile.id,
            profile.name,
        )
    return profile


def detect_board(
    override: str = "auto",
    model_path: str = DEVICE_TREE_MODEL,
    cpuinfo_path: str = CPUINFO_PATH,
) -> BoardProfile:
    """
    Resolve the board profile.

    ``override`` comes from the ``board`` config setting: a known profile id
    pins the profile explicitly; ``auto`` sniffs /proc/device-tree/model,
    then /proc/cpuinfo (its ``Model`` line, then the new-style ``Revision``
    type field) when the device tree is missing. Unrecognized models — and
    hosts with no device tree and no Pi revision — fall back to the
    Raspberry Pi Zero 2 W profile, NOT generic-linux: every unit in the field
    today is a Pi, and an OTA update must never demote a working analog
    deployment to digital-only because a model string changed shape.
    """
    if override and override != "auto":
        profile = PROFILES.get(override)
        if profile is not None:
            return _with_support_warning(profile)
        logger.warning("Unknown board '%s' in config — falling back to auto", override)

    model = _read_model(model_path)
    if model:
        profile_id = _profile_from_model(model.lower())
        if profile_id is None:
            logger.warning("Unrecognized board model %r — assuming Raspberry Pi", model)
            profile_id = "rpi-zero-2w"
        else:
            logger.info("Detected board: %s (%r)", profile_id, model)
        return _with_support_warning(PROFILES[profile_id])

    # An empty device-tree file is "we looked, it said nothing" — the same
    # fallback as before, without consulting whatever cpuinfo this host has.
    # A missing file (a laptop, a chroot) may still be a Pi; cpuinfo says so.
    if model == "":
        return PROFILES["rpi-zero-2w"]

    board_model, revision = _read_cpuinfo(cpuinfo_path)
    if board_model:
        profile_id = _profile_from_model(board_model.lower())
        if profile_id is not None:
            logger.info("Detected board from cpuinfo: %s (%r)", profile_id, board_model)
            return _with_support_warning(PROFILES[profile_id])
    if revision:
        profile_id = _profile_from_revision(revision)
        if profile_id is not None:
            logger.info("Detected board from cpuinfo revision %s: %s", revision, profile_id)
            return _with_support_warning(PROFILES[profile_id])
    return PROFILES["rpi-zero-2w"]
