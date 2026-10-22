"""
Guided first-boot setup wizard — the installer-first commissioning path.

Replaces the CLI wizard as the primary way a dealer/installer commissions a
unit from a phone browser: set a real PIN (the shipped `1234` is banned),
confirm device identity (v2 QR), watch live sensor checks come green, join
the site network, and let the unit register itself with BlueSignal Cloud.
The installer never pastes a device API key. Config writes go through the
atomic YAML editor; the firmware is restarted through the command socket
instead of telling anyone to run systemctl.

While the unit still has the factory PIN and setup hasn't been completed,
every page redirects here (see app.py) — a unit can't be left half set up
by accident.

Site flow v2 (2026-10-03, onboarding v2 2026-10-19): **the network step is
last before cloud registration, and joining the site's Wi-Fi is what
finishes setup on a Wi-Fi site.** The wizard is usually served over the
unit's own setup hotspot, which goes down the moment a join succeeds; the
join handler finishes setup on the unit itself (PIN checked before the radio
is touched, other saved Wi-Fi forgotten, setup marked complete, firmware
restarted) and kicks self-registration. A failed join keeps its reason on
the unit. A unit whose bench card already carries its identity and cloud
key skips the identity step (``CARDED_STEPS``). The HTTP key paste lives
only as a bench/dev override, clearly marked, not on the installer path.
"""

import contextlib
import json
import logging
import re
import threading

from flask import (
    Blueprint,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask.typing import ResponseReturnValue

from cloud.register import kick_register, registration_view
from diagnostics.boot_status import read_published_status
from service_window import read_firmware_version
from service_window.auth import login_required
from service_window.cmd_client import send_command
from service_window.config_editor import read_config, update_config, update_config_section
from service_window.db_reader import DBReader
from service_window.health import (
    apply_board_fault,
    board_fault_banner,
    firmware_snapshot,
    sensor_cards,
    system_cards,
    worst_status,
)
from utils.cardmove import card_status_payload, confirm_binding
from utils.device_keys import identity_qr_payload
from utils.identity import ap_name
from utils.netctl import forget_saved_wifi
from utils.wifi_setup import (
    HIDDEN_NOTE,
    WEAK_SIGNAL_MESSAGE,
    annotate_network,
    band_notice,
    block_reason,
    blocked_networks,
    cell_data_note,
    find_network,
    has_hidden,
    is_weak_rssi,
    joinable_networks,
    missing_probe_message,
    normalize_password,
    password_problem,
    probe_rows,
    unconfirmed_missing,
    unseen_ssid_notice,
)

setup_bp = Blueprint("setup", __name__, url_prefix="/setup")
logger = logging.getLogger("wqm1.setup")


@setup_bp.context_processor
def _firmware_starting_ctx() -> dict[str, bool]:
    """Every setup template can show the starting banner and poll.

    Not the Wi-Fi page: it needs nothing from the monitoring process, and a
    "checking sensors" line there points the installer at the wrong job.
    """
    if request.endpoint == "setup.network":
        return {"firmware_starting": False}
    try:
        ready, _report = firmware_snapshot(current_app.config["CMD_SOCK"])
    except Exception:  # noqa: BLE001 — the page must still render
        ready = False
    return {"firmware_starting": not ready}


_PIN_RE = re.compile(r"^\d{4,8}$")
_CLOUD_KEY_RE = re.compile(r"^[0-9a-zA-Z]{16,128}$")
# LoRaWAN OTAA credentials, both issued by the cloud at claim time.
_APP_KEY_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_APP_EUI_RE = re.compile(r"^[0-9a-fA-F]{16}$")
_ZERO_APP_KEY = "00000000000000000000000000000000"
_FACTORY_PIN = "1234"

# Network, then cloud registration: see the module docstring. ``STEPS`` stays
# exported for callers that render the full track; ``steps_for`` is what
# routes use. Join still finishes setup because the hotspot drops.
STEPS = ["welcome", "pin", "identity", "sensors", "network", "cloud", "done"]
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


def _kick_register() -> None:
    """Start one self-register attempt. Never blocks the setup page.

    The POST can take ``REQUEST_TIMEOUT_S`` (15s) or hang on a half-up
    link. Firmware ``DeviceRegisterWorker`` retries in its own thread;
    this only fires an extra attempt the moment a join succeeds. Values
    are captured here so the worker thread does not need the Flask
    request context.
    """
    config_path = current_app.config["CONFIG_PATH"]
    config = read_config(config_path)
    api_base = str(config.get("cloud_api_base") or "")
    device_id = _identity()["device_id"]
    fw = read_firmware_version()

    def _run() -> None:
        try:
            kick_register(
                api_base=api_base,
                device_id=device_id,
                fw=fw,
                config_path=config_path,
            )
        except Exception as e:  # noqa: BLE001 — join/page must still finish
            logger.warning("background register kick failed: %s", e)

    threading.Thread(target=_run, name="wqm1-register-kick", daemon=True).start()


def _ap_ssid() -> str:
    try:
        return ap_name()
    except Exception:  # noqa: BLE001 — the landing note still has to render
        return "WQM1-xxxx"


def _live_probe_rows() -> list[dict[str, str]]:
    """ADC from the firmware (or the status file) and 1-Wire from the status file."""
    ready, report = firmware_snapshot(current_app.config["CMD_SOCK"])
    status = read_published_status()
    adc = str(report.get("adc") or status.get("adc") or "")
    onewire = str(status.get("onewire") or "")
    rows = probe_rows(adc, onewire)
    if ready:
        return rows
    # Firmware has not answered. Empty/unknown must not look like a missing
    # probe. A status file that already says faulted/absent is a report from
    # a previous firmware run (#139) and still needs the confirm tick.
    for row in rows:
        if row.get("kind") == "unchecked":
            row["word"] = "checking"
    return rows


def _probe_block(form: dict | None = None) -> tuple[list[dict[str, str]], str | None]:
    rows = _live_probe_rows()
    if form is None:
        return rows, None
    pending = unconfirmed_missing(rows, form)
    if not pending:
        return rows, None
    return rows, missing_probe_message(pending)


@setup_bp.route("/")
@login_required
def welcome() -> ResponseReturnValue:
    """The hotspot's landing page is the Wi-Fi page.

    Getting online is the only thing the unit asks for on site. Probes,
    thresholds, alerts and the owner are set from BlueSignal Cloud once it
    is online (founder, 2026-10-22, after WQM-10001). A PIN, a probe list
    and a variant before the join were what made a 2-minute step an hour.
    """
    return redirect(url_for("setup.network"))


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
        qr_json=json.dumps(identity_qr_payload(), separators=(",", ":")),
        registration=registration_view(),
        next_url=url_for("setup.sensors"),
    )


_BACKHAULS = ("wifi", "lte", "none")
_SSID_RE = re.compile(r"^[^\x00-\x1f]{1,32}$")


def _finish_setup(keep_ssid: str | None) -> dict:
    """Mark setup complete on the unit: forget every saved Wi-Fi network but
    the one in use, record completion, restart the firmware. Shared by the
    Wi-Fi join (which finishes setup — its response may never reach the
    phone) and the Finish button (LTE / no-network sites). The caller has
    already refused the factory PIN."""
    forgot = forget_saved_wifi(keep_ssid)
    with contextlib.suppress(OSError):
        confirm_binding()
    update_config_section(
        current_app.config["CONFIG_PATH"], "service_window", {"setup_completed": True}
    )
    restarted = bool(send_command(current_app.config["CMD_SOCK"], "restart").get("ok"))
    return {"forgotten": forgot.get("forgotten", []), "restarted": restarted}


def _notice_for(ssid: str, result: dict, networks: list[dict]) -> dict:
    """Join notice, plus the 2.4 GHz sentence when that name was not in the scan."""
    notice = _join_notice(ssid, result)
    extra = unseen_ssid_notice(networks, ssid)
    told = str(notice.get("error") or "")
    if extra and extra not in told:
        notice["error"] = f"{told} {extra}".strip()
    return notice


def _join_notice(ssid: str, result: dict) -> dict:
    """What the setup page says when a join did not finish.

    ``message`` is the installer sentence. ``error`` is the raw nmcli line,
    kept for logs and for callers that have not grown a message yet.
    """
    told = result.get("message") or result.get("error") or "The unit could not join that network."
    return {
        "ssid": ssid,
        "error": told,
        "reason": result.get("reason"),
        "ap_restored": bool(result.get("ap_restored")),
    }


def _declare(form: dict, backhaul: str) -> None:
    """Record the variant — unticked means NOT fitted, the same rule the
    sensors step applies to probes."""
    values: dict[str, object] = {"backhaul": backhaul}
    # The Wi-Fi page no longer asks about radios. Only a form that carries
    # the radio block (the declare path on a bench) may change them; a
    # missing box is not an untick.
    if "radios_declared" in form:
        values["lora_enabled"] = form.get("lora_enabled") == "on"
        values["gps_enabled"] = form.get("gps_enabled") == "on"
    update_config(current_app.config["CONFIG_PATH"], values)


@setup_bp.route("/network", methods=["GET", "POST"])
@login_required
def network() -> ResponseReturnValue:
    """
    The LAST step (site flow v2). One form, three ways out:

    * ``join`` — record the variant, then join a scanned network. **This
      finishes setup only when wlan0 is associated to that SSID, has a
      usable IPv4 address (not a link-local 169.254 address), and can reach
      the gateway or DNS.** The page is usually served over the unit's own
      setup hotspot, which goes DOWN the moment the join starts, so the phone
      may never see this response: the PIN is checked first, and on success
      the unit finishes on its own (other saved Wi-Fi forgotten, setup recorded,
      firmware restarted). On failure ``netctl`` stores the installer-readable
      reason before it brings the hotspot and the port-80 portal back, so the
      page that reopens says why.
    * ``keep`` — the unit is already on a network (the bench, or a LAN visit):
      record the variant and finish on the network it is using.
    * ``declare`` — LTE or no network here: record the variant and go to the
      go/no-go page, whose Finish button completes setup. "No network here" is
      a first-class outcome: the unit buffers locally.
    * ``rescan`` — refresh the list.
    """
    from diagnostics.explain import explain
    from utils.netctl import (
        ap_active,
        ap_credentials,
        join_network,
        scan_networks,
        usable_ipv4,
    )
    from utils.netinfo import wifi_status

    config_path = current_app.config["CONFIG_PATH"]

    def _scanned() -> list[dict]:
        on_ap_now = ap_active()
        wifi_now = wifi_status()
        station = None if on_ap_now else wifi_now.get("ssid")
        if not (on_ap_now or not station):
            return []
        return [annotate_network(row) for row in scan_networks()]

    def _render_network(**extra: object) -> str:
        config = read_config(config_path)
        on_ap_now = ap_active()
        wifi_now = wifi_status()
        station_ssid = None if on_ap_now else wifi_now.get("ssid")
        cards = (
            {
                "wifi": explain(
                    "wifi",
                    wifi_now["state"],
                    {"ssid": wifi_now["ssid"], "rssi": wifi_now["rssi_dbm"]},
                )
            }
            if station_ssid
            else {}
        )
        scanned = _scanned()
        rows, _ignored = _probe_block()
        rssi = wifi_now.get("rssi_dbm")
        choices = joinable_networks(scanned)
        picked = str(extra.get("selected_ssid") or "")
        hidden_prefill = picked
        if not picked or any(n.get("ssid") == picked for n in choices):
            hidden_prefill = ""
        return render_template(
            "setup/network.html",
            # One step on site now; no progress track for a single page.
            steps=["network"],
            step="network",
            wifi=wifi_now,
            station_ssid=station_ssid,
            station_weak=is_weak_rssi(rssi if isinstance(rssi, int) else None),
            cards=cards,
            on_ap=on_ap_now,
            cell_note=cell_data_note(_ap_ssid()) if on_ap_now else "",
            networks=choices,
            blocked=blocked_networks(scanned),
            hidden_prefill=hidden_prefill,
            band_notice=band_notice(scanned),
            hidden_note=HIDDEN_NOTE if has_hidden(scanned) else None,
            weak_message=WEAK_SIGNAL_MESSAGE,
            last_join=current_app.config.get("LAST_JOIN"),
            backhaul=str(config.get("backhaul") or "wifi"),
            lora_enabled=bool(config.get("lora_enabled", True)),
            gps_enabled=bool(config.get("gps_enabled", True)),
            identity=_identity(),
            registration=registration_view(),
            probe_rows=rows,
            password=extra.get("password", ""),
            trim_warning=extra.get("trim_warning"),
            selected_ssid=extra.get("selected_ssid", ""),
        )

    if request.method == "POST":
        action = request.form.get("action", "")
        if action == "join":
            hidden_name = request.form.get("hidden_ssid", "").strip()
            ssid = hidden_name or request.form.get("ssid", "").strip()
            raw_password = request.form.get("password", "")
            if not _SSID_RE.match(ssid):
                flash("Pick a network from the list.", "error")
                return redirect(url_for("setup.network"))
            scanned = _scanned()
            match = find_network(scanned, ssid)
            refused = block_reason(match)
            if refused:
                flash(refused, "error")
                return redirect(url_for("setup.network"))
            keep_space = request.form.get("keep_trailing_space") == "on"
            acked = request.form.get("trim_ack") == "yes"
            password, trim_warning = normalize_password(
                raw_password, keep_trailing_space=keep_space
            )
            if trim_warning and not acked:
                # Show the warning before the radio is touched. The trimmed
                # password stays in the form so the next tap can join it.
                return _render_network(
                    password=password, trim_warning=trim_warning, selected_ssid=ssid
                )
            problem = password_problem(password, secured=bool(match and match.get("secured")))
            if problem:
                flash(problem, "error")
                return redirect(url_for("setup.network"))
            _declare(request.form, "wifi")
            ap_ssid, ap_pass = ap_credentials()

            def _remember(pending: dict) -> None:
                # Before the hotspot is raised again. A phone that rejoins
                # the moment the AP is up must already find this sentence.
                current_app.config["LAST_JOIN"] = _notice_for(ssid, pending, scanned)

            result = join_network(
                ssid,
                password or None,
                ap_ssid=ap_ssid,
                ap_passphrase=ap_pass,
                on_failure=_remember,
            )
            joined_ip = str(result.get("ip") or "")
            if result.get("connected") and usable_ipv4(joined_ip):
                current_app.config.pop("LAST_JOIN", None)
                _kick_register()
                finished = _finish_setup(ssid)
                rssi = result.get("rssi")
                return render_template(
                    "setup/joined.html",
                    steps=_steps(),
                    step="done",
                    ssid=ssid,
                    ip=joined_ip,
                    identity=_identity(),
                    registration=registration_view(),
                    forgotten=finished["forgotten"],
                    weak=is_weak_rssi(rssi if isinstance(rssi, int) else None),
                    weak_message=WEAK_SIGNAL_MESSAGE,
                    rssi=rssi,
                )
            # Kept on the unit, not flashed: a flash rides in this response,
            # and this response is usually lost with the hotspot. The hook
            # above wrote the same notice before the AP came back.
            current_app.config["LAST_JOIN"] = _notice_for(ssid, result, scanned)
            return redirect(url_for("setup.network"))
        if action == "keep":
            current_status = wifi_status()
            current = current_status.get("ssid")
            current_ip = str(current_status.get("ip") or "")
            if ap_active() or not current or not usable_ipv4(current_ip):
                flash(
                    "This unit is not on a network with an address yet — pick one and join it.",
                    "error",
                )
                return redirect(url_for("setup.network"))
            _declare(request.form, "wifi")
            _kick_register()
            finished = _finish_setup(current)
            rssi = current_status.get("rssi_dbm")
            return render_template(
                "setup/joined.html",
                steps=_steps(),
                step="done",
                ssid=current,
                ip=current_ip,
                identity=_identity(),
                registration=registration_view(),
                forgotten=finished["forgotten"],
                weak=is_weak_rssi(rssi if isinstance(rssi, int) else None),
                weak_message=WEAK_SIGNAL_MESSAGE,
                rssi=rssi,
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
                    "and registers / uploads when a link exists.",
                    "info",
                )
            return redirect(url_for("setup.cloud"))
        # "rescan" and anything else: fall through to a fresh render.
        return redirect(url_for("setup.network"))

    return _render_network()


@setup_bp.route("/cloud", methods=["GET", "POST"])
@login_required
def cloud() -> ResponseReturnValue:
    """Cloud registration status after the network step.

    The installer path has no key-paste field. Saving an API key here is a
    bench/dev override only.
    """
    config = read_config(current_app.config["CONFIG_PATH"])
    if request.method == "GET" and is_carded(config):
        return redirect(url_for("setup.done"))
    if request.method == "GET":
        _kick_register()
        config = read_config(current_app.config["CONFIG_PATH"])
    if request.method == "POST":
        if request.form.get("continue") or request.form.get("skip"):
            return redirect(url_for("setup.done"))
        key = request.form.get("api_key", "").strip()
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

        if key:
            if not _CLOUD_KEY_RE.match(key):
                flash("That doesn't look like a device API key (16–128 letters/numbers).", "error")
            elif lora_error:
                flash(lora_error, "error")
            else:
                update_config(
                    current_app.config["CONFIG_PATH"],
                    {"cloud_enabled": True, "api_key": key, **lora_updates},
                )
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
                    flash(
                        "Bench override saved and verified — the cloud accepted this unit.",
                        "success",
                    )
                elif check["state"] == "degraded":
                    flash(f"Key saved, but {check['detail']} Check it before you leave.", "error")
                else:
                    flash(
                        "Key saved, but the cloud could not be reached to verify it. "
                        "Re-check on the Finish step.",
                        "info",
                    )
                return redirect(url_for("setup.done"))
        elif lora_updates and not lora_error:
            update_config(current_app.config["CONFIG_PATH"], lora_updates)
            flash("LoRa credentials saved.", "success")
            return redirect(url_for("setup.done"))
        elif lora_error:
            flash(lora_error, "error")
        else:
            return redirect(url_for("setup.done"))
    return render_template(
        "setup/cloud.html",
        steps=_steps(),
        step="cloud",
        key_set=bool(config.get("api_key")),
        identity=_identity(),
        registration=registration_view(),
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
    fw_ready, report = firmware_snapshot(current_app.config["CMD_SOCK"])
    awaiting = (not fw_ready) or (not readings)
    cards = apply_board_fault(
        sensor_cards(
            readings,
            orp_enabled=bool(config.get("orp_enabled")),
            config=config,
            awaiting=awaiting,
        ),
        report if fw_ready or report.get("adc") == "faulted" else {},
    )
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
        board_fault=board_fault_banner(report),
        probe_rows=_live_probe_rows(),
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

    fw_ready, report = firmware_snapshot(current_app.config["CMD_SOCK"])
    awaiting = (not fw_ready) or (not readings)
    s_cards = apply_board_fault(
        sensor_cards(
            readings,
            orp_enabled=bool(config.get("orp_enabled")),
            config=config,
            awaiting=awaiting,
        ),
        report if fw_ready or report.get("adc") == "faulted" else {},
    )
    sys_cards = system_cards(readings, config, session, count, pending=pending, awaiting=awaiting)
    checklist = {**s_cards, **sys_cards}
    overall = worst_status(checklist)

    rows, probe_error = _probe_block(request.form if request.method == "POST" else None)
    if request.method == "POST":
        if probe_error:
            flash(probe_error, "error")
            return redirect(url_for("setup.done"))
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
        probe_rows=rows,
        registration=registration_view(),
    )


@setup_bp.route("/card-status")
@login_required
def card_status() -> ResponseReturnValue:
    """Flag the setup page can read later. No template, no Wi-Fi material.

    ``wifi_reconfirm`` is true when this card was commissioned on another Pi
    and still has that unit's site network. Boot will not join it. The page
    that should ask the installer to join again is not changed here.
    """
    return jsonify(card_status_payload())
