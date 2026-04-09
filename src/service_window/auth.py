"""
PIN-based authentication for the service window.
"""

import functools
import time

from flask import (
    Blueprint,
    current_app,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

auth_bp = Blueprint("auth", __name__)

# In-memory rate limiting: {ip: [timestamps]}
_attempts: dict[str, list[float]] = {}
_MAX_ATTEMPTS = 5
_WINDOW_S = 60.0


def login_required(f):
    """Decorator: redirect to login if session not authenticated."""

    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("pin_verified"):
            return redirect(url_for("auth.login", next=request.path))
        return f(*args, **kwargs)

    return wrapper


def _is_rate_limited(ip: str) -> bool:
    """Check if IP has exceeded attempt limit."""
    now = time.time()
    attempts = _attempts.get(ip, [])
    # Prune old attempts
    attempts = [t for t in attempts if now - t < _WINDOW_S]
    _attempts[ip] = attempts
    return len(attempts) >= _MAX_ATTEMPTS


def _record_attempt(ip: str) -> None:
    """Record a login attempt."""
    if ip not in _attempts:
        _attempts[ip] = []
    _attempts[ip].append(time.time())


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        ip = request.remote_addr or "unknown"
        if _is_rate_limited(ip):
            error = "Too many attempts. Try again in a minute."
        else:
            _record_attempt(ip)
            pin = request.form.get("pin", "")
            if pin == current_app.config["PIN"]:
                session["pin_verified"] = True
                next_url = request.args.get("next", "/")
                return redirect(next_url)
            error = "Incorrect PIN."
    return render_template("login.html", error=error)


@auth_bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))
