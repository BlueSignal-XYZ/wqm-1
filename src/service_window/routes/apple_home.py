"""
Service Window — the Apple Home page.

Shows whether the HomeKit accessory is on, advertising and paired, and —
until it is paired — the setup code and its QR, drawn here from the
``apple_home_status`` command's answer. The code never leaves the unit's own
LAN: this page is behind the Service Window login, the cloud heartbeat
carries only ``{enabled, paired}``, and nothing logs the code.

Enable / disable writes ``apple_home_enabled`` through the same validated
config editor the AWG page uses; the firmware picks the change up on its
next sampling cycle (the key is hot), so no restart is needed.
"""

from __future__ import annotations

import io

from flask import (
    Blueprint,
    Response,
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
from utils.config import validate_values

apple_home_bp = Blueprint("apple_home", __name__, url_prefix="/apple-home")


def _status() -> dict:
    result = send_command(current_app.config["CMD_SOCK"], "apple_home_status")
    if not isinstance(result, dict) or not result.get("ok", False):
        return {"available": False, "error": (result or {}).get("error") if result else None}
    out = dict(result)
    out["available"] = True
    return out


@apple_home_bp.route("/")
@login_required
def index() -> str:
    config = read_config(current_app.config["CONFIG_PATH"])
    return render_template(
        "apple_home.html",
        status=_status(),
        enabled=bool(config.get("apple_home_enabled", False)),
    )


@apple_home_bp.route("/enable", methods=["POST"])
@login_required
def enable() -> ResponseReturnValue:
    want = request.form.get("enabled") == "1"
    accepted, errors = validate_values({"apple_home_enabled": want})
    if errors:
        flash("; ".join(errors), "error")
        return redirect(url_for("apple_home.index"))
    update_config(current_app.config["CONFIG_PATH"], accepted)
    # Hot key: the firmware re-reads it on the next cycle. Ask for a reload
    # too, so the accessory starts (or stops) within seconds instead of at
    # the next sample. The reload's own failure is not this page's failure.
    send_command(current_app.config["CMD_SOCK"], "config_reload")
    flash("Apple Home turned on." if want else "Apple Home turned off.", "success")
    return redirect(url_for("apple_home.index"))


@apple_home_bp.route("/qr.svg")
@login_required
def qr_svg() -> Response:
    """The setup URI as a QR, only while unpaired. An empty SVG otherwise."""
    status = _status()
    uri = status.get("setupUri") if status.get("available") else None
    if not uri or status.get("paired"):
        return Response("<svg xmlns='http://www.w3.org/2000/svg'/>", mimetype="image/svg+xml")
    try:
        import qrcode
        import qrcode.image.svg

        qr = qrcode.QRCode(box_size=10, border=2)
        qr.add_data(uri)
        qr.make(fit=True)
        img = qr.make_image(image_factory=qrcode.image.svg.SvgImage)
        buf = io.BytesIO()
        img.save(buf)
        return Response(
            buf.getvalue(), mimetype="image/svg+xml", headers={"Cache-Control": "no-store"}
        )
    except ImportError:
        return Response(
            "<svg xmlns='http://www.w3.org/2000/svg'>"
            "<text y='20'>qrcode package not installed</text></svg>",
            mimetype="image/svg+xml",
        )
