"""
Guided first-boot setup wizard — the installer-first commissioning path.

Replaces the CLI wizard as the primary way a dealer/installer commissions a
unit from a phone browser: set a real PIN (the shipped `1234` is banned),
confirm device identity (QR), connect the cloud API key, watch live sensor
checks come green, and finish on a go/no-go checklist. Config writes go
through the atomic YAML editor; the firmware is restarted through the
command socket instead of telling anyone to run systemctl.

While the unit still has the factory PIN and setup hasn't been completed,
every page redirects here (see app.py) — a unit can't be left half set up
by accident.
"""

import re

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
from service_window.config_editor import read_config, update_config, update_config_section
from service_window.db_reader import DBReader
from service_window.health import sensor_cards, system_cards, worst_status

setup_bp = Blueprint("setup", __name__, url_prefix="/setup")

_PIN_RE = re.compile(r"^\d{4,8}$")
_CLOUD_KEY_RE = re.compile(r"^[0-9a-zA-Z]{16,128}$")
_FACTORY_PIN = "1234"

STEPS = ["welcome", "pin", "identity", "cloud", "sensors", "done"]


def setup_completed(config_path: str) -> bool:
    sw = read_config(config_path).get("service_window") or {}
    return bool(sw.get("setup_completed"))


def needs_setup(app_config: dict) -> bool:
    """Factory PIN still in place and the wizard never finished."""
    return app_config.get("PIN") == _FACTORY_PIN and not setup_completed(
        app_config.get("CONFIG_PATH", "/etc/bluesignal/config.yaml")
    )


def _identity() -> dict[str, str]:
    from service_window.routes.provision import _get_identity

    return _get_identity()


@setup_bp.route("/")
@login_required
def welcome() -> str:
    return render_template("setup/welcome.html", steps=STEPS, step="welcome")


@setup_bp.route("/pin", methods=["GET", "POST"])
@login_required
def pin() -> ResponseReturnValue:
    if request.method == "POST":
        new_pin = request.form.get("pin", "").strip()
        confirm = request.form.get("pin_confirm", "").strip()
        if not _PIN_RE.match(new_pin):
            flash("PIN must be 4–8 digits.", "error")
        elif new_pin == _FACTORY_PIN:
            flash(
                "That's the factory PIN — pick your own so only you can access this unit.", "error"
            )
        elif new_pin != confirm:
            flash("PINs don't match — try again.", "error")
        else:
            update_config_section(
                current_app.config["CONFIG_PATH"], "service_window", {"pin": new_pin}
            )
            current_app.config["PIN"] = new_pin
            flash("PIN set. Keep it with the site records.", "success")
            return redirect(url_for("setup.identity"))
    return render_template("setup/pin.html", steps=STEPS, step="pin")


@setup_bp.route("/identity")
@login_required
def identity() -> str:
    return render_template(
        "setup/identity.html", steps=STEPS, step="identity", identity=_identity()
    )


@setup_bp.route("/cloud", methods=["GET", "POST"])
@login_required
def cloud() -> ResponseReturnValue:
    config = read_config(current_app.config["CONFIG_PATH"])
    if request.method == "POST":
        if request.form.get("skip"):
            flash(
                "Cloud connection skipped — you can add the key later under Provisioning.", "info"
            )
            return redirect(url_for("setup.sensors"))
        key = request.form.get("api_key", "").strip()
        if not _CLOUD_KEY_RE.match(key):
            flash("That doesn't look like a device API key (16–128 letters/numbers).", "error")
        else:
            update_config(
                current_app.config["CONFIG_PATH"], {"cloud_enabled": True, "api_key": key}
            )
            flash("Cloud key saved — it takes effect when setup finishes.", "success")
            return redirect(url_for("setup.sensors"))
    return render_template(
        "setup/cloud.html",
        steps=STEPS,
        step="cloud",
        key_set=bool(config.get("api_key")),
        identity=_identity(),
    )


@setup_bp.route("/sensors")
@login_required
def sensors() -> str:
    config = read_config(current_app.config["CONFIG_PATH"])
    db = DBReader(current_app.config["DB_PATH"])
    try:
        readings = db.get_readings(limit=30)
    except Exception:
        readings = []
    cards = sensor_cards(readings, orp_enabled=bool(config.get("orp_enabled")), config=config)
    ready = all(c["status"] in ("ok", "disabled") for c in cards.values())
    return render_template(
        "setup/sensors.html",
        steps=STEPS,
        step="sensors",
        cards=cards,
        ready=ready,
        have_readings=bool(readings),
    )


@setup_bp.route("/done", methods=["GET", "POST"])
@login_required
def done() -> ResponseReturnValue:
    config = read_config(current_app.config["CONFIG_PATH"])
    db = DBReader(current_app.config["DB_PATH"])
    try:
        readings = db.get_readings(limit=30)
        count = db.get_reading_count()
        session = db.get_lorawan_session()
    except Exception:
        readings, count, session = [], 0, None

    s_cards = sensor_cards(readings, orp_enabled=bool(config.get("orp_enabled")), config=config)
    sys_cards = system_cards(readings, config, session, count)
    checklist = {**s_cards, **sys_cards}
    overall = worst_status(checklist)

    if request.method == "POST":
        update_config_section(
            current_app.config["CONFIG_PATH"], "service_window", {"setup_completed": True}
        )
        # Apply the new config (cloud key etc.) by restarting the firmware via
        # its command socket — no SSH, no systemctl.
        result = send_command(current_app.config["CMD_SOCK"], "restart")
        if result.get("ok"):
            flash("Setup complete — the unit is restarting to apply everything.", "success")
        else:
            flash(
                "Setup saved. The monitoring service will pick it up on its next restart.",
                "info",
            )
        return redirect(url_for("status.index"))

    return render_template(
        "setup/done.html",
        steps=STEPS,
        step="done",
        checklist=checklist,
        overall=overall,
    )
