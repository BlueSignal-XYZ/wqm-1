"""
Service Window — the Home Assistant page.

Shows whether the MQTT publisher is on, connected to the owner's broker and
has announced its entities, and takes the broker settings: host, port,
username, password, TLS, discovery prefix. The password is write-only here —
the form shows *set / not set* and a blank field means *keep what is there*;
nothing secret is ever rendered back into HTML, and the cloud heartbeat
carries only ``{enabled, connected}``.

Every value goes through the firmware's own settings schema before it is
written; the keys are hot, so a ``config_reload`` makes the publisher
reconnect within seconds and no restart is needed.
"""

from __future__ import annotations

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask.typing import ResponseReturnValue

from service_window.auth import login_required
from service_window.cmd_client import send_command
from service_window.config_editor import read_config, update_config
from utils.config import Settings, validate_values

home_assistant_bp = Blueprint("home_assistant", __name__, url_prefix="/home-assistant")

_DEFAULTS = Settings()


def _status() -> dict:
    result = send_command(current_app.config["CMD_SOCK"], "home_assistant_status")
    if not isinstance(result, dict) or not result.get("ok", False):
        return {"available": False, "error": (result or {}).get("error") if result else None}
    out = dict(result)
    out["available"] = True
    out.pop("password", None)  # belt and braces: the firmware never sends it
    return out


@home_assistant_bp.route("/")
@login_required
def index() -> str:
    config = read_config(current_app.config["CONFIG_PATH"])
    values = {
        "mqtt_enabled": bool(config.get("mqtt_enabled", False)),
        "mqtt_host": str(config.get("mqtt_host", "") or ""),
        "mqtt_port": int(config.get("mqtt_port", 0) or 0),
        "mqtt_username": str(config.get("mqtt_username", "") or ""),
        "mqtt_tls": bool(config.get("mqtt_tls", False)),
        "mqtt_discovery_prefix": str(
            config.get("mqtt_discovery_prefix", _DEFAULTS.mqtt_discovery_prefix) or ""
        ),
        "password_set": bool(config.get("mqtt_password")),
    }
    return render_template("home_assistant.html", status=_status(), values=values)


@home_assistant_bp.route("/save", methods=["POST"])
@login_required
def save() -> ResponseReturnValue:
    raw: dict = {
        "mqtt_enabled": request.form.get("mqtt_enabled") == "on",
        "mqtt_host": request.form.get("mqtt_host", "").strip(),
        "mqtt_username": request.form.get("mqtt_username", "").strip(),
        "mqtt_tls": request.form.get("mqtt_tls") == "on",
        "mqtt_discovery_prefix": request.form.get("mqtt_discovery_prefix", "").strip()
        or _DEFAULTS.mqtt_discovery_prefix,
    }
    port = request.form.get("mqtt_port", "").strip()
    if port:
        try:
            raw["mqtt_port"] = int(port)
        except ValueError:
            flash("Port: not a valid number.", "error")
            return redirect(url_for("home_assistant.index"))
    else:
        raw["mqtt_port"] = 0
    password = request.form.get("mqtt_password", "")
    if password:
        # Blank keeps the stored password; "clear" in the dedicated box empties it.
        raw["mqtt_password"] = password
    if request.form.get("clear_password") == "on":
        raw["mqtt_password"] = ""
    if raw["mqtt_enabled"] and not raw["mqtt_host"]:
        flash("Enter the broker's address before turning Home Assistant on.", "error")
        return redirect(url_for("home_assistant.index"))

    accepted, errors = validate_values(raw)
    if errors:
        for err in errors:
            flash(err, "error")
        return redirect(url_for("home_assistant.index"))
    update_config(current_app.config["CONFIG_PATH"], accepted)
    # Every key is hot: ask the firmware to reload so the publisher reconnects
    # now rather than on the next sample. The reload's failure is not ours.
    send_command(current_app.config["CMD_SOCK"], "config_reload")
    flash("Saved and applied.", "success")
    return redirect(url_for("home_assistant.index"))
