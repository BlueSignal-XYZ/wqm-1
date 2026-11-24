#!/usr/bin/env python3
"""End-to-end commissioning simulator: a phone, a WQM-1 and a home router,
each in its own network namespace, running the real setup flow.

    sudo python3 tools/sim/commission.py              # every scenario
    sudo python3 tools/sim/commission.py happy_path   # one, by name
    sudo python3 tools/sim/commission.py --list

Linux, root, a throwaway machine or container: it creates namespaces, a
user (``wqm1sim``) and bridges, and the join stack writes a few of its fixed
paths. Needs ``ip``, ``dnsmasq``, ``busybox`` (udhcpc), ``openssl``, ``curl``
and the repo's Python dependencies.

What is real and what is faked is in ``tools/sim/README.md``. In one line:
the radio is faked (``fake_radio.py``), everything from the address up is
real, and the code under test (``utils.netctl``, ``utils.join``,
``utils.join_service``, ``utils.captive_portal``) is the shipped code,
unpatched apart from the two seams in ``unit.py``.

No real network is touched: the unit's only route is the simulated router,
whose DNS answers BlueSignal's API name with itself and a simulated CA the
unit is told to trust. The Wi-Fi passwords are made-up values for networks
that exist only here, and none is printed.
"""

from __future__ import annotations

import argparse
import contextlib
import http.server
import json
import os
import shutil
import signal
import socketserver
import ssl
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
ROUTER, UNIT, PHONE = "bsr", "bsu", "bsp"
LAN_GW = "192.168.50.1"
HOTSPOT = "192.168.4.1"
SIM_USER = "wqm1sim"
FAKE_TOOLS = (
    "nmcli",
    "iw",
    "systemctl",
    "hostapd_cli",
    "iwgetid",
    "timedatectl",
    "rfkill",
    "iptables",
    "sudo",
    "sim-dhcp-hook",
)
# Made-up credentials for a network that exists only in this simulator.
HOME_SSID = "PondHouse-Sim"
HOME_PSK = "sim-pass-1234"  # nosec B105 - simulated network, not a credential
WRONG_PSK = "sim-pass-9999"  # nosec B105 - deliberately wrong, simulated


def api_host() -> str:
    """BlueSignal's API host, from the shipped default setting."""
    sys.path.insert(0, str(REPO / "src"))
    from utils.config import Settings

    return urlparse(Settings().cloud_api_base).hostname or ""


@dataclass
class Scenario:
    name: str
    title: str
    expect: str  # "joined" or the failure reason the page shows
    password: str = HOME_PSK
    radio: str = "2.4"
    dhcp_delay_s: int = 0
    dhcp: bool = True
    https: bool = True
    probe: str = "204"  # what the router's network answers a captive check
    refuse_5ghz: bool = False
    gone_after_list: bool = False
    retry_password: str | None = None
    answer_within_s: float = 75  # an installer is told about a minute
    rescans_before_seen: int = 1  # a just-released radio's list fills slowly


SCENARIOS = [
    Scenario("happy_path", "Hotspot, pick Wi-Fi, connect", "joined"),
    Scenario(
        "wrong_password_then_retry",
        "Wrong password, reason shown, retry with the right one",
        "wrong_password",
        password=WRONG_PSK,
        retry_password=HOME_PSK,
    ),
    Scenario(
        "slow_address",
        "Slow scan, then DHCP 33 s after association: the lease lands after the join budget",
        "joined",
        rescans_before_seen=4,
        dhcp_delay_s=33,
    ),
    Scenario(
        "network_gone",
        "Router switched off after the list was shown",
        "not_found",
        gone_after_list=True,
    ),
    Scenario(
        "dual_band_5ghz_refuses",
        "One SSID on 2.4 and 5 GHz, Pi 4 radio, 5 GHz never answers",
        "joined",
        radio="dual",
        refuse_5ghz=True,
    ),
    Scenario("no_dhcp", "Router hands out no address", "no_dhcp", dhcp=False),
    Scenario(
        "no_bluesignal",
        "Internet up, BlueSignal unreachable (held 180 s for device approval, spec D5)",
        "no_bluesignal",
        https=False,
        answer_within_s=215,
    ),
    Scenario(
        "captive_sign_in", "Guest network with a sign-in page", "captive_sign_in", probe="portal"
    ),
]


# --- plumbing --------------------------------------------------------------


def sh(
    *argv: str, ns: str | None = None, check: bool = True, timeout: float = 30, **kw: Any
) -> str:
    cmd = list(argv) if ns is None else ["ip", "netns", "exec", ns, *argv]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False, **kw)
    if check and proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:8])}: rc {proc.returncode}: {proc.stderr.strip()}")
    return proc.stdout


def preflight() -> None:
    if os.geteuid() != 0:
        raise SystemExit("Run as root: it creates network namespaces.")
    missing = [
        t for t in ("ip", "dnsmasq", "busybox", "openssl", "curl", "setpriv") if not shutil.which(t)
    ]
    if missing:
        raise SystemExit(f"Missing tools: {', '.join(missing)}")


def teardown() -> None:
    for ns in (ROUTER, UNIT, PHONE):
        for pid in sh("ip", "netns", "pids", ns, check=False).split():
            with contextlib.suppress(OSError):
                os.kill(int(pid), signal.SIGKILL)
        sh("ip", "netns", "del", ns, check=False)


def build_network(sim: Path) -> None:
    teardown()
    time.sleep(0.3)
    for ns in (ROUTER, UNIT, PHONE):
        sh("ip", "netns", "add", ns)
        sh("ip", "link", "set", "lo", "up", ns=ns)
        etc = Path("/etc/netns") / ns
        etc.mkdir(parents=True, exist_ok=True)
    (Path("/etc/netns") / UNIT / "resolv.conf").write_text(f"nameserver {LAN_GW}\n")
    (Path("/etc/netns") / PHONE / "resolv.conf").write_text(f"nameserver {HOTSPOT}\n")
    (Path("/etc/netns") / ROUTER / "resolv.conf").write_text("nameserver 127.0.0.1\n")
    # The two cells: the hotspot (phone side) and the home network (router).
    for br in ("brap", "brlan"):
        sh("ip", "link", "add", br, "type", "bridge", "forward_delay", "0", ns=ROUTER)
        sh("ip", "link", "set", br, "up", ns=ROUTER)
    sh("ip", "addr", "add", f"{LAN_GW}/24", "dev", "brlan", ns=ROUTER)
    # The unit's radio is a cable the fake tools move between the cells.
    sh(
        "ip",
        "link",
        "add",
        "ua",
        "netns",
        ROUTER,
        "type",
        "veth",
        "peer",
        "name",
        "wlan0",
        "netns",
        UNIT,
    )
    sh(
        "ip",
        "link",
        "add",
        "pa",
        "netns",
        ROUTER,
        "type",
        "veth",
        "peer",
        "name",
        "p0",
        "netns",
        PHONE,
    )
    sh("ip", "link", "set", "pa", "master", "brap", "up", ns=ROUTER)
    sh("ip", "link", "set", "p0", "up", ns=PHONE)
    sh("ip", "link", "set", "wlan0", "up", ns=UNIT)
    # The portal binds port 80 as an ordinary user, as its unit does with a capability.
    sh("sysctl", "-qw", "net.ipv4.ip_unprivileged_port_start=0", ns=UNIT)


def make_certs(sim: Path, host: str) -> None:
    pki = sim / "pki"
    pki.mkdir(exist_ok=True)
    ext = pki / "server.ext"
    ext.write_text(
        f"subjectAltName=DNS:{host}\nbasicConstraints=critical,CA:FALSE\n"
        "keyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n"
        "subjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid\n"
    )
    o = ["openssl"]
    ca_ext = ["-addext", "basicConstraints=critical,CA:TRUE"]
    ca_ext += ["-addext", "keyUsage=critical,keyCertSign,cRLSign"]
    sh(*o, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
       "-subj", "/CN=WQM-1 simulator CA", "-keyout", str(pki / "ca.key"),
       "-out", str(pki / "ca.pem"), *ca_ext)  # fmt: skip
    sh(*o, "req", "-newkey", "rsa:2048", "-nodes", "-subj", f"/CN={host}",
       "-keyout", str(pki / "server.key"), "-out", str(pki / "server.csr"))  # fmt: skip
    sh(*o, "x509", "-req", "-in", str(pki / "server.csr"), "-CA", str(pki / "ca.pem"),
       "-CAkey", str(pki / "ca.key"), "-CAcreateserial", "-days", "2",
       "-extfile", str(ext), "-out", str(pki / "server.pem"))  # fmt: skip


# --- the router's services --------------------------------------------------


class _Quiet(http.server.BaseHTTPRequestHandler):
    sim: Path = Path()

    def log_message(self, fmt: str, *args: Any) -> None:
        with open(self.sim / "router-http.log", "a") as f:
            f.write(
                f"{self.client_address[0]} {self.command} {self.headers.get('Host')}{self.path}\n"
            )

    def _answer(self) -> None:
        mode = json.loads((self.sim / "router.json").read_text())
        if self.server.server_address[1] == 80:
            if mode["probe"] == "portal":
                self.send_response(302)
                self.send_header("Location", "http://login.guest-wifi.test/")
                self.end_headers()
                return
            self.send_response(204)
            self.end_headers()
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = do_HEAD = do_POST = _answer  # noqa: N815


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    tls: ssl.SSLContext | None = None
    sim: Path = Path()

    def get_request(self) -> tuple[Any, Any]:
        conn, addr = self.socket.accept()
        if self.tls is None:
            return conn, addr
        mode = json.loads((self.sim / "router.json").read_text())
        if not mode["https"]:
            conn.close()
            raise OSError("BlueSignal is unreachable in this scenario")
        return self.tls.wrap_socket(conn, server_side=True), addr


def serve_router(sim: Path) -> int:
    """Run inside the router namespace: the captive check and BlueSignal."""
    _Quiet.sim = sim
    plain = _Server((LAN_GW, 80), _Quiet)
    secure = _Server((LAN_GW, 443), _Quiet)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(sim / "pki" / "server.pem", sim / "pki" / "server.key")
    secure.tls = ctx
    plain.sim = secure.sim = sim
    threading.Thread(target=plain.serve_forever, daemon=True).start()
    secure.serve_forever()
    return 0


def _dhcp_server(sim: Path) -> list[str]:
    return [
        "ip", "netns", "exec", ROUTER, "dnsmasq", "--keep-in-foreground", "--port=0",
        "--interface=brlan", "--bind-interfaces", "--log-dhcp",
        f"--log-facility={sim / 'router-dhcp.log'}",
        "--dhcp-range=192.168.50.100,192.168.50.150,255.255.255.0,12h",
        f"--dhcp-option=3,{LAN_GW}", f"--dhcp-option=6,{LAN_GW}",
        f"--dhcp-leasefile={sim / 'router.leases'}", "--dhcp-authoritative",
        f"--pid-file={sim / 'router-dhcp.pid'}",
    ]  # fmt: skip


def _late_dhcp(sim: Path, delay_s: float, procs: list[subprocess.Popen[bytes]]) -> None:
    """A router that answers DHCP ``delay_s`` after the unit associates.

    The unit's client keeps asking every 3 s, so the first answer lands
    within 3 s of the server starting. (dnsmasq's own ``--dhcp-reply-delay``
    holds its single thread, so it also stalls the REQUEST that follows and
    models a broken router rather than a slow one.)
    """
    world = (sim / "world.json").read_text()
    end = time.monotonic() + 300
    while radio_state(sim).get("mode") != "station":
        if time.monotonic() > end:
            return
        time.sleep(0.2)
    time.sleep(delay_s)
    with contextlib.suppress(OSError):
        if (sim / "world.json").read_text() != world:
            return  # the scenario is over
    procs.append(subprocess.Popen(_dhcp_server(sim), start_new_session=True))


def start_router(sim: Path, sc: Scenario, host: str) -> list[subprocess.Popen[bytes]]:
    (sim / "router.json").write_text(json.dumps({"https": sc.https, "probe": sc.probe}))
    dns = [
        "ip", "netns", "exec", ROUTER, "dnsmasq", "--keep-in-foreground",
        "--interface=brlan", "--bind-interfaces", "--no-resolv", "--no-hosts",
        f"--address=/{host}/{LAN_GW}",
        f"--address=/connectivitycheck.gstatic.com/{LAN_GW}",
        f"--log-facility={sim / 'router-dns.log'}",
        f"--pid-file={sim / 'router-dns.pid'}",
    ]  # fmt: skip
    procs = [
        subprocess.Popen(dns, start_new_session=True),
        subprocess.Popen(
            ["ip", "netns", "exec", ROUTER, sys.executable, __file__, "--serve-router", str(sim)],
            start_new_session=True,
        ),
    ]
    if sc.dhcp and not sc.dhcp_delay_s:
        procs.append(subprocess.Popen(_dhcp_server(sim), start_new_session=True))
    elif sc.dhcp:
        threading.Thread(target=_late_dhcp, args=(sim, sc.dhcp_delay_s, procs), daemon=True).start()
    time.sleep(0.8)
    return procs


# --- the unit ----------------------------------------------------------------


def unit_env(sim: Path, sc: Scenario) -> dict[str, str]:
    env = {
        "PATH": f"{sim / 'bin'}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "PYTHONPATH": str(REPO / "src"),
        "PYTHONUNBUFFERED": "1",
        "HOME": str(sim),
        "WQM1_SIM_DIR": str(sim),
        "WQM1_SIM_RADIO": sc.radio,
        "BLUESIGNAL_STATE_DIR": str(sim / "state"),
        "BLUESIGNAL_CONFIG": str(sim / "config.yaml"),
        "WQM1_HOSTAPD_CONF": str(sim / "run" / "hostapd.conf"),
        "WQM1_DNSMASQ_CONF": str(sim / "run" / "dnsmasq-ap.conf"),
        "WQM1_PORTAL_MODE_FILE": str(sim / "run" / "portal-mode"),
        "WQM1_WIFI_LOCK": str(sim / "run" / "wifi.lock"),
        "WQM1_JOIN_SOCKET": str(sim / "run" / "wifi-join.sock"),
        "WQM1_SCAN_CACHE": str(sim / "run" / "scan.json"),
        "WQM1_HOTSPOT_GUARD_STATE": str(sim / "run" / "hotspot-guard.json"),
        "SSL_CERT_FILE": str(sim / "pki" / "ca.pem"),
    }
    return env


def world_for(sc: Scenario) -> dict[str, Any]:
    return {
        "radio": sc.radio,
        "scan_s": 2,
        "assoc_s": 3,
        "handshake_fail_s": 6,
        "rescans_before_seen": sc.rescans_before_seen,
        "ip_timeout_s": 45,
        "networks": [
            {
                "ssid": HOME_SSID,
                "psk": HOME_PSK,
                "present": True,
                "refuse_5ghz": sc.refuse_5ghz,
                "bss": [
                    {"bssid": "a0:55:1f:00:00:01", "freq": 2437, "signal": 74, "dbm": -48},
                    {"bssid": "a0:55:1f:00:00:02", "freq": 5180, "signal": 81, "dbm": -42},
                ],
            },
            {
                "ssid": "Neighbor-Sim",
                "psk": "not-ours-0000",  # nosec B105 - simulated network
                "present": True,
                "bss": [{"bssid": "c4:41:1e:00:00:09", "freq": 2462, "signal": 38, "dbm": -77}],
            },
        ],
    }


def prepare_unit(sim: Path, sc: Scenario) -> None:
    for sub in ("state", "run", "systemd", "bin"):
        path = sim / sub
        shutil.rmtree(path, ignore_errors=True)
        path.mkdir(parents=True)
    for name in FAKE_TOOLS:
        (sim / "bin" / name).symlink_to(HERE / "fake_radio.py")
    for stale in sim.glob("*.json"):
        stale.unlink()
    for stale in sim.glob("*.log"):
        stale.unlink()
    for stale in ("ap.leases", "router.leases"):
        with contextlib.suppress(OSError):
            (sim / stale).unlink()
    (sim / "world.json").write_text(json.dumps(world_for(sc)))
    os.chmod(sim / "world.json", 0o644)
    if sh("id", "-u", SIM_USER, check=False).strip() == "":
        sh("useradd", "--system", "--no-create-home", "--shell", "/usr/sbin/nologin", SIM_USER)
    # setup.sh hands the state directory to the install user; the join
    # service trusts that user's socket requests.
    shutil.chown(sim / "state", SIM_USER, SIM_USER)
    os.chmod(sim, 0o755)
    os.chmod(sim / "run", 0o755)


def start_unit(sim: Path, sc: Scenario) -> list[subprocess.Popen[bytes]]:
    env = unit_env(sim, sc)
    py = sys.executable
    unit = str(HERE / "unit.py")
    logs = sim / "unit.log"
    out = open(logs, "a")  # noqa: SIM115 - lives as long as the processes
    join = subprocess.Popen(
        ["ip", "netns", "exec", UNIT, py, unit, "join-service"],
        env=env, stdout=out, stderr=out, start_new_session=True,
    )  # fmt: skip
    time.sleep(1.0)
    rc = subprocess.run(
        ["ip", "netns", "exec", UNIT, py, unit, "start-ap"],
        env=env, stdout=out, stderr=out, timeout=120, check=False,
    ).returncode  # fmt: skip
    if rc != 0:
        raise RuntimeError("The unit could not raise its hotspot; see unit.log")
    portal = subprocess.Popen(
        ["ip", "netns", "exec", UNIT, "setpriv", f"--reuid={SIM_USER}", f"--regid={SIM_USER}",
         "--init-groups", py, unit, "portal"],
        env=env, stdout=out, stderr=out, start_new_session=True,
    )  # fmt: skip
    return [join, portal]


def stop(procs: list[subprocess.Popen[bytes]]) -> None:
    for p in procs:
        with contextlib.suppress(OSError):
            os.killpg(p.pid, signal.SIGTERM)
    for p in procs:
        with contextlib.suppress(subprocess.TimeoutExpired):
            p.wait(timeout=5)


def radio_state(sim: Path) -> dict[str, Any]:
    try:
        return json.loads((sim / "radio.json").read_text())
    except (OSError, ValueError):
        return {}


def stop_radio(sim: Path) -> None:
    state = radio_state(sim)
    for key in ("dhcp_pid", "ap_dnsmasq_pid"):
        if state.get(key):
            with contextlib.suppress(OSError):
                os.kill(int(state[key]), signal.SIGTERM)


# --- the phone ---------------------------------------------------------------


def phone_join_hotspot(sim: Path) -> dict[str, Any] | None:
    """The phone joins "BlueSignal Setup" and asks for an address."""
    with contextlib.suppress(OSError):
        (sim / "lease-p0.json").unlink()
    sh("ip", "addr", "flush", "dev", "p0", ns=PHONE, check=False)
    sh(
        "busybox", "udhcpc", "-i", "p0", "-n", "-q", "-t", "6", "-T", "2",
        "-s", str(sim / "bin" / "sim-dhcp-hook"),
        ns=PHONE, check=False, timeout=40, env={**os.environ, "WQM1_SIM_DIR": str(sim)},
    )  # fmt: skip
    try:
        return json.loads((sim / "lease-p0.json").read_text())
    except (OSError, ValueError):
        return None


def phone_http(
    url: str, *, method: str = "GET", body: dict[str, Any] | None = None
) -> tuple[int, str, str]:
    """(status, Location, body) as the phone's browser sees it. 0 = no answer."""
    argv = [
        "curl",
        "-s",
        "--max-time",
        "8",
        "-o",
        "-",
        "-w",
        "\n%{http_code} %{redirect_url}",
        "-X",
        method,
    ]
    if body is not None:
        argv += ["-H", "Content-Type: application/json", "--data-binary", "@-"]
    proc = subprocess.run(
        ["ip", "netns", "exec", PHONE, *argv, url],
        input=json.dumps(body) if body is not None else None,
        capture_output=True, text=True, timeout=20, check=False,
    )  # fmt: skip
    text, _, tail = proc.stdout.rpartition("\n")
    code, _, location = tail.partition(" ")
    try:
        return int(code), location.strip(), text
    except ValueError:
        return 0, "", ""


def wait_portal(timeout: float = 30) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if phone_http(f"http://{HOTSPOT}/api/state")[0] == 200:
            return True
        time.sleep(1)
    return False


def join_state(sim: Path) -> dict[str, Any]:
    try:
        return json.loads((sim / "state" / "join-state.json").read_text())
    except (OSError, ValueError):
        return {}


def wait_outcome(sim: Path, timeout: float) -> tuple[dict[str, Any], float]:
    start = time.monotonic()
    time.sleep(1)
    while time.monotonic() - start < timeout:
        state = join_state(sim)
        if state.get("state") in ("joined", "failed"):
            return state, time.monotonic() - start
        time.sleep(1)
    return join_state(sim), time.monotonic() - start


# --- one scenario --------------------------------------------------------------


@dataclass
class Result:
    scenario: Scenario
    checks: list[tuple[str, bool, str]] = field(default_factory=list)
    seconds: float = 0.0

    def check(self, what: str, ok: bool, detail: str = "") -> bool:
        self.checks.append((what, ok, detail))
        return ok

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(ok for _, ok, _ in self.checks)


def attempt(
    sim: Path, r: Result, password: str, expect: str, gone_after_list: bool, answer_within_s: float
) -> None:
    lease = phone_join_hotspot(sim)
    ok = r.check(
        "phone gets a hotspot address and the hotspot's DNS",
        bool(lease and lease["ip"].startswith("192.168.4.") and lease.get("dns") == HOTSPOT),
        (lease or {}).get("ip", "no lease"),
    )
    if not ok:
        return
    code, location, _ = phone_http("http://captive.apple.com/hotspot-detect.html")
    r.check(
        "iPhone captive check opens the setup page",
        code == 302 and "/setup/" in location,
        f"{code} {location}",
    )
    code, _, _ = phone_http("http://connectivitycheck.gstatic.com/generate_204")
    r.check("Android captive check is not answered 204", code not in (0, 204), str(code))
    code, _, page = phone_http(f"http://{HOTSPOT}/setup/")
    r.check("setup page loads", code == 200 and "<html" in page.lower(), str(code))
    code, _, raw = phone_http(f"http://{HOTSPOT}/api/networks")
    names: list[str] = []
    with contextlib.suppress(ValueError, TypeError, AttributeError):
        data = json.loads(raw)
        rows = data.get("networks", data) if isinstance(data, dict) else data
        names = [n.get("ssid") for n in rows]
    if not r.check(
        "the list shows the home network", HOME_SSID in names, ", ".join(map(str, names))
    ):
        return
    if gone_after_list:
        world = json.loads((sim / "world.json").read_text())
        world["networks"][0]["present"] = False
        (sim / "world.json").write_text(json.dumps(world))
    posted = time.monotonic()
    body = {"ssid": HOME_SSID, "password": password, "hidden": False}
    code, _, raw = phone_http(f"http://{HOTSPOT}/api/join", method="POST", body=body)
    answer: dict[str, Any] = {}
    with contextlib.suppress(ValueError):
        answer = json.loads(raw)
    if not r.check(
        "join accepted", code in (200, 202) and answer.get("state") == "joining", f"{code} {answer}"
    ):
        return
    state, took = wait_outcome(sim, answer_within_s + 45)
    r.seconds += took
    if expect == "joined":
        r.check(
            "unit reports joined",
            state.get("state") == "joined",
            f"{state.get('state')} {state.get('reason')}",
        )
        lease_file = sim / "lease-wlan0.json"
        unit_ip = json.loads(lease_file.read_text()).get("ip", "") if lease_file.exists() else ""
        r.check(
            "unit holds a lease from the router",
            unit_ip.startswith("192.168.50."),
            unit_ip or "none",
        )
        log = (sim / "router-http.log").read_text() if (sim / "router-http.log").exists() else ""
        r.check(
            "BlueSignal saw the unit over HTTPS", bool(unit_ip) and f"{unit_ip} HEAD" in log, ""
        )
        bss = radio_state(sim).get("bss") or {}
        freq = int(bss.get("freq") or 0)
        r.check("unit is on the 2.4 GHz side", 0 < freq < 3000, f"{freq} MHz")
        r.check("joined within about a minute", took <= answer_within_s, f"{took:.0f} s")
        return
    r.check(
        "unit reports the reason",
        state.get("state") == "failed" and state.get("reason") == expect,
        f"{state.get('state')} {state.get('reason')}",
    )
    # The reason is stored first, then the hotspot comes back. The phone
    # rejoins and reloads, as an installer's would.
    back = None
    while time.monotonic() - posted < answer_within_s + 45:
        if phone_join_hotspot(sim) and wait_portal(timeout=5):
            back = time.monotonic() - posted
            break
    r.check("phone rejoins the hotspot and the page answers", back is not None)
    r.check(
        f"hotspot back within {answer_within_s:.0f} s",
        back is not None and back <= answer_within_s,
        f"{back or 0:.0f} s",
    )
    code, _, raw = phone_http(f"http://{HOTSPOT}/api/state")
    view: dict[str, Any] = {}
    with contextlib.suppress(ValueError):
        view = json.loads(raw)
    r.check(
        "page shows why",
        view.get("state") == "failed" and view.get("reason") == expect,
        f"{code} {view}",
    )
    code, _, _ = phone_http(f"http://{HOTSPOT}/setup/")
    r.check("page is there to retry", code == 200, str(code))


def run(sim: Path, sc: Scenario, host: str) -> Result:
    r = Result(sc)
    build_network(sim)
    prepare_unit(sim, sc)
    procs: list[subprocess.Popen[bytes]] = []
    try:
        procs += start_router(sim, sc, host)
        procs += start_unit(sim, sc)
        if not r.check("setup page is listening on the hotspot", wait_portal_after_lease(sim)):
            return r
        attempt(sim, r, sc.password, sc.expect, sc.gone_after_list, sc.answer_within_s)
        if sc.retry_password and r.passed:
            attempt(sim, r, sc.retry_password, "joined", False, 75)
    except Exception as e:  # noqa: BLE001 - one scenario's crash is its failure
        r.check("ran to the end", False, repr(e))
    finally:
        stop(procs)
        stop_radio(sim)
    return r


def wait_portal_after_lease(sim: Path) -> bool:
    return phone_join_hotspot(sim) is not None and wait_portal()


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("names", nargs="*", help="scenarios to run (default: all)")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--sim-dir", default="/run/wqm1-sim")
    ap.add_argument("--keep", action="store_true", help="leave the namespaces up afterwards")
    ap.add_argument("--serve-router", metavar="SIM_DIR", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if args.serve_router:
        return serve_router(Path(args.serve_router))
    if args.list:
        for sc in SCENARIOS:
            print(f"{sc.name:28} {sc.title}")
        return 0
    chosen = [sc for sc in SCENARIOS if not args.names or sc.name in args.names]
    if not chosen:
        raise SystemExit(f"No such scenario: {' '.join(args.names)} (see --list)")
    preflight()
    sim = Path(args.sim_dir)
    sim.mkdir(parents=True, exist_ok=True)
    host = api_host()
    make_certs(sim, host)
    results = []
    for sc in chosen:
        print(f"... {sc.name}: {sc.title}", flush=True)
        r = run(sim, sc, host)
        results.append(r)
        mark = "PASS" if r.passed else "FAIL"
        print(f"{mark} {sc.name} ({r.seconds:.0f} s to the unit's answer)", flush=True)
        for what, ok, detail in r.checks:
            print(
                f"   {'ok ' if ok else 'BAD'} {what}{f'  [{detail}]' if detail else ''}", flush=True
            )
    if not args.keep:
        teardown()
    report = [
        {"scenario": r.scenario.name, "passed": r.passed, "seconds": round(r.seconds, 1),
         "checks": [{"check": w, "ok": ok, "detail": d} for w, ok, d in r.checks]}
        for r in results
    ]  # fmt: skip
    (sim / "report.json").write_text(json.dumps(report, indent=2))
    failed = [r.scenario.name for r in results if not r.passed]
    passed = len(results) - len(failed)
    print(f"{passed}/{len(results)} scenarios passed. Report: {sim / 'report.json'}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
