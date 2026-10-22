"""
PIN-based authentication for the service window.
"""

import functools
import hmac
import time
from collections.abc import Callable
from typing import Any

from flask import (
    Blueprint,
    current_app,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask.typing import ResponseReturnValue

auth_bp = Blueprint("auth", __name__)

# In-memory rate limiting: {ip: [timestamps]} within the last minute.
_attempts: dict[str, list[float]] = {}
_MAX_ATTEMPTS = 5
_WINDOW_S = 60.0

# Escalation: a 4-digit PIN at 5 tries/minute falls in ~33 hours. Keep the
# per-minute throttle, and after _LOCKOUT_AFTER failed attempts within an hour
# lock the address out for _LOCKOUT_S. {ip: [failure timestamps]} / {ip: until}.
_failures: dict[str, list[float]] = {}
_lockouts: dict[str, float] = {}
_LOCKOUT_AFTER = 15
_LOCKOUT_WINDOW_S = 3600.0
_LOCKOUT_S = 900.0


def login_required(f: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator: redirect to login if session not authenticated."""

    @functools.wraps(f)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if (
            not session.get("pin_verified")
            and not _first_setup_on_hotspot()
            and not _wifi_setup_on_hotspot()
        ):
            return redirect(url_for("auth.login", next=request.path))
        return f(*args, **kwargs)

    return wrapper


def _first_setup_on_hotspot() -> bool:
    """A phone on this unit's own setup hotspot, during FIRST setup only,
    skips the PIN prompt (site flow v2).

    Why that is no weaker: until the wizard's PIN step runs, the PIN is the
    factory ``1234`` — printed in every manual, so asking for it proves
    nothing. What does prove presence is joining the hotspot, which takes the
    unit's own WPA2 passphrase from the card in its box. The exemption ends
    the moment the factory PIN is replaced (``needs_setup`` goes false), and
    never applies on any other network.
    """
    try:
        from service_window.captive import from_hotspot
        from service_window.routes.setup import needs_setup

        return bool(needs_setup(current_app.config)) and from_hotspot()
    except Exception:
        return False


_HOTSPOT_OPEN = frozenset({"setup.welcome", "setup.network", "setup.card_status"})


def _wifi_setup_on_hotspot() -> bool:
    """The setup pages, on this unit's own hotspot, never ask for a PIN.

    The hotspot only comes up while the unit is offline, and joining it takes
    the WPA2 passphrase on the card in the unit's box. That is the proof of
    presence; a PIN on top of it is what sent an installer round in circles
    on 2026-10-21 (set a PIN, get dropped, rejoin, get asked for it). Only
    the Wi-Fi page (and the landing redirect to it) is open this way. The
    PIN page is not: changing a PIN still needs the PIN once setup is done.
    """
    try:
        from service_window.captive import from_hotspot

        return request.endpoint in _HOTSPOT_OPEN and from_hotspot()
    except Exception:
        return False


def factory_pin_refused() -> bool:
    """The factory PIN never logs in from the owner's network.

    Setup no longer asks for a PIN, so a unit online on a site network
    still carries 1234, which is printed in every manual. It works on the
    unit's own hotspot (passphrase-gated) and before setup has finished (the
    bench); anywhere else it is refused.
    """
    try:
        from service_window.captive import from_hotspot
        from service_window.routes.setup import _FACTORY_PIN, needs_setup

        if str(current_app.config.get("PIN")) != _FACTORY_PIN:
            return False
        return not needs_setup(current_app.config) and not from_hotspot()
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
    error = None
    if request.method == "POST":
        ip = request.remote_addr or "unknown"
        locked = _is_locked_out(ip)
        if locked > 0:
            error = f"Too many attempts. Locked out for {int(locked // 60) + 1} minutes."
        elif _is_rate_limited(ip):
            error = "Too many attempts. Try again in a minute."
        else:
            _record_attempt(ip)
            pin = request.form.get("pin", "")
            if factory_pin_refused():
                error = (
                    "This unit still has the factory PIN, which only works on its own "
                    "setup hotspot. Set up this unit from BlueSignal Cloud."
                )
                return render_template("login.html", error=error)
            if hmac.compare_digest(pin.encode(), str(current_app.config["PIN"]).encode()):
                _clear_failures(ip)
                session["pin_verified"] = True
                next_url = request.args.get("next", "/")
                return redirect(next_url)
            _record_failure(ip)
            error = "Incorrect PIN."
    return render_template("login.html", error=error)


@auth_bp.route("/logout")
def logout() -> ResponseReturnValue:
    session.clear()
    return redirect(url_for("auth.login"))
