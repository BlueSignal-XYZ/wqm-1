"""Sensor calibration wizard."""

from datetime import UTC, datetime

from flask import Blueprint, Flask, current_app, flash, redirect, render_template, request, url_for
from flask.typing import ResponseReturnValue

from service_window.auth import login_required
from service_window.config_editor import read_config, update_config

calibration_bp = Blueprint("calibration", __name__, url_prefix="/calibrate")


def _load_cal(app: Flask) -> dict:
    return read_config(app.config["CAL_PATH"])


def _stamp_calibrated(sensor: str) -> None:
    """Record when the sensor was calibrated — feeds the calibration-age
    reminders on the dashboard and in heartbeats (drift detection resets its
    baseline from these timestamps)."""
    cal = read_config(current_app.config["CAL_PATH"])
    stamps = cal.get("calibrated_at")
    stamps = dict(stamps) if isinstance(stamps, dict) else {}
    stamps[sensor] = datetime.now(UTC).isoformat()
    update_config(current_app.config["CAL_PATH"], {"calibrated_at": stamps})


def _calibration_ages(cal: dict) -> dict[str, int | None]:
    """Whole days since each sensor's last calibration (None = unknown)."""
    stamps = cal.get("calibrated_at")
    stamps = stamps if isinstance(stamps, dict) else {}
    ages: dict[str, int | None] = {}
    now = datetime.now(UTC)
    for sensor in ("ph", "tds", "turbidity", "orp"):
        raw = stamps.get(sensor)
        try:
            ages[sensor] = int((now - datetime.fromisoformat(str(raw))).days) if raw else None
        except (ValueError, TypeError):
            ages[sensor] = None
    return ages


@calibration_bp.route("/")
@login_required
def index() -> str:
    cal = _load_cal(current_app)
    return render_template("calibration/index.html", cal=cal, ages=_calibration_ages(cal))


@calibration_bp.route("/ph", methods=["GET", "POST"])
@login_required
def ph() -> ResponseReturnValue:
    if request.method == "POST":
        try:
            v_ph4 = float(request.form["v_ph4"])
            v_ph7 = float(request.form["v_ph7"])
            dv = v_ph7 - v_ph4
            if abs(dv) < 0.001:
                flash("Voltages too close — check probe.", "error")
                return redirect(url_for("calibration.ph"))
            slope = (7.0 - 4.0) / dv
            update_config(
                current_app.config["CAL_PATH"],
                {
                    "ph_v_at_4": v_ph4,
                    "ph_v_at_7": v_ph7,
                    "ph_slope": round(slope, 4),
                },
            )
            _stamp_calibrated("ph")
            flash(f"pH calibrated: slope={slope:.4f}", "success")
        except (ValueError, KeyError):
            flash("Invalid input.", "error")
        return redirect(url_for("calibration.index"))
    cal = _load_cal(current_app)
    return render_template("calibration/ph.html", cal=cal)


@calibration_bp.route("/tds", methods=["GET", "POST"])
@login_required
def tds() -> ResponseReturnValue:
    if request.method == "POST":
        try:
            known_ppm = float(request.form["known_ppm"])
            measured_v = float(request.form["measured_v"])
            if measured_v <= 0:
                flash("Voltage must be positive.", "error")
                return redirect(url_for("calibration.tds"))
            k = known_ppm / measured_v
            update_config(current_app.config["CAL_PATH"], {"tds_k": round(k, 2)})
            _stamp_calibrated("tds")
            flash(f"TDS calibrated: k={k:.2f}", "success")
        except (ValueError, KeyError):
            flash("Invalid input.", "error")
        return redirect(url_for("calibration.index"))
    cal = _load_cal(current_app)
    return render_template("calibration/tds.html", cal=cal)


@calibration_bp.route("/turbidity", methods=["GET", "POST"])
@login_required
def turbidity() -> ResponseReturnValue:
    if request.method == "POST":
        try:
            clear_v = float(request.form["clear_v"])
            update_config(
                current_app.config["CAL_PATH"],
                {
                    "turbidity_v_clear": round(clear_v, 3),
                },
            )
            _stamp_calibrated("turbidity")
            flash(f"Turbidity calibrated: clear water V={clear_v:.3f}", "success")
        except (ValueError, KeyError):
            flash("Invalid input.", "error")
        return redirect(url_for("calibration.index"))
    cal = _load_cal(current_app)
    return render_template("calibration/turbidity.html", cal=cal)


@calibration_bp.route("/orp", methods=["GET", "POST"])
@login_required
def orp() -> ResponseReturnValue:
    if request.method == "POST":
        try:
            known_mv = float(request.form["known_mv"])
            measured_mv = float(request.form["measured_mv"])
            offset = known_mv - measured_mv
            update_config(
                current_app.config["CAL_PATH"],
                {
                    "orp_offset_mv": round(offset, 1),
                },
            )
            _stamp_calibrated("orp")
            flash(f"ORP calibrated: offset={offset:.1f} mV", "success")
        except (ValueError, KeyError):
            flash("Invalid input.", "error")
        return redirect(url_for("calibration.index"))
    cal = _load_cal(current_app)
    return render_template("calibration/orp.html", cal=cal)
