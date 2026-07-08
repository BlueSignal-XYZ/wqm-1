"""Dashboard / status page — plain-language traffic lights first, numbers second."""

import platform
import time

from flask import Blueprint, current_app, render_template

from service_window import read_firmware_version
from service_window.auth import login_required
from service_window.config_editor import read_config
from service_window.db_reader import DBReader
from service_window.health import sensor_cards, system_cards, worst_status

status_bp = Blueprint("status", __name__)

_START_TIME = time.monotonic()


@status_bp.route("/")
@login_required
def index() -> str:
    db = DBReader(current_app.config["DB_PATH"])
    try:
        readings = db.get_readings(limit=30)
        count = db.get_reading_count()
        session = db.get_lorawan_session()
    except Exception:
        readings, count, session = [], 0, None
    latest = readings[0] if readings else None
    config = read_config(current_app.config["CONFIG_PATH"])

    s_cards = sensor_cards(readings, orp_enabled=bool(config.get("orp_enabled")))
    sys_cards = system_cards(readings, config, session, count)
    overall = worst_status({**s_cards, **sys_cards})

    lora_joined = bool(session and session.get("joined"))
    lora_fcnt = session.get("fcnt_up", 0) if session else 0

    return render_template(
        "status.html",
        sensor_cards=s_cards,
        system_cards=sys_cards,
        overall=overall,
        reading=latest,
        reading_count=count,
        lora_joined=lora_joined,
        lora_fcnt=lora_fcnt,
        fw_version=read_firmware_version(),
        hostname=platform.node(),
        uptime_s=int(time.monotonic() - _START_TIME),
    )
