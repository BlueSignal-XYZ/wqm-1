#!/usr/bin/env python3
"""
Boot-time AP fallback (commissioning plan, PR 4).

Run once by ``bluesignal-ap-fallback.service`` after NetworkManager is up:
wait ``AP_GRACE_S`` for a known Wi-Fi network to associate; if none does,
raise the unit's own WPA2 hotspot so an installer with a phone can reach the
Service Window at http://192.168.4.1:8080 — at a well head with no Wi-Fi, or
with a card flashed for a network that does not exist here.

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


def main() -> int:
    from utils.netctl import ensure_reachable

    ssid, passphrase = resolve_credentials()
    result = ensure_reachable(ssid, passphrase)
    if result.get("mode") == "station":
        logger.info("Station link present — AP not needed")
        return 0
    if result.get("ap"):
        logger.warning("No station link — setup AP %s is up at 192.168.4.1", ssid)
        return 0
    logger.error("No station link and the AP could not be raised: %s", result.get("detail"))
    return 1


if __name__ == "__main__":
    sys.exit(main())
