#!/usr/bin/env python3
"""The radio half of the commissioning simulator: nmcli, iw, systemctl and
friends, faked over a real network.

``commission.py`` links this file into ``<sim dir>/bin`` under each tool's
name and puts that directory first on the unit's ``PATH``. The real join code
(``utils.netctl``, ``utils.join``, ``utils.join_service``) then runs unchanged
and shells these "tools" exactly as it shells the real ones on a Pi.

What is fake is only what this kernel cannot give: 802.11. There is no
cfg80211 here, so no virtual radio. Association is modelled by moving the
unit's cable between two switches in the router's namespace:

* ``brap``: the hotspot cell. The phone is on it. hostapd "up" plugs the unit in.
* ``brlan``: the home network. The router (real dnsmasq DHCP and DNS, a real
  HTTPS server standing in for BlueSignal) is on it. A good join plugs the
  unit in, and a real DHCP client (busybox ``udhcpc``) asks the real router
  for an address, as NetworkManager would.

Everything above the radio is real: addresses, routes, DHCP and its delays,
DNS, TLS, the captive portal's sockets and the phone's HTTP requests.

The scene (networks, passwords, which band refuses) comes from
``<sim dir>/world.json``; the radio's state lives in ``<sim dir>/radio.json``.
Standard library only, so the unit's PATH needs no virtualenv. Nothing here
prints a password: a profile's password is compared and never echoed.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

IFACE = "wlan0"
ROUTER_NS = "bsr"
UNIT_PORT = "ua"
AP_BRIDGE = "brap"
LAN_BRIDGE = "brlan"

NOT_FOUND_UP = "Error: Connection activation failed: The Wi-Fi network could not be found."
SECRETS = "Error: Connection activation failed: Secrets were required, but not provided."
IP_TIMEOUT = (
    "Error: Connection activation failed: IP configuration could not be reserved "
    "(no available address, timeout, etc.)."
)
ACTIVATED = (
    "Connection successfully activated (D-Bus active path: "
    "/org/freedesktop/NetworkManager/ActiveConnection/7)"
)
HOSTAPD_DOWN = "Failed to connect to hostapd - wpactrl_open: No such file or directory"


def sim_dir() -> Path:
    return Path(os.environ["WQM1_SIM_DIR"])


def world() -> dict[str, Any]:
    return json.loads((sim_dir() / "world.json").read_text())


def _blank() -> dict[str, Any]:
    return {
        "managed": "yes",
        "mode": "off",
        "active": None,
        "bss": None,
        "profiles": {},
        "cache": [],
        "rescans": 0,
        "dhcp_pid": None,
        "ap_dnsmasq_pid": None,
    }


@contextlib.contextmanager
def radio() -> Iterator[dict[str, Any]]:
    """The radio state, locked for the length of the block, saved after it."""
    base = sim_dir()
    with open(base / "radio.lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = base / "radio.json"
        try:
            state = json.loads(path.read_text())
        except (OSError, ValueError):
            state = _blank()
        yield state
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state))
        os.chmod(tmp, 0o600)
        tmp.replace(path)


def log(line: str) -> None:
    with open(sim_dir() / "radio.log", "a") as f:
        f.write(f"{time.strftime('%H:%M:%S')} {line}\n")


def _ip(*args: str) -> None:
    subprocess.run(["ip", *args], capture_output=True, check=False)


def plug(bridge: str | None) -> None:
    """Move the unit's cable: into the hotspot cell, the home network, or out."""
    base = ["ip", "netns", "exec", ROUTER_NS, "ip", "link", "set", UNIT_PORT]
    if bridge is None:
        subprocess.run([*base, "nomaster"], capture_output=True, check=False)
        subprocess.run([*base, "down"], capture_output=True, check=False)
        return
    subprocess.run([*base, "master", bridge, "up"], capture_output=True, check=False)


def _kill(pid: int | None) -> None:
    if pid:
        with contextlib.suppress(OSError):
            os.kill(int(pid), signal.SIGTERM)


def _network(w: dict[str, Any], ssid: str) -> dict[str, Any] | None:
    for net in w.get("networks", []):
        if net["ssid"] == ssid and net.get("present", True):
            return net
    return None


def drop_station(state: dict[str, Any]) -> None:
    """What NetworkManager does on a deactivate: lease gone, link down."""
    _kill(state.get("dhcp_pid"))
    state["dhcp_pid"] = None
    if state["mode"] in ("station", "connecting"):
        plug(None)
        _ip("addr", "flush", "dev", IFACE)
        _ip("route", "flush", "dev", IFACE)
        state["mode"] = "off"
    state["active"] = None
    state["bss"] = None
    with contextlib.suppress(OSError):
        (sim_dir() / f"lease-{IFACE}.json").unlink()


# --- nmcli -----------------------------------------------------------------


def _split_opts(args: list[str]) -> tuple[list[str], list[str] | None, str | None, bool]:
    fields: list[str] | None = None
    getter: str | None = None
    terse = False
    rest: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-t", "--terse"):
            terse = True
        elif a in ("-f", "--fields") and i + 1 < len(args):
            fields = args[i + 1].split(",")
            i += 1
        elif a in ("-g", "--get-values") and i + 1 < len(args):
            getter = args[i + 1]
            terse = True
            i += 1
        elif a in ("-w", "--wait") and i + 1 < len(args):
            i += 1
        else:
            rest.append(a)
        i += 1
    return rest, fields, getter, terse


def _rows(rows: list[dict[str, str]], fields: list[str]) -> str:
    return "\n".join(":".join(r.get(f, "") for f in fields) for r in rows)


def _pairs(args: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for i in range(0, len(args) - 1, 2):
        out[args[i]] = args[i + 1]
    return out


def nmcli(args: list[str]) -> tuple[int, str, str]:
    rest, fields, getter, _terse = _split_opts(args)
    w = world()
    if rest[:2] == ["device", "status"] or rest[:1] == ["device"] and len(rest) == 1:
        with radio() as s:
            if s["managed"] == "no":
                st = "unmanaged"
            elif s["mode"] == "station":
                st = "connected"
            elif s["mode"] == "connecting":
                st = "connecting (configuring)"
            else:
                st = "disconnected"
            row = {"DEVICE": IFACE, "TYPE": "wifi", "STATE": st, "CONNECTION": s["active"] or "--"}
        return 0, _rows([row], fields or ["DEVICE", "TYPE", "STATE", "CONNECTION"]), ""
    if rest[:3] == ["device", "set", IFACE] and "managed" in rest:
        value = rest[rest.index("managed") + 1] if rest.index("managed") + 1 < len(rest) else "yes"
        with radio() as s:
            if value == "no":
                drop_station(s)
                s["cache"] = []
                s["rescans"] = 0
            s["managed"] = value
        return 0, "", ""
    if rest[:3] == ["device", "wifi", "rescan"]:
        time.sleep(float(w.get("scan_s", 2)))
        with radio() as s:
            if s["managed"] == "no":
                return 1, "", f"Error: Device '{IFACE}' is not managed."
            if s["rescans"] >= int(w.get("rescans_before_seen", 1)):
                s["cache"] = [n["ssid"] for n in w["networks"] if n.get("present", True)]
            s["rescans"] += 1
        return 0, "", ""
    if rest[:3] == ["device", "wifi", "list"]:
        with radio() as s:
            seen = set(s["cache"]) if s["managed"] != "no" else set()
        rows = []
        for net in w["networks"]:
            if net["ssid"] in seen and net.get("present", True):
                for b in net["bss"]:
                    rows.append(
                        {
                            "SSID": net["ssid"],
                            "SIGNAL": str(b["signal"]),
                            "SECURITY": "WPA2" if net.get("psk") else "--",
                            "FREQ": f"{b['freq']} MHz",
                        }
                    )
        return 0, _rows(rows, fields or ["SSID", "SIGNAL", "SECURITY", "FREQ"]), ""
    if rest[:2] == ["device", "disconnect"]:
        with radio() as s:
            drop_station(s)
        return 0, f"Device '{IFACE}' successfully disconnected.", ""
    if rest[:2] == ["device", "show"]:
        lease = _lease(IFACE)
        if getter == "IP4.DNS":
            return 0, (lease or {}).get("dns", ""), ""
        return 0, "", ""
    if rest[:2] == ["connection", "add"]:
        p = _pairs(rest[2:])
        name = p.get("con-name", "")
        with radio() as s:
            s["profiles"][name] = {
                "ssid": p.get("ssid", ""),
                "psk": p.get("wifi-sec.psk"),
                "band": p.get("802-11-wireless.band"),
                "hidden": p.get("802-11-wireless.hidden") in ("yes", "true"),
            }
        log(f"profile {name} added (band lock: {p.get('802-11-wireless.band') or 'none'})")
        return 0, f"Connection '{name}' successfully added.", ""
    if rest[:2] == ["connection", "up"] and len(rest) > 2:
        return _connection_up(rest[2], w)
    if rest[:2] in (["connection", "down"], ["connection", "delete"]) and len(rest) > 2:
        name = rest[2]
        with radio() as s:
            if s["active"] == name or (s["mode"] == "connecting" and rest[1] == "delete"):
                drop_station(s)
            if rest[1] == "delete":
                s["profiles"].pop(name, None)
        return 0, "", ""
    if rest[:2] == ["connection", "modify"] and len(rest) > 2:
        p = _pairs(rest[3:])
        new = p.get("connection.id")
        with radio() as s:
            if new and rest[2] in s["profiles"]:
                s["profiles"][new] = s["profiles"].pop(rest[2])
                if s["active"] == rest[2]:
                    s["active"] = new
        return 0, "", ""
    if rest[:1] == ["connection"]:
        tail = rest[1:]
        with radio() as s:
            if getter and "show" in tail and tail[-1] != "show":
                prof = s["profiles"].get(tail[-1])
                if prof is None:
                    return 10, "", f"Error: {tail[-1]} - no such connection profile."
                return 0, prof["ssid"] if getter == "802-11-wireless.ssid" else "", ""
            names = list(s["profiles"])
            if "--active" in tail:
                names = [n for n in names if n == s["active"]]
            rows = [
                {
                    "NAME": n,
                    "UUID": f"sim-{i}",
                    "TYPE": "802-11-wireless",
                    "DEVICE": IFACE if n == s["active"] else "",
                }
                for i, n in enumerate(names)
            ]
        return 0, _rows(rows, fields or ["NAME", "UUID", "TYPE", "DEVICE"]), ""
    return 0, "", ""


def _lease(iface: str) -> dict[str, Any] | None:
    try:
        return json.loads((sim_dir() / f"lease-{iface}.json").read_text())
    except (OSError, ValueError):
        return None


def _connection_up(name: str, w: dict[str, Any]) -> tuple[int, str, str]:
    with radio() as s:
        prof = s["profiles"].get(name)
        if prof is None:
            return 10, "", f"Error: unknown connection '{name}'."
        drop_station(s)
        in_cache = prof["ssid"] in s["cache"]
    net = _network(w, prof["ssid"])
    if net is None or not (in_cache or prof["hidden"]):
        time.sleep(1.0 if net is None and not prof["hidden"] else 5.0)
        log(f"up {name}: network not found")
        return 10, "", NOT_FOUND_UP
    locked = prof["band"] == "bg"
    dual = w.get("radio") == "dual"
    if dual and not locked and net.get("refuse_5ghz"):
        # The radio picks the 5 GHz side and never gets on. nmcli waits until
        # the caller's timeout kills it, as on the bench.
        with radio() as s:
            s["mode"] = "connecting"
            s["active"] = name
        log(f"up {name}: associating on 5 GHz, no answer")
        time.sleep(3600)
        return 4, "", NOT_FOUND_UP
    if (prof["psk"] or None) != (net.get("psk") or None):
        time.sleep(float(w.get("handshake_fail_s", 6)))
        log(f"up {name}: 4-way handshake failed")
        return 4, "", SECRETS
    if dual and not locked:
        # The radio's own choice: the 5 GHz side first.
        candidates = sorted(net["bss"], key=lambda b: b["freq"] < 3000)
    else:
        candidates = [b for b in net["bss"] if b["freq"] < 3000]
    if not candidates:
        time.sleep(5.0)
        return 10, "", NOT_FOUND_UP
    time.sleep(float(w.get("assoc_s", 3)))
    bss = candidates[0]
    with radio() as s:
        s["mode"] = "station"
        s["active"] = name
        s["bss"] = bss
        plug(LAN_BRIDGE)
        hook = str(sim_dir() / "bin" / "sim-dhcp-hook")
        # Detached, like NetworkManager's own DHCP client: it keeps asking
        # after a caller's nmcli has been killed by its timeout.
        proc = subprocess.Popen(
            ["busybox", "udhcpc", "-i", IFACE, "-f", "-q", "-t", "15", "-T", "3", "-s", hook],
            stdout=subprocess.DEVNULL,
            stderr=open(sim_dir() / "udhcpc.log", "a"),  # noqa: SIM115
            start_new_session=True,
            env=os.environ.copy(),
        )
        s["dhcp_pid"] = proc.pid
    log(f"up {name}: associated on {bss['freq']} MHz, asking for an address")
    deadline = time.monotonic() + float(w.get("ip_timeout_s", 45))
    while time.monotonic() < deadline:
        if _lease(IFACE):
            log(f"up {name}: address {_lease(IFACE)['ip']}")
            return 0, ACTIVATED, ""
        time.sleep(0.5)
    log(f"up {name}: no address in time")
    return 4, "", IP_TIMEOUT


# --- iw, systemctl, hostapd_cli and the rest -------------------------------


def iw(args: list[str]) -> tuple[int, str, str]:
    w = world()
    if args[:3] == ["dev", IFACE, "link"]:
        with radio() as s:
            if s["mode"] != "station" or not s["bss"]:
                return 0, "Not connected.", ""
            prof = s["profiles"].get(s["active"] or "", {})
            b = s["bss"]
        ssid = prof.get("ssid", "")
        return 0, f"Connected to {b['bssid']} (on {IFACE})\n\tSSID: {ssid}\n\tfreq: {b['freq']}", ""
    if args[:3] == ["dev", IFACE, "scan"]:
        time.sleep(float(w.get("scan_s", 2)))
        blocks = []
        for net in w["networks"]:
            if not net.get("present", True):
                continue
            for b in net["bss"]:
                rsn = (
                    "\tRSN:\t * Version: 1\n\t\t * Authentication suites: PSK\n"
                    if net.get("psk")
                    else ""
                )
                blocks.append(
                    f"BSS {b['bssid']}(on {IFACE})\n\tfreq: {b['freq']}\n"
                    f"\tsignal: {b['dbm']:.2f} dBm\n\tSSID: {net['ssid']}\n{rsn}"
                )
        return 0, "".join(blocks), ""
    if args[:2] == ["reg", "get"]:
        return 0, "global\ncountry US: DFS-FCC", ""
    return 0, "", ""


def _unit(args: list[str]) -> str:
    return next((a for a in args[1:] if a.endswith(".service")), "")


def systemctl(args: list[str]) -> tuple[int, str, str]:
    action = next((a for a in args if not a.startswith("-")), "")
    unit = _unit(args)
    if action in ("start", "restart") and unit == "wqm1-setup-hostapd.service":
        with radio() as s:
            drop_station(s)
            s["mode"] = "ap"
            plug(AP_BRIDGE)
        log("hostapd: BlueSignal Setup beaconing")
        return 0, "", ""
    if action == "stop" and unit == "wqm1-setup-hostapd.service":
        with radio() as s:
            if s["mode"] == "ap":
                s["mode"] = "off"
                plug(None)
        log("hostapd: stopped")
        return 0, "", ""
    if action in ("start", "restart") and unit == "wqm1-setup-dnsmasq.service":
        with radio() as s:
            _kill(s.get("ap_dnsmasq_pid"))
            base = sim_dir()
            proc = subprocess.Popen(
                [
                    "dnsmasq",
                    "--keep-in-foreground",
                    f"--conf-file={os.environ['WQM1_DNSMASQ_CONF']}",
                    f"--dhcp-leasefile={base / 'ap.leases'}",
                    f"--pid-file={base / 'ap-dnsmasq.pid'}",
                    "--log-facility=" + str(base / "ap-dnsmasq.log"),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            time.sleep(0.4)
            if proc.poll() is not None:
                err = (proc.stderr.read() if proc.stderr else b"").decode(errors="replace")
                return 1, "", err.strip() or "dnsmasq failed"
            s["ap_dnsmasq_pid"] = proc.pid
        return 0, "", ""
    if action == "stop" and unit == "wqm1-setup-dnsmasq.service":
        with radio() as s:
            _kill(s.get("ap_dnsmasq_pid"))
            s["ap_dnsmasq_pid"] = None
        return 0, "", ""
    if action == "is-active":
        with radio() as s:
            up = (unit == "wqm1-setup-hostapd.service" and s["mode"] == "ap") or (
                unit == "wqm1-setup-dnsmasq.service" and bool(s.get("ap_dnsmasq_pid"))
            )
        return (0, "active", "") if up else (3, "inactive", "")
    return 0, "", ""


def hostapd_cli(args: list[str]) -> tuple[int, str, str]:
    with radio() as s:
        up = s["mode"] == "ap"
    if not up:
        return 1, "", HOSTAPD_DOWN
    if args and args[-1] == "status":
        return 0, "state=ENABLED\nssid[0]=BlueSignal Setup\nchannel=6", ""
    return 0, "", ""


def iwgetid(args: list[str]) -> tuple[int, str, str]:
    with radio() as s:
        prof = s["profiles"].get(s["active"] or "") if s["mode"] == "station" else None
    return (0, prof["ssid"], "") if prof else (255, "", "")


def timedatectl(args: list[str]) -> tuple[int, str, str]:
    if "--value" in args:
        return 0, "yes", ""
    return 0, "NTPSynchronized=yes", ""


def dhcp_hook(args: list[str]) -> tuple[int, str, str]:
    """``udhcpc -s`` script: what NetworkManager does with a lease."""
    event = args[0] if args else ""
    env = dict(os.environ)  # udhcpc passes the lease in lower-case variables
    iface = env.get("interface", IFACE)
    lease = sim_dir() / f"lease-{iface}.json"
    if event == "deconfig":
        _ip("addr", "flush", "dev", iface)
        with contextlib.suppress(OSError):
            lease.unlink()
    elif event in ("bound", "renew"):
        ip = env.get("ip", "")
        mask = env.get("mask") or "24"
        router = (env.get("router") or "").split()
        dns = (env.get("dns") or "").split()
        _ip("addr", "flush", "dev", iface)
        _ip("addr", "add", f"{ip}/{mask}", "dev", iface)
        if router:
            _ip("route", "replace", "default", "via", router[0], "dev", iface)
        lease.write_text(json.dumps({"ip": ip, "router": router[:1], "dns": " ".join(dns)}))
    return 0, "", ""


TOOLS = {
    "nmcli": nmcli,
    "iw": iw,
    "systemctl": systemctl,
    "hostapd_cli": hostapd_cli,
    "iwgetid": iwgetid,
    "timedatectl": timedatectl,
    "sim-dhcp-hook": dhcp_hook,
}


def main(argv: list[str]) -> int:
    name = Path(argv[0]).name
    if name == "sudo":
        os.execvp(argv[1], argv[1:])
    tool = TOOLS.get(name)
    if tool is None:
        # rfkill, iptables and anything else the join stack touches: done.
        return 0
    try:
        rc, out, err = tool(argv[1:])
    except Exception as e:  # noqa: BLE001 - a fake must answer like a tool
        log(f"{name} {argv[1:3]} crashed: {e!r}")
        return 1
    if name == "nmcli" or name in ("iw", "systemctl"):
        log(f"{name} {' '.join(_safe(argv[1:]))} -> {rc}")
    if out:
        sys.stdout.write(out + ("" if out.endswith("\n") else "\n"))
    if err:
        sys.stderr.write(err + "\n")
    return rc


def _safe(args: list[str]) -> list[str]:
    """argv for the log with any password replaced."""
    out = list(args)
    for i, a in enumerate(out[:-1]):
        if a in ("wifi-sec.psk", "password"):
            out[i + 1] = "<hidden>"
    return out


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
