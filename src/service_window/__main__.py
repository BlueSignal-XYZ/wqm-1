"""Entry point for the service window: python3 -m service_window

    python3 -m service_window                       # a real unit: /etc/bluesignal
    python3 -m service_window --config /path.yaml   # a virtual unit's own config
                              [--port 8081]

``--config`` (or ``BLUESIGNAL_CONFIG``) points the factory at a different
firmware config file; its ``service_window:`` block supplies db_path,
cmd_sock, cal_path and the port, and its own path becomes CONFIG_PATH. That
is what lets ten virtual units each run a Service Window on one host.
"""

import argparse
import os

from service_window.app import create_app


def _parse(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="service_window")
    parser.add_argument("--config", default=os.environ.get("BLUESIGNAL_CONFIG"))
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--host", default="0.0.0.0")  # nosec B104 — the AP/LAN interface
    return parser.parse_args(argv)


def build(argv: list[str] | None = None):  # type: ignore[no-untyped-def]
    args = _parse(argv)
    overrides: dict[str, object] = {}
    if args.config:
        overrides["CONFIG_PATH"] = args.config
    if args.port is not None:
        overrides["SERVICE_PORT"] = args.port
    app = create_app(overrides or None, config_path=args.config)
    return app, args


if __name__ == "__main__":
    app, args = build()
    app.run(host=args.host, port=app.config.get("SERVICE_PORT", 8080))
