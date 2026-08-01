"""
Service Window — Flask application factory.

Lightweight local web UI for WQM-1 commissioning, calibration, and diagnostics.
"""

import logging
import os
import secrets

from flask import Flask, flash, redirect, request
from werkzeug.wrappers.response import Response

from service_window.auth import auth_bp
from service_window.config_editor import ConfigWriteError

# Canonical (lowercase) setting names, keyed by the Flask-style uppercase
# spelling a caller naturally reaches for.
#
# The YAML `service_window:` section uses lowercase keys, so that is what this
# factory reads. But an override dict passed to create_app() is destined for
# app.config, where the convention is uppercase — so every caller wrote
# {"CONFIG_PATH": ...}, which the lowercase reads below never saw. The override
# was silently dropped and the production default won.
#
# In production that was invisible (__main__ calls create_app() with no args),
# but under test it meant CONFIG_PATH, DB_PATH, CAL_PATH and CMD_SOCK all
# pointed at the real /etc/bluesignal and /var/lib/bluesignal paths instead of
# tmp_path — so running the suite on an actual device would have written to
# live config. Accept both spellings rather than picking a winner.
_SETTING_ALIASES = {
    "SERVICE_PORT": "port",
    "PIN": "pin",
    # nosec B105 - a setting-name mapping, not a credential. The value is the
    # YAML key to read; the secret itself comes from SW_SECRET_KEY or a fresh
    # token_hex(32) below.
    "SECRET_KEY": "secret_key",  # nosec B105
    "DB_PATH": "db_path",
    "CONFIG_PATH": "config_path",
    "CAL_PATH": "cal_path",
    "CMD_SOCK": "cmd_sock",
}


def create_app(config: dict | None = None) -> Flask:
    """Create and configure the Flask app.

    `config` may use either the canonical lowercase setting names or their
    Flask-style uppercase equivalents. Any key this factory does not recognise
    (e.g. ``TESTING``) is passed through to ``app.config`` untouched.
    """
    app = Flask(__name__)

    # Load service window config from YAML or environment
    sw_config = _load_sw_config()
    passthrough: dict = {}
    for key, value in (config or {}).items():
        canonical = _SETTING_ALIASES.get(key)
        if canonical is not None:
            sw_config[canonical] = value
        elif key in _SETTING_ALIASES.values():
            sw_config[key] = value
        else:
            passthrough[key] = value

    app.config["SERVICE_PORT"] = sw_config.get("port", 8080)
    app.config["PIN"] = str(sw_config.get("pin", "1234"))
    app.config["SECRET_KEY"] = sw_config.get(
        "secret_key", os.environ.get("SW_SECRET_KEY", secrets.token_hex(32))
    )

    # Database path
    app.config["DB_PATH"] = sw_config.get("db_path", "/var/lib/bluesignal/wqm1.db")
    app.config["CONFIG_PATH"] = sw_config.get("config_path", "/etc/bluesignal/config.yaml")
    app.config["CAL_PATH"] = sw_config.get("cal_path", "/etc/bluesignal/calibration.yaml")
    app.config["CMD_SOCK"] = sw_config.get("cmd_sock", "/var/run/bluesignal/cmd.sock")

    # Flask-native keys the caller set that this factory has no opinion on —
    # TESTING being the one the suite relies on.
    app.config.update(passthrough)

    # Register blueprints
    app.register_blueprint(auth_bp)

    from service_window.routes.calibration import calibration_bp
    from service_window.routes.diagnostics import diagnostics_bp
    from service_window.routes.lora import lora_bp
    from service_window.routes.provision import provision_bp
    from service_window.routes.relays import relays_bp
    from service_window.routes.sensors import sensors_bp
    from service_window.routes.status import status_bp

    app.register_blueprint(status_bp)
    app.register_blueprint(sensors_bp)
    app.register_blueprint(calibration_bp)
    app.register_blueprint(lora_bp)
    app.register_blueprint(relays_bp)
    app.register_blueprint(diagnostics_bp)
    app.register_blueprint(provision_bp)

    # One handler rather than a try/except at each of the six update_config
    # call sites. A failed config write used to surface as a bare Flask 500,
    # which from the browser is indistinguishable from "the device rejected my
    # key" — the operator has no way to know it was a filesystem permission.
    # Now the real reason, and the fix, is flashed onto the page they are on.
    @app.errorhandler(ConfigWriteError)
    def _config_write_failed(exc: ConfigWriteError) -> Response:
        logging.getLogger("wqm1.service_window").error("Config write failed: %s", exc)
        flash(str(exc), "error")
        return redirect(request.referrer or "/")

    return app


def _load_sw_config() -> dict:
    """Load service_window section from /etc/bluesignal/config.yaml."""
    try:
        from pathlib import Path

        import yaml

        path = Path("/etc/bluesignal/config.yaml")
        if path.exists():
            with path.open() as f:
                raw = yaml.safe_load(f) or {}
            return raw.get("service_window", {})
    except Exception as e:
        logging.getLogger("wqm1.service_window").debug("Could not load SW config: %s", e)
    return {}
