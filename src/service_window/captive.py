"""
Captive portal for the setup hotspot (site flow v2).

When a phone joins ``WQM1-xxxx``, it asks a known URL whether the network has
internet (iOS ``captive.apple.com/hotspot-detect.html``, Android
``/generate_204``, Windows ``/connecttest.txt``…). The hotspot's dnsmasq
answers every name with 192.168.4.1 and ``utils.captive_portal`` serves port
80 on that address, so the probe lands on the setup page. The same routes
are mounted here on 8080: a probe that still reaches this process (a
bookmark, or a leftover NAT rule) is sent to ``http://192.168.4.1/setup/``
with no port, which is port 80. A redirect to ``:8080`` is what makes some
handsets skip the captive sheet.

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

from flask import Blueprint, redirect, request
from werkzeug.wrappers import Response

from utils.captive_portal import PORTAL_URL, PROBE_PATHS, RELAY_HEADER, RELAY_VALUE

# app.py imports PROBE_PATHS so a captive check is allowed through the
# setup gate. The tuple itself lives in utils.captive_portal.
__all__ = [
    "PROBE_PATHS",
    "captive_bp",
    "foreign_host_redirect",
    "from_hotspot",
    "hotspot_up",
    "on_the_unit",
    "reset_cache",
    "setup_url",
]

captive_bp = Blueprint("captive", __name__)

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
    # Port 80, no ``:8080``. The phone's captive sheet will not follow a
    # redirect onto another port. 8080 still serves this app directly.
    return PORTAL_URL


def _from_loopback() -> bool:
    try:
        return ipaddress.ip_address(request.remote_addr or "").is_loopback
    except ValueError:
        return False


def _relayed_from_hotspot() -> bool:
    """The port-80 relay passed this request on for a phone on the hotspot.

    The relay connects from 127.0.0.1 and marks every request; it strips the
    mark from anything a phone sends. A loopback request without the mark
    did not come through the relay.
    """
    return _from_loopback() and request.headers.get(RELAY_HEADER) == RELAY_VALUE


def on_the_unit() -> bool:
    """The request was made on the unit itself, not relayed for a phone.

    That is the bench SSH tunnel (``ssh -L 8080:127.0.0.1:8080 pi@<unit>``,
    Decision 2 in docs/install-build-plan.md) or a process on the unit. The
    port-80 relay also connects from loopback, but it marks every request it
    passes on, so a phone on the hotspot never counts as on the unit.

    Nothing else on the unit may proxy the network to this port: anything
    that did would get the whole Service Window.
    """
    return _from_loopback() and RELAY_HEADER not in request.headers


def from_hotspot() -> bool:
    """The request came from a phone on this unit's own setup hotspot:
    directly on 8080 from the hotspot subnet, or through the port-80 relay
    (which is how the phone's captive sheet loads the setup page)."""
    if not hotspot_up():
        return False
    if _virtual():
        return True
    if _relayed_from_hotspot():
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
