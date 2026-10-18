#!/usr/bin/env python3
"""
Boot-time AP fallback (commissioning plan, PR 4).

Run once by ``bluesignal-ap-fallback.service`` after NetworkManager is up.
The service does not wait for ``bluesignal-wqm`` and does not wait for
``network-online.target``: a failed sensor must not stop setup, and an
unconfigured unit has no network for that target to announce. This script
unblocks the radio, brings ``wlan0`` up, waits out the station grace period
only when a Wi-Fi profile is actually saved, and retries the hotspot if the
interface is still down. An installer with a phone then reaches the Service
Window at http://192.168.4.1:8080 — at a well head with no Wi-Fi, or with a
card flashed for a network that does not exist here.

The SSID is ``WQM1-`` + the last four characters of the device id
(utils.identity.ap_name) and the passphrase comes from the provisioned
identity file the bench wrote (``ap_passphrase``). A unit with no identity
file (a Pi-serial unit from before the label) uses a passphrase derived from
its own Pi serial, which is printed by ``scripts/diagnostics.sh`` so it can be
written on the enclosure. Never a shared default.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(_SRC))

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s [ap-fallback] %(message)s"
)
logger = logging.getLogger("wqm1.ap_fallback")


def derived_passphrase(pi_serial: str) -> str:
    """See utils.netctl.derived_passphrase — one definition, used by the
    Service Window's join path and by this boot-time script alike."""
    from utils.netctl import derived_passphrase as _derived

    return _derived(pi_serial)


def resolve_credentials() -> tuple[str, str]:
    from utils.netctl import ap_credentials

    return ap_credentials()


def service_window_port(config_path: str = "/etc/bluesignal/config.yaml") -> int:
    """The Service Window's port from the firmware config (default 8080)."""
    try:
        import yaml

        with open(config_path) as f:
            raw = yaml.safe_load(f) or {}
        return int((raw.get("service_window") or {}).get("port") or 8080)
    except (OSError, ValueError, TypeError, ImportError):
        return 8080


def hotspot_failure_status(detail: object, passphrase: str) -> str:
    """``failed: <reason>`` for wqm1-status.txt.

    The reason is the NetworkManager / wpa_supplicant line ``nmcli`` returned
    (``Error: Connection activation failed: ...``). The passphrase is removed
    so a secret echoed by nmcli cannot land on the boot partition. The word
    "passphrase" is not used in this line: the publisher refuses a file that
    contains it.
    """
    error = ""
    if isinstance(detail, dict):
        error = str(detail.get("error") or "")
    elif detail:
        error = str(detail)
    error = " ".join(error.split())
    if passphrase:
        error = error.replace(passphrase, "[redacted]")
    return f"failed: {error or 'unknown'}"


def _write_status(*, hotspot: str, ssid: str) -> None:
    """Best-effort. A status-file failure must not decide whether the AP
    stays up — the file is how a pulled card is read, not the hotspot."""
    try:
        from diagnostics.boot_status import write_boot_status

        write_boot_status(hotspot=hotspot, ssid=ssid)
    except Exception as e:  # noqa: BLE001 — never fail the AP over the status file
        logger.error("Could not write boot status: %s", e)


def main() -> int:
    from utils.netctl import ensure_captive_redirect, ensure_reachable

    # The captive-portal NAT rule is installed every boot, whatever happens
    # next: it matches only hotspot clients, so it is inert on a unit that
    # joins its saved network, and in place the moment the AP comes up —
    # including when a failed join in the setup wizard brings the AP back.
    ensure_captive_redirect(service_window_port())
    ssid, passphrase = resolve_credentials()
    result = ensure_reachable(ssid, passphrase)
    if result.get("mode") == "station":
        logger.info("Station link present — AP not needed")
        _write_status(hotspot="station", ssid=ssid)
        return 0
    if result.get("ap"):
        logger.warning("No station link — setup AP %s is up at 192.168.4.1", ssid)
        _write_status(hotspot="up", ssid=ssid)
        return 0
    failure = hotspot_failure_status(result.get("detail"), passphrase)
    logger.error("No station link and the AP could not be raised: %s", failure)
    _write_status(hotspot=failure, ssid=ssid)
    return 1


if __name__ == "__main__":
    sys.exit(main())
