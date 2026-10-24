"""
Local Service Window access.

Captain, 2026-10-24: there is no PIN anywhere. Setup is Wi-Fi only, on this
unit's own hotspot. After setup the local page stays off; owners use
BlueSignal Cloud. See docs/launch-audit.md (M7) and SECURITY.md.
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
    """Hotspot setup pages are open. Everything else is refused.

    Tests may set ``TESTING`` and ``TEST_OPEN_WINDOW`` on the app. Both are
    off in production; a leftover session cookie is not a way in.
    """

    @functools.wraps(f)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if _wifi_setup_on_hotspot():
            return f(*args, **kwargs)
        if current_app.config.get("TESTING") and current_app.config.get("TEST_OPEN_WINDOW"):
            return f(*args, **kwargs)
        return service_window_closed()

    return wrapper


def _wifi_setup_on_hotspot() -> bool:
    """The setup pages, on this unit's own hotspot, never ask for a PIN.

    The hotspot only comes up while the unit is offline, and joining it takes
    the WPA2 passphrase on the card in the unit's box. That is the proof of
    presence. Owners use BlueSignal Cloud; there is no PIN on the hotspot
    and none later.
    """
    try:
        from service_window.captive import from_hotspot

        return request.endpoint in _HOTSPOT_OPEN and from_hotspot()
    except Exception:
        return False


@auth_bp.route("/login", methods=["GET", "POST"])
def login() -> ResponseReturnValue:
    """Closed. There is no PIN to enter on the hotspot or the LAN."""
    return service_window_closed()


@auth_bp.route("/logout")
def logout() -> ResponseReturnValue:
    session.clear()
    return redirect(url_for("auth.login"))
