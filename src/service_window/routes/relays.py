"""Relay manual control page.

The irrigation hold's channel (control/irrigation_hold.py) is labelled and
its manual buttons are disabled while the hold is enabled: a manual OFF would
silently release a hold. The firmware refuses the command either way; the
page just does not offer it. There is no edit UI for the hold here — the
cloud device page is its editor, and this process cannot read the remote
config overlay the settings arrive in, so it asks the firmware
(``irrigation_hold_status``) instead.
"""

from flask import (
    Blueprint,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask.typing import ResponseReturnValue

from control.irrigation_hold import status_line
from service_window.auth import login_required
from service_window.cmd_client import send_command

relays_bp = Blueprint("relays", __name__, url_prefix="/relays")


def _hold_status() -> dict | None:
    """The live hold snapshot, or None when the firmware cannot be asked or
    the hold is off (the page then shows four ordinary relays)."""
    result = send_command(current_app.config["CMD_SOCK"], "irrigation_hold_status")
    if not result.get("ok") or not result.get("enabled"):
        return None
    relay = result.get("relay")
    if not isinstance(relay, int) or not 1 <= relay <= 4 or result.get("error"):
        return None
    return {**result, "line": status_line(result)}


@relays_bp.route("/")
@login_required
def index() -> str:
    return render_template("relays.html", hold=_hold_status())


@relays_bp.route("/set", methods=["POST"])
@login_required
def set_relay() -> ResponseReturnValue:
    try:
        channel = int(request.form["channel"])
        state = request.form["state"] == "on"
    except (KeyError, ValueError):
        flash("Invalid relay command.", "error")
        return redirect(url_for("relays.index"))

    if channel < 1 or channel > 4:
        flash("Relay channel must be 1-4.", "error")
        return redirect(url_for("relays.index"))

    sock_path = current_app.config["CMD_SOCK"]
    result = send_command(sock_path, "relay_set", channel=channel, state=state)

    if result.get("ok"):
        flash(f"Relay {channel} {'ON' if state else 'OFF'}", "success")
    else:
        flash(f"Command failed: {result.get('error', 'unknown')}", "error")
    return redirect(url_for("relays.index"))


@relays_bp.route("/api/set", methods=["POST"])
@login_required
def api_set_relay() -> ResponseReturnValue:
    """JSON API for relay control (used by JS)."""
    data = request.get_json(silent=True) or {}
    channel = data.get("channel")
    state = data.get("state")

    if not isinstance(channel, int) or channel < 1 or channel > 4:
        return jsonify({"ok": False, "error": "channel must be 1-4"}), 400
    if not isinstance(state, bool):
        return jsonify({"ok": False, "error": "state must be boolean"}), 400

    sock_path = current_app.config["CMD_SOCK"]
    result = send_command(sock_path, "relay_set", channel=channel, state=state)
    status = 200 if result.get("ok") else 502
    return jsonify(result), status
