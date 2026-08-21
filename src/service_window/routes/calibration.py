"""Sensor calibration wizard."""

from datetime import UTC, datetime

from flask import (
    Blueprint,
    Flask,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask.typing import ResponseReturnValue

from sensors.tds import compensated_voltage, k_from_reference
from service_window.auth import login_required
from service_window.cmd_client import send_command
from service_window.config_editor import read_config, update_config
from service_window.db_reader import DBReader

calibration_bp = Blueprint("calibration", __name__, url_prefix="/calibrate")

# Nominal calibration temperature. TDS reference solutions are specified at
# 25 °C, so this is also the right assumption when the probe is unavailable.
NOMINAL_TEMP_C = 25.0


def _load_cal(app: Flask) -> dict:
    return read_config(app.config["CAL_PATH"])


def _latest_temp_c() -> tuple[float | None, float]:
    """Most recent measured water temperature, and the value to compensate with.

    Returns (measured, used). `measured` is None when the DS18B20 is absent or
    has not reported — in which case `used` falls back to 25 °C and the wizard
    SAYS SO. Silently assuming 25 °C would put a beaker calibrated at 30 °C
    about 10% out with nothing on screen to suggest it.
    """
    try:
        readings = DBReader(current_app.config["DB_PATH"]).get_readings(limit=1)
    except Exception:  # noqa: BLE001 — a missing DB must not break the wizard
        readings = []
    measured = readings[0].get("temp_c") if readings else None
    if not isinstance(measured, int | float):
        return None, NOMINAL_TEMP_C
    return float(measured), float(measured)


@calibration_bp.route("/live.json")
@login_required
def live() -> ResponseReturnValue:
    """Live ADC voltages for the wizards.

    The firmware daemon reads the bus; this process only asks. See
    `_adc_voltages` in main.py for why that boundary is not negotiable.
    """
    result = send_command(current_app.config["CMD_SOCK"], "adc_voltages")
    if not result.get("ok"):
        return jsonify(result), 502

    measured_temp, used_temp = _latest_temp_c()
    channels = result.get("channels")
    channels = channels if isinstance(channels, dict) else {}

    # The TDS wizard needs the voltage `k` is actually defined against, not the
    # ADC voltage — so the page can show the installer the exact number the
    # form wants and the ambiguity disappears.
    tds = channels.get("tds")
    if isinstance(tds, dict) and isinstance(tds.get("adcVolts"), int | float):
        tds["calibrationVolts"] = round(compensated_voltage(tds["adcVolts"], used_temp), 5)

    return jsonify(
        {
            "ok": True,
            "channels": channels,
            "tempC": measured_temp,
            "tempUsedC": used_temp,
            "tempAssumed": measured_temp is None,
        }
    )


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
            # Field renamed from `measured_v` deliberately. That name meant
            # "some voltage" and was solved as k = ppm / v, while the sensor
            # multiplies k by the divider- and temperature-compensated voltage
            # — so entering the only voltage a person can observe made every
            # subsequent reading 3.2x high, permanently and silently. A rename
            # means a stale form post fails loudly instead of being
            # reinterpreted under the new meaning.
            adc_volts = float(request.form["adc_volts"])
        except (ValueError, KeyError):
            flash("Invalid input.", "error")
            return redirect(url_for("calibration.tds"))

        if known_ppm <= 0:
            flash("Reference solution must be greater than 0 ppm.", "error")
            return redirect(url_for("calibration.tds"))

        _, used_temp = _latest_temp_c()
        try:
            k = k_from_reference(known_ppm, adc_volts, used_temp)
        except ValueError as e:
            # A non-conducting probe would otherwise yield an enormous k that
            # makes every later reading wrong without ever looking wrong.
            flash(f"Cannot calibrate: {e}. Check immersion and wiring.", "error")
            return redirect(url_for("calibration.tds"))

        update_config(current_app.config["CAL_PATH"], {"tds_k": round(k, 2)})
        _stamp_calibrated("tds")
        flash(
            f"TDS calibrated: k={k:.2f} ppm/V from {known_ppm:.0f} ppm "
            f"at {adc_volts:.4f} V (compensated to {used_temp:.1f} °C)",
            "success",
        )
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
