"""
OTA agent entry point — ``python3 -m ota``.

Run by systemd (``bluesignal-ota.service``) as root: the agent needs to flip
the /opt/bluesignal/current symlink, restart services, and reboot the host on
request from the Service Window. When OTA is disabled or the device has no API
key (not yet commissioned) it watches for reboot requests for one restart
interval and then exits — systemd's Restart=always + RestartSec acts as a slow
re-check of the settings, and the reboot path stays alive in the meantime.
"""

import logging
import signal
import sys
import threading
import types
from logging.handlers import RotatingFileHandler
from pathlib import Path

from ota.agent import OTAAgent
from utils.config import Settings, get_settings
from utils.identity import get_device_id

logger = logging.getLogger("wqm1.ota")

# How long to keep watching for host-reboot requests when OTA polling is off,
# before exiting so systemd restarts us and the settings are re-read. Matched to
# the unit's RestartSec=30, so the reboot button is live roughly half the time
# and worst-case latency on an OTA-disabled unit is about half a minute.
IDLE_REBOOT_WATCH_S = 30


def _setup_logging(settings: Settings) -> None:
    """Same pattern as src/main.py, but to ota.log next to the main log."""
    root = logging.getLogger("wqm1")
    root.setLevel(logging.INFO)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    log_path = Path(settings.log_path).parent / "ota.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fh = RotatingFileHandler(
            str(log_path),
            maxBytes=settings.log_max_bytes,
            backupCount=settings.log_backup_count,
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except Exception as e:
        root.warning("Could not open log file %s: %s", log_path, e)


def main() -> int:
    settings = get_settings()
    _setup_logging(settings)

    device_id = get_device_id()
    agent = OTAAgent(settings, device_id)
    stop_event = threading.Event()

    def _handle_signal(signum: int, frame: types.FrameType | None) -> None:
        logger.info("Received signal %d, stopping OTA agent...", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # Idle on OTA, but still the only root process that can reboot this host.
    idle_reason = None
    if not settings.ota_enabled:
        idle_reason = "OTA disabled (ota_enabled=false)"
    elif not settings.api_key:
        idle_reason = "no device API key configured"
    if idle_reason:
        logger.info("%s — not polling; watching for reboot requests", idle_reason)
        agent.watch_reboot_requests(stop_event, IDLE_REBOOT_WATCH_S)
        return 0

    logger.info(
        "OTA agent starting (device=%s, poll=%ds, installed=%s)",
        device_id,
        settings.ota_poll_s,
        agent.installed_version(),
    )
    agent.run_forever(stop_event)
    logger.info("OTA agent stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
