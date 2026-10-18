"""
Plain-text boot status on the FAT partition.

A field unit that never joins Wi-Fi cannot be read over the network. The
card can: `/boot/firmware` is the first partition, and a Mac mounts it.
`wqm1-status.txt` is rewritten on boot and whenever the sensor or radio
state changes. It names the firmware, the board, the I2C buses the kernel
enumerated, wlan0, the setup hotspot, and the last fatal error.

No secrets. A line that looks like a key, a passphrase, or a private key
is replaced before the file is written.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import subprocess  # nosec B404 - fixed argv, sudo -n of one installer script
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger("wqm1.boot_status")

_KEYS = (
    "firmware",
    "board",
    "i2c",
    "wlan0",
    "hotspot",
    "adc",
    "adc_error",
    "faulted",
    "fatal",
    "updated",
)

# The publisher installed by setup.sh. The firmware user may sudo -n it;
# it copies one file and takes no arguments.
_PUBLISH_SCRIPT = "/opt/bluesignal/current/scripts/wqm1-publish-status.sh"
_STATE_PATH = Path("/var/lib/bluesignal/wqm1-status.txt")
_BOOT_PATHS = (
    Path("/boot/firmware/wqm1-status.txt"),
    Path("/boot/wqm1-status.txt"),
)

_SECRET = re.compile(
    r"(api[_-]?key|app[_-]?key|passphrase|wifi-sec|psk=|BEGIN [A-Z ]*PRIVATE KEY)",
    re.IGNORECASE,
)


def status_paths() -> list[Path]:
    """Where a reader should look, most specific first."""
    env = os.environ.get("WQM1_STATUS_FILE")
    paths: list[Path] = []
    if env:
        paths.append(Path(env))
    paths.append(_STATE_PATH)
    paths.extend(_BOOT_PATHS)
    return paths


def parse_status(text: str) -> dict[str, str]:
    """Parse the key: value lines. Ignores the title and blank lines."""
    found: dict[str, str] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower()
        if key in _KEYS:
            found[key] = value.strip()
    return found


def read_published_status() -> dict[str, str]:
    """The newest status file a reader can open, or {}."""
    for path in status_paths():
        try:
            parsed = parse_status(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if parsed:
            return parsed
    return {}


def read_board_model() -> str:
    """Device-tree model, the same string the firmware profiles from."""
    try:
        raw = Path("/proc/device-tree/model").read_bytes().replace(b"\x00", b"")
    except OSError:
        return "unknown"
    text = raw.decode("utf-8", errors="replace").strip()
    return text or "unknown"


def read_i2c_summary() -> str:
    """Kernel I2C clients. Userspace devices (the ADS1115) are not listed
    here — the ``adc`` line is that probe. An empty bus is reported as such
    rather than by scanning it: a scan of a wedged bus is how a boot hangs."""
    root = Path("/sys/bus/i2c/devices")
    if not root.is_dir():
        return "not present"
    names = sorted(p.name for p in root.iterdir())
    return ", ".join(names) if names else "bus not enumerated"


def read_wlan_state(iface: str = "wlan0") -> str:
    """operstate from sysfs: up, down, dormant, or absent.

    cloud-init's ``Up=False`` is this file reading ``down`` (or the
    interface not existing yet). No NetworkManager call, so a stuck
    ``nmcli`` cannot block the status write."""
    path = Path(f"/sys/class/net/{iface}/operstate")
    if not path.exists():
        return "absent"
    try:
        return path.read_text(encoding="utf-8").strip() or "unknown"
    except OSError:
        return "unknown"


def read_hotspot_state() -> str:
    """``up`` when the setup AP profile is active, else ``down``.

    A failure to ask NetworkManager is ``unknown`` — not ``down`` — so a
    status file written while ``nmcli`` is missing does not claim the
    hotspot is off. ``ap_active`` collapses that failure to False, which
    is right for "should I raise the AP" and wrong for this file."""
    try:
        from utils.netctl import AP_CONNECTION_NAME, _nmcli
    except Exception as e:  # noqa: BLE001 — status must be writable anyway
        logger.debug("hotspot state unavailable: %s", e)
        return "unknown"
    try:
        rc, out, _ = _nmcli("-t", "-f", "NAME,DEVICE", "connection", "show", "--active")
    except Exception as e:  # noqa: BLE001
        logger.debug("hotspot state failed: %s", e)
        return "unknown"
    if rc != 0:
        return "unknown"
    active = any(line.startswith(f"{AP_CONNECTION_NAME}:") for line in out.splitlines())
    return "up" if active else "down"


def render_status(fields: dict[str, str]) -> str:
    """One plain-text snapshot. Every key is always present.

    A line that matches a credential pattern is replaced in full. Replacing
    only the matched token would leave the secret sitting after the colon.
    """
    lines = ["WQM-1 boot status"]
    for key in _KEYS:
        value = fields.get(key, "")
        line = f"{key}: {value}"
        if _SECRET.search(line):
            line = f"{key}: [redacted]"
        lines.append(line)
    return "\n".join(lines) + "\n"


def _firmware_version() -> str:
    try:
        from utils.config import FIRMWARE_VERSION

        return FIRMWARE_VERSION
    except Exception:  # noqa: BLE001
        return "unknown"


def _read_previous() -> dict[str, str]:
    for path in status_paths():
        try:
            parsed = parse_status(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if parsed:
            return parsed
    return {}


def _write_atomic(path: Path, text: str) -> bool:
    """Write ``path`` if its directory exists and is writable. The env
    override may point at a directory we are allowed to create; the FAT
    and /var/lib paths are never created here (a test must not mkdir
    them on a developer machine)."""
    env = os.environ.get("WQM1_STATUS_FILE")
    create_parent = env is not None and path == Path(env)
    parent = path.parent
    if create_parent:
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            return False
    if not parent.is_dir() or not os.access(parent, os.W_OK):
        return False
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.chmod(tmp, 0o644)
        tmp.replace(path)
    except OSError as e:
        logger.debug("status write %s failed: %s", path, e)
        with contextlib.suppress(OSError):
            tmp.unlink()
        return False
    return True


def _sudo_publish() -> bool:
    """Ask the root helper to copy the state file onto the FAT partition."""
    script = os.environ.get("WQM1_STATUS_PUBLISH", _PUBLISH_SCRIPT)
    if not Path(script).is_file():
        return False
    try:
        if os.geteuid() == 0:
            return False
    except AttributeError:
        return False
    try:
        out = subprocess.run(  # nosec B603 - fixed argv, no shell
            ["sudo", "-n", script],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.debug("status publish helper failed: %s", e)
        return False
    if out.returncode != 0:
        logger.debug("status publish helper rc %s: %s", out.returncode, out.stderr.strip())
        return False
    return True


def write_boot_status(
    *,
    board: str | None = None,
    i2c: str | None = None,
    wlan0: str | None = None,
    hotspot: str | None = None,
    adc: str | None = None,
    adc_error: str | None = None,
    faulted: list[str] | None = None,
    fatal: str | None = None,
) -> str:
    """Refresh the status file and return the text that was written.

    Arguments override what this call knows. Anything omitted is kept from
    the previous file (so the access-point script does not wipe a sensor
    fault, and the firmware does not wipe a hotspot line it did not just
    measure) and, for the live radio and board lines, re-read.
    """
    prev = _read_previous()
    faulted_text = prev.get("faulted", "") if faulted is None else ", ".join(faulted)
    # The access-point script is the one that measured the hotspot. A later
    # write from the firmware (user pi, nmcli may be blocked) must not
    # replace that line with a failed probe.
    if hotspot is not None:
        hotspot_text = hotspot
    elif prev.get("hotspot"):
        hotspot_text = prev["hotspot"]
    else:
        hotspot_text = read_hotspot_state()
    fields = {
        "firmware": _firmware_version(),
        "board": board or prev.get("board") or read_board_model(),
        "i2c": i2c if i2c is not None else read_i2c_summary(),
        "wlan0": wlan0 if wlan0 is not None else read_wlan_state(),
        "hotspot": hotspot_text,
        "adc": adc if adc is not None else prev.get("adc", "unknown"),
        "adc_error": adc_error if adc_error is not None else prev.get("adc_error", ""),
        "faulted": faulted_text,
        "fatal": fatal if fatal is not None else prev.get("fatal", "none"),
        "updated": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    text = render_status(fields)
    wrote_state = False
    wrote_boot = False
    env = os.environ.get("WQM1_STATUS_FILE")
    if env and _write_atomic(Path(env), text):
        wrote_state = True
    if _write_atomic(_STATE_PATH, text):
        wrote_state = True
    for path in _BOOT_PATHS:
        if _write_atomic(path, text):
            wrote_boot = True
    if wrote_state and not wrote_boot:
        wrote_boot = _sudo_publish()
    if not wrote_state and not wrote_boot:
        logger.debug("boot status not written (no writable status path)")
    return text
