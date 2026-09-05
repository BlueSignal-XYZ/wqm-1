"""
AWG circuit — the installer's view of the smart breaker integration.

One page does three jobs:

* **Status** — the live ``awg_status`` snapshot from the firmware (breaker
  position, link health, last power sample, fail-safe state) rendered as the
  same traffic-light card the dashboard shows.
* **Switch** — ON / OFF for the AWG load through ``awg_set``. That goes via the
  controller (interlock relay first, then the breaker), never straight to a
  coil, so the fail-safe bookkeeping sees every request.
* **Bind** — the ``smart_breaker_*`` settings an installer collects on site:
  vendor, Eaton site / device UUIDs, the panel label, circuit ampacity, the
  interlock relay channel, the fail-safe mode, and the Eaton developer
  credentials. Everything is validated against the firmware's own settings
  schema before it is written, so the page can never save a value the firmware
  would refuse to boot with.

Credentials are write-only here: the form shows *set / not set* and a blank
field means *keep what is there*. Nothing secret is ever rendered back into
HTML. The API base / token URL stay in the advanced config file — they only
change when Eaton moves portal generations, which is a documented step in
docs/smart-breaker-integration.md, not a knob for the field.
"""

import re
from typing import Any

from flask import (
    Blueprint,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask.typing import ResponseReturnValue

from service_window.auth import login_required
from service_window.cmd_client import send_command
from service_window.config_editor import read_config, update_config
from service_window.health import smart_breaker_card
from utils.config import (
    SETTINGS_SCHEMA,
    SMART_BREAKER_FAIL_SAFE_MODES,
    SMART_BREAKER_VENDORS,
    Settings,
    validate_values,
)

awg_bp = Blueprint("awg", __name__, url_prefix="/awg")

_DEFAULTS = Settings()

# Eaton identifies locations and devices by UUID. Checked here so a pasted
# serial number or panel label is caught while the installer is still on
# site, instead of surfacing as a 404 from Eaton on the first poll.
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

# Binding fields the form edits, in display order. Secrets are handled
# separately (see SECRET_FIELDS) because blank means "unchanged" for them.
BINDING_FIELDS: tuple[str, ...] = (
    "smart_breaker_vendor",
    "smart_breaker_site_id",
    "smart_breaker_device_id",
    "smart_breaker_circuit_label",
    "smart_breaker_circuit_amps",
    "smart_breaker_interlock_relay",
    "smart_breaker_fail_safe",
    "smart_breaker_unreachable_grace_s",
    "smart_breaker_poll_s",
)

SECRET_FIELDS: tuple[str, ...] = (
    "smart_breaker_client_id",
    "smart_breaker_client_secret",
    "smart_breaker_subscription_key",
)

VENDOR_LABELS: dict[str, str] = {
    "none": "Not installed",
    "relay_only": "Relay only (no smart breaker)",
    "ableedge": "Eaton AbleEdge smart breaker",
}

FAIL_SAFE_LABELS: dict[str, str] = {
    "off": "OFF — switch the AWG off (recommended for compressor loads)",
    "last": "LAST — leave the circuit as it was",
    "on": "ON — switch the AWG on",
}


def _status(config: dict[str, Any]) -> dict[str, Any]:
    """Live controller snapshot, or a stub when the firmware is not answering."""
    result = send_command(current_app.config["CMD_SOCK"], "awg_status")
    if not isinstance(result, dict):
        result = {"ok": False, "error": "bad response"}
    result.setdefault("configured", False)
    result["card"] = smart_breaker_card(config, result)
    return result


@awg_bp.route("/")
@login_required
def index() -> str:
    config = read_config(current_app.config["CONFIG_PATH"])
    values = {key: config.get(key, getattr(_DEFAULTS, key)) for key in BINDING_FIELDS}
    secrets_set = {key: bool(config.get(key)) for key in SECRET_FIELDS}
    return render_template(
        "awg.html",
        status=_status(config),
        values=values,
        specs={key: SETTINGS_SCHEMA[key] for key in BINDING_FIELDS},
        secrets_set=secrets_set,
        vendors=[(v, VENDOR_LABELS.get(v, v)) for v in SMART_BREAKER_VENDORS],
        fail_safe_modes=[(m, FAIL_SAFE_LABELS.get(m, m)) for m in SMART_BREAKER_FAIL_SAFE_MODES],
    )


@awg_bp.route("/status.json")
@login_required
def status_json() -> ResponseReturnValue:
    """Snapshot for the page's live refresh — same shape as ``awg_status``
    plus the rendered health card."""
    config = read_config(current_app.config["CONFIG_PATH"])
    return jsonify(_status(config))


def _parse_state(raw: object) -> bool | None:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        if raw.lower() in ("on", "true", "1"):
            return True
        if raw.lower() in ("off", "false", "0"):
            return False
    return None


def _switch(state: bool, reason: str | None) -> dict[str, Any]:
    return send_command(
        current_app.config["CMD_SOCK"],
        "awg_set",
        state=state,
        source="service_window",
        reason=reason or None,
    )


def _describe(result: dict[str, Any], state: bool) -> tuple[str, str]:
    """Flash-friendly (message, category) for an ``awg_set`` result."""
    word = "on" if state else "off"
    if result.get("ok"):
        breaker = result.get("breaker")
        if breaker == "confirmed":
            return f"AWG circuit switched {word} — breaker confirmed.", "success"
        if breaker == "n/a":
            return f"Interlock relay switched {word}.", "success"
        return f"AWG circuit switched {word}.", "success"
    err = result.get("error") or "no reason given"
    if result.get("interlockOk") and not state:
        return (
            f"Interlock relay is off, but the breaker did not confirm: {err}. "
            "The command is queued and will retry when the link returns.",
            "error",
        )
    return f"Could not switch the AWG circuit {word}: {err}", "error"


@awg_bp.route("/set", methods=["POST"])
@login_required
def set_circuit() -> ResponseReturnValue:
    """Form fallback (no JavaScript)."""
    state = _parse_state(request.form.get("state"))
    if state is None:
        flash("Choose ON or OFF.", "error")
        return redirect(url_for("awg.index"))
    result = _switch(state, request.form.get("reason", "").strip()[:120])
    message, category = _describe(result, state)
    flash(message, category)
    return redirect(url_for("awg.index"))


@awg_bp.route("/api/set", methods=["POST"])
@login_required
def api_set_circuit() -> ResponseReturnValue:
    data = request.get_json(silent=True) or {}
    state = _parse_state(data.get("state"))
    if state is None:
        return jsonify({"ok": False, "error": "state must be boolean"}), 400
    reason = data.get("reason")
    result = _switch(state, str(reason).strip()[:120] if reason else None)
    message, _ = _describe(result, state)
    return jsonify({**result, "message": message})


@awg_bp.route("/bind", methods=["POST"])
@login_required
def bind() -> ResponseReturnValue:
    """Save the breaker binding. Restart-required keys flash a reminder."""
    config_path = current_app.config["CONFIG_PATH"]
    raw: dict[str, Any] = {}
    errors: list[str] = []

    for key in BINDING_FIELDS:
        spec = SETTINGS_SCHEMA[key]
        value = request.form.get(key, "").strip()
        if spec.type is int:
            if not value:
                # Blank number -> the schema default (0 = "none" for amps and
                # the interlock channel), not "unchanged": an installer
                # clearing the box means clear it.
                raw[key] = getattr(_DEFAULTS, key)
                continue
            try:
                raw[key] = int(value)
            except ValueError:
                errors.append(f"{_label(key)}: not a whole number.")
        else:
            raw[key] = value

    for key in SECRET_FIELDS:
        value = request.form.get(key, "")
        if value.strip():
            raw[key] = value.strip()

    vendor = raw.get("smart_breaker_vendor", "none")
    if vendor == "ableedge":
        device_id = raw.get("smart_breaker_device_id", "")
        if not device_id:
            errors.append("Device UUID is required for an Eaton AbleEdge binding.")
        elif not _UUID_RE.match(device_id):
            errors.append(
                "Device UUID does not look like a UUID (8-4-4-4-12 hex). Copy it from "
                "the Eaton developer portal, not the breaker's serial number."
            )
        site_id = raw.get("smart_breaker_site_id", "")
        if site_id and not _UUID_RE.match(site_id):
            errors.append("Site UUID does not look like a UUID (8-4-4-4-12 hex).")
        if not raw.get("smart_breaker_circuit_amps"):
            errors.append(
                "Circuit ampacity is required — read it off the breaker handle; it is not guessed."
            )
        existing = read_config(config_path)
        for key in SECRET_FIELDS:
            if not raw.get(key) and not existing.get(key):
                errors.append(f"{_label(key)} is required for an Eaton AbleEdge binding.")
    elif vendor == "relay_only" and not raw.get("smart_breaker_interlock_relay"):
        errors.append("Relay-only mode needs an interlock relay channel (1–4).")

    if errors:
        for err in errors:
            flash(err, "error")
        return redirect(url_for("awg.index"))

    accepted, schema_errors = validate_values(raw)
    if schema_errors:
        for err in schema_errors:
            flash(err, "error")
        return redirect(url_for("awg.index"))

    update_config(config_path, accepted)
    if any(not SETTINGS_SCHEMA[k].hot for k in accepted):
        flash(
            "Binding saved. Restart the monitoring service below to apply it — the "
            "breaker link is not live until then.",
            "success",
        )
    else:
        send_command(current_app.config["CMD_SOCK"], "config_reload")
        flash("Saved and applied.", "success")
    return redirect(url_for("awg.index"))


@awg_bp.route("/restart", methods=["POST"])
@login_required
def restart() -> ResponseReturnValue:
    result = send_command(current_app.config["CMD_SOCK"], "restart")
    flash(
        "Restarting the monitoring service… the breaker link comes up in about a minute."
        if result.get("ok")
        else "Could not reach the monitoring service — it may already be restarting.",
        "success" if result.get("ok") else "error",
    )
    return redirect(url_for("awg.index"))


_LABELS: dict[str, str] = {
    "smart_breaker_vendor": "Vendor",
    "smart_breaker_site_id": "Site UUID",
    "smart_breaker_device_id": "Device UUID",
    "smart_breaker_circuit_label": "Panel label",
    "smart_breaker_circuit_amps": "Circuit ampacity",
    "smart_breaker_interlock_relay": "Interlock relay",
    "smart_breaker_fail_safe": "Fail-safe",
    "smart_breaker_unreachable_grace_s": "Grace period",
    "smart_breaker_poll_s": "Poll interval",
    "smart_breaker_client_id": "Eaton client ID",
    # nosec B105 - a form label, not a credential.
    "smart_breaker_client_secret": "Eaton client secret",  # nosec B105
    "smart_breaker_subscription_key": "Eaton subscription key",
}


def _label(key: str) -> str:
    return _LABELS.get(key, key)
