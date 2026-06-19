"""LoRaWAN configuration page."""

import re

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask.typing import ResponseReturnValue

from service_window.auth import login_required
from service_window.config_editor import read_config, update_config
from service_window.db_reader import DBReader

lora_bp = Blueprint("lora", __name__, url_prefix="/lora")

_HEX32_RE = re.compile(r"^[0-9a-fA-F]{32}$")


@lora_bp.route("/")
@login_required
def index() -> str:
    db = DBReader(current_app.config["DB_PATH"])
    session = db.get_lorawan_session()
    config = read_config(current_app.config["CONFIG_PATH"])
    app_key = config.get("app_key", "")
    is_default = app_key == "00000000000000000000000000000000" or not app_key
    return render_template(
        "lora.html",
        session=session,
        app_key=app_key,
        is_default_key=is_default,
    )


@lora_bp.route("/appkey", methods=["POST"])
@login_required
def set_appkey() -> ResponseReturnValue:
    app_key = request.form.get("app_key", "").strip()
    if not _HEX32_RE.match(app_key):
        flash("AppKey must be exactly 32 hex characters.", "error")
        return redirect(url_for("lora.index"))

    update_config(current_app.config["CONFIG_PATH"], {"app_key": app_key})
    flash("AppKey updated. Restart the firmware to apply.", "success")
    return redirect(url_for("lora.index"))
