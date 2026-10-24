"""
Local Service Window access.

Captain, 2026-10-24: there is no PIN anywhere. Setup is Wi-Fi only, on this
unit's own hotspot. After setup the local page stays off; owners use
BlueSignal Cloud. See docs/launch-audit.md (M7) and SECURITY.md.
"""

import functools
import time
from collections.abc import Callable
from typing import Any

from flask import (
    Blueprint,
    redirect,
    request,
    session,
    url_for,
)
from flask.typing import ResponseReturnValue

auth_bp = Blueprint("auth", __name__)

# In-memory rate limiting is kept so a leftover caller cannot hammer /login.
# /login itself no longer accepts a PIN.
_attempts: dict[str, list[float]] = {}
_MAX_ATTEMPTS = 5
_WINDOW_S = 60.0

_failures: dict[str, list[float]] = {}
_lockouts: dict[str, float] = {}
_LOCKOUT_AFTER = 15
_LOCKOUT_WINDOW_S = 3600.0
_LOCKOUT_S = 900.0

# Setup routes a phone on this unit's own hotspot may hit with no PIN.
# setup.pin is not listed: there is no PIN to set or enter.
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
    """Decorator: hotspot setup pages are open; everything else needs a
    leftover test session or is refused. There is no PIN prompt."""

    @functools.wraps(f)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if _wifi_setup_on_hotspot():
            return f(*args, **kwargs)
        if session.get("pin_verified"):
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


def _is_locked_out(ip: str, now: float | None = None) -> float:
    """Seconds of lockout remaining for ``ip`` (0 = not locked out)."""
    now = time.time() if now is None else now
    until = _lockouts.get(ip)
    if until is None:
        return 0.0
    if now >= until:
        _lockouts.pop(ip, None)
        return 0.0
    return until - now


def _record_failure(ip: str, now: float | None = None) -> None:
    """Count a wrong PIN; lock the address out once the hourly budget is spent."""
    now = time.time() if now is None else now
    recent = [t for t in _failures.get(ip, []) if now - t < _LOCKOUT_WINDOW_S]
    recent.append(now)
    _failures[ip] = recent
    if len(recent) >= _LOCKOUT_AFTER:
        _lockouts[ip] = now + _LOCKOUT_S
        _failures.pop(ip, None)


def _clear_failures(ip: str) -> None:
    _failures.pop(ip, None)
    _lockouts.pop(ip, None)


def _is_rate_limited(ip: str) -> bool:
    """Check if IP has exceeded attempt limit."""
    now = time.time()
    if _is_locked_out(ip, now) > 0:
        return True
    attempts = _attempts.get(ip, [])
    attempts = [t for t in attempts if now - t < _WINDOW_S]
    if attempts:
        _attempts[ip] = attempts
    else:
        _attempts.pop(ip, None)
    _prune_stale_ips(now)
    return len(attempts) >= _MAX_ATTEMPTS


def _prune_stale_ips(now: float) -> None:
    """Remove IPs with no recent attempts to prevent unbounded dict growth."""
    stale = [ip for ip, ts in _attempts.items() if not ts or now - ts[-1] >= _WINDOW_S]
    for ip in stale:
        del _attempts[ip]


def _record_attempt(ip: str) -> None:
    """Record a login attempt."""
    if ip not in _attempts:
        _attempts[ip] = []
    _attempts[ip].append(time.time())


@auth_bp.route("/login", methods=["GET", "POST"])
def login() -> ResponseReturnValue:
    """Closed. There is no PIN to enter on the hotspot or the LAN."""
    return service_window_closed()


@auth_bp.route("/logout")
def logout() -> ResponseReturnValue:
    session.clear()
    return redirect(url_for("auth.login"))
