"""
Device Identity

Generates unique device identifiers from the host board's hardware serial
number. All identities are deterministic — the same board always produces
the same IDs.

Where the serial comes from, in order:

1. ``Serial`` in ``/proc/cpuinfo`` — the Raspberry Pi. Every unit in the
   field reads this line, so it stays first and its result is unchanged.
2. ``/proc/device-tree/serial-number`` — set by U-Boot from the SoC's
   fused ID on Allwinner boards (the Orange Pi Zero 3W), whose arm64
   kernel prints no ``Serial`` line in cpuinfo.
3. The Allwinner SID eFuse exported by the nvmem driver.

Nothing here is the durable identity of a shipped unit — the printed label
is (commissioning plan, PR 1); this is the fallback a card without an
identity file derives its id from, and the hardware attribute a heartbeat
reports so a board swap shows as a changed serial under an unchanged id.
"""

import logging

logger = logging.getLogger("wqm1.identity")

_PROC_CPUINFO = "/proc/cpuinfo"
_DT_SERIAL_NUMBER = "/proc/device-tree/serial-number"
_SUNXI_SID = "/sys/bus/nvmem/devices/sunxi-sid0/nvmem"

# BlueSignal OUI prefix for LoRaWAN DevEUI
_OUI_PREFIX = "0018B200"

# TTN application EUI — replace with your own allocation
APP_EUI = bytes.fromhex("0000000000000000")  # Replace with your TTN AppEUI


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
        16-character hex string (e.g. "10000000abcdef01"), or all zeros on a
        host with no readable serial (development/testing).
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
    return "0000000000000000"


#: The honest name for what this reads on a non-Pi host.
get_host_serial = get_pi_serial


def get_device_id(serial: str | None = None) -> str:
    """
    Generate BlueSignal device ID.

    Format: BS-WQM1-{last 12 hex chars of Pi serial}

    Args:
        serial: Pi serial (reads from /proc/cpuinfo if not provided)
    """
    if serial is None:
        serial = get_pi_serial()
    suffix = serial[-12:].lower()
    return f"BS-WQM1-{suffix}"


def get_dev_eui(serial: str | None = None) -> bytes:
    """
    Generate LoRaWAN DevEUI (8 bytes).

    Format: 0018B200{last 8 hex chars of Pi serial}

    Args:
        serial: Pi serial (reads from /proc/cpuinfo if not provided)
    """
    if serial is None:
        serial = get_pi_serial()
    suffix = serial[-8:].lower()
    return bytes.fromhex(f"{_OUI_PREFIX}{suffix}")


def get_ble_name(device_id: str | None = None) -> str:
    """
    Generate BLE advertisement name for commissioning.

    Format: BlueSignal-{last 4 hex chars of device ID}
    """
    if device_id is None:
        device_id = get_device_id()
    suffix = device_id[-4:]
    return f"BlueSignal-{suffix}"
