"""Provisioning wizard — web-based first-boot commissioning."""

import io
import json
import logging
import re
import subprocess

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

from service_window import read_firmware_version
from service_window.auth import login_required
from service_window.config_editor import read_config, update_config

provision_bp = Blueprint("provision", __name__, url_prefix="/provision")

_HEX32_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_CLOUD_KEY_RE = re.compile(r"^[0-9a-zA-Z]{16,128}$")
_DEFAULT_INGEST_URL = "https://us-central1-waterquality-trading.cloudfunctions.net/ingestReading"
_DEFAULT_COMMAND_URL = "https://us-central1-waterquality-trading.cloudfunctions.net/deviceCommands"


def _get_identity() -> dict[str, str]:
    """Generate device identity info."""
    try:
        from utils.identity import get_ble_name, get_dev_eui, get_device_id

        device_id = get_device_id()
        dev_eui = get_dev_eui().hex().upper()
        ble_name = get_ble_name(device_id)
    except Exception as e:
        logging.getLogger("wqm1.provision").debug("Identity generation failed: %s", e)
        device_id = "BS-WQM1-unknown"
        dev_eui = "0000000000000000"
        ble_name = "BlueSignal-0000"
    return {
        "device_id": device_id,
        "dev_eui": dev_eui,
        "ble_name": ble_name,
    }


@provision_bp.route("/")
@login_required
def index() -> str:
    identity = _get_identity()
    config = read_config(current_app.config["CONFIG_PATH"])
    app_key = config.get("app_key", "")
    is_provisioned = app_key != "00000000000000000000000000000000" and bool(app_key)
    cloud = {
        "enabled": bool(config.get("cloud_enabled", False)),
        "key_set": bool(config.get("api_key", "")),
        "ingest_url": config.get("cloud_ingest_url") or _DEFAULT_INGEST_URL,
        "command_url": config.get("cloud_command_url") or _DEFAULT_COMMAND_URL,
    }
    return render_template(
        "provision/index.html",
        identity=identity,
        is_provisioned=is_provisioned,
        cloud=cloud,
    )


@provision_bp.route("/identity")
@login_required
def identity() -> str:
    identity = _get_identity()
    return render_template("provision/identity.html", identity=identity)


@provision_bp.route("/appkey", methods=["POST"])
@login_required
def set_appkey() -> str:
    app_key = request.form.get("app_key", "").strip()
    if not _HEX32_RE.match(app_key):
        flash("AppKey must be exactly 32 hex characters.", "error")
        return redirect(url_for("provision.index"))

    update_config(current_app.config["CONFIG_PATH"], {"app_key": app_key})
    flash("AppKey saved.", "success")
    return redirect(url_for("provision.index"))


@provision_bp.route("/cloud", methods=["POST"])
@login_required
def set_cloud() -> str:
    """Commission the unit for the cloud (HTTP/WiFi) path: store the device API
    key + endpoints and toggle cloud mode. Coexists with LoRaWAN."""
    path = current_app.config["CONFIG_PATH"]
    api_key = request.form.get("api_key", "").strip()
    ingest = request.form.get("cloud_ingest_url", "").strip()
    command = request.form.get("cloud_command_url", "").strip()
    enabled = request.form.get("cloud_enabled") == "on"

    updates: dict[str, object] = {"cloud_enabled": enabled}

    if api_key:
        if not _CLOUD_KEY_RE.match(api_key):
            flash("Device API key looks invalid (expected 16–128 alphanumeric chars).", "error")
            return redirect(url_for("provision.index"))
        updates["api_key"] = api_key
    if ingest:
        if not ingest.startswith("https://"):
            flash("Ingest URL must start with https://", "error")
            return redirect(url_for("provision.index"))
        updates["cloud_ingest_url"] = ingest
    if command:
        if not command.startswith("https://"):
            flash("Command URL must start with https://", "error")
            return redirect(url_for("provision.index"))
        updates["cloud_command_url"] = command

    # Don't enable cloud mode without a key (now or already saved).
    if enabled and not api_key and not read_config(path).get("api_key"):
        flash("Enter the device API key before enabling cloud mode.", "error")
        return redirect(url_for("provision.index"))

    update_config(path, updates)
    flash("Cloud settings saved — restart the firmware service to apply.", "success")
    return redirect(url_for("provision.index"))


@provision_bp.route("/verify")
@login_required
def verify() -> str:
    from pathlib import Path

    script = None
    for p in [
        "/opt/bluesignal/scripts/diagnostics.sh",
        str(Path(__file__).parents[3] / "scripts" / "diagnostics.sh"),
    ]:
        if Path(p).exists():
            script = p
            break

    output = ""
    if script:
        try:
            result = subprocess.run(
                ["bash", script],
                capture_output=True,
                text=True,
                timeout=30,
            )
            output = result.stdout + result.stderr
        except Exception as e:
            output = f"Error: {e}"
    else:
        output = "diagnostics.sh not found"

    return render_template("provision/verify.html", output=output)


@provision_bp.route("/report")
@login_required
def report() -> Response:
    identity = _get_identity()
    config = read_config(current_app.config["CONFIG_PATH"])
    report_data = {
        **identity,
        "app_key_set": config.get("app_key", "") != "00000000000000000000000000000000",
        "firmware_version": read_firmware_version(),
    }
    return Response(
        json.dumps(report_data, indent=2),
        mimetype="application/json",
        headers={"Content-Disposition": "attachment; filename=wqm1-identity.json"},
    )


@provision_bp.route("/qr.svg")
@login_required
def qr_svg() -> Response:
    """Generate QR code SVG for device identity."""
    identity = _get_identity()
    qr_data = json.dumps(identity)

    try:
        import qrcode
        import qrcode.image.svg

        qr = qrcode.QRCode(box_size=10, border=2)
        qr.add_data(qr_data)
        qr.make(fit=True)
        img = qr.make_image(image_factory=qrcode.image.svg.SvgImage)
        buf = io.BytesIO()
        img.save(buf)
        return Response(buf.getvalue(), mimetype="image/svg+xml")
    except ImportError:
        return Response(
            "<svg><text y='20'>qrcode package not installed</text></svg>",
            mimetype="image/svg+xml",
        )
