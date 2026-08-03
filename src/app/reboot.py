"""
Host reboot requests — the unprivileged half.

The firmware and the Service Window both run as the install user (see
`systemd/bluesignal-wqm.service`), so neither can reboot the host, and neither
is given sudo rights to do it. They write a request flag instead; the OTA
agent — which already runs as root for release symlink flips and `systemctl
restart` — picks it up and performs the reboot (`ota.agent.OTAAgent`).

Requesting a reboot is not free: the unit stops sampling and stops evaluating
control rules for the ~60 s it takes to come back. So the request drives the
relays to their fail-safe (de-energised) state first, and only then writes the
flag.
"""

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("wqm1.reboot")

# Watched by the root OTA agent (ota.agent.REBOOT_FLAG, same file). Lives in
# the state dir, which setup.sh chowns to the install user.
REBOOT_REQUEST_FLAG = Path("/var/lib/bluesignal/reboot-request")


def relays_failsafe(relays: Any) -> bool:
    """Drive every relay coil to its de-energised state.

    True means the coils are known to be off — including on headerless boards,
    which carry no relays at all.
    """
    if relays is None:
        return True
    try:
        relays.all_off()
    except Exception as e:  # noqa: BLE001 — a relay fault must not mask the reboot
        logger.error("Could not force relays to fail-safe: %s", e)
        return False
    return True


def request_host_reboot(relays: Any, flag_path: Path = REBOOT_REQUEST_FLAG) -> dict[str, Any]:
    """Ask the root OTA agent to reboot the host.

    Relays first: a reboot suspends rule evaluation, and a coil left energised
    would stay energised, unsupervised, for the whole window — a dosing pump
    that was mid-cycle keeps dosing. A failed fail-safe does *not* block the
    reboot (GPIO reverts to inputs across a reboot, so the coils drop either
    way, and refusing would break the one recovery path that still works when
    SSH is dead) — it is reported instead, so the operator hears about it.
    """
    relays_safe = relays_failsafe(relays)
    try:
        flag_path.parent.mkdir(parents=True, exist_ok=True)
        flag_path.touch()
    except OSError as e:
        logger.error("Could not write reboot request %s: %s", flag_path, e)
        return {"ok": False, "error": f"could not request reboot: {e}", "relaysSafe": relays_safe}
    logger.warning("Host reboot requested (relays safe: %s) — flag %s", relays_safe, flag_path)
    return {"ok": True, "rebooting": True, "relaysSafe": relays_safe}
