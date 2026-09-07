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
        if not session.get("pin_verified"):
            return redirect(url_for("auth.login", next=request.path))
        return f(*args, **kwargs)

    return wrapper


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
