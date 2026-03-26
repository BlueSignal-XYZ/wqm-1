"""
Device Identity

Generates unique device identifiers from the Raspberry Pi's hardware serial
number. All identities are deterministic — same Pi always produces same IDs.
"""

import logging

logger = logging.getLogger("wqm1.identity")

# BlueSignal OUI prefix for LoRaWAN DevEUI
_OUI_PREFIX = "0018B200"

# TTN application EUI — replace with your own allocation
APP_EUI = bytes.fromhex("0000000000000000")  # Replace with your TTN AppEUI


def get_pi_serial() -> str:
    """
    Read the Raspberry Pi hardware serial from /proc/cpuinfo.

    Returns:
        16-character hex string (e.g. "10000000abcdef01")
    """
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("Serial"):
                    serial = line.split(":")[-1].strip().lower()
                    return serial.zfill(16)
    except Exception as e:
        logger.warning("Could not read Pi serial: %s", e)

    # Fallback for non-Pi platforms (development/testing)
    return "0000000000000000"


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
