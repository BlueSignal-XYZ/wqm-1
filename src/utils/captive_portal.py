"""
The Wi-Fi page on port 80 (install plan §4).

A phone that joins the setup network probes a well-known URL
(``captive.apple.com/hotspot-detect.html``, Android ``/generate_204``,
Windows ``/connecttest.txt``). The setup network's dnsmasq answers every
name with 192.168.4.1. This process binds that address on port 80, answers
the probe with a redirect to ``http://192.168.4.1/setup/``, and serves that
page itself:

* ``GET /`` and ``GET /setup/``: the Wi-Fi page. One HTML document with its
  CSS and JS inline, no external request, English or Spanish from
  ``Accept-Language``. Every sentence is in ``utils.setup_copy``.
* ``GET /api/networks``: the cached scan (``netctl.read_scan_cache``).
* ``POST /api/rescan``: refresh that cache without dropping the setup
  network.
* ``POST /api/join`` ``{ssid, password, hidden, keep_space, trim_ack}``:
  validated here, then started by ``utils.join_service``.
* ``GET /api/state``: the last join (``utils.join.read_state``).
* Anything else: 404 with one line that links ``/setup/``.

**Nothing is proxied to the Service Window (8080).** A phone on the setup
network reaches this page and nothing else on the unit (I1), and the page
works with the firmware and the Service Window stopped (I7).

No page this process serves reloads itself or replaces its form (I5): the
page polls ``/api/state`` every 3 s and edits one status line.

The process runs as the install user with ``CAP_NET_BIND_SERVICE``. It does
not run as root, and a join needs root, so ``/api/join`` and
``/api/rescan`` go to ``utils.join_service`` over a local socket. It binds
only while the mode file says ``up`` and 192.168.4.1 is configured, which
is the setup network. ``join_network`` writes ``down: …`` when the join
starts and ``down: station`` when the unit is on the site network, and the
socket closes. Nothing here listens on the site LAN.

DNS (``address=/#/192.168.4.1`` and DHCP option 114) is served by a
dedicated dnsmasq bound to ``wlan0``, started with the setup network and
stopped before a station join, so the wildcard does not follow the unit
onto the site network.
"""

from __future__ import annotations

import contextlib
import html
import http.client
import json
import logging
import os
import shutil
import signal
import socket
import subprocess  # nosec B404
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Any

logger = logging.getLogger("wqm1.captive_portal")

PORTAL_HOST = "192.168.4.1"
PORTAL_PORT = 80
PORTAL_URL = f"http://{PORTAL_HOST}/setup/"
DNSMASQ_CONF = Path("/run/wqm1/dnsmasq-ap.conf")
_MODE_PATH = Path("/run/bluesignal/captive-portal.mode")
_UNIT_NAME = "bluesignal-captive-portal.service"
SETUP_PATH = "/setup/"
PAGE_PATHS = frozenset({"/", "/setup", SETUP_PATH})
# The API takes a few small JSON fields. Anything larger is not this page.
MAX_BODY = 4096
# Requests served at once on port 80, and how long a silent one is kept.
MAX_CLIENTS = 16
CLIENT_TIMEOUT_S = 15.0
POLL_MS = 3000
# A failure older than this is not news to a phone that opens the page.
STATE_FRESH_S = 30 * 60
# While a join runs, the run loop checks its result this often.
JOIN_WATCH_S = 2.0
JOIN_WATCH_FOR_S = 10 * 60

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
# not change). The answer is still the page, never success.
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
# The port-80 relay to the Service Window is gone (§4); nothing sends this
# mark now. ``service_window.captive`` still reads it until the wizard and
# its hotspot checks are deleted (§9).
RELAY_HEADER = "X-WQM1-Relay"
RELAY_VALUE = "hotspot"

_SECURITY_HEADERS = (
    ("Cache-Control", "no-store"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    # The page makes no request to anything but this listener (I7).
    (
        "Content-Security-Policy",
        "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
        "connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'; "
        "frame-ancestors 'none'",
    ),
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
    are redirected. Treating every IP as ours would serve the page under a
    foreign name that happened to be numeric and skip the sheet.
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


_API_ROUTES = {
    ("GET", "/api/networks"): "networks",
    ("HEAD", "/api/networks"): "networks",
    ("GET", "/api/state"): "state",
    ("HEAD", "/api/state"): "state",
    ("POST", "/api/join"): "join",
    ("POST", "/api/rescan"): "rescan",
}


def classify_request(path: str, host: str, method: str = "GET") -> str:
    """``redirect``, ``page``, ``api:<name>`` or ``not_found``.

    Probes redirect even when the Host is our own address. Any other host is
    a name dnsmasq pointed here: redirect, so typing a URL opens the setup
    page instead of whatever that site would have been. On our own address
    only the page and its four API routes answer (I1).
    """
    if is_probe(path, host):
        return "redirect"
    if not is_own_host(host):
        return "redirect"
    route = request_path(path)
    verb = (method or "GET").upper()
    if route in PAGE_PATHS and verb in ("GET", "HEAD"):
        return "page"
    api = _API_ROUTES.get((verb, route))
    if api:
        return f"api:{api}"
    return "not_found"


def cross_site(method: str, headers: Any, host: str) -> bool:
    """A POST that another site's page sent. Refused.

    A phone on the setup network often keeps cellular data, so a page it
    loads elsewhere could POST to http://192.168.4.1/api/join. A browser
    marks such a request (``Sec-Fetch-Site``) or names its origin; curl and
    the page itself pass.
    """
    if (method or "").upper() != "POST":
        return False
    site = headers.get("Sec-Fetch-Site")
    if site is not None and site not in ("same-origin", "none"):
        return True
    origin = headers.get("Origin")
    if origin is None:
        return False
    named = (host or "").strip().lower()
    return origin.strip().lower() not in {f"http://{named}", f"https://{named}"}


# --- The page ---------------------------------------------------------------

_PAGE_CSS = """
:root{color-scheme:light}
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
background:#f4f7fa;color:#14202b;font-size:17px;line-height:1.45}
main{max-width:30rem;margin:0 auto;padding:1.5rem 1.1rem 3rem}
.brand{display:flex;align-items:center;gap:.5rem;color:#3b5266;font-size:.8rem;
font-weight:650;letter-spacing:.12em;text-transform:uppercase;margin-bottom:1.4rem}
h1{font-size:1.5rem;margin:0 0 .4rem;letter-spacing:-.01em}
p{margin:.4rem 0}
.muted{color:#4a5d6e;font-size:.95rem}
#status{min-height:1.5em;margin:1rem 0;padding:.75rem .9rem;border-radius:.6rem;
background:#e3eef8;color:#0d2f4f;font-weight:600}
#status:empty{display:none}
fieldset{border:0;margin:1.2rem 0 .4rem;padding:0}
legend{font-weight:650;margin-bottom:.4rem}
.net{display:flex;align-items:center;gap:.7rem;padding:.75rem .8rem;background:#fff;
border:1px solid #d3dde6;border-radius:.6rem;margin:.4rem 0}
.net input{width:1.2rem;height:1.2rem;flex:none}
.net .name{flex:1;overflow-wrap:anywhere}
.net .bars{color:#3b5266;font-family:monospace;letter-spacing:.05em}
.net.blocked{opacity:.65}
.net.blocked .why{display:block;font-size:.85rem;color:#7a3b00}
label.field{display:block;font-weight:650;margin:1rem 0 .3rem}
input[type=text],input[type=password]{width:100%;font-size:1.05rem;padding:.7rem .75rem;
border:1px solid #9fb1c1;border-radius:.5rem;background:#fff;color:#14202b}
.check{display:flex;align-items:center;gap:.5rem;margin:.6rem 0;font-size:.95rem}
.note{font-size:.95rem;color:#7a3b00;margin:.5rem 0}
.note:empty{display:none}
button{font-size:1.05rem;border-radius:.6rem;padding:.8rem 1rem;border:0;cursor:pointer}
#connect{width:100%;background:#0b5cad;color:#fff;font-weight:650;margin-top:1rem}
#rescan{background:#e3eaf0;color:#14202b;margin:.4rem 0}
[hidden]{display:none!important}
"""

_PAGE_JS = """
(function(){
var C=JSON.parse(document.getElementById("copy").textContent);
var list=document.getElementById("networks");
var other=document.getElementById("other");
var otherBox=document.getElementById("other-box");
var nameIn=document.getElementById("name");
var pw=document.getElementById("password");
var show=document.getElementById("show");
var keepBox=document.getElementById("keep-box");
var keep=document.getElementById("keep");
var note=document.getElementById("pick-note");
var notice=document.getElementById("notice");
var status=document.getElementById("status");
var nets={};
var trimAck=false;
var lastKey=null;
var first=true;
function fmt(t,ssid){return t.split("{ssid}").join(ssid||"");}
function say(t){status.textContent=t||"";}
function picked(){
  if(other.checked){return {ssid:nameIn.value.trim(),hidden:true};}
  var r=list.querySelector("input:checked");
  return r?{ssid:r.value,hidden:false}:{ssid:"",hidden:false};
}
function noteFor(n){
  if(!n){return "";}
  var out=[];
  if(n.note){out.push(C[n.note]||"");}
  if(n.weak){out.push(C.weak);}
  if(n.phone){out.push(C.phone_hotspot);}
  return out.join(" ");
}
function onPick(){
  var p=picked();
  otherBox.hidden=!other.checked;
  note.textContent=other.checked?"":noteFor(nets[p.ssid]);
}
function row(n){
  var lab=document.createElement("label");
  lab.className="net"+(n.joinable?"":" blocked");
  var r=document.createElement("input");
  r.type="radio";r.name="net";r.value=n.ssid;r.disabled=!n.joinable;
  r.addEventListener("change",onPick);
  var name=document.createElement("span");
  name.className="name";name.textContent=n.ssid;
  if(!n.joinable&&n.note){
    var why=document.createElement("span");
    why.className="why";why.textContent=C[n.note]||"";name.appendChild(why);
  }
  var bars=document.createElement("span");
  bars.className="bars";bars.textContent=n.bar_text||"";
  lab.appendChild(r);lab.appendChild(name);lab.appendChild(bars);
  return lab;
}
function render(data){
  var keepPick=picked();
  while(list.firstChild){list.removeChild(list.firstChild);}
  nets={};
  var rows=data.networks||[];
  for(var i=0;i<rows.length;i++){nets[rows[i].ssid]=rows[i];list.appendChild(row(rows[i]));}
  if(!rows.length){var e=document.createElement("p");e.className="muted";
    e.textContent=C.none_found;list.appendChild(e);}
  var notes=[];
  if(data.only_24){notes.push(C.only_24);}
  if(data.hidden){notes.push(C.hidden_nearby);}
  notice.textContent=notes.join(" ");
  if(keepPick.ssid&&!keepPick.hidden){choose(keepPick.ssid);}
  onPick();
}
function choose(ssid){
  var radios=list.querySelectorAll("input");
  for(var i=0;i<radios.length;i++){
    if(radios[i].value===ssid&&!radios[i].disabled){radios[i].checked=true;return true;}
  }
  return false;
}
function get(path){
  return fetch(path,{cache:"no-store",credentials:"same-origin"})
    .then(function(r){return r.json();});
}
function post(path,body){
  return fetch(path,{method:"POST",cache:"no-store",credentials:"same-origin",
    headers:{"Content-Type":"application/json"},body:JSON.stringify(body)})
    .then(function(r){return r.json();});
}
function load(){
  return get("/api/networks").then(render).catch(function(){say(C.try_again);});
}
function showState(s){
  var key=[s.state,s.ssid,s.reason,s.at].join("|");
  if(key===lastKey){return;}
  lastKey=key;
  if(s.state==="joining"){say(fmt(C.busy,s.ssid));}
  else if(s.state==="joined"){say(fmt(C.joined,s.ssid));}
  else if(s.state==="failed"){
    say((C[s.reason]||C.no_association));
    if(first&&s.ssid&&!choose(s.ssid)){other.checked=true;nameIn.value=s.ssid;}
    onPick();
  }
  first=false;
}
function poll(){
  get("/api/state").then(showState).catch(function(){});
}
other.addEventListener("change",onPick);
show.addEventListener("change",function(){pw.type=show.checked?"text":"password";});
pw.addEventListener("input",function(){trimAck=false;keepBox.hidden=true;});
document.getElementById("rescan").addEventListener("click",function(){
  say(C.rescanning);
  post("/api/rescan",{}).then(function(r){
    if(r.ok){lastKey=null;say("");return load();}
    say(C.rescan_failed);
  }).catch(function(){say(C.rescan_failed);});
});
document.getElementById("form").addEventListener("submit",function(ev){
  ev.preventDefault();
  var p=picked();
  if(!p.ssid){say(C.pick_first);return;}
  post("/api/join",{ssid:p.ssid,password:pw.value,hidden:p.hidden,
    keep_space:keep.checked,trim_ack:trimAck}).then(function(r){
    if(r.state==="joining"){lastKey=null;say(fmt(C.joining,r.ssid||p.ssid));return;}
    if(r.state==="busy"){say(fmt(C.busy,r.ssid||p.ssid));return;}
    if(r.error==="trailing_space"){trimAck=true;keepBox.hidden=false;say(C.trailing_space);return;}
    say(C[r.error]||C.try_again);
  }).catch(function(){say(C.try_again);});
});
load().then(poll);
setInterval(poll,%(poll_ms)d);
})();
"""

_BRAND_SVG = (
    '<svg viewBox="0 0 28 28" width="20" height="20" aria-hidden="true">'
    '<path d="M4 8c4-3 12-3 20 0M4 14c4-3 12-3 20 0M4 20c4-3 12-3 20 0" '
    'fill="none" stroke="#0b5cad" stroke-width="2.2" stroke-linecap="round"/></svg>'
)

_PAGE_CACHE: dict[str, bytes] = {}


def _script_json(value: Any) -> str:
    """JSON safe inside a ``<script>`` element."""
    return (
        json.dumps(value, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def render_page(lang: str = "en") -> bytes:
    """The Wi-Fi page in ``lang``. One document, nothing external (I7).

    The page never names the unit (spec section 2: Cloud finds the unit).
    """
    from utils import setup_copy

    if lang not in setup_copy.LANGUAGES:
        lang = "en"
    cached = _PAGE_CACHE.get(lang)
    if cached is not None:
        return cached
    c = setup_copy.LANGUAGES[lang]
    e = {key: html.escape(value, quote=True) for key, value in c.items()}
    script = _PAGE_JS % {"poll_ms": POLL_MS}
    copy_json = _script_json(c)
    page = f"""<!DOCTYPE html>
<html lang="{e["lang"]}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e["title"]}</title>
<style>{_PAGE_CSS}</style>
</head>
<body>
<main>
<div class="brand">{_BRAND_SVG}BlueSignal</div>
<h1>{e["title"]}</h1>
<p>{e["lead"]}</p>
<p class="muted">{e["stay_connected"]}</p>
<p id="status" role="status" aria-live="polite"></p>
<form id="form" novalidate>
<fieldset>
<legend>{e["networks"]}</legend>
<div id="networks"></div>
<label class="net"><input type="radio" name="net" id="other" value="">
<span class="name">{e["other"]}</span></label>
</fieldset>
<p id="notice" class="note"></p>
<button type="button" id="rescan">{e["rescan"]}</button>
<div id="other-box" hidden>
<label class="field" for="name">{e["name_label"]}</label>
<input type="text" id="name" maxlength="32" autocomplete="off" autocapitalize="none"
 autocorrect="off" spellcheck="false">
</div>
<p id="pick-note" class="note"></p>
<label class="field" for="password">{e["password_label"]}</label>
<input type="password" id="password" maxlength="64" autocomplete="off" autocapitalize="none"
 autocorrect="off" spellcheck="false">
<label class="check"><input type="checkbox" id="show"> {e["show_password"]}</label>
<label class="check" id="keep-box" hidden><input type="checkbox" id="keep">
 {e["keep_space"]}</label>
<p class="muted">{e["drop_warning"]}</p>
<button type="submit" id="connect">{e["connect"]}</button>
</form>
</main>
<script type="application/json" id="copy">{copy_json}</script>
<script>{script}</script>
</body>
</html>
"""
    body = page.encode("utf-8")
    _PAGE_CACHE[lang] = body
    return body


def not_found_page() -> bytes:
    """One line that links the Wi-Fi page. No unit detail."""
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>Not found</title></head><body>"
        f'<p><a href="{SETUP_PATH}">Open the Wi-Fi setup page</a></p>'
        "</body></html>"
    ).encode()


# --- The API ----------------------------------------------------------------


def _networks_view(networks: list[dict[str, Any]]) -> dict[str, Any]:
    """What the page lists: joinable networks first, then refused ones."""
    from utils.wifi_setup import band_notice, block_code, has_hidden, looks_like_phone_hotspot

    rows = []
    for net in networks:
        ssid = str(net.get("ssid") or "")
        if not ssid or net.get("hidden"):
            continue
        refused = block_code(net)
        code = refused
        if refused is None and net.get("security") == "open":
            code = "open_network"
        rows.append(
            {
                "ssid": ssid,
                "joinable": refused is None,
                "secured": bool(net.get("secured")),
                "bars": int(net.get("bars") or 0),
                "bar_text": str(net.get("bar_text") or ""),
                "weak": bool(net.get("weak")),
                "phone": looks_like_phone_hotspot(ssid),
                "note": code,
            }
        )
    rows.sort(key=lambda r: (not r["joinable"], -r["bars"]))
    return {
        "networks": rows,
        "hidden": has_hidden(networks),
        "only_24": band_notice(networks) is not None,
    }


def load_networks() -> list[dict[str, Any]]:
    """The cached scan. The simulator scans its virtual radio instead."""
    from utils import netctl, netsim

    if netsim.is_virtual():
        return netctl.scan_networks()
    return list(netctl.read_scan_cache().get("networks") or [])


def state_view(now: datetime | None = None) -> dict[str, Any]:
    """The last join, for the page. Never a password, never nmcli text."""
    from utils import join, setup_copy

    state = join.read_state()
    view = {
        "state": state.get("state") or "idle",
        "ssid": state.get("ssid"),
        "reason": state.get("reason"),
        "at": state.get("at"),
    }
    if view["state"] == "failed":
        if view["reason"] not in setup_copy.en:
            view["reason"] = "no_association"
        if _older_than(view["at"], STATE_FRESH_S, now):
            return {"state": "idle", "ssid": None, "reason": None, "at": None}
    return view


def _older_than(stamp: Any, seconds: float, now: datetime | None = None) -> bool:
    if not isinstance(stamp, str):
        return True
    try:
        when = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return True
    return (now or datetime.now(UTC)) - when > timedelta(seconds=seconds)


def validate_join(body: Any, networks: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str]:
    """``(join args, "")`` or ``(None, <copy key>)``.

    The same rules as the Service Window's form, through the same helpers:
    ``block_code``, ``normalize_password``, ``password_problem_code``.
    """
    from utils.netctl import valid_ssid
    from utils.wifi_setup import (
        block_code,
        find_network,
        normalize_password,
        password_problem_code,
    )

    if not isinstance(body, dict):
        return None, "try_again"
    hidden = body.get("hidden") is True
    raw_ssid = body.get("ssid")
    ssid = raw_ssid if isinstance(raw_ssid, str) else ""
    if hidden:
        ssid = ssid.strip()
    if not ssid:
        return None, "pick_first"
    if not valid_ssid(ssid):
        return None, "network_name_bad"
    match = None if hidden else find_network(networks, ssid)
    refused = block_code(match)
    if refused:
        return None, refused
    raw = body.get("password")
    if raw is not None and not isinstance(raw, str):
        return None, "try_again"
    password, trimmed = normalize_password(
        raw or "", keep_trailing_space=body.get("keep_space") is True
    )
    if trimmed and body.get("trim_ack") is not True:
        return None, "trailing_space"
    problem = password_problem_code(password, secured=bool(match and match.get("secured")))
    if problem:
        return None, problem
    return {"ssid": ssid, "password": password or None, "hidden": hidden or match is None}, ""


# --- The join watcher (registration as soon as the join is proven) ----------

_watch_lock = threading.Lock()
_watching: dict[str, Any] = {}


def watch_join(ssid: str) -> None:
    """Remember a started join so the run loop can kick registration."""
    with _watch_lock:
        _watching.update(ssid=ssid, until=time.monotonic() + JOIN_WATCH_FOR_S, next=0.0)


def _kick_register() -> None:
    """One registration attempt as the install user, in the background.

    The firmware's register worker retries on its own, but its backoff can
    reach an hour on a unit that sat offline; this is the attempt that makes
    Cloud see the unit right after the join.
    """

    def _run() -> None:
        try:
            import yaml

            from cloud.register import kick_register

            config_path = os.environ.get("BLUESIGNAL_CONFIG", "/etc/bluesignal/config.yaml")
            try:
                with open(config_path, encoding="utf-8") as fh:
                    config = yaml.safe_load(fh) or {}
            except (OSError, ValueError):
                config = {}
            api_base = str(config.get("cloud_api_base") or "") or None
            kick_register(api_base=api_base, config_path=config_path)
        except Exception as e:  # noqa: BLE001 - the join is already done
            logger.warning("Register after join failed: %s", e)

    threading.Thread(target=_run, name="wqm1-register-kick", daemon=True).start()


def check_watched_join(now: float | None = None) -> str | None:
    """Kick registration once the watched join is proven. Returns the state seen."""
    from utils import join

    clock = time.monotonic() if now is None else now
    with _watch_lock:
        if not _watching:
            return None
        if clock > float(_watching["until"]):
            _watching.clear()
            return None
        if clock < float(_watching["next"]):
            return None
        _watching["next"] = clock + JOIN_WATCH_S
        ssid = _watching["ssid"]
    state = join.read_state()
    seen = str(state.get("state") or "")
    if seen == "joining":
        return seen
    with _watch_lock:
        _watching.clear()
    if seen == "joined" and state.get("ssid") == ssid:
        _kick_register()
    return seen


# --- Reachability probes (status file) --------------------------------------


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


# --- The server ---------------------------------------------------------------


def _default_join(ssid: str, password: str | None, *, hidden: bool) -> dict[str, Any]:
    from utils import join_service

    return join_service.request_join(ssid, password, hidden=hidden)


def _default_rescan() -> dict[str, Any]:
    from utils import join_service

    return join_service.request_rescan()


class PortalServer(ThreadingMixIn, HTTPServer):
    """Listens on one address. ``join_request`` and ``rescan_request`` reach root.

    One thread per request, at most ``MAX_CLIENTS`` at once. A Rescan waits
    up to 30 s on the root service; served in turn, it froze every phone's
    status poll and captive probe for as long as the scan ran. A request
    over the limit is closed at once, never queued, and an idle connection
    is dropped after ``CLIENT_TIMEOUT_S``, so the open network cannot pin
    the page by holding sockets.
    """

    allow_reuse_address = True
    daemon_threads = True
    # Closing on a station join must not wait for a slow rescan to answer.
    block_on_close = False
    join_request: Callable[..., dict[str, Any]] = staticmethod(_default_join)
    rescan_request: Callable[[], dict[str, Any]] = staticmethod(_default_rescan)
    networks_source: Callable[[], list[dict[str, Any]]] = staticmethod(load_networks)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._slots = threading.BoundedSemaphore(MAX_CLIENTS)

    def server_bind(self) -> None:
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        super().server_bind()

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class PortalHandler(BaseHTTPRequestHandler):
    """Captive answers, the Wi-Fi page and its API. Nothing is proxied."""

    protocol_version = "HTTP/1.0"
    server: PortalServer
    # Socket timeout for a slow or silent client (StreamRequestHandler).
    timeout = CLIENT_TIMEOUT_S

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.info("%s - " + fmt, self.address_string(), *args)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._handle()

    def do_HEAD(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._handle()

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._handle()

    # Every other verb is a 404 like any other path (I1), not a 501.
    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_POST  # noqa: N815

    def _handle(self) -> None:
        host = self.headers.get("Host", "")
        kind = classify_request(self.path, host, self.command)
        if kind == "redirect":
            self._redirect()
        elif kind == "page":
            from utils.setup_copy import pick_language

            lang = pick_language(self.headers.get("Accept-Language", ""))
            self._send(200, render_page(lang), "text/html; charset=utf-8")
        elif kind.startswith("api:"):
            self._api(kind[4:], host)
        else:
            self._send(404, not_found_page(), "text/html; charset=utf-8")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        for key, value in _SECURITY_HEADERS:
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD" and body:
            self.wfile.write(body)

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self._send(status, body, "application/json")

    def _redirect(self) -> None:
        # Empty body. "Success" or "Microsoft Connect Test" is the answer
        # that tells the phone to skip the sheet.
        self.send_response(302)
        self.send_header("Location", PORTAL_URL)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _read_json(self) -> tuple[bool, Any]:
        length_header = self.headers.get("Content-Length")
        try:
            length = int(length_header or "0")
        except ValueError:
            return False, None
        if length < 0 or length > MAX_BODY:
            return False, None
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return True, {}
        try:
            return True, json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return True, None

    def _api(self, name: str, host: str) -> None:
        if cross_site(self.command, self.headers, host):
            self._json(403, {"error": "try_again"})
            return
        if name == "networks":
            self._json(200, _networks_view(self.server.networks_source()))
        elif name == "state":
            self._json(200, state_view())
        elif name == "rescan":
            self._rescan()
        else:
            self._join()

    def _rescan(self) -> None:
        ok, _body = self._read_json()
        if not ok:
            self._json(413, {"ok": False, "error": "rescan_failed"})
            return
        answer = self.server.rescan_request()
        if answer.get("ok"):
            self._json(200, {"ok": True})
        else:
            self._json(200, {"ok": False, "error": "rescan_failed"})

    def _join(self) -> None:
        ok, body = self._read_json()
        if not ok:
            self._json(413, {"error": "try_again"})
            return
        args, problem = validate_join(body, self.server.networks_source())
        if args is None:
            self._json(400, {"error": problem})
            return
        answer = self.server.join_request(args["ssid"], args["password"], hidden=args["hidden"])
        state = answer.get("state")
        if state == "joining":
            watch_join(args["ssid"])
            self._json(202, {"state": "joining", "ssid": args["ssid"]})
        elif state == "busy":
            self._json(409, {"state": "busy", "ssid": answer.get("ssid") or args["ssid"]})
        else:
            from utils.setup_copy import en

            error = str(answer.get("error") or "")
            if error == "unavailable" or error not in en:
                error = "try_again"
            self._json(503 if error == "try_again" else 400, {"error": error})


def make_server(host: str, port: int) -> PortalServer:
    return PortalServer((host, port), PortalHandler)


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
    close. Each accepted request runs on its own thread (``PortalServer``),
    so this loop keeps turning while a rescan waits on the root service.
    """
    check = address_up if address_up is not None else (lambda: address_is_local(bind[0]))
    stopped = stop if stop is not None else (lambda: False)
    server: PortalServer | None = None
    while not stopped():
        with contextlib.suppress(Exception):
            check_watched_join()
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


# --- Installing the units on an image that has not re-run setup.sh ----------


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


def release_unit_path(name: str = _UNIT_NAME) -> Path | None:
    override = os.environ.get("WQM1_PORTAL_UNIT")
    if override and name == _UNIT_NAME:
        return Path(override)
    installed = Path("/opt/bluesignal/current/systemd") / name
    if installed.is_file():
        return installed
    packaged = Path(__file__).resolve().parents[2] / "systemd" / name
    if packaged.is_file():
        return packaged
    return None


def ensure_packaged_unit(name: str, *, as_service_user: bool) -> dict[str, Any]:
    """Install unit ``name`` if this image does not have it yet, and start it.

    Root only. An OTA flip updates ``/opt/bluesignal/current`` but does not
    re-run ``setup.sh``, so the first boot of this code copies the unit.
    An existing unit is left in place: ``setup.sh`` rewrites ``User=``.
    ``as_service_user`` writes the install user into ``User=pi``.
    """
    try:
        if os.geteuid() != 0:
            return {"ok": False, "error": "not root"}
    except AttributeError:
        return {"ok": False, "error": "not root"}
    dest = Path("/etc/systemd/system") / name
    if not dest.is_file():
        src = release_unit_path(name)
        if src is None:
            return {"ok": False, "error": "unit file missing"}
        text = src.read_text(encoding="utf-8")
        if as_service_user:
            text = text.replace("User=pi\n", f"User={_service_user()}\n", 1)
        try:
            dest.write_text(text, encoding="utf-8")
        except OSError as e:
            return {"ok": False, "error": str(e)}
        rc, _out, err = _systemctl("daemon-reload")
        if rc != 0:
            return {"ok": False, "error": err or f"daemon-reload rc {rc}"}
    rc, _out, err = _systemctl("enable", "--now", name)
    if rc != 0:
        return {"ok": False, "error": err or f"systemctl rc {rc}"}
    return {"ok": True}


def ensure_portal_service() -> dict[str, Any]:
    """The port-80 unit. Not ordered after the AP fallback — starting it from
    that fallback would deadlock on ``After=``. The process waits on the mode
    file."""
    return ensure_packaged_unit(_UNIT_NAME, as_service_user=True)


def _bind_from_env() -> tuple[str, int]:
    """``WQM1_PORTAL_BIND=host:port`` for the simulator; the hotspot otherwise."""
    raw = os.environ.get("WQM1_PORTAL_BIND", "").strip()
    if not raw:
        return PORTAL_HOST, PORTAL_PORT
    host, _, port = raw.rpartition(":")
    try:
        return host or PORTAL_HOST, int(port)
    except ValueError:
        return PORTAL_HOST, PORTAL_PORT


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
    run_loop(bind=_bind_from_env(), stop=lambda: halt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
