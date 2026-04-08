"""Dashboard / status page."""

import platform
import time

from flask import Blueprint, current_app, render_template

from service_window.auth import login_required
from service_window.db_reader import DBReader

status_bp = Blueprint("status", __name__)

_START_TIME = time.monotonic()


def _read_version() -> str:
    try:
        from pathlib import Path

        for p in ["/opt/bluesignal/VERSION", str(Path(__file__).parents[3] / "VERSION")]:
            path = Path(p)
            if path.exists():
                return path.read_text().strip()
    except Exception:
        pass
    return "unknown"


@status_bp.route("/")
@login_required
def index():
    db = DBReader(current_app.config["DB_PATH"])
    latest = db.get_latest_reading()
    count = db.get_reading_count()
    session = db.get_lorawan_session()

    lora_joined = bool(session and session.get("joined"))
    lora_fcnt = session.get("fcnt_up", 0) if session else 0

    return render_template(
        "status.html",
        reading=latest,
        reading_count=count,
        lora_joined=lora_joined,
        lora_fcnt=lora_fcnt,
        fw_version=_read_version(),
        hostname=platform.node(),
        uptime_s=int(time.monotonic() - _START_TIME),
    )
