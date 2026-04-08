"""Sensor calibration wizard."""

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for

from service_window.auth import login_required
from service_window.config_editor import read_config, update_config

calibration_bp = Blueprint("calibration", __name__, url_prefix="/calibrate")


def _load_cal(app) -> dict:
    return read_config(app.config["CAL_PATH"])


@calibration_bp.route("/")
@login_required
def index():
    cal = _load_cal(current_app)
    return render_template("calibration/index.html", cal=cal)


@calibration_bp.route("/ph", methods=["GET", "POST"])
@login_required
def ph():
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
            flash(f"pH calibrated: slope={slope:.4f}", "success")
        except (ValueError, KeyError):
            flash("Invalid input.", "error")
        return redirect(url_for("calibration.index"))
    cal = _load_cal(current_app)
    return render_template("calibration/ph.html", cal=cal)


@calibration_bp.route("/tds", methods=["GET", "POST"])
@login_required
def tds():
    if request.method == "POST":
        try:
            known_ppm = float(request.form["known_ppm"])
            measured_v = float(request.form["measured_v"])
            if measured_v <= 0:
                flash("Voltage must be positive.", "error")
                return redirect(url_for("calibration.tds"))
            k = known_ppm / measured_v
            update_config(current_app.config["CAL_PATH"], {"tds_k": round(k, 2)})
            flash(f"TDS calibrated: k={k:.2f}", "success")
        except (ValueError, KeyError):
            flash("Invalid input.", "error")
        return redirect(url_for("calibration.index"))
    cal = _load_cal(current_app)
    return render_template("calibration/tds.html", cal=cal)


@calibration_bp.route("/turbidity", methods=["GET", "POST"])
@login_required
def turbidity():
    if request.method == "POST":
        try:
            clear_v = float(request.form["clear_v"])
            update_config(
                current_app.config["CAL_PATH"],
                {
                    "turbidity_v_clear": round(clear_v, 3),
                },
            )
            flash(f"Turbidity calibrated: clear water V={clear_v:.3f}", "success")
        except (ValueError, KeyError):
            flash("Invalid input.", "error")
        return redirect(url_for("calibration.index"))
    cal = _load_cal(current_app)
    return render_template("calibration/turbidity.html", cal=cal)


@calibration_bp.route("/orp", methods=["GET", "POST"])
@login_required
def orp():
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
            flash(f"ORP calibrated: offset={offset:.1f} mV", "success")
        except (ValueError, KeyError):
            flash("Invalid input.", "error")
        return redirect(url_for("calibration.index"))
    cal = _load_cal(current_app)
    return render_template("calibration/orp.html", cal=cal)
