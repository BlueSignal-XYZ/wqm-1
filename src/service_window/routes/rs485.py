"""
RS485 (Modbus) probe setup + guided calibration.

Installer-facing pages for the Honde probe family: discover probes on the
bus, give each one a unique address (they all ship as address 1), enable
them in config, and run the guided calibrations (chlorine zero/slope in a
flow cell, 5-in-1 pH buffer points, EC slope).

Bus access happens in short per-request transactions on the same serial
port the firmware samples from. A collision with a sampling read simply
fails CRC and is retried — both sides tolerate it — and during first-time
setup the firmware isn't polling the bus at all (the probes aren't enabled
in config yet).
"""

from typing import Any

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
from service_window.config_editor import read_config, update_config

rs485_bp = Blueprint("rs485", __name__, url_prefix="/rs485")

# probe key -> (driver attribute name, config prefix, human label)
PROBE_TYPES: dict[str, dict[str, str]] = {
    "chlorine": {"label": "Residual chlorine", "prefix": "rs485_chlorine"},
    "orp": {"label": "Digital ORP", "prefix": "rs485_orp"},
    "multi": {"label": "5-in-1 (pH/EC/TDS/salinity/temp)", "prefix": "rs485_multi"},
}

SCAN_ADDRESSES = range(1, 17)


def _port(config: dict[str, Any]) -> str:
    return str(config.get("rs485_port") or "/dev/ttyUSB0")


def _make_bus(config: dict[str, Any]):
    from sensors.modbus import ModbusBus

    return ModbusBus(_port(config), timeout_s=0.3, retries=2)


def _make_probe(probe_type: str, bus: Any, address: int) -> Any:
    from sensors.honde import HondeChlorineSensor, HondeMultiSensor, HondeOrpSensor

    cls = {
        "chlorine": HondeChlorineSensor,
        "orp": HondeOrpSensor,
        "multi": HondeMultiSensor,
    }[probe_type]
    return cls(bus, address)


def _probe_config(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    probes: dict[str, dict[str, Any]] = {}
    for key, meta in PROBE_TYPES.items():
        prefix = meta["prefix"]
        probes[key] = {
            "label": meta["label"],
            "enabled": bool(config.get(f"{prefix}_enabled")),
            "address": int(config.get(f"{prefix}_addr") or 1),
        }
    return probes


@rs485_bp.route("/")
@login_required
def index() -> str:
    config = read_config(current_app.config["CONFIG_PATH"])
    return render_template(
        "rs485/index.html",
        probes=_probe_config(config),
        port=_port(config),
    )


@rs485_bp.route("/scan", methods=["POST"])
@login_required
def scan() -> str:
    """Full bus scan — which addresses answer right now."""
    config = read_config(current_app.config["CONFIG_PATH"])
    bus = _make_bus(config)
    try:
        found = bus.scan(SCAN_ADDRESSES)
        error = None
    except Exception as e:  # noqa: BLE001 — adapter missing/unplugged
        found, error = [], str(e)
    finally:
        bus.close()
    return render_template(
        "rs485/index.html",
        probes=_probe_config(config),
        port=_port(config),
        scan_result=found,
        scan_error=error,
        scanned=True,
    )


@rs485_bp.route("/assign", methods=["GET", "POST"])
@login_required
def assign() -> ResponseReturnValue:
    """
    Guided re-addressing: with exactly ONE new probe connected, discover it
    via the broadcast address, write its new unique address, and enable it
    in config. The firmware picks the probe up on its next restart.
    """
    config = read_config(current_app.config["CONFIG_PATH"])

    if request.method == "POST":
        probe_type = request.form.get("probe_type", "")
        if probe_type not in PROBE_TYPES:
            flash("Choose which probe you are adding.", "error")
            return redirect(url_for("rs485.assign"))
        try:
            new_address = int(request.form["new_address"])
            if not 1 <= new_address <= 247:
                raise ValueError
        except (KeyError, ValueError):
            flash("The address must be a number between 1 and 247.", "error")
            return redirect(url_for("rs485.assign"))

        bus = _make_bus(config)
        try:
            current = bus.query_single_address()
            if current is None:
                flash(
                    "No probe answered. Connect exactly one new probe to the "
                    "bus, check its 12V supply, and try again.",
                    "error",
                )
                return redirect(url_for("rs485.assign"))
            if current != new_address:
                _make_probe(probe_type, bus, current).set_address(new_address)
                # Confirm the probe answers at its new home before saving.
                if not bus.probe(new_address):
                    flash(
                        f"The probe took address {new_address} but did not "
                        "answer there — power-cycle it and scan the bus.",
                        "error",
                    )
                    return redirect(url_for("rs485.index"))
        except Exception as e:  # noqa: BLE001 — surface bus errors to the installer
            flash(f"Bus error while re-addressing: {e}", "error")
            return redirect(url_for("rs485.assign"))
        finally:
            bus.close()

        prefix = PROBE_TYPES[probe_type]["prefix"]
        update_config(
            current_app.config["CONFIG_PATH"],
            {f"{prefix}_enabled": True, f"{prefix}_addr": new_address},
        )
        flash(
            f"{PROBE_TYPES[probe_type]['label']} probe is set up at address "
            f"{new_address}. Restart the unit (Settings page) to start reading it.",
            "success",
        )
        return redirect(url_for("rs485.index"))

    # Suggest the first free address so two probes can't collide.
    taken = {p["address"] for p in _probe_config(config).values() if p["enabled"]}
    suggested = next(a for a in SCAN_ADDRESSES if a not in taken)
    return render_template("rs485/assign.html", probe_types=PROBE_TYPES, suggested=suggested)


@rs485_bp.route("/calibrate/chlorine", methods=["GET", "POST"])
@login_required
def calibrate_chlorine() -> ResponseReturnValue:
    """Two-point chlorine calibration (zero + slope) in the flow cell."""
    config = read_config(current_app.config["CONFIG_PATH"])
    if not config.get("rs485_chlorine_enabled"):
        flash("Set up the chlorine probe first.", "error")
        return redirect(url_for("rs485.index"))
    address = int(config.get("rs485_chlorine_addr") or 1)

    if request.method == "POST":
        action = request.form.get("action", "")
        bus = _make_bus(config)
        probe = _make_probe("chlorine", bus, address)
        try:
            if action == "zero":
                probe.calibrate_zero()
                flash("Zero point saved. Now run the high-point step.", "success")
            elif action == "slope":
                try:
                    reference = float(request.form["reference_mgl"])
                except (KeyError, ValueError):
                    flash("Enter the lab-verified chlorine value in mg/L.", "error")
                    return redirect(url_for("rs485.calibrate_chlorine"))
                probe.calibrate_slope(reference)
                _stamp("chlorine")
                flash(
                    f"Chlorine calibrated against {reference:g} mg/L. "
                    "Verify the live reading settles near that value.",
                    "success",
                )
                return redirect(url_for("calibration.index"))
            else:
                flash("Unknown calibration step.", "error")
        except ValueError as e:
            flash(str(e), "error")
        except Exception as e:  # noqa: BLE001
            flash(f"Bus error during calibration: {e}", "error")
        finally:
            bus.close()
        return redirect(url_for("rs485.calibrate_chlorine"))

    # GET: show the live value + raw AD counts so the installer can watch
    # for stability before confirming a point (refresh to re-read).
    bus = _make_bus(config)
    probe = _make_probe("chlorine", bus, address)
    try:
        live = {"value_mgl": probe.read(), "ad_counts": probe.read_ad()}
    finally:
        bus.close()
    return render_template("rs485/calibrate_chlorine.html", live=live, address=address)


@rs485_bp.route("/calibrate/multi", methods=["GET", "POST"])
@login_required
def calibrate_multi() -> ResponseReturnValue:
    """5-in-1 guided calibration: pH buffer points and EC slope."""
    config = read_config(current_app.config["CONFIG_PATH"])
    if not config.get("rs485_multi_enabled"):
        flash("Set up the 5-in-1 probe first.", "error")
        return redirect(url_for("rs485.index"))
    address = int(config.get("rs485_multi_addr") or 1)

    if request.method == "POST":
        action = request.form.get("action", "")
        bus = _make_bus(config)
        probe = _make_probe("multi", bus, address)
        try:
            if action == "ph_point":
                buffer = request.form.get("buffer", "")
                probe.calibrate_ph_point(buffer)
                _stamp("ph")
                flash(f"pH {buffer} buffer point saved.", "success")
            elif action == "ec_slope":
                try:
                    slope = float(request.form["slope"])
                except (KeyError, ValueError):
                    flash("Enter the EC slope (1.000 = factory).", "error")
                    return redirect(url_for("rs485.calibrate_multi"))
                probe.set_ec_slope(slope)
                _stamp("conductivity")
                flash(f"EC slope set to {slope:g}.", "success")
            else:
                flash("Unknown calibration step.", "error")
        except ValueError as e:
            flash(str(e), "error")
        except Exception as e:  # noqa: BLE001
            flash(f"Bus error during calibration: {e}", "error")
        finally:
            bus.close()
        return redirect(url_for("rs485.calibrate_multi"))

    bus = _make_bus(config)
    probe = _make_probe("multi", bus, address)
    try:
        live = probe.read_all()
    finally:
        bus.close()
    return render_template("rs485/calibrate_multi.html", live=live, address=address)


def _stamp(sensor: str) -> None:
    from service_window.routes.calibration import _stamp_calibrated

    _stamp_calibrated(sensor)
