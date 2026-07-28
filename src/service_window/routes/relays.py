"""Relay manual control page."""

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

from service_window.auth import login_required
from service_window.cmd_client import send_command
from utils.config import (
    CHANNEL_ROLES,
    CONTACTS,
    FAIL_SAFE_STATES,
    LOAD_TYPES,
    RELAY_MAX_CURRENT_A_NC,
    RELAY_MAX_CURRENT_A_NO,
    validate_channel_config,
)

relays_bp = Blueprint("relays", __name__, url_prefix="/relays")


@relays_bp.route("/")
@login_required
def index() -> str:
    sock_path = current_app.config["CMD_SOCK"]
    status = send_command(sock_path, "channel_status")
    control = status.get("status") if status.get("ok") else None
    return render_template("relays.html", control=control)


@relays_bp.route("/commission/<int:channel>")
@login_required
def commission(channel: int) -> ResponseReturnValue:
    """
    Commissioning wizard for one channel.

    Walks role -> contact -> fail-safe direction -> load type + current ->
    dwell times -> test-fire. The channel stays inert throughout; only a
    confirmed test-fire records that the wiring was actually verified.
    """
    if channel < 1 or channel > 4:
        flash("Relay channel must be 1-4.", "error")
        return redirect(url_for("relays.index"))

    sock_path = current_app.config["CMD_SOCK"]
    status = send_command(sock_path, "channel_status")
    return render_template(
        "relays_commission.html",
        channel=channel,
        roles=CHANNEL_ROLES,
        contacts=CONTACTS,
        fail_safe_states=FAIL_SAFE_STATES,
        load_types=LOAD_TYPES,
        max_a_no=RELAY_MAX_CURRENT_A_NO,
        max_a_nc=RELAY_MAX_CURRENT_A_NC,
        control=status.get("status") if status.get("ok") else None,
    )


@relays_bp.route("/commission/<int:channel>/validate", methods=["POST"])
@login_required
def validate(channel: int) -> ResponseReturnValue:
    """Dry-run the operator's answers through the same validator the daemon uses."""
    data = request.get_json(silent=True) or request.form.to_dict()
    raw = dict(data)
    raw["channel"] = channel
    cfg, errors = validate_channel_config(raw)
    return jsonify({"ok": not errors, "errors": errors, "valid": cfg is not None})


@relays_bp.route("/commission/<int:channel>/test-fire", methods=["POST"])
@login_required
def test_fire(channel: int) -> ResponseReturnValue:
    """
    Pulse the channel so the installer can watch the load move.

    Requires the operator to type the exact confirmation string. The daemon
    re-checks it — this is not a client-side gate.
    """
    data = request.get_json(silent=True) or request.form.to_dict()
    sock_path = current_app.config["CMD_SOCK"]
    result = send_command(
        sock_path,
        "channel_test_fire",
        channel=channel,
        confirm=data.get("confirm", ""),
        duration_s=data.get("duration_s", 2.0),
    )
    return jsonify(result), (200 if result.get("ok") else 400)


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
