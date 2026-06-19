"""Sensor detail and history."""

from flask import Blueprint, current_app, jsonify, render_template
from flask.typing import ResponseReturnValue

from service_window.auth import login_required
from service_window.db_reader import DBReader

sensors_bp = Blueprint("sensors", __name__, url_prefix="/sensors")


@sensors_bp.route("/")
@login_required
def index() -> str:
    db = DBReader(current_app.config["DB_PATH"])
    readings = db.get_readings(limit=50)
    return render_template("sensors.html", readings=readings)


@sensors_bp.route("/data.json")
@login_required
def data_json() -> ResponseReturnValue:
    db = DBReader(current_app.config["DB_PATH"])
    readings = db.get_readings(limit=50)
    # Reverse to chronological order for charts
    readings.reverse()
    return jsonify(readings)
