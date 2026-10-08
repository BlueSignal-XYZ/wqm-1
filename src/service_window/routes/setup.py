"""
Guided first-boot setup wizard — the installer-first commissioning path.

Replaces the CLI wizard as the primary way a dealer/installer commissions a
unit from a phone browser: set a real PIN (the shipped `1234` is banned),
confirm device identity (QR), connect the cloud API key, watch live sensor
checks come green, and finish on a go/no-go checklist. Config writes go
through the atomic YAML editor; the firmware is restarted through the
command socket instead of telling anyone to run systemctl.

While the unit still has the factory PIN and setup hasn't been completed,
every page redirects here (see app.py) — a unit can't be left half set up
by accident.

Site flow v2 (2026-10-03): **the network is the LAST step, and joining the
site's Wi-Fi is what finishes setup.** The wizard is usually served over the
unit's own setup hotspot, which goes down the moment a join succeeds; when
the join sat in the middle, every step after it had to be reached from the
customer's network at a hostname every golden-image unit shares. Now
everything that needs the page happens first, and the join is the one action
whose response the phone may never see — so the join handler finishes setup
on the unit itself (PIN checked before the radio is touched, other saved Wi-Fi
forgotten, setup marked complete, firmware restarted). A failed join keeps its
reason on the unit, so the page that reopens when the hotspot comes back says
why. A unit whose bench card already carries its identity and cloud key skips
the identity and cloud steps (``CARDED_STEPS``).
"""

import re

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask.typing import ResponseReturnValue

from service_window.auth import login_required
from service_window.cmd_client import send_command
from service_window.config_editor import read_config, update_config, update_config_section
from service_window.db_reader import DBReader
from service_window.health import sensor_cards, system_cards, worst_status

setup_bp = Blueprint("setup", __name__, url_prefix="/setup")

_PIN_RE = re.compile(r"^\d{4,8}$")
_CLOUD_KEY_RE = re.compile(r"^[0-9a-zA-Z]{16,128}$")
# LoRaWAN OTAA credentials, both issued by the cloud at claim time.
_APP_KEY_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_APP_EUI_RE = re.compile(r"^[0-9a-fA-F]{16}$")
_ZERO_APP_KEY = "00000000000000000000000000000000"
_FACTORY_PIN = "1234"

# Network last: see the module docstring. ``STEPS`` stays exported for
# callers that render the full track; ``steps_for`` is what routes use.
STEPS = ["welcome", "pin", "identity", "cloud", "sensors", "network", "done"]
CARDED_STEPS = ["welcome", "pin", "sensors", "network", "done"]


def is_carded(config: dict) -> bool:
    """True when the bench card supplied this unit's identity AND cloud key,
    so the identity and cloud-key steps have nothing to ask."""
    from utils.identity import read_provisioned_identity

    try:
        provisioned = bool(read_provisioned_identity())
    except Exception:
        provisioned = False
    return provisioned and bool(config.get("api_key")) and bool(config.get("cloud_enabled", True))


def steps_for(config: dict) -> list[str]:
    return CARDED_STEPS if is_carded(config) else STEPS


def _steps() -> list[str]:
    return steps_for(read_config(current_app.config["CONFIG_PATH"]))


def _next_url(step: str) -> str:
    steps = _steps()
    i = steps.index(step) if step in steps else 0
    return url_for(f"setup.{steps[min(i + 1, len(steps) - 1)]}")


def setup_completed(config_path: str) -> bool:
    sw = read_config(config_path).get("service_window") or {}
    return bool(sw.get("setup_completed"))


def needs_setup(app_config: dict) -> bool:
    """Factory PIN still in place and the wizard never finished."""
    return app_config.get("PIN") == _FACTORY_PIN and not setup_completed(
        app_config.get("CONFIG_PATH", "/etc/bluesignal/config.yaml")
    )


def _identity() -> dict[str, str]:
    from service_window.routes.provision import _get_identity

    return _get_identity()


@setup_bp.route("/")
@login_required
def welcome() -> str:
    config = read_config(current_app.config["CONFIG_PATH"])
    return render_template(
        "setup/welcome.html",
        steps=steps_for(config),
        step="welcome",
        carded=is_carded(config),
    )


@setup_bp.route("/pin", methods=["GET", "POST"])
@login_required
def pin() -> ResponseReturnValue:
    if request.method == "POST":
        new_pin = request.form.get("pin", "").strip()
        confirm = request.form.get("pin_confirm", "").strip()
        if not _PIN_RE.match(new_pin):
            flash("PIN must be 4–8 digits.", "error")
        elif new_pin == _FACTORY_PIN:
            flash(
                "That's the factory PIN — pick your own so only you can access this unit.", "error"
            )
        elif new_pin != confirm:
            flash("PINs don't match — try again.", "error")
        else:
            update_config_section(
                current_app.config["CONFIG_PATH"], "service_window", {"pin": new_pin}
            )
            current_app.config["PIN"] = new_pin
            # The hotspot exemption in auth.login_required ends the moment the
            # factory PIN is gone; the person who just chose the PIN stays in.
            session["pin_verified"] = True
            flash("PIN set. Keep it with the site records.", "success")
            return redirect(_next_url("pin"))
    return render_template("setup/pin.html", steps=_steps(), step="pin")


@setup_bp.route("/identity")
@login_required
def identity() -> ResponseReturnValue:
    if is_carded(read_config(current_app.config["CONFIG_PATH"])):
        return redirect(url_for("setup.sensors"))
    return render_template(
        "setup/identity.html",
        steps=STEPS,
        step="identity",
        identity=_identity(),
        next_url=url_for("setup.cloud"),
    )


_BACKHAULS = ("wifi", "lte", "none")
_SSID_RE = re.compile(r"^[^\x00-\x1f]{1,32}$")


def _finish_setup(keep_ssid: str | None) -> dict:
    """Mark setup complete on the unit: forget every saved Wi-Fi network but
    the one in use, record completion, restart the firmware. Shared by the
    Wi-Fi join (which finishes setup — its response may never reach the
    phone) and the Finish button (LTE / no-network sites). The caller has
    already refused the factory PIN."""
    from utils.netctl import forget_saved_wifi

    forgot = forget_saved_wifi(keep_ssid)
    update_config_section(
        current_app.config["CONFIG_PATH"], "service_window", {"setup_completed": True}
    )
    restarted = bool(send_command(current_app.config["CMD_SOCK"], "restart").get("ok"))
    return {"forgotten": forgot.get("forgotten", []), "restarted": restarted}


def _declare(form: dict, backhaul: str) -> None:
    """Record the variant — unticked means NOT fitted, the same rule the
    sensors step applies to probes."""
    update_config(
        current_app.config["CONFIG_PATH"],
        {
            "backhaul": backhaul,
            "lora_enabled": form.get("lora_enabled") == "on",
            "gps_enabled": form.get("gps_enabled") == "on",
        },
    )


@setup_bp.route("/network", methods=["GET", "POST"])
@login_required
def network() -> ResponseReturnValue:
    """
    The LAST step (site flow v2). One form, three ways out:

    * ``join`` — record the variant, then join a scanned network. **This
      finishes setup.** The page is usually served over the unit's own setup
      hotspot, which goes DOWN the moment the join starts, so the phone may
      never see this response: the PIN is checked first, and on success the
      unit finishes on its own (other saved Wi-Fi forgotten, setup recorded,
      firmware restarted). On failure ``netctl`` brings the hotspot straight
      back and the reason is kept on the unit, so the page that reopens says
      why.
    * ``keep`` — the unit is already on a network (the bench, or a LAN visit):
      record the variant and finish on the network it is using.
    * ``declare`` — LTE or no network here: record the variant and go to the
      go/no-go page, whose Finish button completes setup. "No network here" is
      a first-class outcome: the unit buffers locally.
    * ``rescan`` — refresh the list.
    """
    from diagnostics.explain import explain
    from utils.netctl import ap_active, ap_credentials, join_network, scan_networks
    from utils.netinfo import wifi_status

    config_path = current_app.config["CONFIG_PATH"]

    if request.method == "POST":
        action = request.form.get("action", "")
        if action in ("join", "keep") and current_app.config.get("PIN") == _FACTORY_PIN:
            # Checked BEFORE the radio is touched: after a successful join
            # nobody on the hotspot can be sent back to the PIN step.
            flash("Set your own PIN first — the factory PIN is not allowed.", "error")
            return redirect(url_for("setup.pin"))
        if action == "join":
            ssid = request.form.get("ssid", "").strip()
            password = request.form.get("password", "")
            if not _SSID_RE.match(ssid):
                flash("Pick a network from the list.", "error")
                return redirect(url_for("setup.network"))
            _declare(request.form, "wifi")
            ap_ssid, ap_pass = ap_credentials()
            result = join_network(ssid, password or None, ap_ssid=ap_ssid, ap_passphrase=ap_pass)
            if result.get("connected"):
                current_app.config.pop("LAST_JOIN", None)
                finished = _finish_setup(ssid)
                return render_template(
                    "setup/joined.html",
                    steps=_steps(),
                    step="done",
                    ssid=ssid,
                    identity=_identity(),
                    forgotten=finished["forgotten"],
                )
            # Kept on the unit, not flashed: a flash rides in this response,
            # and this response is usually lost with the hotspot.
            current_app.config["LAST_JOIN"] = {
                "ssid": ssid,
                "error": result.get("error") or "no reason given",
                "ap_restored": bool(result.get("ap_restored")),
            }
            return redirect(url_for("setup.network"))
        if action == "keep":
            current = wifi_status().get("ssid")
            if ap_active() or not current:
                flash("This unit is not on a network yet — pick one and join it.", "error")
                return redirect(url_for("setup.network"))
            _declare(request.form, "wifi")
            finished = _finish_setup(current)
            return render_template(
                "setup/joined.html",
                steps=_steps(),
                step="done",
                ssid=current,
                identity=_identity(),
                forgotten=finished["forgotten"],
            )
        if action == "declare":
            backhaul = request.form.get("backhaul", "")
            if backhaul not in ("lte", "none"):
                flash("Choose how this unit reaches the cloud.", "error")
                return redirect(url_for("setup.network"))
            _declare(request.form, backhaul)
            if backhaul == "none":
                flash(
                    "Recorded: no network at this site. The unit buffers readings locally "
                    "and uploads when a link exists.",
                    "info",
                )
            return redirect(url_for("setup.done"))
        # "rescan" and anything else: fall through to a fresh render.
        return redirect(url_for("setup.network"))

    config = read_config(config_path)
    on_ap = ap_active()
    wifi = wifi_status()
    # While the hotspot is up, the "associated network" NetworkManager reports
    # is the hotspot itself — not a site network.
    station_ssid = None if on_ap else wifi.get("ssid")
    cards = (
        {
            "wifi": explain(
                "wifi",
                wifi["state"],
                {"ssid": wifi["ssid"], "rssi": wifi["rssi_dbm"]},
            )
        }
        if station_ssid
        else {}
    )
    networks = scan_networks() if (on_ap or not station_ssid) else []
    return render_template(
        "setup/network.html",
        steps=_steps(),
        step="network",
        wifi=wifi,
        station_ssid=station_ssid,
        cards=cards,
        on_ap=on_ap,
        networks=networks,
        last_join=current_app.config.get("LAST_JOIN"),
        backhaul=str(config.get("backhaul") or "wifi"),
        lora_enabled=bool(config.get("lora_enabled", True)),
        gps_enabled=bool(config.get("gps_enabled", True)),
        identity=_identity(),
    )


@setup_bp.route("/cloud", methods=["GET", "POST"])
@login_required
def cloud() -> ResponseReturnValue:
    config = read_config(current_app.config["CONFIG_PATH"])
    if request.method == "GET" and is_carded(config):
        # The bench card wrote the key and the bench verified it.
        return redirect(url_for("setup.sensors"))
    if request.method == "POST":
        if request.form.get("skip"):
            flash(
                "Cloud connection skipped — you can add the key later under Provisioning.", "info"
            )
            return redirect(url_for("setup.sensors"))
        key = request.form.get("api_key", "").strip()
        # LoRaWAN OTAA credentials come from the same claim as the HTTP key.
        # Optional: a WiFi-only site never needs them, but a unit that finishes
        # setup without them can never join — the wizard used to omit them
        # entirely, so every wizard-commissioned unit had the all-zero sentinel.
        app_key = request.form.get("app_key", "").strip()
        app_eui = request.form.get("app_eui", "").strip()
        lora_updates: dict[str, str] = {}
        lora_error = None
        if app_key:
            if _APP_KEY_RE.match(app_key):
                lora_updates["app_key"] = app_key.lower()
            else:
                lora_error = "The LoRa AppKey must be exactly 32 hex characters."
        if app_eui:
            if _APP_EUI_RE.match(app_eui):
                lora_updates["app_eui"] = app_eui.lower()
            else:
                lora_error = "The JoinEUI must be exactly 16 hex characters."

        if not _CLOUD_KEY_RE.match(key):
            flash("That doesn't look like a device API key (16–128 letters/numbers).", "error")
        elif lora_error:
            flash(lora_error, "error")
        else:
            update_config(
                current_app.config["CONFIG_PATH"],
                {"cloud_enabled": True, "api_key": key, **lora_updates},
            )
            # Verify the key against the cloud NOW rather than reporting
            # "saved" and letting a mistyped key surface as a device that
            # simply never appears online. The probe is read-only.
            from utils.netinfo import verify_device_key

            check = verify_device_key(
                config.get(
                    "cloud_api_base",
                    "https://us-central1-waterquality-trading.cloudfunctions.net/app",
                ),
                _identity()["device_id"],
                key,
            )
            if check["state"] == "ok":
                flash("Cloud key saved and verified — the cloud accepted this unit.", "success")
            elif check["state"] == "degraded":
                flash(f"Key saved, but {check['detail']} Check it before you leave.", "error")
            else:
                flash(
                    "Key saved, but the cloud could not be reached to verify it. "
                    "Re-check on the Finish step.",
                    "info",
                )
            return redirect(url_for("setup.sensors"))
    return render_template(
        "setup/cloud.html",
        steps=STEPS,
        step="cloud",
        key_set=bool(config.get("api_key")),
        identity=_identity(),
    )


# Probes the installer declares on the Sensors step. The analog four are the
# ones that used to be ASSUMED fitted — the assumption that let a bare board
# publish pH for nine hours. RS485 probes already had their own toggles.
FITTABLE_PROBES = (
    ("ph_enabled", "pH"),
    ("tds_enabled", "TDS"),
    ("turbidity_enabled", "Turbidity"),
    ("temperature_enabled", "Temperature"),
    ("orp_enabled", "ORP (analog)"),
    # Flow meter, pulse type, on the GPIO harness (2.3.0). The RS485 clamp-on
    # meter is declared on the RS485 page like the other bus devices.
    ("flow_pulse_enabled", "Flow meter (pulse, GPIO harness)"),
)


_CORE_PROBES = ("ph_enabled", "tds_enabled", "turbidity_enabled", "temperature_enabled")
_PROBE_NAMES = {
    "ph": "pH",
    "tds": "TDS",
    "turbidity": "turbidity",
    "temperature": "temperature",
    "orp": "ORP",
    "chlorine": "chlorine",
    "conductivity": "conductivity",
    "salinity": "salinity",
    "flow": "flow meter",
}


@setup_bp.route("/sensors", methods=["GET", "POST"])
@login_required
def sensors() -> ResponseReturnValue:
    path = current_app.config["CONFIG_PATH"]

    if request.method == "POST":
        # An unchecked box is a real declaration of "not fitted", so every key
        # is written explicitly rather than only the checked ones. Anything
        # omitted would keep its previous value and the operator would have no
        # way to un-declare a probe they had removed.
        updates = {key: (request.form.get(key) == "on") for key, _ in FITTABLE_PROBES}
        update_config(path, updates)
        fitted = [label for key, label in FITTABLE_PROBES if updates[key]]
        flash(
            (
                "Fitted probes recorded: " + ", ".join(fitted) + ". "
                "Takes effect when setup finishes."
                if fitted
                else "No probes declared — this unit will record no water data until "
                "one is fitted and declared here."
            ),
            "success" if fitted else "info",
        )
        return redirect(url_for("setup.sensors"))

    config = read_config(path)
    db = DBReader(current_app.config["DB_PATH"])
    try:
        readings = db.get_readings(limit=30)
    except Exception:
        readings = []
    cards = sensor_cards(readings, orp_enabled=bool(config.get("orp_enabled")), config=config)
    ready = all(c["status"] in ("ok", "disabled") for c in cards.values())
    # Absent key = fitted for the core four ONLY, matching health.py — a unit
    # upgrading from before these keys existed must not appear to have lost
    # its probes. ORP and the flow meter are opt-in: absent means not fitted.
    # (The checkbox used to default flow to ticked while the health card said
    # "flow meter is not installed" — two answers on one page.)
    fitment = {key: bool(config.get(key, key in _CORE_PROBES)) for key, _ in FITTABLE_PROBES}
    # Lead with the probes that are fitted; name the rest in one line rather
    # than five grey "not installed" cards burying the four that matter.
    fitted_cards = {k: c for k, c in cards.items() if c.get("status") != "disabled"}
    not_fitted = [_PROBE_NAMES.get(k, k) for k, c in cards.items() if c.get("status") == "disabled"]
    return render_template(
        "setup/sensors.html",
        steps=_steps(),
        step="sensors",
        cards=cards,
        fitted_cards=fitted_cards,
        not_fitted=not_fitted,
        ready=ready,
        have_readings=bool(readings),
        probes=FITTABLE_PROBES,
        fitment=fitment,
        carded=is_carded(config),
    )


@setup_bp.route("/done", methods=["GET", "POST"])
@login_required
def done() -> ResponseReturnValue:
    config = read_config(current_app.config["CONFIG_PATH"])
    db = DBReader(current_app.config["DB_PATH"])
    try:
        readings = db.get_readings(limit=30)
        count = db.get_reading_count()
        pending = db.get_pending_count()
        session = db.get_lorawan_session()
    except Exception:
        readings, count, pending, session = [], 0, 0, None

    s_cards = sensor_cards(readings, orp_enabled=bool(config.get("orp_enabled")), config=config)
    sys_cards = system_cards(readings, config, session, count, pending=pending)
    checklist = {**s_cards, **sys_cards}
    overall = worst_status(checklist)

    if request.method == "POST":
        # The wizard cannot finish on the factory PIN. The PIN step refuses
        # 1234 on its own page, but nothing stopped a unit reaching Finish
        # with the shipped PIN still in place (test #2 of the commissioning
        # plan) — and a finished unit with PIN 1234 is a unit anyone on the
        # site network can drive.
        if current_app.config.get("PIN") == _FACTORY_PIN:
            flash("Set your own PIN before finishing — the factory PIN is not allowed.", "error")
            return redirect(url_for("setup.pin"))
        from utils.netctl import ap_active
        from utils.netinfo import wifi_status

        # Keep the network the unit is on (a Wi-Fi site finishing here from
        # the LAN); on an LTE or no-network site keep none — the bench
        # network must not follow the unit into the field.
        keep = None
        if str(config.get("backhaul") or "wifi") == "wifi" and not ap_active():
            keep = wifi_status().get("ssid")
        finished = _finish_setup(keep)
        if finished["restarted"]:
            flash("Setup complete — the unit is restarting to apply everything.", "success")
        else:
            flash(
                "Setup saved. The monitoring service will pick it up on its next restart.",
                "info",
            )
        return redirect(url_for("status.index"))

    return render_template(
        "setup/done.html",
        steps=_steps(),
        step="done",
        checklist=checklist,
        overall=overall,
        pending=pending,
        reading_count=count,
        backhaul=str(config.get("backhaul") or "wifi"),
    )
