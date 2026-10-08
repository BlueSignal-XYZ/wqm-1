"""
Captive portal for the setup hotspot (site flow v2).

When a phone joins ``WQM1-xxxx``, it asks a known URL whether the network has
internet (iOS ``captive.apple.com/hotspot-detect.html``, Android
``/generate_204``, Windows ``/connecttest.txt``…). The hotspot's dnsmasq
answers every name with 192.168.4.1 and a NAT rule sends port 80 here
(``setup.sh``, ``utils.netctl.ensure_captive_redirect``), so the probe lands
on this app — and anything but the expected answer makes the phone open the
setup page by itself. The installer types no address.

Two pieces, both inert unless the hotspot is up:

* the probe paths redirect to the setup page;
* a request for any OTHER host name while the hotspot is up (the phone
  resolving ``example.com`` to us) is sent to the setup page on the hotspot's
  own address, so the session cookie lives on one origin.

A simulated unit (``WQM1_VIRTUAL_NET``) is reached through localhost or a
tunnel, never through a hotspot, so the host rule is off there and the probe
paths redirect relatively.
"""

from __future__ import annotations

import ipaddress
import time

from flask import Blueprint, current_app, redirect, request
from werkzeug.wrappers import Response

captive_bp = Blueprint("captive", __name__)

PROBE_PATHS = (
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

_AP_CACHE_S = 5.0
_ap_cache: dict[str, float | bool] = {"at": 0.0, "value": False}


def hotspot_up() -> bool:
    """Whether the setup AP is up, cached briefly — this runs on every
    request and ``nmcli`` is not free on a Pi Zero."""
    now = time.monotonic()
    if now - float(_ap_cache["at"]) < _AP_CACHE_S:
        return bool(_ap_cache["value"])
    from utils.netctl import ap_active

    try:
        value = bool(ap_active())
    except Exception:
        value = False
    _ap_cache.update(at=now, value=value)
    return value


def reset_cache() -> None:
    _ap_cache.update(at=0.0, value=False)


def _virtual() -> bool:
    from utils import netsim

    return netsim.is_virtual()


def setup_url() -> str:
    if _virtual():
        return "/setup/"
    from utils.netctl import AP_ADDRESS

    port = int(current_app.config.get("SERVICE_PORT") or 8080)
    return f"http://{AP_ADDRESS}:{port}/setup/"


def from_hotspot() -> bool:
    """The request came from a phone on this unit's own setup hotspot."""
    if not hotspot_up():
        return False
    if _virtual():
        return True
    from utils.netctl import AP_NETWORK

    try:
        return ipaddress.ip_address(request.remote_addr or "") in ipaddress.ip_network(AP_NETWORK)
    except ValueError:
        return False


def _is_own_host(host: str) -> bool:
    name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    name = name.strip("[]").lower()
    if not name or name == "localhost" or name.endswith(".local"):
        return True
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


def foreign_host_redirect() -> Response | None:
    """before_request hook: while the hotspot is up, a request addressed to
    some other site's name is the phone's DNS answering with us — send it to
    the setup page."""
    if _virtual() or not hotspot_up():
        return None
    if request.path.startswith("/static"):
        return None
    if _is_own_host(request.host or ""):
        return None
    return redirect(setup_url(), code=302)


@captive_bp.route("/hotspot-detect.html")
@captive_bp.route("/library/test/success.html")
@captive_bp.route("/generate_204")
@captive_bp.route("/gen_204")
@captive_bp.route("/connecttest.txt")
@captive_bp.route("/ncsi.txt")
@captive_bp.route("/redirect")
@captive_bp.route("/canonical.html")
@captive_bp.route("/success.txt")
@captive_bp.route("/check_network_status.txt")
def probe() -> Response:
    # Never the "Success"/204 the phone hopes for: that answer is what tells
    # it to skip the portal.
    return redirect(setup_url(), code=302)
