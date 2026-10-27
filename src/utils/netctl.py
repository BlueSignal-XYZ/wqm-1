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
  never concurrent with a station link — AP+STA on ``brcmfmac`` is fragile, so
  the rule avoids the question. Zero W, Zero 2 W, Pi 3 and Pi 4 all expose
  the onboard radio as ``wlan0`` under that driver (the chip differs; this
  call does not). The AP stays up while the unit is
  unassociated, which is also how a permanently-offline well head is reached
  next year.
* **The Wi-Fi join.** From the Service Window's network step: list networks,
  join one. Success is association to that SSID, a usable IPv4 address
  (not link-local), and a gateway or DNS that answers, within about a
  minute. Joining tears the AP down, so ``join_network`` brings the AP and
  the port-80 portal straight back on failure — an installer locked out by
  a typo, a missing DHCP lease, or a router that does not answer is worse
  than no feature.

Two more since site flow v2 (2026-10-03):

* **Forget the bench network.** ``forget_saved_wifi(keep=…)`` deletes every
  saved Wi-Fi profile except the one the unit is now using (and the AP's own
  profile). The golden image remembers the shop network; a unit in the field
  must not, both because every card would otherwise carry the shop password
  and because a unit near the shop would prefer it to the customer's.
* **The captive portal.** While the AP is up, ``utils.captive_portal`` listens
  on 192.168.4.1:80 (not as root) and answers captive probes with a redirect
  to ``http://192.168.4.1/setup/``. ``start_ap`` arms that listener;
  ``join_network`` disarms it the moment a station joins, so port 80 is not
  left open on the site LAN. DNS and DHCP for that subnet are a dedicated
  dnsmasq bound to ``wlan0`` (wildcard to 192.168.4.1, DHCP option 114).
  It is stopped before the join, so it does not answer on the site LAN.
  ``remove_legacy_captive_nat`` deletes the old port-80 NAT redirect: that
  rule stole the packets from the listener and sent phones to ``:8080``.
  Root only, from the boot-time AP fallback.

The setup AP is hostapd, not NetworkManager shared mode. NetworkManager
1.52.1 always adds ``WPA-PSK-SHA256`` when ``key-mgmt`` is ``wpa-psk`` and
the supplicant supports PMF; ``pmf=disable`` only drops SAE in AP mode.
``brcmfmac`` treats that SHA256 AKM as the MFP suite, and BCM43430/1
firmware 7.45.98 does not complete the handshake when a phone selects it
with management-frame protection off. hostapd is told ``wpa_key_mgmt=WPA-PSK``
and ``ieee80211w=0``. While the AP is up, ``wlan0`` is unmanaged in
NetworkManager. The join still uses ``nmcli`` after the interface is handed
back. Every call returns a result dict and never raises.

**Simulated units never reach this code's subprocesses.** When
``WQM1_VIRTUAL_NET`` is set, every function delegates to ``utils.netsim`` — a
virtual radio — so a simulator running the real Service Window on a laptop
cannot scan or reconnect that laptop's own Wi-Fi.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import ipaddress
import logging
import os
import re
import secrets
import shutil
import signal
import socket
import ssl
import subprocess  # nosec B404 - fixed argv, no shell, resolved binaries only
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from utils import netsim
from utils.captive_portal import (
    arm_portal,
    disarm_portal,
    ensure_dnsmasq_conf,
    wait_until_reachable,
)
from utils.cardmove import confirm_binding, wifi_reconfirm_required
from utils.wifi_setup import (
    CAPTIVE_JOIN_MESSAGE,
    annotate_network,
    format_station_rssi,
    parse_iw_link,
    parse_nmcli_wifi,
    probe_sign_in_page,
    wifi_connect_argv,
)
from utils.wifi_setup import parse_iw_scan as _parse_iw_scan

logger = logging.getLogger("wqm1.netctl")

AP_CONNECTION_NAME = "wqm1-setup-ap"
# How long boot keeps actively trying a SAVED network before it raises the
# setup hotspot. 45 s of passive waiting was not enough on a Pi Zero W
# (WQM-10001, 2026-10-21); the grace now rescans and brings the profile up
# itself, and it is long enough for two full attempts.
AP_GRACE_S = 90
# A saved-profile listing that FAILS (NetworkManager not answering yet at
# boot) is retried this many times before it is read as "nothing saved".
SAVED_LIST_RETRIES = 6
SAVED_LIST_RETRY_S = 3.0
# The setup AP is hostapd on wlan0, not an NM shared connection. NM 1.52.1
# cannot be told to offer WPA-PSK without WPA-PSK-SHA256, and that AKM
# fails the 4-way handshake on BCM43430/1 firmware 7.45.98 when PMF is off.
HOSTAPD_UNIT = "wqm1-setup-hostapd.service"
DNSMASQ_UNIT = "wqm1-setup-dnsmasq.service"
# Where setup.sh installs units. Patchable so a test can point at a tmp dir.
# An image that has the units here and not under the release tree must still
# be able to start hostapd.
SYSTEMD_DIR = Path("/etc/systemd/system")
HOSTAPD_CTRL = "/run/wqm1/hostapd"
# Channel 6. 11 is the other non-overlapping 2.4 GHz channel; one is pinned
# so the beacon does not move between boots.
AP_CHANNEL = 6
AP_CHANNELS = (6, 11)
# How long to wait for wlan0 to leave "unavailable" (rfkill, firmware still
# loading, interface not created yet) before the hotspot attempt. The Pi
# Zero W's SDIO radio is often still down well after NetworkManager has
# finished starting; one shot against that interface never comes up.
RADIO_WAIT_S = 20.0
AP_ATTEMPTS = 3
AP_SUBNET = "192.168.4.1/24"
AP_ADDRESS = "192.168.4.1"
AP_NETWORK = "192.168.4.0/24"
DEFAULT_WIFI_IFACE = "wlan0"
JOIN_TIMEOUT_S = 40
# Whole join, including association, DHCP, and a gateway or DNS probe.
# nmcli's own activation timeout is JOIN_TIMEOUT_S and sits inside this budget.
ADDRESS_WAIT_S = 55.0
# A join is tried under a temporary profile name. Only a join that reached
# BlueSignal renames it to the SSID; a failure deletes only the temporary
# profile, so a wrong password re-typed for a proven network keeps it (I3).
TRY_PROFILE_PREFIX = "bs-try-"
# An older profile for the same SSID, renamed aside until the new one is in place.
OLD_PROFILE_PREFIX = "bs-old-"
# Success means BlueSignal answered over HTTPS through this network. A router
# that asks the owner to approve new devices (Eero, Firewalla) is approved in
# this window without anyone redoing the join (spec D5).
BLUESIGNAL_HOLD_S = 180.0
BLUESIGNAL_POLL_S = 10.0
HTTPS_TIMEOUT_S = 8.0
# The HTTP Date header is unauthenticated. It may move a stuck clock forward
# by at most this many calendar years, inclusive; never backwards (spec D6).
CLOCK_STEP_MAX_YEARS = 2
ADDRESS_POLL_S = 2.0
# After the setup AP lets go of wlan0, NetworkManager's scan list is EMPTY:
# it could not scan while hostapd held the radio. `nmcli device wifi connect`
# against an empty list answers "No network with SSID ... found" at once, so
# a join that did not wait for a scan failed every time (field, 2026-10-21:
# WQM-10001 could not join a router in the same room). The join rescans and
# waits up to SCAN_WAIT_S for the SSID before connecting.
SCAN_WAIT_S = 20.0
SCAN_POLL_S = 2.0
PROBE_TIMEOUT_S = 0.8
_SSID_RE = re.compile(r"^[^\x00-\x1f]{1,32}$")
_INET_RE = re.compile(r"\binet\s+(\d+\.\d+\.\d+\.\d+)")
_GW_RE = re.compile(r"^default\s+via\s+(\d+\.\d+\.\d+\.\d+)", re.MULTILINE)

# Short tails for ``wqm1-status.txt`` (``station: fail: <this>``). The setup
# page uses the longer sentences in ``_JOIN_MESSAGES``.
_STATION_FAIL = {
    "wrong_password": "wrong password",  # nosec B105 — failure word, not a secret
    "not_found": "network not found",
    "no_dhcp": "no address",
    "no_internet": "no internet",
    "no_association": "could not join",
    "invalid_ssid": "invalid network name",
    "captive_sign_in": "sign-in page",
    "no_bluesignal": "no BlueSignal",
}
_JOIN_MESSAGES = {
    "wrong_password": (
        "The password was not accepted (wrong password). "  # nosec B105 — failure sentence, not a secret
        "Check it with the owner and try again."
    ),
    "not_found": ("That network was not found. Move the unit closer to the router and scan again."),
    "no_dhcp": (
        "The unit joined the Wi-Fi but did not get an address from the router (no DHCP). "
        "Check that the router is handing out addresses, then try again."
    ),
    "no_dhcp_link_local": (
        "The unit joined the Wi-Fi but only has a link-local address (169.254.x), "
        "not one from the router. Check that the router is handing out addresses, "
        "then try again."
    ),
    "no_internet": (
        "The unit has an address but cannot reach the router or DNS (no internet). "
        "Check the router's connection, then try again."
    ),
    "captive_sign_in": CAPTIVE_JOIN_MESSAGE,
    "no_association": (
        "The unit could not join that network. Check the password and that the router "
        "is on, then try again."
    ),
    "invalid_ssid": "That network name cannot be used. Pick one from the list.",
    "no_bluesignal": (
        "The unit is on the Wi-Fi but could not reach BlueSignal. If the router asks "
        "you to approve new devices, approve this one, then try again."
    ),
}


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
    the enclosure. Never a fleet-wide default — an open or shared AP is
    still an open AP."""
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


def _regdom(text: str) -> str:
    """Country code from ``iw reg get``, or "" when it did not say."""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("country "):
            token = line.split()[1] if len(line.split()) > 1 else ""
            return token.split(":", 1)[0]
    return ""


def prepare_wifi_radio(iface: str = DEFAULT_WIFI_IFACE) -> None:
    """Make the onboard radio usable for a hotspot.

    Raspberry Pi OS leaves the radio soft-blocked until a regulatory domain
    is set, and ``brcmfmac`` keeps ``wlan0`` down (IFF_UP clear — cloud-init
    reports ``Up=False``) while the domain is world/unset (``00``). The
    golden image's first-boot script does this too, but that script is not
    installed by ``setup.sh`` and it is not ordered before
    ``network-online.target``, so the access-point path has to do it itself.
    Every call is best-effort and idempotent. Never raises.
    """
    if netsim.is_virtual():
        return
    _run(["rfkill", "unblock", "wifi"])
    _run(["rfkill", "unblock", "wlan"])
    rc, out, _ = _run(["iw", "reg", "get"])
    # Only fill in an unset domain. Do not overwrite a country an installer
    # already chose. US matches the golden image's kernel command line.
    if rc != 0 or _regdom(out) in ("", "00", "UNSET"):
        _run(["iw", "reg", "set", "US"])
    _nmcli("radio", "wifi", "on")
    _run(["ip", "link", "set", iface, "up"])
    # The brcmfmac probe enables power save before any connection profile
    # exists. start_ap turns it off again once hostapd is beaconing.
    _run(["iw", "dev", iface, "set", "power_save", "off"])
    _nmcli("device", "set", iface, "managed", "yes")


_USABLE_STATES = frozenset(
    {
        "connected",
        "disconnected",
        "connecting",
        "deactivating",
        "disconnecting",
        # The setup AP takes wlan0 away from NetworkManager. unmanaged is
        # that state, not a radio that has failed to appear.
        "unmanaged",
    }
)


def iface_state(iface: str = DEFAULT_WIFI_IFACE) -> str | None:
    """NetworkManager's state for ``iface``, or None if it is not listed."""
    if netsim.is_virtual():
        return "disconnected"
    rc, out, _ = _nmcli("-t", "-f", "DEVICE,STATE", "device", "status")
    if rc != 0:
        return None
    for line in out.splitlines():
        parts = line.split(":")
        if len(parts) >= 2 and parts[0] == iface:
            return parts[1] or None
    return None


def iface_usable(iface: str = DEFAULT_WIFI_IFACE) -> bool:
    """True when ``iface`` can host the setup AP or a station join.

    ``unavailable`` is rfkill or firmware that has not come up.
    ``unmanaged`` means NetworkManager is not configuring it, which is
    what AP mode asks for. A missing interface is not usable.
    """
    return iface_state(iface) in _USABLE_STATES


def station_connected(iface: str = DEFAULT_WIFI_IFACE) -> bool:
    """True when the Wi-Fi interface has an active NetworkManager connection
    that is NOT our own access point."""
    if netsim.is_virtual():
        return netsim.station_connected()
    rc, out, _ = _nmcli("-t", "-f", "DEVICE,STATE,CONNECTION", "device", "status")
    if rc != 0:
        return False
    for line in out.splitlines():
        parts = line.split(":")
        if len(parts) >= 3 and parts[0] == iface:
            return parts[1] == "connected" and parts[2] != AP_CONNECTION_NAME
    return False


def hostapd_conf_path() -> Path:
    override = os.environ.get("WQM1_HOSTAPD_CONF")
    if override:
        return Path(override)
    return Path("/run/wqm1/hostapd.conf")


def wifi_country() -> str:
    """Regulatory domain hostapd should advertise.

    The same rule as ``prepare_wifi_radio``: a country ``iw reg get``
    already reports is kept, and an unset domain is US.
    """
    if netsim.is_virtual():
        return "US"
    rc, out, _ = _run(["iw", "reg", "get"])
    code = _regdom(out) if rc == 0 else ""
    if len(code) == 2 and code.isalpha():
        return code.upper()
    return "US"


def hostapd_config(
    ssid: str,
    passphrase: str,
    iface: str = DEFAULT_WIFI_IFACE,
    country: str = "US",
    channel: int = AP_CHANNEL,
) -> str:
    """hostapd configuration for the setup AP.

    ``wpa_key_mgmt`` is ``WPA-PSK`` and nothing else. No SHA256 AKM, no SAE.
    ``ieee80211w=0`` and ``rsn_pairwise=CCMP``. The country code is the one
    ``prepare_wifi_radio`` leaves set.
    """
    code = (country or "").strip().upper()
    if len(code) != 2 or not code.isalpha():
        code = "US"
    use_channel = channel if channel in AP_CHANNELS else AP_CHANNEL
    lines = [
        f"interface={iface}",
        "driver=nl80211",
        f"ssid={ssid}",
        "hw_mode=g",
        f"channel={use_channel}",
        f"country_code={code}",
        "ieee80211d=1",
        "auth_algs=1",
        "wpa=2",
        "wpa_key_mgmt=WPA-PSK",
        f"wpa_passphrase={passphrase}",
        "rsn_pairwise=CCMP",
        "ieee80211w=0",
        f"ctrl_interface={HOSTAPD_CTRL}",
        "ctrl_interface_group=0",
    ]
    return "\n".join(lines) + "\n"


def hostapd_state() -> str | None:
    """hostapd control-interface state, or None when it cannot be asked.

    ``ENABLED`` means the AP is beaconing. ``""`` means hostapd_cli ran and
    the AP is not up. None means the binary is missing or the socket
    refused this user, which a status file must not record as "down".
    """
    if netsim.is_virtual():
        return "ENABLED" if netsim.ap_active() else ""
    rc, out, err = _run(["hostapd_cli", "-p", HOSTAPD_CTRL, "status"], timeout=5)
    if rc == 127:
        return None
    blob = "\n".join(part for part in (out, err) if part)
    if "permission denied" in blob.lower():
        return None
    for line in blob.splitlines():
        if line.startswith("state="):
            return line.split("=", 1)[1].strip()
    return ""


def ap_active(iface: str = DEFAULT_WIFI_IFACE) -> bool:
    """True when hostapd reports ``state=ENABLED`` on ``iface``.

    The interface argument is kept for callers that used to ask
    NetworkManager. The control socket is the one ``hostapd_config`` sets.
    """
    del iface
    if netsim.is_virtual():
        return netsim.ap_active()
    return hostapd_state() == "ENABLED"


def _nm_error(rc: int, out: str, err: str) -> str:
    """One line, stderr first. nmcli puts ``Error: ...`` on stderr."""
    parts: list[str] = []
    for chunk in (err, out):
        text = " ".join((chunk or "").split())
        if text and text not in parts:
            parts.append(text)
    return " | ".join(parts) if parts else f"nmcli rc {rc}"


def _write_private(path: Path, text: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, mode)
    tmp.replace(path)


def _systemctl(*args: str, timeout: float = 30.0) -> tuple[int, str, str]:
    return _run(["systemctl", *args], timeout=timeout)


def _set_managed(iface: str, managed: bool) -> tuple[int, str, str]:
    return _nmcli("device", "set", iface, "managed", "yes" if managed else "no")


def _packaged_unit(name: str) -> Path | None:
    packaged = Path(__file__).resolve().parents[2] / "systemd" / name
    if packaged.is_file():
        return packaged
    installed = Path("/opt/bluesignal/current/systemd") / name
    if installed.is_file():
        return installed
    return None


def ensure_ap_units() -> dict[str, Any]:
    """Install the hostapd and dnsmasq units if this process is root.

    ``setup.sh`` copies them into the release tree and into
    ``SYSTEMD_DIR``. An image that has not re-run setup still needs them
    the first time the AP is raised. A unit already installed under
    ``SYSTEMD_DIR`` is enough when the release tree has no ``systemd/``
    directory — that is the bench image that reported the unit missing
    and never started hostapd. They are not enabled: nothing starts
    them at boot except ``start_ap``.
    """
    try:
        root = os.geteuid() == 0
    except AttributeError:
        root = False
    if not root:
        return {"ok": True, "skipped": "not root"}
    changed = False
    for name in (HOSTAPD_UNIT, DNSMASQ_UNIT):
        src = _packaged_unit(name)
        dest = SYSTEMD_DIR / name
        if src is None:
            if dest.is_file():
                continue
            return {"ok": False, "error": f"{name} unit file missing"}
        text = src.read_text(encoding="utf-8")
        try:
            if dest.is_file() and dest.read_text(encoding="utf-8") == text:
                continue
            dest.write_text(text, encoding="utf-8")
        except OSError as e:
            return {"ok": False, "error": str(e)}
        changed = True
    if changed:
        rc, _out, err = _systemctl("daemon-reload")
        if rc != 0:
            return {"ok": False, "error": err or f"daemon-reload rc {rc}"}
    return {"ok": True, "reloaded": changed}


def _wait_hostapd() -> bool:
    for attempt in range(25):
        if hostapd_state() == "ENABLED":
            return True
        if attempt < 24:
            time.sleep(0.2)
    return False


def _release_ap_iface(iface: str) -> None:
    """Stop both units, drop 192.168.4.1, and give wlan0 back to NM."""
    _systemctl("stop", DNSMASQ_UNIT)
    _systemctl("stop", HOSTAPD_UNIT)
    _run(["ip", "addr", "flush", "dev", iface])
    _nmcli("connection", "delete", AP_CONNECTION_NAME)
    _set_managed(iface, True)


def start_ap(ssid: str, passphrase: str, iface: str = DEFAULT_WIFI_IFACE) -> dict[str, Any]:
    """Raise the setup hotspot with hostapd. WPA2-PSK, 192.168.4.1/24.

    Refuses a passphrase under 8 characters (WPA2 minimum) rather than
    raising an open network — an open AP is still an open AP.

    NetworkManager is told to leave ``wlan0`` alone, then hostapd beacons
    and dnsmasq serves DHCP and the captive DNS. A leftover
    ``wqm1-setup-ap`` NM profile is deleted so a later ``managed yes``
    cannot raise the shared-mode AP that advertises WPA-PSK-SHA256.
    """
    if not _SSID_RE.match(ssid or ""):
        return {"ok": False, "error": "invalid ssid"}
    if not passphrase or len(passphrase) < 8:
        return {"ok": False, "error": "passphrase must be at least 8 characters"}
    if any(ord(ch) < 32 for ch in passphrase):
        return {"ok": False, "error": "invalid passphrase"}
    if netsim.is_virtual():
        result = netsim.start_ap(ssid, passphrase)
        if result.get("ok"):
            _arm_portal()
        return result
    if ap_active(iface):
        _arm_portal()
        return {"ok": True, "ssid": ssid, "already": True}
    installed = ensure_ap_units()
    if not installed.get("ok"):
        return {"ok": False, "error": str(installed.get("error") or "ap units")}
    # Best-effort. An unmanaged or idle interface has nothing to disconnect.
    _nmcli("device", "disconnect", iface)
    _nmcli("connection", "delete", AP_CONNECTION_NAME)
    rc, out, err = _set_managed(iface, False)
    if rc != 0:
        reason = _nm_error(rc, out, err)
        logger.error("Could not unmanage %s: %s", iface, reason)
        return {"ok": False, "error": reason}
    try:
        _write_private(
            hostapd_conf_path(),
            hostapd_config(ssid, passphrase, iface, wifi_country(), AP_CHANNEL),
            0o600,
        )
    except OSError as e:
        _set_managed(iface, True)
        return {"ok": False, "error": str(e)}
    dns = ensure_dnsmasq_conf()
    if not dns.get("ok"):
        _set_managed(iface, True)
        return {"ok": False, "error": str(dns.get("error") or "dnsmasq config")}
    _run(["ip", "link", "set", iface, "up"])
    rc, out, err = _run(["ip", "addr", "replace", AP_SUBNET, "dev", iface])
    if rc != 0:
        _set_managed(iface, True)
        return {"ok": False, "error": err or out or f"ip addr rc {rc}"}
    rc, out, err = _systemctl("start", HOSTAPD_UNIT)
    if rc != 0 or not _wait_hostapd():
        reason = _nm_error(rc, out, err) if rc != 0 else "hostapd did not reach ENABLED"
        logger.error("hostapd start failed: %s", reason)
        _release_ap_iface(iface)
        return {"ok": False, "error": reason}
    _run(["iw", "dev", iface, "set", "power_save", "off"])
    rc, out, err = _systemctl("start", DNSMASQ_UNIT)
    if rc != 0:
        reason = _nm_error(rc, out, err)
        logger.error("dnsmasq start failed: %s", reason)
        _release_ap_iface(iface)
        return {"ok": False, "error": reason}
    logger.info("Setup AP up: %s on %s", ssid, iface)
    _arm_portal()
    return {"ok": True, "ssid": ssid, "address": AP_ADDRESS}


def stop_ap(iface: str = DEFAULT_WIFI_IFACE) -> dict[str, Any]:
    """Stop dnsmasq, then hostapd, flush 192.168.4.1, and return wlan0 to NM."""
    if netsim.is_virtual():
        return netsim.stop_ap()
    _release_ap_iface(iface)
    return {"ok": True}


def parse_iw_scan(text: str) -> list[dict[str, Any]]:
    """SSIDs from ``iw dev wlan0 scan``, strongest first.

    NetworkManager cannot scan while ``wlan0`` is unmanaged for hostapd.
    ``iw`` can. Band, bars, and security (enterprise, WPA3-only, open) come
    from the same text. A hidden SSID is kept so the setup page can ask for
    the name.
    """
    return _parse_iw_scan(text)


def _scan_iw(iface: str) -> list[dict[str, Any]]:
    rc, out, _ = _run(["iw", "dev", iface, "scan"], timeout=20)
    if rc != 0:
        return []
    return parse_iw_scan(out)


def scan_networks(iface: str = DEFAULT_WIFI_IFACE) -> list[dict[str, Any]]:
    """Visible networks, strongest first. Never raises; empty on failure.

    While the setup AP holds ``wlan0``, NetworkManager is not managing it,
    so the scan goes through ``iw``. A station link still uses ``nmcli``.
    """
    if netsim.is_virtual():
        return [annotate_network(n) for n in netsim.scan_networks()]
    if ap_active(iface) or iface_state(iface) == "unmanaged":
        return _scan_iw(iface)
    _nmcli("device", "wifi", "rescan", "ifname", iface, timeout=20)
    rc, out, _ = _nmcli(
        "-t",
        "-f",
        "SSID,SIGNAL,SECURITY,FREQ",
        "device",
        "wifi",
        "list",
        "ifname",
        iface,
    )
    if rc != 0:
        return []
    return parse_nmcli_wifi(out)


def read_station_rssi(iface: str = DEFAULT_WIFI_IFACE) -> int | None:
    """dBm of the station link just joined, or None when ``iw`` did not say.

    Called after a successful join so ``wqm1-status.txt`` can record
    ``station_rssi``. A simulated unit reports the virtual radio.
    """
    if netsim.is_virtual():
        return netsim.rssi_dbm()
    rc, out, _ = _run(["iw", "dev", iface, "link"], timeout=5)
    if rc != 0:
        return None
    return parse_iw_link(out)


def _sign_in_verdict() -> str:
    """``captive``, ``clear``, or ``unknown``. A probe error stays unknown."""
    try:
        return probe_sign_in_page()
    except Exception:  # noqa: BLE001 — the join result must not depend on the probe
        logger.debug("Sign-in probe failed", exc_info=True)
        return "unknown"


def usable_ipv4(addr: str) -> bool:
    """True for a DHCP (or static) IPv4 address an installer can reach.

    Link-local ``169.254.x`` is what the stack invents when no one answered
    DHCP. Loopback, ``0.0.0.0``, and multicast are not a site address either.
    """
    text = (addr or "").split("/", 1)[0].strip()
    try:
        ip = ipaddress.IPv4Address(text)
    except (ValueError, ipaddress.AddressValueError):
        return False
    return not (ip.is_link_local or ip.is_loopback or ip.is_unspecified or ip.is_multicast)


def _is_link_local(addr: str) -> bool:
    text = (addr or "").split("/", 1)[0].strip()
    try:
        return ipaddress.IPv4Address(text).is_link_local
    except (ValueError, ipaddress.AddressValueError):
        return False


def parse_ipv4_addrs(text: str) -> list[str]:
    """IPv4 addresses from ``ip -4 -o addr`` output, in order."""
    return _INET_RE.findall(text or "")


def parse_default_gateway(text: str) -> str | None:
    """The ``default via`` address on one interface's route table, if any."""
    match = _GW_RE.search(text or "")
    return match.group(1) if match else None


def _redact_secret(text: str, secret: str | None) -> str:
    cleaned = " ".join((text or "").split())
    if secret and len(secret) >= 4 and secret in cleaned:
        cleaned = cleaned.replace(secret, "[redacted]")
    return cleaned


def _classify_connect_error(text: str) -> str | None:
    """A fast failure from nmcli's own text, before the address wait.

    DHCP and route failures are not decided here: the poll is what tells
    a late lease from a dead router.
    """
    low = (text or "").lower()
    if "secret" in low or "wrong password" in low or "invalid password" in low:
        return "wrong_password"
    if "no network with ssid" in low or "ssid not found" in low:
        return "not_found"
    return None


def _join_message(kind: str, link_local: list[str]) -> str:
    if kind == "no_dhcp" and link_local:
        return _JOIN_MESSAGES["no_dhcp_link_local"]
    return _JOIN_MESSAGES.get(kind, _JOIN_MESSAGES["no_association"])


def _join_result(
    *,
    ok: bool,
    connected: bool,
    ap_restored: bool,
    error: str | None,
    message: str | None = None,
    reason: str | None = None,
    ip: str | None = None,
) -> dict[str, Any]:
    return {
        "ok": ok,
        "connected": connected,
        "ap_restored": ap_restored,
        "error": error,
        "message": message,
        "reason": reason,
        "ip": ip,
    }


def associated_ssid(iface: str = DEFAULT_WIFI_IFACE) -> str | None:
    """SSID of the active station on ``iface``, or None when it is the AP
    or there is no association."""
    if netsim.is_virtual():
        return netsim.current_ssid()
    rc, out, _ = _nmcli("-t", "-f", "DEVICE,STATE,CONNECTION", "device", "status")
    if rc != 0:
        return None
    for line in out.splitlines():
        parts = line.split(":")
        if len(parts) >= 3 and parts[0] == iface and parts[1] == "connected":
            name = parts[2]
            if not name or name in ("--", AP_CONNECTION_NAME):
                return None
            rc2, ssid, _ = _nmcli("-g", "802-11-wireless.ssid", "connection", "show", name)
            return (ssid or name) if rc2 == 0 else name
    return None


def read_iface_net(iface: str = DEFAULT_WIFI_IFACE) -> dict[str, Any]:
    """Association, IPv4 addresses, gateway, and DNS for ``iface``.

    Never raises. A simulated unit reports the virtual station address and
    does not read the laptop's routes.
    """
    empty: dict[str, Any] = {
        "ssid": None,
        "usable": [],
        "link_local": [],
        "gateway": None,
        "dns": [],
    }
    if netsim.is_virtual():
        ssid = netsim.current_ssid()
        if not ssid:
            return empty
        return {
            "ssid": ssid,
            "usable": ["192.168.1.57"],
            "link_local": [],
            "gateway": "192.168.1.1",
            "dns": ["192.168.1.1"],
        }
    ssid = associated_ssid(iface)
    rc, out, _ = _run(["ip", "-4", "-o", "addr", "show", "dev", iface])
    addrs = parse_ipv4_addrs(out if rc == 0 else "")
    usable = [addr for addr in addrs if usable_ipv4(addr)]
    link_local = [addr for addr in addrs if _is_link_local(addr)]
    rc2, routes, _ = _run(["ip", "-4", "route", "show", "dev", iface])
    gateway = parse_default_gateway(routes if rc2 == 0 else "")
    dns: list[str] = []
    rc3, dns_out, _ = _nmcli("-g", "IP4.DNS", "device", "show", iface)
    if rc3 == 0:
        for line in (dns_out or "").splitlines():
            host = line.strip().split("/", 1)[0]
            if host and host != "--" and usable_ipv4(host):
                dns.append(host)
    return {
        "ssid": ssid,
        "usable": usable,
        "link_local": link_local,
        "gateway": gateway,
        "dns": dns,
    }


def _tcp_answered(host: str, port: int, timeout: float) -> bool:
    """True when ``host`` accepts the TCP connection or refuses it.

    A refusal means the host is on the network and answered. A timeout or
    an unreachable route does not.
    """
    if not host:
        return False
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        rc = sock.connect_ex((host, int(port)))
    except OSError:
        return False
    finally:
        sock.close()
    return rc in (0, errno.ECONNREFUSED)


def _dns_query() -> bytes:
    """One recursive A query for example.com. Any DNS reply counts."""
    header = b"\x00\x01\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
    question = b"\x07example\x03com\x00\x00\x01\x00\x01"
    return header + question


def _dns_answered(host: str, timeout: float) -> bool:
    if not host:
        return False
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout)
        sock.sendto(_dns_query(), (host, 53))
        data, _addr = sock.recvfrom(512)
    except OSError:
        return False
    finally:
        sock.close()
    return len(data) >= 12


def reach_gateway_or_dns(
    gateway: str | None,
    dns: list[str],
    *,
    timeout: float = PROBE_TIMEOUT_S,
    clock: Any = time.monotonic,
    deadline: float | None = None,
) -> bool:
    """True when the gateway answers TCP/80 or TCP/443, or a DNS server answers.

    Stops at ``deadline`` so a dead router cannot stretch the join past
    ``ADDRESS_WAIT_S``. Connection refused counts: the host is there.
    """

    def pending() -> bool:
        return deadline is None or clock() < deadline

    if (
        gateway
        and pending()
        and (_tcp_answered(gateway, 443, timeout) or _tcp_answered(gateway, 80, timeout))
    ):
        return True
    targets: list[str] = []
    for host in [*(dns or []), gateway or ""]:
        if host and host not in targets:
            targets.append(host)
    for host in targets:
        if not pending():
            return False
        if _dns_answered(host, timeout):
            return True
    return False


# ``ssl`` verify codes for a certificate the clock says is not valid yet,
# or no longer valid. Either means the unit's clock may be wrong.
_CERT_TIME_CODES = {9, 10}


def _is_cert_time_error(err: BaseException | None) -> bool:
    if isinstance(err, ssl.SSLCertVerificationError):
        if getattr(err, "verify_code", None) in _CERT_TIME_CODES:
            return True
        text = str(getattr(err, "verify_message", "") or err).lower()
        return "not yet valid" in text or "has expired" in text
    return False


def _https_status(url: str, timeout: float = HTTPS_TIMEOUT_S) -> str:
    """``ok`` when the HTTPS server at ``url`` answered with any status,
    ``time`` on a certificate-time error, else ``fail``.

    Certificate verification stays on: an answer proves DNS, routing and TLS
    through this network. The status code does not matter.
    """
    request = urllib.request.Request(url, method="HEAD")  # noqa: S310 - https only
    try:
        with urllib.request.urlopen(  # nosec B310 - fixed https URL, verified TLS
            request, timeout=timeout, context=ssl.create_default_context()
        ):
            return "ok"
    except urllib.error.HTTPError:
        return "ok"
    except urllib.error.URLError as e:
        return "time" if _is_cert_time_error(e.reason) else "fail"
    except ssl.SSLCertVerificationError as e:
        return "time" if _is_cert_time_error(e) else "fail"
    except (OSError, ValueError):
        return "fail"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Stop at the first answer. A host that sends plain HTTP on to HTTPS
    answers with a 3xx that already carries ``Date``; following it would
    hit the very certificate check a wrong clock fails."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        return None


_DATE_OPENER = urllib.request.build_opener(_NoRedirect)


def _http_date(url: str, timeout: float = HTTPS_TIMEOUT_S) -> datetime | None:
    """The ``Date`` header from a plain-HTTP answer, or None. Unauthenticated.

    Any status counts, a redirect included: it is not followed.
    """
    request = urllib.request.Request(url, method="HEAD")  # noqa: S310 - Date only
    headers: Any = None
    try:
        with _DATE_OPENER.open(  # nosec B310 - fixed URL; only the Date header is read
            request, timeout=timeout
        ) as resp:
            headers = resp.headers
    except urllib.error.HTTPError as e:
        headers = e.headers
    except (OSError, ValueError):
        return None
    raw = headers.get("Date") if headers is not None else None
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if parsed is None or parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _add_years(when: datetime, years: int) -> datetime:
    try:
        return when.replace(year=when.year + years)
    except ValueError:  # 29 February into a year without one
        return when.replace(year=when.year + years, day=28)


def clock_step_allowed(now: datetime, header: datetime) -> bool:
    """True when ``header`` is ahead of ``now`` by at most CLOCK_STEP_MAX_YEARS
    calendar years (inclusive). Never backwards.

    The cap bounds a forged ``Date``; it does not make the header trusted. A
    unit further behind recovers through GPS (``utils.clock.observe_gps``).
    """
    return now < header <= _add_years(now, CLOCK_STEP_MAX_YEARS)


CLOCK_STEP_MARKER = Path("/run/bluesignal/clock-stepped")


def clock_step_marker() -> Path:
    """Written after a clock step; /run is emptied by a reboot."""
    override = os.environ.get("WQM1_CLOCK_STEP_MARKER")
    return Path(override) if override else CLOCK_STEP_MARKER


# Set by a step in this process. The marker is what a reboot clears; this is
# what still refuses a second step when the marker could not be written.
_STEPPED_IN_PROCESS = False


def _clock_stepped_this_boot() -> bool:
    return _STEPPED_IN_PROCESS or clock_step_marker().exists()


def _mark_clock_stepped() -> None:
    global _STEPPED_IN_PROCESS
    _STEPPED_IN_PROCESS = True
    try:
        path = clock_step_marker()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") + "\n")
    except OSError as e:
        logger.warning(
            "Clock step not recorded on disk; refusing further steps in this process: %s", e
        )


def _ntp_synced() -> bool:
    from utils.clock import ClockDiscipline

    return ClockDiscipline().ntp_synced()


def _set_clock(when: datetime) -> bool:
    from utils.clock import _set_system_time

    return _set_system_time(when)


def _repair_clock(
    host: str,
    *,
    http_date: Callable[[str], datetime | None],
    now: Callable[[], datetime],
    set_time: Callable[[datetime], bool],
    ntp_synced: Callable[[], bool],
    stepped_this_boot: Callable[[], bool] = _clock_stepped_this_boot,
    mark_stepped: Callable[[], None] = _mark_clock_stepped,
) -> dict[str, Any]:
    """Move a stuck clock forward from the API host's ``Date`` header.

    At most once per boot. The header is unauthenticated, so the cap bounds
    one forged step; refusing a second one stops a LAN that keeps forging it
    from walking the clock forward two years per join.
    """
    if ntp_synced():
        return {"action": "none", "reason": "ntp"}
    if stepped_this_boot():
        logger.warning("Not moving the clock again: it was already set forward this boot")
        return {"action": "refused", "reason": "once_per_boot"}
    header = http_date(f"http://{host}/")
    current = now()
    if header is None:
        return {"action": "none", "reason": "no_date"}
    if not clock_step_allowed(current, header):
        logger.warning(
            "Not moving the clock from %s to an HTTP Date of %s (outside the %d-year cap)",
            current.strftime("%Y-%m-%dT%H:%M:%SZ"),
            header.strftime("%Y-%m-%dT%H:%M:%SZ"),
            CLOCK_STEP_MAX_YEARS,
        )
        return {"action": "refused", "reason": "step"}
    ok = False
    try:
        ok = bool(set_time(header))
    except Exception as e:  # noqa: BLE001 - a failed correction is logged, never fatal
        logger.error("clock correction from HTTP Date failed: %s", e)
    if ok:
        mark_stepped()
        logger.warning(
            "System clock set forward from the API host's Date header: %s -> %s",
            current.strftime("%Y-%m-%dT%H:%M:%SZ"),
            header.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
    return {"action": "set" if ok else "set_failed", "reason": "cert_time"}


def reach_bluesignal(
    api_base: str,
    *,
    sleep: Callable[[float], Any] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    hold_s: float = BLUESIGNAL_HOLD_S,
    poll_s: float = BLUESIGNAL_POLL_S,
    probe: Callable[[str], str] | None = None,
    http_date: Callable[[str], datetime | None] | None = None,
    now: Callable[[], datetime] | None = None,
    set_time: Callable[[datetime], bool] | None = None,
    ntp_synced: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """True only when BlueSignal answered over HTTPS through this network.

    Tries every ``poll_s`` for ``hold_s``. On a certificate-time error with
    timesyncd unsynced, moves the clock forward from the API host's ``Date``
    header (bounded by ``clock_step_allowed``) and retries once.
    Returns ``{"ok", "clock"}``.
    """
    probe = probe or _https_status
    http_date = http_date or _http_date
    now = now or (lambda: datetime.now(UTC))
    set_time = set_time or _set_clock
    ntp_synced = ntp_synced or _ntp_synced
    host = urlparse(api_base or "").hostname
    if not host:
        logger.error("No BlueSignal API host configured; the join cannot be proven")
        return {"ok": False, "clock": None}
    url = f"https://{host}/"
    deadline = clock() + hold_s
    clock_record: dict[str, Any] | None = None
    while True:
        verdict = probe(url)
        if verdict == "ok":
            return {"ok": True, "clock": clock_record}
        if verdict == "time" and clock_record is None:
            clock_record = _repair_clock(
                host,
                http_date=http_date,
                now=now,
                set_time=set_time,
                ntp_synced=ntp_synced,
            )
            if clock_record.get("action") == "set" and probe(url) == "ok":
                return {"ok": True, "clock": clock_record}
        if clock() >= deadline:
            logger.warning("BlueSignal did not answer through this network in %.0f s", hold_s)
            return {"ok": False, "clock": clock_record}
        sleep(poll_s)


def _default_api_base() -> str:
    try:
        from utils.config import get_settings

        return str(get_settings().cloud_api_base or "")
    except Exception as e:  # noqa: BLE001 - an unreadable config is a failed proof, not a crash
        logger.error("Could not read cloud_api_base: %s", e)
        return ""


def _publish_station(**fields: Any) -> None:
    """Best-effort ``wqm1-status.txt`` update. A failed write must not decide
    whether the join, or the hotspot that follows it, succeeded."""
    try:
        from diagnostics.boot_status import write_boot_status

        write_boot_status(**fields)
    except Exception as e:  # noqa: BLE001 — the join result is the status, not the file
        logger.error("Could not record station status: %s", e)


def _station_ready(iface: str = DEFAULT_WIFI_IFACE) -> dict[str, Any] | None:
    """Station link plus a usable IPv4, or None.

    Used at boot. Gateway reachability is deliberately not required: an
    ISP outage during reboot must not drop a commissioned unit off the
    site Wi-Fi and raise the setup hotspot in its place. ``join_network``
    is the stricter check.
    """
    if not station_connected(iface):
        return None
    snap = read_iface_net(iface)
    if not snap.get("usable"):
        return None
    return snap


def _restore_ap(
    had_ap: bool,
    ap_ssid: str | None,
    ap_passphrase: str | None,
    iface: str,
    sleep: Any,
) -> bool:
    """Bring the setup hotspot back. ``start_ap`` arms the portal."""
    if not (had_ap and ap_ssid and ap_passphrase):
        return False
    restored = False
    for attempt in range(max(1, AP_ATTEMPTS)):
        restored = bool(start_ap(ap_ssid, ap_passphrase, iface).get("ok"))
        if restored or attempt + 1 >= AP_ATTEMPTS:
            break
        sleep(2)
    return restored


def _visible_ssids(iface: str) -> set[str]:
    """SSIDs in NetworkManager's scan list right now (no rescan here)."""
    rc, out, _ = _nmcli(
        "-t",
        "-f",
        "SSID,SIGNAL,SECURITY,FREQ",
        "device",
        "wifi",
        "list",
        "ifname",
        iface,
    )
    if rc != 0:
        return set()
    return {str(n.get("ssid") or "") for n in parse_nmcli_wifi(out) if n.get("ssid")}


def wait_for_ssid(
    ssid: str,
    iface: str = DEFAULT_WIFI_IFACE,
    sleep: Any = time.sleep,
    clock: Any = time.monotonic,
    wait_s: float = SCAN_WAIT_S,
) -> bool:
    """Rescan until ``ssid`` is in NetworkManager's list, or ``wait_s`` passes.

    True when it was seen. False is not a verdict on its own: a hidden
    network never appears in a scan, so the caller still tries the join.
    """
    deadline = clock() + wait_s
    while True:
        _nmcli("device", "wifi", "rescan", "ifname", iface, timeout=20)
        if ssid in _visible_ssids(iface):
            return True
        if clock() >= deadline:
            return False
        sleep(SCAN_POLL_S)


def _connect(
    ssid: str,
    password: str | None,
    iface: str,
    sleep: Any,
    clock: Any,
    *,
    name: str | None = None,
    hidden: bool = False,
) -> tuple[int, str, str]:
    """Scan, connect, and retry the one failure a slow scan causes.

    1. Wait for the SSID to appear in a fresh scan (a just-released radio
       has an empty list).
    2. If NetworkManager still answers "not found", rescan once more and
       retry.
    3. Then try it as a hidden network (an active probe for this SSID),
       which does not need the network to be in the list at all.

    A ``hidden`` network is never in a scan, so it goes straight to the
    active probe. ``name`` is the temporary profile name; a failed attempt's
    profile is deleted before the next one.

    Returns (rc, nmcli text with the password redacted, last stage).
    """
    base = wifi_connect_argv(ssid, password, iface, name=name)
    if hidden:
        attempts = [("hidden", [*base, "hidden", "yes"])]
    else:
        seen = wait_for_ssid(ssid, iface, sleep=sleep, clock=clock)
        if not seen:
            logger.warning("%s not seen in a scan yet; trying the join anyway", ssid)
        attempts = [("scan", base), ("rescan", base), ("hidden", [*base, "hidden", "yes"])]
    rc, nm_text, stage = 0, "", attempts[0][0]
    for i, (stage, argv) in enumerate(attempts):
        if i == 1:
            wait_for_ssid(ssid, iface, sleep=sleep, clock=clock, wait_s=SCAN_WAIT_S / 2)
        if i and name:
            _nmcli("connection", "delete", name)
        rc, out, err = _nmcli(*argv, timeout=float(JOIN_TIMEOUT_S))
        nm_text = _redact_secret(err or out or (f"nmcli rc {rc}" if rc else ""), password)
        if rc == 0 or _classify_connect_error(nm_text) != "not_found":
            return rc, nm_text, stage
        logger.warning("Join %s: %s (attempt %d/%d)", ssid, nm_text, i + 1, len(attempts))
    return rc, nm_text, stage


def join_network(
    ssid: str,
    password: str | None,
    ap_ssid: str | None = None,
    ap_passphrase: str | None = None,
    iface: str = DEFAULT_WIFI_IFACE,
    sleep: Any = time.sleep,
    clock: Any = time.monotonic,
    address_wait_s: float = ADDRESS_WAIT_S,
    on_failure: Callable[[dict[str, Any]], None] | None = None,
    *,
    hidden: bool = False,
    api_base: str | None = None,
    reach: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Join ``ssid`` and report success only when the unit reached BlueSignal.

    Success requires all four, the first three polled until
    ``address_wait_s`` (about a minute): ``wlan0`` is associated to this
    SSID, it has a usable IPv4 address (a DHCP or static lease — not
    link-local ``169.254.x``), that network's gateway or a DNS server
    answers, and then BlueSignal answers over HTTPS (``reach_bluesignal``,
    up to ``BLUESIGNAL_HOLD_S``). The gateway/DNS step stays first, so
    ``no_internet`` remains a distinct reason from ``no_bluesignal``.

    The join runs under a temporary ``bs-try-`` profile. Success renames it
    to the SSID and deletes older profiles for that SSID; failure deletes
    only the temporary profile, so a proven network survives a wrong
    password typed for it (I3).

    On any failure the setup hotspot is raised again when its name and
    passphrase were given, which also arms the port-80 portal. ``on_failure``
    runs after the reason is known and before that raise, so the setup page
    can store the sentence the phone will show the moment the hotspot is back.

    Returns ``ok``, ``connected``, ``ap_restored``, ``error`` (nmcli text, or
    None), ``message`` (installer sentence, or None), ``reason`` (short code,
    or None), and ``ip``. The caller MUST have warned the user before calling
    this: the AP goes down the moment the join starts.
    """
    if not _SSID_RE.match(ssid or ""):
        return _join_result(
            ok=False,
            connected=False,
            ap_restored=False,
            error="invalid ssid",
            message=_JOIN_MESSAGES["invalid_ssid"],
            reason="invalid_ssid",
        )
    if netsim.is_virtual():
        result = netsim.join_network(ssid, password, ap_ssid, ap_passphrase)
        if not result.get("connected"):
            _notify_failure(on_failure, result)
        _portal_after_join(result)
        if result.get("connected"):
            rssi = netsim.rssi_dbm()
            result["rssi"] = rssi
            _publish_station(
                station=f"ok {result.get('ip') or ''}".strip(),
                hotspot="station",
                portal="down: station",
                station_rssi=format_station_rssi(rssi),
            )
            _confirm_moved_card()
        else:
            short = _STATION_FAIL.get(str(result.get("reason") or ""), "could not join")
            _publish_station(
                station=f"fail: {short}",
                hotspot="up" if result.get("ap_restored") else "down",
                portal="up (80)" if result.get("ap_restored") else "down: hotspot down",
            )
        return result
    had_ap = ap_active(iface)
    if had_ap:
        stop_ap(iface)
        # The address is about to disappear. Close port 80 before the join
        # so a phone that is still associated is not kept on our portal,
        # and a successful join cannot leave the listener up (#136).
        _disarm_portal("joining")
    try_name = f"{TRY_PROFILE_PREFIX}{secrets.token_hex(4)}"
    # One argv element for the password, so a space or a quote is the
    # password and not a shell metacharacter. Nothing here logs that element.
    rc, nm_text, _stage = _connect(
        ssid, password, iface, sleep, clock, name=try_name, hidden=hidden
    )
    # The address budget starts once NetworkManager has the network, so a
    # slow scan does not eat the DHCP wait.
    deadline = clock() + address_wait_s
    fast = _classify_connect_error(nm_text) if rc != 0 else None
    if fast in ("wrong_password", "not_found"):
        snap = read_iface_net(iface)
        # A stale error string must not undo a link that is actually up.
        if snap.get("ssid") != ssid or not snap.get("usable"):
            return _fail_join(
                ssid=ssid,
                password=password,
                kind=fast,
                nm_text=nm_text,
                link_local=list(snap.get("link_local") or []),
                had_ap=had_ap,
                ap_ssid=ap_ssid,
                ap_passphrase=ap_passphrase,
                iface=iface,
                sleep=sleep,
                on_failure=on_failure,
                profile=try_name,
            )
    while True:
        snap = read_iface_net(iface)
        usable = list(snap.get("usable") or [])
        on_target = snap.get("ssid") == ssid and bool(usable)
        reached = on_target and reach_gateway_or_dns(
            snap.get("gateway"),
            list(snap.get("dns") or []),
            clock=clock,
            deadline=deadline,
        )
        if on_target and reached:
            if _sign_in_verdict() == "captive":
                return _fail_join(
                    ssid=ssid,
                    password=password,
                    kind="captive_sign_in",
                    nm_text=nm_text,
                    link_local=list(snap.get("link_local") or []),
                    had_ap=had_ap,
                    ap_ssid=ap_ssid,
                    ap_passphrase=ap_passphrase,
                    iface=iface,
                    sleep=sleep,
                    on_failure=on_failure,
                    profile=try_name,
                )
            proof = (reach or reach_bluesignal)(
                api_base if api_base is not None else _default_api_base(),
                sleep=sleep,
                clock=clock,
            )
            if not proof.get("ok"):
                return _fail_join(
                    ssid=ssid,
                    password=password,
                    kind="no_bluesignal",
                    nm_text=nm_text,
                    link_local=list(snap.get("link_local") or []),
                    had_ap=had_ap,
                    ap_ssid=ap_ssid,
                    ap_passphrase=ap_passphrase,
                    iface=iface,
                    sleep=sleep,
                    on_failure=on_failure,
                    profile=try_name,
                )
            _promote_try_profile(try_name, ssid, iface)
            ip = str(usable[0])
            rssi = read_station_rssi(iface)
            logger.info("Joined %s at %s", ssid, ip)
            _disarm_portal("station")
            _publish_station(
                station=f"ok {ip}",
                hotspot="station",
                portal="down: station",
                station_rssi=format_station_rssi(rssi),
            )
            _confirm_moved_card()
            joined = _join_result(
                ok=True,
                connected=True,
                ap_restored=False,
                error=None,
                ip=ip,
            )
            joined["rssi"] = rssi
            return joined
        if clock() >= deadline:
            kind = _failure_kind(ssid, snap, nm_text)
            if kind == "no_internet" and _sign_in_verdict() == "captive":
                kind = "captive_sign_in"
            return _fail_join(
                ssid=ssid,
                password=password,
                kind=kind,
                nm_text=nm_text,
                link_local=list(snap.get("link_local") or []),
                had_ap=had_ap,
                ap_ssid=ap_ssid,
                ap_passphrase=ap_passphrase,
                iface=iface,
                sleep=sleep,
                on_failure=on_failure,
                profile=try_name,
            )
        sleep(ADDRESS_POLL_S)


def _failure_kind(ssid: str, snap: dict[str, Any], nm_text: str) -> str:
    if snap.get("ssid") == ssid and snap.get("usable"):
        return "no_internet"
    if snap.get("ssid") == ssid:
        return "no_dhcp"
    low = (nm_text or "").lower()
    if "ip configuration" in low or "no available address" in low:
        return "no_dhcp"
    return _classify_connect_error(nm_text) or "no_association"


def _rename_profile(old: str, new: str) -> bool:
    rc, out, err = _nmcli("connection", "modify", old, "connection.id", new)
    if rc != 0:
        logger.warning("Could not rename %s to %s: %s", old, new, err or out)
    return rc == 0


def _promote_try_profile(try_name: str, ssid: str, iface: str = DEFAULT_WIFI_IFACE) -> None:
    """After a proven join, make the temporary profile the SSID's only one.

    Nothing is deleted until the new profile carries the SSID's name, so a
    failed step leaves the network saved (I3): older profiles for ``ssid``
    are first renamed out of the way (NetworkManager allows two profiles
    with one name, and a delete by that name would take both), the
    temporary profile is renamed to ``ssid``, and only then are the older
    ones deleted. If the rename fails, the older ones get their names back
    and the temporary profile stays saved under its own name.
    Renaming keeps the saved password; re-creating the profile would drop it.
    """
    older = [p["name"] for p in saved_wifi(iface) if p["ssid"] == ssid and p["name"] != try_name]
    parked: list[tuple[str, str]] = []
    for name in older:
        aside = f"{OLD_PROFILE_PREFIX}{secrets.token_hex(3)}"
        if not _rename_profile(name, aside):
            for original, moved in reversed(parked):
                _rename_profile(moved, original)
            return
        parked.append((name, aside))
    if not _rename_profile(try_name, ssid):
        for original, moved in reversed(parked):
            _rename_profile(moved, original)
        return
    for _original, moved in parked:
        _nmcli("connection", "delete", moved)


def _notify_failure(
    on_failure: Callable[[dict[str, Any]], None] | None, pending: dict[str, Any]
) -> None:
    if on_failure is None:
        return
    try:
        on_failure(dict(pending))
    except Exception:  # noqa: BLE001 — the hotspot still has to come back
        logger.exception("Join failure hook failed")


def _fail_join(
    *,
    ssid: str,
    password: str | None,
    kind: str,
    nm_text: str,
    link_local: list[str],
    had_ap: bool,
    ap_ssid: str | None,
    ap_passphrase: str | None,
    iface: str,
    sleep: Any,
    on_failure: Callable[[dict[str, Any]], None] | None,
    profile: str | None = None,
) -> dict[str, Any]:
    """Record the reason, then raise the hotspot. The hook runs first so the
    setup page has the sentence before the phone can rejoin.

    Only ``profile`` (the temporary ``bs-try-`` profile) is deleted. The
    SSID's own profile, if one was proven earlier, is never deleted here.
    """
    message = _join_message(kind, link_local)
    short = _STATION_FAIL.get(kind, "could not join")
    logger.warning("Join %s failed (%s): %s", ssid, kind, nm_text or message)
    # Drop the failed attempt's profile so NM does not keep retrying a bad
    # password and blocking the AP from coming back.
    if profile:
        _nmcli("connection", "delete", profile)
    pending = _join_result(
        ok=False,
        connected=False,
        ap_restored=False,
        error=_redact_secret(nm_text, password) or message,
        message=message,
        reason=kind,
    )
    _publish_station(station=f"fail: {short}", portal="down: joining")
    _notify_failure(on_failure, pending)
    restored = _restore_ap(had_ap, ap_ssid, ap_passphrase, iface, sleep)
    pending["ap_restored"] = restored
    if restored:
        portal = wait_until_reachable(seconds=3.0, pause=sleep)
    else:
        _disarm_portal("hotspot down")
        portal = "down: hotspot down"
    _publish_station(
        station=f"fail: {short}",
        hotspot="up" if restored else "down",
        portal=portal,
    )
    return pending


def _confirm_moved_card() -> None:
    """A finished join is the reconfirm. A missing binding file is a no-op."""
    try:
        confirm_binding()
    except Exception:
        logger.exception("Could not record card reconfirm")


def _hold_site_wifi(iface: str) -> None:
    """Do not keep a site profile that belongs to the Pi this card left.

    Autoconnect is turned off and wlan0 is disconnected. The profiles stay,
    so the setup page can join one again. A virtual radio only drops its
    station; it must not call nmcli on the machine running the simulator.
    """
    if netsim.is_virtual():
        netsim.release_station()
        return
    for prof in saved_wifi(iface):
        _nmcli("connection", "modify", prof["name"], "connection.autoconnect", "no")
    if station_connected(iface):
        _nmcli("device", "disconnect", iface)


def ensure_reachable(
    ap_ssid: str,
    ap_passphrase: str,
    grace_s: float = AP_GRACE_S,
    iface: str = DEFAULT_WIFI_IFACE,
    sleep: Any = time.sleep,
    clock: Any = time.monotonic,
    radio_wait_s: float = RADIO_WAIT_S,
    ap_attempts: int = AP_ATTEMPTS,
) -> dict[str, Any]:
    """Boot-time rule: if a station link is up, keep it; otherwise raise the AP.

    Called by ``scripts/wqm1-ap-fallback.py`` from a oneshot unit. Never from
    the firmware's sampling loop, and never dependent on that process: a
    sensor that failed to initialise must not stop the setup hotspot.

    An unconfigured unit has no saved Wi-Fi profile, so there is nothing for
    the station grace period to wait for — waiting it out is how the hotspot
    was still down when ``wlan0`` itself was still down. The grace period
    stays for a unit that DOES have a saved network (a site whose AP is slow
    to answer). The radio is unblocked and ``wlan0`` is given ``radio_wait_s``
    to become usable before the hotspot command, and that command is retried:
    one hotspot activation against an interface that is still
    ``unavailable`` used to exit the oneshot for the rest of the boot.

    A card that moved off the Pi it was commissioned on, with a site
    profile still saved, does not take that station. Setup has to join
    again. See ``utils.cardmove``.
    """
    reconfirm = False
    try:
        reconfirm = wifi_reconfirm_required()
    except Exception:
        logger.exception("card move check failed")
    if reconfirm:
        logger.warning(
            "Card moved onto this Pi with a site network saved — "
            "not joining it until setup reconfirms"
        )
        _hold_site_wifi(iface)
    prepare_wifi_radio(iface)
    radio_deadline = clock() + radio_wait_s
    unusable_station = False

    def _take_station() -> dict[str, Any] | None:
        if reconfirm:
            return None
        snap = _station_ready(iface)
        if not snap:
            return None
        ip = str(snap["usable"][0])
        _disarm_portal("station")
        return {"ok": True, "mode": "station", "ap": False, "ip": ip}

    def _note_unusable() -> None:
        nonlocal unusable_station
        if station_connected(iface) and _station_ready(iface) is None:
            unusable_station = True

    while True:
        taken = _take_station()
        if taken:
            return taken
        _note_unusable()
        if iface_usable(iface) or clock() >= radio_deadline:
            break
        sleep(2)
    taken = _take_station()
    if taken:
        return taken
    _note_unusable()
    # None saved profiles → do not burn the grace period. A failure to list
    # profiles (nmcli missing or not up yet) also returns an empty list;
    # trying the AP is the point of this service, and the attempts below
    # still back off.
    # Ask for the saved profiles until NetworkManager answers. A failed
    # listing used to read as "nothing saved", so a slow NetworkManager at
    # boot skipped the grace entirely and the unit sat on its hotspot with
    # the site network saved and never tried.
    profiles: list[dict[str, str]] = []
    if not reconfirm:
        for i in range(SAVED_LIST_RETRIES):
            listed = _list_saved_wifi(iface)
            if listed is not None:
                profiles = listed
                break
            if i + 1 < SAVED_LIST_RETRIES:
                sleep(SAVED_LIST_RETRY_S)
    grace = grace_s if profiles else 0.0
    started = clock()
    last_error = ""
    while clock() - started < grace:
        taken = _take_station()
        if taken:
            return taken
        _note_unusable()
        if not station_connected(iface):
            # Drive it: a fresh scan, then bring each saved profile up. Waiting
            # for NetworkManager's own autoconnect was the passive half of the
            # field failure.
            for prof in profiles:
                # Each attempt can block 6 s + JOIN_TIMEOUT_S. One that would
                # end past the grace deadline is not started, so the oneshot
                # stays inside bluesignal-ap-fallback.service's
                # TimeoutStartSec and still raises the hotspot.
                if clock() - started + 6.0 + JOIN_TIMEOUT_S > grace:
                    break
                wait_for_ssid(prof["ssid"], iface, sleep=sleep, clock=clock, wait_s=6.0)
                rc, out, err = _nmcli(
                    "connection", "up", prof["name"], "ifname", iface, timeout=float(JOIN_TIMEOUT_S)
                )
                if rc == 0:
                    last_error = ""
                    break
                last_error = (err or out or f"nmcli rc {rc}").strip()
                logger.warning("Saved network %s did not come up: %s", prof["ssid"], last_error)
        # Give DHCP a moment before the next attempt.
        for _ in range(5):
            taken = _take_station()
            if taken:
                return taken
            if clock() - started >= grace:
                break
            sleep(3)
    # A station that associated and never got a lease still owns wlan0.
    # Release it so the hotspot command is not refused.
    if station_connected(iface) and _station_ready(iface) is None:
        unusable_station = True
        logger.warning(
            "Station on %s has no usable IPv4 — releasing it to raise the setup AP",
            iface,
        )
        _nmcli("device", "disconnect", iface)
    last: dict[str, Any] = {}
    attempts = max(1, ap_attempts)
    for attempt in range(attempts):
        taken = _take_station()
        if taken:
            return taken
        if not iface_usable(iface):
            prepare_wifi_radio(iface)
        last = start_ap(ap_ssid, ap_passphrase, iface)
        if last.get("ok"):
            break
        if attempt + 1 < attempts:
            logger.warning(
                "Setup AP attempt %d/%d failed (%s) — retrying",
                attempt + 1,
                attempts,
                last.get("error"),
            )
            sleep(2)
    ok = bool(last.get("ok"))
    result: dict[str, Any] = {"ok": ok, "mode": "ap", "ap": ok, "detail": last}
    if unusable_station:
        result["station_failure"] = "no address"
    elif last_error:
        # Written to wqm1-status.txt, which is how a pulled card is read.
        kind = _classify_connect_error(last_error) or "no_association"
        result["station_failure"] = _STATION_FAIL.get(kind, "could not join")
    return result


# While the setup hotspot is up and nobody is on it, a saved site network is
# tried again on this cadence (bluesignal-wifi-retry.timer). Power comes back
# after an outage and the Pi boots faster than the router; without a retry the
# unit sat on its hotspot until someone power-cycled it.
RETRY_STATION_WAIT_S = 40.0
# bluesignal-wifi-retry.service is killed at TimeoutStartSec=150. The retry
# starts no saved profile that could run past this, which leaves time to
# bring the hotspot back.
RETRY_BUDGET_S = 110.0
_RETRY_SSID_WAIT_S = 8.0


def ap_clients(iface: str = DEFAULT_WIFI_IFACE) -> int | None:
    """Phones associated to the setup hotspot right now, or None if unknown."""
    rc, out, _ = _run(["iw", "dev", iface, "station", "dump"], timeout=5)
    if rc != 0:
        return None
    return sum(1 for line in out.splitlines() if line.startswith("Station "))


def retry_saved_station(
    ap_ssid: str,
    ap_passphrase: str,
    iface: str = DEFAULT_WIFI_IFACE,
    sleep: Any = time.sleep,
    clock: Any = time.monotonic,
    wait_s: float = RETRY_STATION_WAIT_S,
) -> dict[str, Any]:
    """Try the saved site network again from the hotspot. Never strands anyone.

    Does nothing unless the hotspot is up, NO phone is associated to it (an
    installer mid-setup is never cut off), and a site network is saved. On
    failure the hotspot comes back with the same name and password.
    """
    if netsim.is_virtual():
        return {"skipped": "virtual radio"}
    if not ap_active(iface):
        return {"skipped": "hotspot not up"}
    clients = ap_clients(iface)
    if clients is None:
        return {"skipped": "could not read hotspot clients"}
    if clients > 0:
        return {"skipped": "a phone is on the hotspot"}
    profiles = _list_saved_wifi(iface) or []
    if not profiles:
        return {"skipped": "no saved network"}
    # A card that moved onto another Pi holds its saved site network until
    # setup confirms it (see ensure_reachable). The retry must hold it too,
    # or the unit joins the previous site's Wi-Fi six minutes after boot.
    try:
        if wifi_reconfirm_required():
            return {"skipped": "card moved; setup must confirm the site network"}
    except Exception:
        logger.exception("card move check failed")
        return {"skipped": "card move check failed"}
    # Count again just before the hotspot goes down: a phone may have joined
    # while the saved profiles were being listed.
    clients = ap_clients(iface)
    if clients is None:
        return {"skipped": "could not read hotspot clients"}
    if clients > 0:
        return {"skipped": "a phone is on the hotspot"}
    joined: dict[str, Any] | None = None
    last_error = ""
    restored = False
    failure = "could not join"
    try:
        # Inside the try: a SIGTERM or an error from here on must still
        # bring the hotspot back.
        stop_ap(iface)
        _disarm_portal("joining")
        joined, last_error = _retry_profiles(profiles, iface, sleep, clock, wait_s)
    finally:
        # Also on an exception: the hotspot is down from here on, and the
        # timer's next run skips a unit whose hotspot is not up. SIGTERM is
        # held until the hotspot is back, so systemd's stop cannot cut the
        # restore short; the held signal is delivered right after.
        if joined is None:
            with _sigterm_held():
                if station_connected(iface):
                    _nmcli("device", "disconnect", iface)
                restored = _restore_ap(True, ap_ssid, ap_passphrase, iface, sleep)
                kind = (
                    "no_dhcp"
                    if last_error == "no address"
                    else (_classify_connect_error(last_error) or "no_association")
                )
                failure = _STATION_FAIL.get(kind, "could not join")
                _publish_station(
                    station=f"fail: {failure}",
                    hotspot="up" if restored else "down",
                    portal="up (80)" if restored else "down: hotspot down",
                )
    if joined is not None:
        return joined
    return {"ok": restored, "mode": "ap", "ap": restored, "station_failure": failure}


@contextlib.contextmanager
def _sigterm_held() -> Any:
    """Hold SIGTERM for the block, then deliver it.

    The handler is swapped rather than the signal masked: a mask covers one
    thread, and a process-directed signal can land on another. Python runs
    signal handlers on the main thread only, so off it this does nothing.
    """
    import threading

    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.getsignal(signal.SIGTERM)
    if previous is None:
        # Installed from C: Python cannot put it back, so leave it alone.
        yield
        return
    held: list[tuple[int, Any]] = []
    signal.signal(signal.SIGTERM, lambda signum, frame: held.append((signum, frame)))
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)
        if held:
            if callable(previous):
                previous(*held[0])
            elif previous == signal.SIG_DFL:
                os.kill(os.getpid(), signal.SIGTERM)


def _retry_profiles(
    profiles: list[dict[str, str]],
    iface: str,
    sleep: Any,
    clock: Any,
    wait_s: float,
) -> tuple[dict[str, Any] | None, str]:
    """Bring each saved profile up in turn, inside ``RETRY_BUDGET_S``.
    Returns the station result on a join, else None and the last error."""
    started = clock()
    per_profile = _RETRY_SSID_WAIT_S + JOIN_TIMEOUT_S + wait_s
    last_error = ""
    for i, prof in enumerate(profiles):
        # The first profile is always tried; a later one only if it fits.
        if i and clock() - started + per_profile > RETRY_BUDGET_S:
            break
        wait_for_ssid(prof["ssid"], iface, sleep=sleep, clock=clock, wait_s=_RETRY_SSID_WAIT_S)
        rc, out, err = _nmcli(
            "connection", "up", prof["name"], "ifname", iface, timeout=float(JOIN_TIMEOUT_S)
        )
        if rc != 0:
            last_error = (err or out or f"nmcli rc {rc}").strip()
            continue
        deadline = clock() + wait_s
        while True:
            snap = _station_ready(iface)
            if snap:
                ip = str(snap["usable"][0])
                _disarm_portal("station")
                _publish_station(
                    station=f"ok {ip}",
                    hotspot="station",
                    portal="down: station",
                    station_rssi=format_station_rssi(read_station_rssi(iface)),
                )
                logger.info("Saved network %s joined on retry at %s", prof["ssid"], ip)
                return {"ok": True, "mode": "station", "ip": ip}, ""
            if clock() >= deadline:
                last_error = "no address"
                break
            sleep(ADDRESS_POLL_S)
    return None, last_error


def saved_wifi(iface: str = DEFAULT_WIFI_IFACE) -> list[dict[str, str]]:
    """Saved Wi-Fi profiles as ``[{"name", "ssid"}]``, excluding the setup AP.
    The profile NAME and the network's SSID usually match (``nmcli device
    wifi connect`` names the profile after the SSID) but need not — a profile
    written into the golden image by hand can be called anything — so both
    are read."""
    listed = _list_saved_wifi(iface)
    return listed if listed is not None else []


def _list_saved_wifi(iface: str = DEFAULT_WIFI_IFACE) -> list[dict[str, str]] | None:
    """``saved_wifi``, but None when NetworkManager did not answer — which
    is not the same as "nothing saved"."""
    if netsim.is_virtual():
        return [{"name": s, "ssid": s} for s in netsim.saved_wifi()]
    rc, out, _ = _nmcli("-t", "-f", "NAME,TYPE", "connection", "show")
    if rc != 0:
        return None
    found: list[dict[str, str]] = []
    for line in out.splitlines():
        # nmcli -t escapes a ':' inside a field as '\:'; split on the last one.
        name, sep, ctype = line.rpartition(":")
        if not sep:
            continue
        name = name.replace("\\:", ":")
        if ctype != "802-11-wireless" or name == AP_CONNECTION_NAME:
            continue
        rc2, ssid, _ = _nmcli("-g", "802-11-wireless.ssid", "connection", "show", name)
        found.append({"name": name, "ssid": ssid if rc2 == 0 and ssid else name})
    return found


def forget_saved_wifi(keep: str | None, iface: str = DEFAULT_WIFI_IFACE) -> dict[str, Any]:
    """Delete every saved Wi-Fi profile whose SSID is not ``keep``.

    Called when setup finishes. ``keep`` is the network the unit is using now
    (the customer's), or None on an LTE or no-network site, where nothing is
    kept. Never touches the setup AP's profile — the AP fallback must still be
    able to raise it. Returns ``{"ok", "forgotten": [ssid…], "kept": ssid|None}``.
    """
    forgotten: list[str] = []
    failed: list[str] = []
    for prof in saved_wifi(iface):
        if keep and prof["ssid"] == keep:
            continue
        if netsim.is_virtual():
            ok = netsim.forget_wifi(prof["ssid"])
        else:
            rc, _, _ = _nmcli("connection", "delete", prof["name"])
            ok = rc == 0
        (forgotten if ok else failed).append(prof["ssid"])
    if forgotten:
        logger.info("Forgot saved Wi-Fi: %s (kept %s)", ", ".join(forgotten), keep or "none")
    if failed:
        logger.warning("Could not forget saved Wi-Fi: %s", ", ".join(failed))
    return {"ok": not failed, "forgotten": forgotten, "failed": failed, "kept": keep}


def _arm_portal() -> None:
    arm_portal()


def _disarm_portal(reason: str) -> None:
    disarm_portal(reason)


def _portal_after_join(result: dict[str, Any]) -> None:
    """A joined station closes port 80. A restored AP opens it again."""
    if result.get("connected"):
        _disarm_portal("station")
    elif result.get("ap_restored"):
        _arm_portal()
    else:
        _disarm_portal("hotspot down")


def _captive_rule(port: int, iface: str) -> list[str]:
    return [
        "-t",
        "nat",
        "PREROUTING",
        "-i",
        iface,
        "-s",
        AP_NETWORK,
        "-d",
        AP_ADDRESS,
        "-p",
        "tcp",
        "--dport",
        "80",
        "-j",
        "REDIRECT",
        "--to-ports",
        str(int(port)),
    ]


def remove_captive_redirect(port: int = 8080, iface: str = DEFAULT_WIFI_IFACE) -> dict[str, Any]:
    """Delete the legacy port-80 NAT redirect if it is installed.

    Idempotent. The portal listens on 192.168.4.1:80 itself; a REDIRECT to
    the Service Window port steals those packets and the phone is sent to
    ``:8080``. Missing iptables means there is no rule to delete.
    """
    if netsim.is_virtual():
        return {"ok": True, "virtual": True}
    rule = _captive_rule(port, iface)
    table, chain, match = rule[:2], rule[2], rule[3:]
    rc, _, err = _run(["iptables", *table, "-C", chain, *match])
    if rc == 127:
        return {"ok": True, "skipped": err or "iptables not found"}
    if rc != 0:
        return {"ok": True, "absent": True}
    rc, out, err = _run(["iptables", *table, "-D", chain, *match])
    if rc != 0:
        logger.warning("Captive NAT rule not removed: %s", err or out or f"rc {rc}")
        return {"ok": False, "error": err or out or f"iptables rc {rc}"}
    logger.info("Removed legacy captive NAT redirect to port %s", port)
    return {"ok": True, "removed": True}


def remove_legacy_captive_nat(port: int = 8080, iface: str = DEFAULT_WIFI_IFACE) -> dict[str, Any]:
    """Delete the NAT redirect for ``port`` and for the old default 8080."""
    ports = [port] if int(port) == 8080 else [int(port), 8080]
    results = [remove_captive_redirect(p, iface) for p in ports]
    failed = [r for r in results if not r.get("ok")]
    if failed:
        return failed[0]
    return {"ok": True, "results": results}


def ensure_captive_redirect(port: int = 8080, iface: str = DEFAULT_WIFI_IFACE) -> dict[str, Any]:
    """Used to install the NAT redirect. It now removes it.

    Callers from before the port-80 listener still invoke this at boot.
    Adding the rule back would hide the listener from the phone.
    """
    return remove_captive_redirect(port, iface)
