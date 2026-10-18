"""
Setup-hotspot captive portal on port 80.

A phone that joins ``WQM1-xxxx`` probes a well-known URL
(``captive.apple.com/hotspot-detect.html``, Android ``/generate_204``,
Windows ``/connecttest.txt``). The hotspot's dnsmasq answers every name
with 192.168.4.1. This process binds that address on port 80 and answers
the probe with a redirect to ``http://192.168.4.1/setup/`` — the page
itself, not ``:8080``. A redirect onto another port is what makes some
handsets drop the captive sheet and show "no internet" instead.

The Service Window stays on 8080. Requests for this unit's own address
are proxied there, so the setup UI is what the phone renders, and an
installer who already has ``:8080`` bookmarked still reaches it directly.

The process runs as the install user with ``CAP_NET_BIND_SERVICE``. It
does not run as root. It binds only while the mode file says ``up`` and
192.168.4.1 is configured, which is the setup AP. ``join_network`` writes
``down: station`` when the unit joins the site, and the socket closes.
Nothing here listens on the site LAN.

DNS (``address=/#/192.168.4.1`` and DHCP option 114) is served by a
dedicated dnsmasq bound to ``wlan0``, started with the setup AP and
stopped before a station join, so the wildcard does not follow the unit
onto the site network.
"""

from __future__ import annotations

import http.client
import logging
import os
import shutil
import signal
import socket
import subprocess  # nosec B404
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger("wqm1.captive_portal")

PORTAL_HOST = "192.168.4.1"
PORTAL_PORT = 80
PORTAL_URL = f"http://{PORTAL_HOST}/setup/"
DNSMASQ_CONF = Path("/run/wqm1/dnsmasq-ap.conf")
_MODE_PATH = Path("/run/bluesignal/captive-portal.mode")
_UNIT_NAME = "bluesignal-captive-portal.service"
_MAX_BODY = 2_000_000

# Paths a captive check fetches. Answering any of these with the vendor's
# "success" body (or with 204) tells the phone the network has internet
# and it never opens the sheet.
PROBE_PATHS: tuple[str, ...] = (
    "/hotspot-detect.html",
    "/library/test/success.html",
    "/generate_204",
    "/gen_204",
    "/connecttest.txt",
    "/ncsi.txt",
    "/redirect",
    "/canonical.html",
    "/success.txt",
    "/check_network_status.txt",
)

# Hosts those checks use. A request for one of these is a probe even when
# the path is not in the list above (a vendor adds a path; the host does
# not change). The answer is still the portal, never success.
PROBE_HOSTS: frozenset[str] = frozenset(
    {
        "captive.apple.com",
        "www.apple.com",
        "www.appleiphonecell.com",
        "connectivitycheck.gstatic.com",
        "connectivitycheck.android.com",
        "clients3.google.com",
        "play.googleapis.com",
        "www.msftconnecttest.com",
        "msftconnecttest.com",
        "www.msftncsi.com",
        "ipv6.msftconnecttest.com",
        "detectportal.firefox.com",
    }
)

_OWN_HOSTS = frozenset({PORTAL_HOST, "localhost", "127.0.0.1"})
_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)


def dnsmasq_shared_config() -> str:
    """Captive DNS lines the port-80 portal relies on.

    ``address=/#/192.168.4.1`` makes every name the phone looks up resolve
    here. DHCP option 114 (RFC 8910) is the captive-portal URI; iOS and
    Android request it. These lines are part of ``dnsmasq_ap_config``.
    The process that reads them is stopped before a station join.
    """
    return (
        "# BlueSignal WQM-1 setup hotspot captive portal\n"
        "# Included in the dedicated setup-AP dnsmasq, not a station link.\n"
        f"address=/#/{PORTAL_HOST}\n"
        f'dhcp-option=114,"{PORTAL_URL}"\n'
    )


def dnsmasq_ap_config(iface: str = "wlan0") -> str:
    """dnsmasq for the setup AP, bound to ``iface`` only.

    DHCP is 192.168.4.10-254. The captive lines (wildcard DNS and DHCP
    option 114) are ``dnsmasq_shared_config``.
    """
    return (
        "# BlueSignal WQM-1 setup AP. Bound to the hotspot interface only.\n"
        "# wqm1-setup-dnsmasq.service runs this while hostapd is up.\n"
        f"interface={iface}\n"
        "bind-interfaces\n"
        f"listen-address={PORTAL_HOST}\n"
        "except-interface=lo\n"
        "dhcp-range=192.168.4.10,192.168.4.254,255.255.255.0,12h\n"
        f"dhcp-option=3,{PORTAL_HOST}\n"
        f"dhcp-option=6,{PORTAL_HOST}\n"
        "dhcp-authoritative\n"
        "no-resolv\n" + dnsmasq_shared_config()
    )


def dnsmasq_conf_path() -> Path:
    override = os.environ.get("WQM1_DNSMASQ_CONF")
    if override:
        return Path(override)
    return DNSMASQ_CONF


def ensure_dnsmasq_conf(path: Path | None = None) -> dict[str, Any]:
    """Write the setup-AP dnsmasq config if it differs. Never raises.

    Failure is logged. ``start_ap`` treats a failed write as a failed AP
    so a phone is not left associated with no DHCP and no captive DNS.
    """
    target = path or dnsmasq_conf_path()
    text = dnsmasq_ap_config()
    try:
        if target.is_file() and target.read_text(encoding="utf-8") == text:
            return {"ok": True, "unchanged": True, "path": str(target)}
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    except OSError as e:
        logger.warning("Captive DNS config not written: %s", e)
        return {"ok": False, "error": str(e), "path": str(target)}
    return {"ok": True, "path": str(target)}


def mode_path() -> Path:
    override = os.environ.get("WQM1_PORTAL_MODE_FILE")
    if override:
        return Path(override)
    return _MODE_PATH


def _write_mode(text: str) -> None:
    path = mode_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")
    except OSError as e:
        logger.warning("Portal mode not written: %s", e)


def arm_portal() -> None:
    """The setup AP is up. The listener may bind 192.168.4.1:80."""
    _write_mode("up")


def disarm_portal(reason: str) -> None:
    """Stop listening. ``reason`` is the status-file tail (``station``, …).

    Called when a station joins and when this boot is not raising the AP.
    The listener closes even if 192.168.4.1 is still configured.
    """
    clean = " ".join((reason or "stopped").split())
    _write_mode(f"down: {clean}")


def read_mode() -> str:
    try:
        text = mode_path().read_text(encoding="utf-8").strip()
    except OSError:
        return "down: stopped"
    return text or "down: stopped"


def portal_should_bind(mode: str, address_up: bool) -> bool:
    """Listen only for an armed hotspot whose address is actually configured."""
    return mode.strip() == "up" and bool(address_up)


def host_name(host: str) -> str:
    """Hostname from a Host header, without a port and without brackets."""
    text = (host or "").strip().lower()
    if text.startswith("["):
        end = text.find("]")
        return text[1:end] if end > 1 else text
    if text.count(":") == 1:
        text = text.split(":", 1)[0]
    return text


def is_own_host(host: str) -> bool:
    """True for the hotspot address, localhost, and ``*.local``.

    Any other name is a captive check or a URL the installer typed. Those
    are redirected. Treating every IP as ours would proxy a foreign name
    that happened to be numeric and skip the sheet.
    """
    name = host_name(host)
    return not name or name in _OWN_HOSTS or name.endswith(".local")


def request_path(raw: str) -> str:
    path = (raw or "/").split("?", 1)[0] or "/"
    if not path.startswith("/"):
        path = "/" + path
    return path


def is_probe(path: str, host: str) -> bool:
    if request_path(path) in PROBE_PATHS:
        return True
    return host_name(host) in PROBE_HOSTS


def classify_request(path: str, host: str) -> str:
    """``redirect`` (captive response) or ``proxy`` (the setup UI on port 80).

    Probes redirect even when the Host is our own address. Anything else
    addressed to us is the setup page and is proxied to the Service Window.
    Any other host is a name dnsmasq pointed here: redirect, so typing a
    URL opens the sheet's page instead of whatever that site would have been.
    """
    if is_probe(path, host):
        return "redirect"
    if is_own_host(host):
        return "proxy"
    return "redirect"


def rewrite_location(location: str, backend_port: int) -> str:
    """Keep the browser on port 80.

    The Service Window builds an absolute URL with its own port when it
    sees a request that arrived on 8080. The phone must not be sent there:
    that is the bug this portal exists to close.
    """
    if not location:
        return location
    parts = urlsplit(location)
    on_us = parts.scheme == "http" and parts.hostname in {PORTAL_HOST, "127.0.0.1"}
    same_port = parts.port == backend_port or (parts.port is None and backend_port == 80)
    if on_us and same_port:
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"
        return f"http://{PORTAL_HOST}{path}"
    if f":{backend_port}" in location and location.startswith(f"http://{PORTAL_HOST}:"):
        return location.replace(f":{backend_port}", "", 1)
    return location


def address_is_local(ip: str) -> bool:
    """True when ``ip`` is configured on this host. No nmcli, no port 80."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((ip, 0))
    except OSError:
        return False
    finally:
        sock.close()
    return True


def _short(exc: BaseException) -> str:
    text = " ".join(str(exc).split())
    return (text or exc.__class__.__name__)[:160]


def http_probe(
    host: str = PORTAL_HOST,
    port: int = PORTAL_PORT,
    timeout: float = 1.5,
) -> tuple[bool, str]:
    """Whether port 80 returned a captive redirect, not an internet-success."""
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request(
            "GET",
            "/hotspot-detect.html",
            headers={"Host": "captive.apple.com", "Connection": "close"},
        )
        resp = conn.getresponse()
        location = resp.getheader("Location") or ""
        body = resp.read(256)
        status = resp.status
    except OSError as e:
        return False, _short(e)
    finally:
        conn.close()
    if status == 204 or (status == 200 and b"Success" in body):
        return False, "probe answered success"
    if ":8080" in location:
        return False, "redirect leaves port 80"
    if status in (301, 302, 303, 307, 308) and location == PORTAL_URL:
        return True, "up (80)"
    return False, f"HTTP {status}"


def measure_portal() -> str:
    """``up (80)`` or ``down: <reason>`` from a live probe of port 80."""
    if not address_is_local(PORTAL_HOST):
        return f"down: {PORTAL_HOST} is not local"
    ok, detail = http_probe()
    if ok:
        return "up (80)"
    return f"down: {detail}"


def portal_line_for_hotspot(hotspot: str) -> str:
    """Status-file value. A station link is ``down: station`` and is not a fault."""
    text = (hotspot or "").strip()
    if text == "station":
        return "down: station"
    if text != "up" and not text.startswith("up "):
        return "down: hotspot down"
    return measure_portal()


def wait_until_reachable(seconds: float = 8.0, pause: Callable[[float], None] = time.sleep) -> str:
    """Probe until the portal answers or the address is not ours.

    ``192.168.4.1 is not local`` returns immediately: waiting cannot make
    an address appear that this host does not have. A unit whose listener
    is still starting gets ``seconds``.
    """
    deadline = time.monotonic() + seconds
    last = "down: not checked"
    while True:
        last = measure_portal()
        if last.startswith("up") or "not local" in last or time.monotonic() >= deadline:
            return last
        pause(0.4)


def backend_port(config_path: str = "/etc/bluesignal/config.yaml") -> int:
    """Service Window port. The portal proxies to it; it does not replace it."""
    env = os.environ.get("WQM1_PORTAL_BACKEND")
    if env:
        try:
            return int(env)
        except ValueError:
            return 8080
    try:
        import yaml

        with open(config_path, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        return int((raw.get("service_window") or {}).get("port") or 8080)
    except (OSError, ValueError, TypeError, ImportError):
        return 8080


class PortalServer(HTTPServer):
    """Listens on one address. ``backend_port`` is the Service Window."""

    allow_reuse_address = True
    backend_port: int = 8080

    def server_bind(self) -> None:
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        super().server_bind()


class PortalHandler(BaseHTTPRequestHandler):
    """Captive answers on the socket, setup UI proxied to the Service Window."""

    protocol_version = "HTTP/1.0"
    server: PortalServer

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.info("%s - " + fmt, self.address_string(), *args)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._handle()

    def do_HEAD(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._handle()

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._handle()

    def _backend(self) -> int:
        port = getattr(self.server, "backend_port", 8080)
        return int(port) if isinstance(port, int) else 8080

    def _handle(self) -> None:
        host = self.headers.get("Host", "")
        if classify_request(self.path, host) == "redirect":
            self._redirect()
            return
        self._proxy()

    def _unavailable(self) -> None:
        body = b"setup page is not up yet\n"
        self.send_response(503)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Retry-After", "2")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _redirect(self) -> None:
        # Empty body. "Success" or "Microsoft Connect Test" is the answer
        # that tells the phone to skip the sheet.
        self.send_response(302)
        self.send_header("Location", PORTAL_URL)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _proxy(self) -> None:
        length_header = self.headers.get("Content-Length")
        body: bytes | None = None
        if length_header:
            try:
                length = int(length_header)
            except ValueError:
                self._redirect()
                return
            if length < 0 or length > _MAX_BODY:
                self.send_error(413)
                return
            body = self.rfile.read(length) if length else b""
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in _HOP_HEADERS and key.lower() != "content-length"
        }
        conn = http.client.HTTPConnection("127.0.0.1", self._backend(), timeout=8)
        try:
            conn.request(self.command, self.path, body=body, headers=headers)
            resp = conn.getresponse()
            payload = resp.read(_MAX_BODY)
            status = resp.status
            resp_headers = resp.getheaders()
        except OSError as e:
            # 302 to /setup/ here would loop: this request is already the
            # setup page, and the Service Window is the thing that is down.
            logger.warning("Portal proxy failed: %s", e)
            self._unavailable()
            return
        finally:
            conn.close()
        self.send_response(status)
        sent_location = False
        for key, value in resp_headers:
            if key.lower() in _HOP_HEADERS or key.lower() in {"content-length", "server", "date"}:
                continue
            if key.lower() == "location":
                value = rewrite_location(value, self._backend())
                sent_location = True
            self.send_header(key, value)
        if status in (301, 302, 303, 307, 308) and not sent_location:
            self.send_header("Location", PORTAL_URL)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD" and payload:
            self.wfile.write(payload)


def make_server(host: str, port: int, backend: int | None = None) -> PortalServer:
    server = PortalServer((host, port), PortalHandler)
    server.backend_port = backend_port() if backend is None else backend
    return server


def run_loop(
    bind: tuple[str, int] = (PORTAL_HOST, PORTAL_PORT),
    address_up: Callable[[], bool] | None = None,
    on_bind: Callable[[PortalServer], None] | None = None,
    poll_s: float = 0.5,
    stop: Callable[[], bool] | None = None,
) -> None:
    """Bind while the hotspot is armed, and close the socket otherwise.

    ``address_up`` defaults to "is 192.168.4.1 configured". Tests pass a
    stub so the loop can bind 127.0.0.1 and still prove the station-join
    close. One request at a time: a Pi Zero does not need a thread per phone.
    """
    check = address_up if address_up is not None else (lambda: address_is_local(bind[0]))
    stopped = stop if stop is not None else (lambda: False)
    server: PortalServer | None = None
    while not stopped():
        want = portal_should_bind(read_mode(), check())
        if want and server is None:
            try:
                server = make_server(bind[0], bind[1])
            except OSError as e:
                logger.warning("Portal bind %s:%s failed: %s", bind[0], bind[1], e)
                time.sleep(poll_s)
                continue
            logger.info("Portal listening on %s:%s", *server.server_address[:2])
            if on_bind is not None:
                on_bind(server)
        if server is not None and not want:
            logger.info("Portal closed (%s)", read_mode())
            server.server_close()
            server = None
        if server is None:
            time.sleep(poll_s)
            continue
        server.timeout = poll_s
        try:
            server.handle_request()
        except OSError as e:
            logger.warning("Portal accept failed: %s", e)
            server.server_close()
            server = None
    if server is not None:
        server.server_close()


def _systemctl(*args: str) -> tuple[int, str, str]:
    exe = shutil.which("systemctl")
    if not exe:
        return 127, "", "systemctl not found"
    try:
        out = subprocess.run(  # nosec B603
            [exe, *args],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return 1, "", str(e)
    return out.returncode, out.stdout.strip(), out.stderr.strip()


def _service_user() -> str:
    """The user the Service Window already runs as, else ``pi``."""
    unit = Path("/etc/systemd/system/bluesignal-service-window.service")
    try:
        for line in unit.read_text(encoding="utf-8").splitlines():
            if line.startswith("User="):
                user = line.split("=", 1)[1].strip()
                if user:
                    return user
    except OSError:
        pass
    return "pi"


def release_unit_path() -> Path | None:
    override = os.environ.get("WQM1_PORTAL_UNIT")
    if override:
        return Path(override)
    installed = Path("/opt/bluesignal/current/systemd") / _UNIT_NAME
    if installed.is_file():
        return installed
    packaged = Path(__file__).resolve().parents[2] / "systemd" / _UNIT_NAME
    if packaged.is_file():
        return packaged
    return None


def ensure_portal_service() -> dict[str, Any]:
    """Install the unit if this image does not have it yet, and start it.

    Root only. An OTA flip updates ``/opt/bluesignal/current`` but does not
    re-run ``setup.sh``, so the first boot of this code copies the unit.
    An existing unit is left in place: ``setup.sh`` rewrites ``User=``.
    Not ordered after the AP fallback — starting it from that fallback
    would deadlock on ``After=``. The process waits on the mode file.
    """
    try:
        if os.geteuid() != 0:
            return {"ok": False, "error": "not root"}
    except AttributeError:
        return {"ok": False, "error": "not root"}
    dest = Path("/etc/systemd/system") / _UNIT_NAME
    if not dest.is_file():
        src = release_unit_path()
        if src is None:
            return {"ok": False, "error": "unit file missing"}
        text = src.read_text(encoding="utf-8").replace("User=pi\n", f"User={_service_user()}\n", 1)
        try:
            dest.write_text(text, encoding="utf-8")
        except OSError as e:
            return {"ok": False, "error": str(e)}
        rc, _out, err = _systemctl("daemon-reload")
        if rc != 0:
            return {"ok": False, "error": err or f"daemon-reload rc {rc}"}
    rc, _out, err = _systemctl("enable", "--now", _UNIT_NAME)
    if rc != 0:
        return {"ok": False, "error": err or f"systemctl rc {rc}"}
    return {"ok": True}


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s [captive-portal] %(message)s"
    )
    halt = False

    def _stop(_signum: int, _frame: object) -> None:
        nonlocal halt
        halt = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    run_loop(stop=lambda: halt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
