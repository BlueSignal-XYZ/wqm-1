"""
Network facts for commissioning — what link is this unit actually on, and can
it reach the cloud?

Read-only by design. This module reports and probes; it never reconfigures the
network. That boundary is deliberate: the Service Window is served *over* the
same link it would be changing, so a failed reconfiguration would strand the
installer on a dead page with no way back in. Joining a different network is a
console/imager job (or, later, an AP-mode provisioning flow that can fall back
to its own hotspot) — not something to attempt from a page you're standing on.

Every function returns None / a status string on failure and never raises: a
diagnostics page that crashes is worse than one that says "unknown".
"""

from __future__ import annotations

import json
import logging
import shutil
import socket
import subprocess  # nosec B404 - fixed argv, no shell, resolved binaries only
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

# Signal thresholds in dBm, judged for a unit that must hold a link unattended
# for months inside a sealed enclosure — not for a phone you're holding.
RSSI_GOOD_DBM = -65
RSSI_MARGINAL_DBM = -78

_PROBE_TIMEOUT_S = 6.0


def _run(argv: list[str], timeout: float = 3.0) -> str | None:
    """Run a fixed argv with no shell. Returns stdout, or None on any failure."""
    exe = shutil.which(argv[0])
    if not exe:
        return None
    try:
        out = subprocess.run(  # nosec B603 - resolved path, fixed argv, shell=False
            [exe, *argv[1:]],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def current_ssid() -> str | None:
    """SSID of the associated network, via NetworkManager then wireless-tools."""
    # Bookworm ships NetworkManager; ask it first.
    out = _run(["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"])
    if out:
        for line in out.splitlines():
            if line.startswith("yes:"):
                ssid = line.split(":", 1)[1].strip()
                if ssid:
                    return ssid
    out = _run(["iwgetid", "-r"])
    return out or None


def local_ip() -> str | None:
    """
    The address this unit would use to reach the internet.

    Opens a UDP socket to a public address and reads the local end. UDP is
    connectionless, so nothing is transmitted — this only asks the kernel which
    interface and source address the route table would pick.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(1.0)
        sock.connect(("8.8.8.8", 53))
        return str(sock.getsockname()[0])
    except OSError:
        return None
    finally:
        sock.close()


def signal_state(rssi_dbm: int | None) -> str:
    """Map RSSI to an explain() state: ok / degraded / down."""
    if rssi_dbm is None:
        return "down"
    if rssi_dbm >= RSSI_GOOD_DBM:
        return "ok"
    if rssi_dbm >= RSSI_MARGINAL_DBM:
        return "degraded"
    return "degraded"


def wifi_status() -> dict[str, Any]:
    """{ssid, rssi_dbm, ip, state} — state is an explain() system state."""
    from utils.health import read_wifi_rssi_dbm

    ssid = current_ssid()
    rssi = read_wifi_rssi_dbm()
    ip = local_ip()
    # No SSID and no RSSI means no association at all, whatever the route table
    # says (a wired or USB-tethered unit lands here too, and its cloud check
    # below is what actually matters).
    state = "down" if (ssid is None and rssi is None) else signal_state(rssi)
    return {"ssid": ssid, "rssi_dbm": rssi, "ip": ip, "state": state}


def verify_device_key(api_base: str, device_id: str, api_key: str) -> dict[str, Any]:
    """
    Prove the pasted key authorizes THIS device, end to end.

    GET /v2/devices/{id}/config is read-only and bound to the device's own key,
    so it is safe to call from a wizard and its status codes separate the three
    failures an installer actually hits:

        200/204 -> key is valid and bound to this device
        401     -> key is wrong, empty, or unknown to the cloud
        403     -> key is real but belongs to a DIFFERENT device
        no HTTP -> the network can't reach the cloud at all

    Returns {"state": ok|degraded|down, "detail": str, "status": int|None}.
    """
    if not api_base or not api_key:
        return {"state": "down", "detail": "No cloud key saved yet.", "status": None}

    url = f"{api_base.rstrip('/')}/v2/devices/{device_id}/config"
    if not url.lower().startswith(("http://", "https://")):
        return {"state": "down", "detail": "Cloud URL is not HTTP(S).", "status": None}

    req = urllib.request.Request(url, headers={"X-API-Key": api_key}, method="GET")
    try:
        # B310: scheme validated above; endpoint comes from config, not user input.
        with urllib.request.urlopen(req, timeout=_PROBE_TIMEOUT_S) as resp:  # nosec B310
            return {
                "state": "ok",
                "detail": f"Cloud accepted the key (HTTP {resp.status}).",
                "status": resp.status,
            }
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            detail = (
                "The cloud says this key belongs to a different device."
                if e.code == 403
                else "The cloud rejected this key."
            )
            return {"state": "degraded", "detail": detail, "status": e.code}
        # Any other HTTP status still proves the cloud is reachable.
        return {
            "state": "degraded",
            "detail": f"Cloud responded HTTP {e.code}.",
            "status": e.code,
        }
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as e:
        logger.warning("Cloud key check failed: %s", e)
        return {"state": "down", "detail": "Could not reach the cloud.", "status": None}
