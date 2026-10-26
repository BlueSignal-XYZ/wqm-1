"""
Local Service Window access.

Captain, 2026-10-24: there is no PIN anywhere. Setup is Wi-Fi only, on this
unit's own hotspot. After setup the local page stays off the network;
owners use BlueSignal Cloud. Staff reach it at the bench over an SSH tunnel
to 127.0.0.1:8080. See docs/launch-audit.md (M7) and SECURITY.md.
"""

import functools
from collections.abc import Callable
from typing import Any

from flask import (
    Blueprint,
    current_app,
    redirect,
    request,
    session,
    url_for,
)
from flask.typing import ResponseReturnValue

auth_bp = Blueprint("auth", __name__)

# Setup routes a phone on this unit's own hotspot may hit with no PIN.
_HOTSPOT_OPEN = frozenset(
    {
        "setup.welcome",
        "setup.network",
        "setup.card_status",
        "setup.identity",
        "setup.sensors",
        "setup.cloud",
        "setup.done",
        "setup.join_status",
    }
)

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]"})
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

_CLOSED_BODY = (
    "<!DOCTYPE html><html lang=en><head><meta charset=utf-8>"
    "<meta name=viewport content='width=device-width, initial-scale=1'>"
    "<title>BlueSignal Cloud</title></head><body>"
    "<p>This unit has no local sign-in. Use BlueSignal Cloud.</p>"
    "</body></html>"
)


def service_window_closed() -> ResponseReturnValue:
    """LAN (and any non-hotspot) request: do not serve the local page."""
    return _CLOSED_BODY, 403, {"Content-Type": "text/html; charset=utf-8"}


def login_required(f: Callable[..., Any]) -> Callable[..., Any]:
    """Hotspot setup pages are open, and every page is open on the unit
    itself (the bench SSH tunnel). Everything else is refused.

    Tests may set ``TESTING`` and ``TEST_OPEN_WINDOW`` on the app. Both are
    off in production; a leftover session cookie is not a way in.
    """

    @functools.wraps(f)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if _wifi_setup_on_hotspot():
            return f(*args, **kwargs)
        if _on_the_unit():
            return f(*args, **kwargs)
        if current_app.config.get("TESTING") and current_app.config.get("TEST_OPEN_WINDOW"):
            return f(*args, **kwargs)
        return service_window_closed()

    return wrapper


def _wifi_setup_on_hotspot() -> bool:
    """The setup pages, on this unit's own hotspot, never ask for a PIN.

    The hotspot comes up when wlan0 has no usable station IPv4. Ethernet and
    LTE are not this check: a unit online on those still raises the AP if
    wlan0 has no station. Joining the hotspot takes the WPA2 passphrase on
    the card in the unit's box. That is the proof of presence. Owners use
    BlueSignal Cloud; there is no PIN on the hotspot and none later.
    """
    try:
        from service_window.captive import from_hotspot

        return request.endpoint in _HOTSPOT_OPEN and from_hotspot() and _same_origin_write()
    except Exception:
        return False


def _on_the_unit() -> bool:
    """Bench access: the SSH tunnel to 127.0.0.1:8080 (Decision 2, 24 Oct
    2026). A phone on the home network never arrives from loopback, and the
    port-80 relay marks what it passes on for the hotspot."""
    try:
        from service_window.captive import on_the_unit

        return on_the_unit() and _bench_request_is_ours()
    except Exception:
        return False


def _bench_request_is_ours() -> bool:
    """The tunnel's own browser tab, not another site that browser opened.

    While the tunnel is up, any page in the staff browser can send a request
    to 127.0.0.1:8080, and it arrives from loopback like the tunnel does.
    Two checks keep those out:

    * Host must name loopback. A DNS-rebinding page arrives with its own
      name as Host, and could otherwise read every page.
    * A request that changes something must be same-origin
      (``_same_origin_write``).

    curl and scripts on the bench send neither header, so they pass.
    """
    host = (request.host or "").lower()
    name = host.split("]", 1)[0] + "]" if host.startswith("[") else host.rsplit(":", 1)[0]
    if name not in _LOOPBACK_HOSTS:
        return False
    return _same_origin_write()


def _same_origin_write() -> bool:
    """A request that changes something came from this page, not another site.

    The hotspot needs it as much as the tunnel does: a phone on the setup
    hotspot often keeps cellular, so a page it loads elsewhere can POST to
    http://192.168.4.1/setup/network. The port-80 relay passes the phone's
    Host header through, so Origin is compared with the Host the browser
    used. A client that sends neither header (curl) passes.
    """
    if request.method in _SAFE_METHODS:
        return True
    site = request.headers.get("Sec-Fetch-Site")
    if site is not None and site not in {"same-origin", "none"}:
        return False
    origin = request.headers.get("Origin")
    host = (request.host or "").lower()
    return origin is None or origin.lower() in {f"http://{host}", f"https://{host}"}


@auth_bp.route("/login", methods=["GET", "POST"])
def login() -> ResponseReturnValue:
    """Closed. There is no PIN to enter on the hotspot or the LAN."""
    return service_window_closed()


@auth_bp.route("/logout")
def logout() -> ResponseReturnValue:
    session.clear()
    return redirect(url_for("auth.login"))
