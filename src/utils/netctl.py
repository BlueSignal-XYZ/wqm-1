"""
Network MUTATION for commissioning — the access point and the Wi-Fi join.

``utils.netinfo`` reports and must never reconfigure the link it is served
over (``tests/test_netinfo.py::TestReadOnlyBoundary``). That intent is right:
a page that reconfigures the network it is being read over can strand the
installer on a dead tab. This module is the deliberate sibling that DOES
mutate, with its own boundary: nothing here is called from a request handler
without the page first saying what is about to happen to the connection.

Two jobs (commissioning plan, PR 4):

* **The access point.** At boot, if no known Wi-Fi network associates within
  ``AP_GRACE_S``, raise a WPA2 hotspot named after the unit (``WQM1-0001``)
  with the per-unit passphrase from the identity file. It is a FALLBACK,
  never concurrent with a station link — Pi Zero 2W AP+STA on ``brcmfmac`` is
  fragile, so the rule avoids the question. The AP stays up while the unit is
  unassociated, which is also how a permanently-offline well head is reached
  next year.
* **The Wi-Fi join.** From the Service Window's network step: list networks,
  join one. Joining tears the AP down, so ``join_network`` brings the AP
  straight back on failure — an installer locked out by a typo is worse than
  no feature.

Everything shells NetworkManager's ``nmcli`` with a fixed argv and no shell,
the same tool ``netinfo`` already reads through. Every call returns a result
dict and never raises.
"""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
import subprocess  # nosec B404 - fixed argv, no shell, resolved binaries only
import time
from typing import Any

logger = logging.getLogger("wqm1.netctl")

AP_CONNECTION_NAME = "wqm1-setup-ap"
AP_GRACE_S = 45
AP_SUBNET = "192.168.4.1/24"
DEFAULT_WIFI_IFACE = "wlan0"
JOIN_TIMEOUT_S = 40
_SSID_RE = re.compile(r"^[^\x00-\x1f]{1,32}$")


def _run(argv: list[str], timeout: float = 15.0) -> tuple[int, str, str]:
    """Run a fixed argv with no shell. Returns (rc, stdout, stderr); a
    missing binary is rc 127."""
    exe = shutil.which(argv[0])
    if not exe:
        return 127, "", f"{argv[0]} not found"
    try:
        out = subprocess.run(  # nosec B603 - resolved path, fixed argv, shell=False
            [exe, *argv[1:]],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return 1, "", str(e)
    return out.returncode, out.stdout.strip(), out.stderr.strip()


def _nmcli(*args: str, timeout: float = 15.0) -> tuple[int, str, str]:
    return _run(["nmcli", *args], timeout=timeout)


def derived_passphrase(pi_serial: str) -> str:
    """AP passphrase for a unit with no identity file: 12 hex from a hash of
    its Pi serial. Printed by scripts/diagnostics.sh so it can be written on
    the enclosure. Never a fleet-wide default — an open or shared AP into a
    PIN-gated page is still an open AP."""
    return hashlib.sha256(f"wqm1-ap:{pi_serial}".encode()).hexdigest()[:12]


def ap_credentials() -> tuple[str, str]:
    """(SSID, passphrase) for this unit's setup AP.

    SSID is ``WQM1-`` + the last four characters of the device id; the
    passphrase is the bench-provisioned ``ap_passphrase`` from the identity
    file, else derived from the Pi serial.
    """
    from utils.identity import ap_name, get_device_id, get_pi_serial, read_provisioned_identity

    ssid = ap_name(get_device_id())
    prov = read_provisioned_identity() or {}
    passphrase = str(prov.get("ap_passphrase") or "")
    if len(passphrase) < 8:
        passphrase = derived_passphrase(get_pi_serial())
    return ssid, passphrase


def station_connected(iface: str = DEFAULT_WIFI_IFACE) -> bool:
    """True when the Wi-Fi interface has an active NetworkManager connection
    that is NOT our own access point."""
    rc, out, _ = _nmcli("-t", "-f", "DEVICE,STATE,CONNECTION", "device", "status")
    if rc != 0:
        return False
    for line in out.splitlines():
        parts = line.split(":")
        if len(parts) >= 3 and parts[0] == iface:
            return parts[1] == "connected" and parts[2] != AP_CONNECTION_NAME
    return False


def ap_active(iface: str = DEFAULT_WIFI_IFACE) -> bool:
    rc, out, _ = _nmcli("-t", "-f", "NAME,DEVICE", "connection", "show", "--active")
    if rc != 0:
        return False
    return any(line.startswith(f"{AP_CONNECTION_NAME}:") for line in out.splitlines())


def start_ap(ssid: str, passphrase: str, iface: str = DEFAULT_WIFI_IFACE) -> dict[str, Any]:
    """Raise the setup hotspot. WPA2-PSK, private subnet, NM's own DHCP.

    Refuses a passphrase under 8 characters (WPA2 minimum) rather than
    raising an open network — an open AP into a PIN-gated page is still an
    open AP.
    """
    if not _SSID_RE.match(ssid or ""):
        return {"ok": False, "error": "invalid ssid"}
    if not passphrase or len(passphrase) < 8:
        return {"ok": False, "error": "passphrase must be at least 8 characters"}
    if ap_active(iface):
        return {"ok": True, "ssid": ssid, "already": True}
    # A stale profile from a previous boot would hold the old passphrase.
    _nmcli("connection", "delete", AP_CONNECTION_NAME)
    rc, out, err = _nmcli(
        "device",
        "wifi",
        "hotspot",
        "ifname",
        iface,
        "con-name",
        AP_CONNECTION_NAME,
        "ssid",
        ssid,
        "password",
        passphrase,
        timeout=30,
    )
    if rc != 0:
        logger.error("AP start failed: %s %s", out, err)
        return {"ok": False, "error": err or out or f"nmcli rc {rc}"}
    # Pin the subnet so the printed setup URL is always the same address.
    _nmcli(
        "connection",
        "modify",
        AP_CONNECTION_NAME,
        "ipv4.addresses",
        AP_SUBNET,
        "ipv4.method",
        "shared",
    )
    _nmcli("connection", "modify", AP_CONNECTION_NAME, "connection.autoconnect", "no")
    logger.info("Setup AP up: %s on %s", ssid, iface)
    return {"ok": True, "ssid": ssid, "address": AP_SUBNET.split("/")[0]}


def stop_ap(iface: str = DEFAULT_WIFI_IFACE) -> dict[str, Any]:
    rc, out, err = _nmcli("connection", "down", AP_CONNECTION_NAME)
    if rc != 0 and "not an active connection" not in (err + out).lower():
        return {"ok": False, "error": err or out}
    return {"ok": True}


def scan_networks(iface: str = DEFAULT_WIFI_IFACE) -> list[dict[str, Any]]:
    """Visible networks, strongest first. Never raises; empty on failure."""
    _nmcli("device", "wifi", "rescan", "ifname", iface, timeout=20)
    rc, out, _ = _nmcli(
        "-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list", "ifname", iface
    )
    if rc != 0:
        return []
    seen: dict[str, dict[str, Any]] = {}
    for line in out.splitlines():
        parts = line.split(":")
        if len(parts) < 2 or not parts[0]:
            continue
        ssid = parts[0]
        try:
            signal = int(parts[1])
        except ValueError:
            signal = 0
        security = parts[2] if len(parts) > 2 else ""
        if ssid not in seen or seen[ssid]["signal"] < signal:
            seen[ssid] = {
                "ssid": ssid,
                "signal": signal,
                "secured": bool(security and security != "--"),
            }
    return sorted(seen.values(), key=lambda n: -n["signal"])


def join_network(
    ssid: str,
    password: str | None,
    ap_ssid: str | None = None,
    ap_passphrase: str | None = None,
    iface: str = DEFAULT_WIFI_IFACE,
    sleep: Any = time.sleep,
) -> dict[str, Any]:
    """Join ``ssid``. On failure, bring the setup AP back (when its name and
    passphrase are given) so the installer is never locked out.

    Returns ``{"ok": bool, "connected": bool, "ap_restored": bool, "error": str|None}``.
    The caller MUST have warned the user before calling this: the AP goes
    down the moment the join starts.
    """
    if not _SSID_RE.match(ssid or ""):
        return {"ok": False, "connected": False, "ap_restored": False, "error": "invalid ssid"}
    had_ap = ap_active(iface)
    if had_ap:
        stop_ap(iface)
    argv = ["device", "wifi", "connect", ssid, "ifname", iface]
    if password:
        argv += ["password", password]
    rc, out, err = _nmcli(*argv, timeout=JOIN_TIMEOUT_S)
    connected = rc == 0 and station_connected(iface)
    if not connected:
        # Give NM one more beat before judging — association can lag the
        # command's return on this radio.
        sleep(2)
        connected = station_connected(iface)
    if connected:
        # A wrong password leaves a half-made profile behind; a good join
        # leaves a profile NM autoconnects at next boot, which is the point.
        logger.info("Joined %s", ssid)
        return {"ok": True, "connected": True, "ap_restored": False, "error": None}
    reason = err or out or f"nmcli rc {rc}"
    logger.warning("Join %s failed: %s", ssid, reason)
    # Drop the failed profile so NM does not keep retrying a bad password
    # and blocking the AP from coming back.
    _nmcli("connection", "delete", ssid)
    restored = False
    if had_ap and ap_ssid and ap_passphrase:
        restored = bool(start_ap(ap_ssid, ap_passphrase, iface).get("ok"))
    return {"ok": False, "connected": False, "ap_restored": restored, "error": reason}


def ensure_reachable(
    ap_ssid: str,
    ap_passphrase: str,
    grace_s: float = AP_GRACE_S,
    iface: str = DEFAULT_WIFI_IFACE,
    sleep: Any = time.sleep,
    clock: Any = time.monotonic,
) -> dict[str, Any]:
    """Boot-time rule: wait up to ``grace_s`` for a station link; if none,
    raise the AP. Called by ``scripts/wqm1-ap-fallback.py`` from a oneshot
    unit after NetworkManager, never from the firmware's sampling loop."""
    started = clock()
    while clock() - started < grace_s:
        if station_connected(iface):
            return {"ok": True, "mode": "station", "ap": False}
        sleep(3)
    result = start_ap(ap_ssid, ap_passphrase, iface)
    return {
        "ok": bool(result.get("ok")),
        "mode": "ap",
        "ap": bool(result.get("ok")),
        "detail": result,
    }
