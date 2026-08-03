"""
Simple settings — the handful of plain options an installer actually tunes.

Everything else (URLs, credentials, radio parameters) stays in the advanced
config file / remote config, deliberately out of reach here. Values are
validated against the firmware's own settings schema
(utils.config.validate_values) so this page can never write a value the
firmware would refuse to boot with.
"""

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
from service_window.cmd_client import send_command
from service_window.config_editor import read_config, update_config
from utils.config import SETTINGS_SCHEMA, Settings, validate_values

settings_bp = Blueprint("settings", __name__, url_prefix="/settings")

# The simple set: key -> (label, help text). Bounds come from the schema.
SIMPLE_SETTINGS: dict[str, tuple[str, str]] = {
    "sensor_read_s": ("Reading interval (seconds)", "How often the water is sampled."),
    "sync_interval_s": ("Cloud upload interval (seconds)", "How often readings are uploaded."),
    "heartbeat_s": ("Health report interval (seconds)", "How often the unit reports its health."),
    "adaptive_sampling_enabled": (
        "Adaptive sampling",
        "Sample faster automatically when the water is changing quickly.",
    ),
    "orp_enabled": ("ORP probe installed", "Turn on only if an ORP probe is connected."),
    "fan_on_temp_c": ("Fan on above (°C)", "Enclosure temperature that switches the fan on."),
}

_DEFAULTS = Settings()

# Typed rather than clicked: a reboot stops sampling and suspends every control
# rule for about a minute, so it must not be one mis-tap away on a phone held
# in one hand at a wet install. Case-insensitive — the word is the deliberate
# act, not the shift key.
REBOOT_CONFIRMATION = "REBOOT"


@settings_bp.route("/", methods=["GET", "POST"])
@login_required
def index() -> ResponseReturnValue:
    config_path = current_app.config["CONFIG_PATH"]
    config = read_config(config_path)

    if request.method == "POST":
        if request.form.get("restart"):
            result = send_command(current_app.config["CMD_SOCK"], "restart")
            flash(
                "Restarting the monitoring service…"
                if result.get("ok")
                else "Could not reach the monitoring service — it may already be restarting.",
                "success" if result.get("ok") else "error",
            )
            return redirect(url_for("settings.index"))

        if request.form.get("reboot"):
            return _reboot()

        raw: dict = {}
        for key in SIMPLE_SETTINGS:
            spec = SETTINGS_SCHEMA[key]
            if spec.type is bool:
                raw[key] = request.form.get(key) == "on"
            else:
                value = request.form.get(key, "").strip()
                if not value:
                    continue
                try:
                    raw[key] = spec.type(value)
                except ValueError:
                    flash(f"{SIMPLE_SETTINGS[key][0]}: not a valid number.", "error")
                    return redirect(url_for("settings.index"))

        accepted, errors = validate_values(raw)
        if errors:
            for err in errors:
                flash(err, "error")
            return redirect(url_for("settings.index"))

        update_config(config_path, accepted)
        restart_needed = any(not SETTINGS_SCHEMA[k].hot for k in accepted)
        if restart_needed:
            flash("Saved. Restart the monitoring service below to apply everything.", "success")
        else:
            send_command(current_app.config["CMD_SOCK"], "config_reload")
            flash("Saved and applied.", "success")
        return redirect(url_for("settings.index"))

    values = {}
    for key in SIMPLE_SETTINGS:
        values[key] = config.get(key, getattr(_DEFAULTS, key))
    specs = {key: SETTINGS_SCHEMA[key] for key in SIMPLE_SETTINGS}
    return render_template(
        "settings.html",
        simple=SIMPLE_SETTINGS,
        values=values,
        specs=specs,
        reboot_word=REBOOT_CONFIRMATION,
    )


def _reboot() -> ResponseReturnValue:
    """Reboot the whole unit — the recovery path when the Service Window is
    reachable but SSH is not.

    Nothing here holds root. The firmware drives the relays to fail-safe and
    leaves a request the root OTA agent executes; see `app.reboot`.
    """
    typed = request.form.get("reboot_confirm", "").strip()
    if typed.upper() != REBOOT_CONFIRMATION:
        flash(
            f"Type {REBOOT_CONFIRMATION} in the box to confirm — nothing was rebooted.",
            "error",
        )
        return redirect(url_for("settings.index"))

    result = send_command(current_app.config["CMD_SOCK"], "reboot")
    if not result.get("ok"):
        flash(
            "Could not reboot: the monitoring service did not accept the request "
            f"({result.get('error', 'no reason given')}). The unit is still running.",
            "error",
        )
    elif result.get("relaysSafe") is False:
        flash(
            "Rebooting — but the relays could not be confirmed off. Check them "
            "when the unit comes back, in about a minute.",
            "error",
        )
    else:
        flash(
            "Relays are off and the unit is rebooting. This page will not "
            "respond for about a minute.",
            "success",
        )
    return redirect(url_for("settings.index"))
