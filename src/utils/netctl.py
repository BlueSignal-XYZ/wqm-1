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

import errno
import hashlib
import ipaddress
import logging
import os
import re
import shutil
import socket
import subprocess  # nosec B404 - fixed argv, no shell, resolved binaries only
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from utils import netsim
from utils.captive_portal import (
    arm_portal,
    disarm_portal,
    ensure_dnsmasq_conf,
    wait_until_reachable,
)

logger = logging.getLogger("wqm1.netctl")

AP_CONNECTION_NAME = "wqm1-setup-ap"
AP_GRACE_S = 45
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
ADDRESS_POLL_S = 2.0
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
    "no_association": (
        "The unit could not join that network. Check the password and that the router "
        "is on, then try again."
    ),
    "invalid_ssid": "That network name cannot be used. Pick one from the list.",
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
    raising an open network — an open AP into a PIN-gated page is still an
    open AP.

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


def _dbm_to_percent(dbm: float) -> int:
    """Map dBm onto the 0–100 scale the setup page already shows."""
    return max(0, min(100, int(round((dbm + 100.0) * 2))))


def parse_iw_scan(text: str) -> list[dict[str, Any]]:
    """SSIDs from ``iw dev wlan0 scan``, strongest first.

    NetworkManager cannot scan while ``wlan0`` is unmanaged for hostapd.
    ``iw`` can. A BSS with an RSN or WPA information element is secured.
    """
    found: dict[str, dict[str, Any]] = {}
    ssid: str | None = None
    signal = 0
    secured = False

    def flush() -> None:
        nonlocal ssid, signal, secured
        if ssid:
            prev = found.get(ssid)
            if prev is None or int(prev["signal"]) < signal:
                found[ssid] = {"ssid": ssid, "signal": signal, "secured": secured}
        ssid = None
        signal = 0
        secured = False

    for raw in (text or "").splitlines():
        if raw.startswith("BSS "):
            flush()
            continue
        line = raw.strip()
        if line.startswith("SSID:"):
            ssid = line.split(":", 1)[1].strip() or None
        elif line.startswith("signal:"):
            parts = line.split()
            if len(parts) >= 2:
                try:
                    signal = _dbm_to_percent(float(parts[1]))
                except ValueError:
                    signal = 0
        elif line.startswith("RSN:") or line.startswith("WPA:"):
            secured = True
    flush()
    return sorted(found.values(), key=lambda item: -int(item["signal"]))


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
        return netsim.scan_networks()
    if ap_active(iface) or iface_state(iface) == "unmanaged":
        return _scan_iw(iface)
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
) -> dict[str, Any]:
    """Join ``ssid`` and report success only when the unit is actually on it.

    Success requires all three, polled until ``address_wait_s`` (about a
    minute): ``wlan0`` is associated to this SSID, it has a usable IPv4
    address (a DHCP or static lease — not link-local ``169.254.x``), and
    that network's gateway or a DNS server answers.

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
            _publish_station(
                station=f"ok {result.get('ip') or ''}".strip(),
                hotspot="station",
                portal="down: station",
            )
        else:
            short = _STATION_FAIL.get(str(result.get("reason") or ""), "could not join")
            _publish_station(
                station=f"fail: {short}",
                hotspot="up" if result.get("ap_restored") else "down",
                portal="up (80)" if result.get("ap_restored") else "down: hotspot down",
            )
        return result
    deadline = clock() + address_wait_s
    had_ap = ap_active(iface)
    if had_ap:
        stop_ap(iface)
        # The address is about to disappear. Close port 80 before the join
        # so a phone that is still associated is not kept on our portal,
        # and a successful join cannot leave the listener up (#136).
        _disarm_portal("joining")
    argv = ["device", "wifi", "connect", ssid, "ifname", iface]
    if password:
        argv += ["password", password]
    remaining = max(1.0, deadline - clock())
    rc, out, err = _nmcli(*argv, timeout=min(float(JOIN_TIMEOUT_S), remaining))
    nm_text = _redact_secret(err or out or (f"nmcli rc {rc}" if rc else ""), password)
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
            ip = str(usable[0])
            logger.info("Joined %s at %s", ssid, ip)
            _disarm_portal("station")
            _publish_station(station=f"ok {ip}", hotspot="station", portal="down: station")
            return _join_result(
                ok=True,
                connected=True,
                ap_restored=False,
                error=None,
                ip=ip,
            )
        if clock() >= deadline:
            kind = _failure_kind(ssid, snap, nm_text)
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
) -> dict[str, Any]:
    """Record the reason, then raise the hotspot. The hook runs first so the
    setup page has the sentence before the phone can rejoin."""
    message = _join_message(kind, link_local)
    short = _STATION_FAIL.get(kind, "could not join")
    logger.warning("Join %s failed (%s): %s", ssid, kind, nm_text or message)
    # Drop the failed profile so NM does not keep retrying a bad password
    # and blocking the AP from coming back.
    _nmcli("connection", "delete", ssid)
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
    """
    prepare_wifi_radio(iface)
    radio_deadline = clock() + radio_wait_s
    unusable_station = False

    def _take_station() -> dict[str, Any] | None:
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
    grace = grace_s if saved_wifi(iface) else 0.0
    started = clock()
    while clock() - started < grace:
        taken = _take_station()
        if taken:
            return taken
        _note_unusable()
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
    return result


def saved_wifi(iface: str = DEFAULT_WIFI_IFACE) -> list[dict[str, str]]:
    """Saved Wi-Fi profiles as ``[{"name", "ssid"}]``, excluding the setup AP.
    The profile NAME and the network's SSID usually match (``nmcli device
    wifi connect`` names the profile after the SSID) but need not — a profile
    written into the golden image by hand can be called anything — so both
    are read."""
    if netsim.is_virtual():
        return [{"name": s, "ssid": s} for s in netsim.saved_wifi()]
    rc, out, _ = _nmcli("-t", "-f", "NAME,TYPE", "connection", "show")
    if rc != 0:
        return []
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
