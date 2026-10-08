"""
Device Identity

Two sources, one precedence, applied identically to the device id and the
LoRaWAN DevEUI:

1. **Provisioned** — ``bluesignal-identity.json`` on the FAT boot partition,
   written at the bench by the factory script (marketplace
   ``scripts/commission-device.cjs --label``). This is the printed label,
   ``WQM-10001``, and it is the DURABLE identity: a warranty Pi swap moves the
   card, and the unit keeps its id, its history and its credit evidence. The
   DevEUI lives here too, because the SX1262 is on the WQM-1 HAT, not on the
   Pi — deriving it from the Pi CPU serial tied the radio's identity to the one
   component a swap replaces.
2. **Derived** — the Raspberry Pi hardware serial, exactly as every unit in the
   field before the label existed. No file means no change: ``BS-WQM1-`` + the
   last 12 hex of ``/proc/cpuinfo`` Serial, DevEUI = OUI prefix + last 8 hex.

Where the derived serial comes from, in order:

- ``Serial`` in ``/proc/cpuinfo`` — the Raspberry Pi. Every unit in the field
  reads this line, so it stays first and its result is unchanged.
- ``/proc/device-tree/serial-number`` — set by U-Boot from the SoC's fused ID
  on Allwinner boards (the Orange Pi Zero 3W), whose arm64 kernel prints no
  ``Serial`` line in cpuinfo.
- The Allwinner SID eFuse exported by the nvmem driver.

A malformed file is logged and IGNORED, never partially applied — a unit with
a bad card boots as the Pi it is rather than as half a label.

The Pi serial is still read and reported (``hardware_identity()`` → heartbeat
``piSerial``/``derivedId``) so support can find a unit by either name.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger("wqm1.identity")

_PROC_CPUINFO = "/proc/cpuinfo"
_DT_SERIAL_NUMBER = "/proc/device-tree/serial-number"
_SUNXI_SID = "/sys/bus/nvmem/devices/sunxi-sid0/nvmem"

# BlueSignal OUI prefix for the DERIVED LoRaWAN DevEUI. Only used when no
# provisioned DevEUI exists. New units get theirs assigned at the bench from
# the namespace chosen in the commissioning plan (PR 0); this prefix is kept
# for the units already registered under it.
_OUI_PREFIX = "0018B200"

# TTN application EUI — replace with your own allocation
APP_EUI = bytes.fromhex("0000000000000000")  # Replace with your TTN AppEUI

# Where the factory script's file lands. /boot/firmware is the Bookworm mount
# point, /boot the Bullseye one — the same dual path setup.sh uses for
# config.txt. The FAT partition is the point: writable from any laptop with a
# card reader, so ten cards can be provisioned without ten booted Pis.
PROVISIONED_IDENTITY_PATHS: tuple[str, ...] = (
    "/boot/firmware/bluesignal-identity.json",
    "/boot/bluesignal-identity.json",
)

# A process may point at a different file (the fleet simulator runs N units
# on one host, each with its own identity). This is a PATH, not an override
# of the precedence: whatever file it names is validated like the real one.
IDENTITY_FILE_ENV = "BLUESIGNAL_IDENTITY_FILE"

# The printed label: WQM- plus exactly five digits, uppercase. This regex is
# mirrored byte for byte by WQM1_LABEL_RE in marketplace
# functions/v2/deviceSerial.js — change both or neither.
LABEL_RE = re.compile(r"^WQM-\d{5}$")

# Reserved for virtual units only. It is neither ``WQM-`` nor ``BS-WQM1-`` so
# a simulated record can never be mistaken for a real one, the marketplace's
# canonicaliser passes it through untouched, and a stray one is greppable.
SIM_SERIAL_PREFIX = "SIM-WQM1-"
SIM_SERIAL_RE = re.compile(r"^SIM-WQM1-\d{5}$")

_DEV_EUI_RE = re.compile(r"^[0-9a-fA-F]{16}$")
_PI_SERIAL_RE = re.compile(r"^[0-9a-f]{16}$")

# The non-Pi fallback serial. Every non-Pi host reports it, which is why a
# fleet of virtual units MUST carry provisioned identities — without them
# they would all be BS-WQM1-000000000000.
FALLBACK_PI_SERIAL = "0000000000000000"


def _normalise(serial: str) -> str:
    """Lower-case hex, last 16 digits, zero-padded on the left."""
    hexdigits = "".join(c for c in serial.strip().lower() if c in "0123456789abcdef")
    if not hexdigits:
        return ""
    return hexdigits[-16:].zfill(16)


def _serial_from_cpuinfo() -> str:
    with open(_PROC_CPUINFO) as f:
        for line in f:
            if line.startswith("Serial"):
                return _normalise(line.split(":")[-1])
    return ""


def _serial_from_device_tree() -> str:
    with open(_DT_SERIAL_NUMBER, "rb") as f:
        raw = f.read(64)
    return _normalise(raw.replace(b"\x00", b"").decode("ascii", errors="ignore"))


def _serial_from_sunxi_sid() -> str:
    with open(_SUNXI_SID, "rb") as f:
        raw = f.read(16)
    # The SID is a 128-bit fused chip id; its first 8 bytes are unique per
    # die, which is what the 16-hex-digit identity needs.
    return _normalise(raw[:8].hex()) if len(raw) >= 8 else ""


def get_pi_serial() -> str:
    """
    Read the host board's hardware serial.

    Returns:
        16-character hex string (e.g. "10000000abcdef01"), or
        ``FALLBACK_PI_SERIAL`` on a host with no readable serial.
    """
    for source, reader in (
        ("/proc/cpuinfo", _serial_from_cpuinfo),
        ("device-tree serial-number", _serial_from_device_tree),
        ("sunxi SID", _serial_from_sunxi_sid),
    ):
        try:
            serial = reader()
        except Exception as e:  # noqa: BLE001 — try the next source
            logger.debug("No serial from %s: %s", source, e)
            continue
        if serial and set(serial) != {"0"}:
            if source != "/proc/cpuinfo":
                logger.info("Host serial read from %s", source)
            return serial

    logger.warning("Could not read a hardware serial from any source")
    # Fallback for hosts with no readable serial (development/testing)
    return FALLBACK_PI_SERIAL


#: The honest name for what this reads on a non-Pi host.
get_host_serial = get_pi_serial


def is_label_serial(serial: str) -> bool:
    """True for a printed-label id (WQM-NNNNN)."""
    return bool(LABEL_RE.match(serial))


def is_simulated_serial(serial: str) -> bool:
    """True for a virtual unit's id (SIM-WQM1-NNNNN)."""
    return bool(SIM_SERIAL_RE.match(serial))


def _identity_paths() -> tuple[str, ...]:
    override = os.environ.get(IDENTITY_FILE_ENV)
    if override:
        return (override,)
    return PROVISIONED_IDENTITY_PATHS


def read_provisioned_identity(paths: tuple[str, ...] | None = None) -> dict[str, Any] | None:
    """Read and validate the factory-written identity file.

    Returns the validated dict (``serial`` uppercased, ``dev_eui`` uppercased
    when present, every other key passed through) or ``None`` when no file
    exists or the file is malformed. Malformed is logged at WARNING once per
    call and the caller falls back to the derived identity — it never gets a
    half-applied label.
    """
    for raw_path in paths or _identity_paths():
        path = Path(raw_path)
        try:
            if not path.is_file():
                continue
            data = json.loads(path.read_text())
        except (OSError, ValueError) as e:
            logger.warning("Provisioned identity at %s unreadable — ignoring: %s", path, e)
            return None

        if not isinstance(data, dict):
            logger.warning("Provisioned identity at %s is not an object — ignoring", path)
            return None
        serial = data.get("serial")
        if not isinstance(serial, str):
            logger.warning("Provisioned identity at %s has no serial — ignoring", path)
            return None
        serial = serial.strip().upper()
        if not (is_label_serial(serial) or is_simulated_serial(serial)):
            logger.warning(
                "Provisioned identity at %s carries serial %r, which is neither a "
                "WQM-NNNNN label nor a SIM-WQM1-NNNNN virtual id — ignoring",
                path,
                serial,
            )
            return None
        dev_eui = data.get("dev_eui")
        if dev_eui is not None:
            if not isinstance(dev_eui, str) or not _DEV_EUI_RE.match(dev_eui.strip()):
                logger.warning(
                    "Provisioned identity at %s has a malformed dev_eui — ignoring the file "
                    "so the id and the DevEUI never disagree about which source they came from",
                    path,
                )
                return None
            dev_eui = dev_eui.strip().upper()

        out = dict(data)
        out["serial"] = serial
        if dev_eui is not None:
            out["dev_eui"] = dev_eui
        out["_path"] = str(path)
        return out
    return None


def get_derived_id(serial: str | None = None) -> str:
    """
    The Pi-derived BlueSignal device ID, regardless of any provisioned file.

    Format: BS-WQM1-{last 12 hex chars of Pi serial}
    """
    if serial is None:
        serial = get_pi_serial()
    suffix = serial[-12:].lower()
    return f"BS-WQM1-{suffix}"


def get_derived_dev_eui(serial: str | None = None) -> bytes:
    """The Pi-derived DevEUI: 0018B200{last 8 hex chars of Pi serial}."""
    if serial is None:
        serial = get_pi_serial()
    suffix = serial[-8:].lower()
    return bytes.fromhex(f"{_OUI_PREFIX}{suffix}")


def get_device_id(serial: str | None = None) -> str:
    """
    The device's identity: provisioned label if a valid file exists, else
    the Pi-derived id.

    Args:
        serial: an explicit Pi serial. When given, the derived form of THAT
            serial is returned and no file is consulted (the callers that pass
            one are computing a derived id on purpose).
    """
    if serial is not None:
        return get_derived_id(serial)
    provisioned = read_provisioned_identity()
    if provisioned is not None:
        return str(provisioned["serial"])
    return get_derived_id()


def get_dev_eui(serial: str | None = None) -> bytes:
    """
    LoRaWAN DevEUI (8 bytes), same precedence as ``get_device_id``:
    provisioned ``dev_eui`` if the file carries one, else Pi-derived.

    A provisioned file with a serial but no ``dev_eui`` falls back to the
    derived DevEUI for the radio only — the id is still the label.
    """
    if serial is not None:
        return get_derived_dev_eui(serial)
    provisioned = read_provisioned_identity()
    if provisioned is not None and provisioned.get("dev_eui"):
        return bytes.fromhex(str(provisioned["dev_eui"]))
    return get_derived_dev_eui()


def identity_source() -> str:
    """``"provisioned"`` when a valid identity file is in force, else ``"derived"``."""
    return "provisioned" if read_provisioned_identity() is not None else "derived"


def hardware_identity() -> dict[str, str]:
    """What the heartbeat reports so support can find a unit by either name.

    ``piSerial`` is omitted on a host with no Pi serial (the fallback zeros
    are not an identity), so the cloud never stores sixteen zeros as a fact.
    """
    out: dict[str, str] = {"identitySource": identity_source()}
    pi = get_pi_serial()
    if _PI_SERIAL_RE.match(pi) and pi != FALLBACK_PI_SERIAL:
        out["piSerial"] = pi
        out["derivedId"] = get_derived_id(pi)
    return out


def get_ble_name(device_id: str | None = None) -> str:
    """
    Generate BLE advertisement name for commissioning.

    Format: BlueSignal-{last 4 hex chars of device ID}
    """
    if device_id is None:
        device_id = get_device_id()
    suffix = device_id[-4:]
    return f"BlueSignal-{suffix}"


def ap_name(device_id: str | None = None) -> str:
    """The Wi-Fi access-point SSID the unit raises when it cannot associate.

    ``WQM1-0001`` for label ``WQM-10001`` (the last four digits, which is what
    is printed large on the enclosure); ``WQM1-`` + last four hex for a derived
    id; the same rule for a virtual unit.
    """
    if device_id is None:
        device_id = get_device_id()
    return f"WQM1-{device_id[-4:].upper()}"
