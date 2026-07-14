"""
Service Window — Flask application factory.

Lightweight local web UI for WQM-1 commissioning, calibration, and diagnostics.
"""

import logging
import os
import secrets

from flask import Flask

from service_window.auth import auth_bp


def create_app(config: dict | None = None) -> Flask:
    """Create and configure the Flask app."""
    app = Flask(__name__)

    # Load service window config from YAML or environment
    sw_config = _load_sw_config()
    if config:
        sw_config.update(config)

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

    # Direct Flask-config overrides (UPPERCASE keys — used by tests and any
    # embedder that wants to bypass the YAML-derived lowercase mapping above).
    if config:
        app.config.update({k: v for k, v in config.items() if k.isupper()})

    # Register blueprints
    app.register_blueprint(auth_bp)

    from service_window.routes.calibration import calibration_bp
    from service_window.routes.diagnostics import diagnostics_bp
    from service_window.routes.lora import lora_bp
    from service_window.routes.provision import provision_bp
    from service_window.routes.relays import relays_bp
    from service_window.routes.rs485 import rs485_bp
    from service_window.routes.sensors import sensors_bp
    from service_window.routes.settings import settings_bp
    from service_window.routes.setup import needs_setup, setup_bp
    from service_window.routes.status import status_bp

    app.register_blueprint(status_bp)
    app.register_blueprint(sensors_bp)
    app.register_blueprint(calibration_bp)
    app.register_blueprint(rs485_bp)
    app.register_blueprint(lora_bp)
    app.register_blueprint(relays_bp)
    app.register_blueprint(diagnostics_bp)
    app.register_blueprint(provision_bp)
    app.register_blueprint(setup_bp)
    app.register_blueprint(settings_bp)

    # Until the factory PIN is replaced and the setup wizard finished, every
    # page funnels into /setup — a unit can't be left half-commissioned by
    # accident, and the shipped PIN can't quietly stay in service.
    @app.before_request
    def _force_setup():  # type: ignore[reportUnusedFunction]
        from flask import redirect, request

        allowed = ("/setup", "/login", "/logout", "/static", "/provision/qr.svg")
        if request.path.startswith(allowed):
            return None
        if needs_setup(app.config):
            return redirect("/setup/")
        return None

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
