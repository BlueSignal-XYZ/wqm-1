import logging
from pathlib import Path

_logger = logging.getLogger("wqm1.service_window")


def read_firmware_version() -> str:
    """Read firmware version from VERSION file."""
    try:
        for p in ["/opt/bluesignal/VERSION", str(Path(__file__).parents[2] / "VERSION")]:
            path = Path(p)
            if path.exists():
                return path.read_text().strip()
    except Exception:
        _logger.debug("Could not read VERSION file")
    return "unknown"
