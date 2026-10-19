"""
Plain-text boot status on the FAT partition.

A field unit that never joins Wi-Fi cannot be read over the network. The
card can: `/boot/firmware` is the first partition, and a Mac mounts it.
`wqm1-status.txt` is rewritten on boot and whenever the sensor or radio
state changes. The first line is the commissioning verdict
(``COMMISSION: READY`` or ``COMMISSION: FAIL: <reason>``). The rest names
the firmware, the board and its revision code, the kernel, the Wi-Fi chip
and firmware, undervoltage, the I2C buses the kernel enumerated, wlan0,
the setup hotspot (SSID and the last AP error), the station join
(``station: ok <ip>`` or ``station: fail: <reason>``), the station signal
(``station_rssi``), the port-80 captive portal, ADC, 1-Wire, GPS, uptime,
and the last fatal error.

No secrets. A line that looks like a key, a passphrase, or a private key
is replaced before the file is written. The hotspot passphrase is never a
field.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import subprocess  # nosec B404 - fixed argv, sudo -n of one installer script
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from utils.device_keys import registration_status

logger = logging.getLogger("wqm1.boot_status")

_KEYS = (
    "commission",
    "firmware",
    "board",
    "revision",
    "kernel",
    "wifi",
    "power",
    "i2c",
    "wlan0",
    "ssid",
    "hotspot",
    "station",
    "station_rssi",
    "portal",
    "registration",
    "adc",
    "onewire",
    "gps",
    "adc_error",
    "faulted",
    "fatal",
    "uptime",
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
    """``up`` when hostapd reports ``state=ENABLED``, else ``down``.

    A missing ``hostapd_cli``, or a control socket this user cannot open,
    is ``unknown`` — not ``down`` — so a status file written before the
    tool is installed does not claim the hotspot is off. ``ap_active``
    collapses that failure to False, which is right for "should I raise
    the AP" and wrong for this file."""
    try:
        from utils.netctl import hostapd_state
    except Exception as e:  # noqa: BLE001 — status must be writable anyway
        logger.debug("hotspot state unavailable: %s", e)
        return "unknown"
    try:
        state = hostapd_state()
    except Exception as e:  # noqa: BLE001
        logger.debug("hotspot state failed: %s", e)
        return "unknown"
    if state is None:
        return "unknown"
    return "up" if state == "ENABLED" else "down"


def _portal_line(hotspot: str) -> str:
    """``up (80)`` or ``down: <reason>``. A probe failure must not raise."""
    try:
        from utils.captive_portal import portal_line_for_hotspot

        return portal_line_for_hotspot(hotspot)
    except Exception as e:  # noqa: BLE001 — the status file is still written
        logger.debug("portal state failed: %s", e)
        if (hotspot or "").startswith("up"):
            return f"down: {e}"
        return "down: hotspot down"


def read_revision() -> str:
    """Pi revision code from ``/proc/cpuinfo`` (``9000c1``, ``a02082``, …)."""
    try:
        text = Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "unknown"
    for line in text.splitlines():
        if line.startswith("Revision"):
            rev = line.split(":", 1)[-1].strip()
            return rev or "unknown"
    return "unknown"


def read_kernel() -> str:
    try:
        text = Path("/proc/sys/kernel/osrelease").read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"
    return text or "unknown"


def read_uptime() -> str:
    try:
        raw = Path("/proc/uptime").read_text(encoding="utf-8").split()[0]
        return f"{int(float(raw))}s"
    except (OSError, ValueError, IndexError):
        return "unknown"


_BRCM_FW_RE = re.compile(
    r"Firmware:\s+(\S+)\s+.*?version\s+(\d+\.\d+\.\d+(?:\.\w+)?)(?:\s+(\(TOB\)))?",
    re.IGNORECASE,
)
_wifi_cache: str | None = None


def parse_brcmfmac_firmware(text: str) -> str:
    """``BCM43430/1 7.45.98 (TOB)`` from a brcmfmac firmware log line, or ""."""
    match = _BRCM_FW_RE.search(text or "")
    if not match:
        return ""
    version = match.group(2)
    if match.group(3):
        version = f"{version} {match.group(3)}"
    return f"{match.group(1)} {version}"


def _run_text(argv: list[str], timeout: float = 3.0) -> tuple[int, str]:
    exe = shutil.which(argv[0])
    if not exe:
        return 127, ""
    try:
        out = subprocess.run(  # nosec B603 - resolved path, fixed argv, shell=False
            [exe, *argv[1:]],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return out.returncode, (out.stdout or "") + (out.stderr or "")


def read_wifi_firmware() -> str:
    """Chip and firmware from the brcmfmac line in the kernel log.

    Cached: the version does not change until the next boot, and dmesg is
    not free on a Pi Zero. ``unknown`` when the line is not there (the
    driver has not loaded, or dmesg is not readable).
    """
    global _wifi_cache
    if _wifi_cache is not None:
        return _wifi_cache
    _rc, text = _run_text(["dmesg"])
    parsed = parse_brcmfmac_firmware(text)
    _wifi_cache = parsed or "unknown"
    return _wifi_cache


# Bits of `vcgencmd get_throttled`. Current conditions fail commissioning;
# the since-boot bits are reported and do not, on their own.
_THROTTLE_NOW = (
    (0x1, "undervoltage"),
    (0x2, "freq-capped"),
    (0x4, "throttled"),
    (0x8, "soft-temp-limit"),
)
_THROTTLE_PAST = (
    (0x10000, "undervoltage"),
    (0x20000, "freq-capped"),
    (0x40000, "throttled"),
    (0x80000, "soft-temp-limit"),
)


def parse_throttled(text: str) -> str:
    """Decode ``throttled=0x…``. ``unknown`` when vcgencmd did not say."""
    match = re.search(r"throttled=(0x[0-9a-fA-F]+)", text or "")
    if not match:
        return "unknown"
    value = int(match.group(1), 16)
    if value == 0:
        return "ok"
    now = [name for bit, name in _THROTTLE_NOW if value & bit]
    past = [name for bit, name in _THROTTLE_PAST if value & bit]
    parts: list[str] = []
    if now:
        parts.append("now " + ",".join(now))
    if past:
        parts.append("since-boot " + ",".join(past))
    parts.append(f"throttled={match.group(1)}")
    return " ".join(parts)


def read_power() -> str:
    """Undervoltage / throttle. ``unknown`` when ``vcgencmd`` is not installed."""
    rc, text = _run_text(["vcgencmd", "get_throttled"])
    if rc != 0 and "throttled=" not in text:
        return "unknown"
    return parse_throttled(text)


def read_onewire_state() -> str:
    """Kernel 1-Wire bus. A ``28-`` device is a DS18B20."""
    root = Path("/sys/bus/w1/devices")
    if not root.is_dir():
        return "absent"
    try:
        ids = sorted(p.name for p in root.iterdir() if p.name.startswith("28-"))
    except OSError:
        return "unknown"
    if not ids:
        return "no probe"
    return "ok"


def onewire_line(sensor: Any) -> str:
    """Status from a live DS18B20, or ``not fitted`` when the app has none."""
    if sensor is None:
        return "not fitted"
    try:
        available = getattr(sensor, "available", None)
        if callable(available):
            return "ok" if available() else "no probe"
    except Exception as e:  # noqa: BLE001 — the line is diagnostic
        logger.debug("onewire status failed: %s", e)
        return "unknown"
    return "ok"


def gps_line(receiver: Any) -> str:
    """``fix`` / ``no fix`` from a live receiver, or ``absent`` when there is none."""
    if receiver is None:
        return "absent"
    try:
        fix = getattr(receiver, "last_fix", None)
        if callable(fix):
            fix = fix()
        quality = getattr(fix, "fix_quality", 0) if fix is not None else 0
        return "fix" if quality else "no fix"
    except Exception as e:  # noqa: BLE001
        logger.debug("gps status failed: %s", e)
        return "unknown"


def read_gps_state() -> str:
    """UART node only. Opening the port would steal it from the firmware."""
    for node in ("/dev/serial0", "/dev/ttyAMA0", "/dev/ttyS0"):
        if Path(node).exists():
            return "port present"
    return "absent"


def _kept(explicit: str | None, previous: str, probed: str) -> str:
    if explicit is not None:
        return explicit
    if previous:
        return previous
    return probed


def commission_verdict(fields: dict[str, str]) -> str:
    """``READY`` or ``FAIL: <reason>``.

    READY means an installer can reach the unit and the board is not in a
    state that will drop the hotspot or hide the sensors: the AP is up, or
    a station link is up, and there is no current undervoltage and no known
    ADC fault. A missing GPS fix or an unfitted 1-Wire probe does not fail
    commissioning — both are optional on the bench.
    """
    reasons: list[str] = []
    hotspot = (fields.get("hotspot") or "").strip()
    wlan = (fields.get("wlan0") or "").strip()
    reachable = (
        hotspot == "up"
        or hotspot.startswith("up ")
        or hotspot == "station"
        or (hotspot == "down" and wlan == "up")
        or hotspot in ("", "unknown")
    )
    if hotspot.startswith("failed"):
        detail = hotspot.split(":", 1)[1].strip() if ":" in hotspot else ""
        reasons.append(detail or "hotspot failed")
    elif hotspot == "starting":
        reasons.append("hotspot starting")
    elif not reachable:
        reasons.append("hotspot down")
    # A failed site join while the hotspot is up is how the installer gets
    # back in. It is not a board failure. A station line that says fail
    # while we claim to be on that station is.
    station = (fields.get("station") or "").strip()
    registration = (fields.get("registration") or "").strip()
    if registration == "conflict":
        reasons.append("identity conflict")
    if hotspot == "station" and station.startswith("fail"):
        detail = station.split(":", 1)[1].strip() if ":" in station else station
        reasons.append(detail or "station failed")
    # The portal is only required while the hotspot is up. A station link
    # has closed port 80 on purpose; that is not a commissioning failure.
    hotspot_up = hotspot == "up" or hotspot.startswith("up ")
    if hotspot_up:
        portal = (fields.get("portal") or "").strip()
        if not portal.startswith("up"):
            reasons.append(f"portal {portal}" if portal else "portal down")
    power = (fields.get("power") or "").strip()
    if power.startswith("now "):
        reasons.append(power)
    if (fields.get("adc") or "").strip() == "faulted":
        reasons.append("adc faulted")
    fatal = (fields.get("fatal") or "").strip()
    if fatal and fatal not in ("none", "unknown"):
        reasons.append(fatal)
    if not reasons:
        return "READY"
    return "FAIL: " + "; ".join(reasons)


def render_status(fields: dict[str, str]) -> str:
    """One plain-text snapshot. Every key is always present.

    A line that matches a credential pattern is replaced in full. Replacing
    only the matched token would leave the secret sitting after the colon.
    """
    verdict = fields.get("commission") or commission_verdict(fields)
    verdict_line = f"COMMISSION: {verdict}"
    if _SECRET.search(verdict_line):
        verdict_line = "COMMISSION: [redacted]"
    lines = [verdict_line, "WQM-1 boot status"]
    for key in _KEYS:
        if key == "commission":
            continue
        value = fields.get(key, "")
        line = f"{key}: {value}"
        if _SECRET.search(line):
            line = f"{key}: [redacted]"
        lines.append(line)
    return "\n".join(lines) + "\n"


def _read_ssid() -> str:
    """The setup SSID. Never the passphrase — ``ap_name`` does not know it."""
    try:
        from utils.identity import ap_name

        return ap_name()
    except Exception as e:  # noqa: BLE001 — status must still be writable
        logger.debug("ssid unavailable: %s", e)
        return ""


def _registration_line() -> str:
    """pending / registered / conflict / error. Never a secret."""
    try:
        status = registration_status()
    except Exception as e:  # noqa: BLE001 — status must still be writable
        logger.debug("registration status unavailable: %s", e)
        return "unknown"
    if status in ("pending", "registered", "conflict", "error"):
        return status
    return "unknown"


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
    """Write ``path`` if its directory exists and is writable.

    The env override may point at a directory we are allowed to create;
    the FAT and /var/lib paths are never created here (a test must not
    mkdir them on a developer machine). The temp file is fsynced before
    rename, and the directory is fsynced after, so a power cut still
    leaves a readable ``wqm1-status.txt`` on the boot partition."""
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
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.chmod(tmp, 0o644)
            os.fsync(handle.fileno())
        tmp.replace(path)
    except OSError as e:
        logger.debug("status write %s failed: %s", path, e)
        with contextlib.suppress(OSError):
            tmp.unlink()
        return False
    # The rename is a directory entry. Without this fsync a power cut can
    # leave the FAT directory pointing at neither the old nor the new file.
    # Some filesystems reject a directory fsync; the file is already in place
    # and the publish helper also calls sync.
    try:
        dir_fd = os.open(parent, os.O_RDONLY)
    except OSError as e:
        logger.debug("status dir open %s failed: %s", parent, e)
        return True
    try:
        os.fsync(dir_fd)
    except OSError as e:
        logger.debug("status dir fsync %s failed: %s", parent, e)
    finally:
        os.close(dir_fd)
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
    station: str | None = None,
    station_rssi: str | None = None,
    ssid: str | None = None,
    adc: str | None = None,
    adc_error: str | None = None,
    onewire: str | None = None,
    gps: str | None = None,
    wifi: str | None = None,
    power: str | None = None,
    kernel: str | None = None,
    revision: str | None = None,
    uptime: str | None = None,
    faulted: list[str] | None = None,
    fatal: str | None = None,
    portal: str | None = None,
    registration: str | None = None,
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
    if ssid is not None:
        ssid_text = ssid
    elif prev.get("ssid"):
        ssid_text = prev["ssid"]
    else:
        ssid_text = _read_ssid()
    if station is not None:
        station_text = station
    elif prev.get("station"):
        station_text = prev["station"]
    else:
        station_text = ""
    if station_rssi is not None:
        rssi_text = station_rssi
    elif prev.get("station_rssi"):
        rssi_text = prev["station_rssi"]
    else:
        rssi_text = ""
    if portal is not None:
        portal_text = portal
    elif hotspot_text == "up" or str(hotspot_text).startswith("up "):
        portal_text = _portal_line(hotspot_text)
    elif prev.get("portal"):
        portal_text = prev["portal"]
    else:
        portal_text = _portal_line(hotspot_text)
    # Always re-read unless the caller overrode it: this lives on the
    # persistent data path and must not stay "pending" after register.
    registration_text = registration if registration is not None else _registration_line()
    fields = {
        "firmware": _firmware_version(),
        "board": board or prev.get("board") or read_board_model(),
        "revision": revision if revision is not None else read_revision(),
        "kernel": kernel if kernel is not None else read_kernel(),
        "wifi": wifi if wifi is not None else read_wifi_firmware(),
        "power": power if power is not None else read_power(),
        "i2c": i2c if i2c is not None else read_i2c_summary(),
        "wlan0": wlan0 if wlan0 is not None else read_wlan_state(),
        "ssid": ssid_text,
        "hotspot": hotspot_text,
        "station": station_text,
        "station_rssi": rssi_text,
        "portal": portal_text,
        "registration": registration_text,
        "adc": adc if adc is not None else prev.get("adc", "unknown"),
        "onewire": _kept(onewire, prev.get("onewire", ""), read_onewire_state()),
        "gps": _kept(gps, prev.get("gps", ""), read_gps_state()),
        "adc_error": adc_error if adc_error is not None else prev.get("adc_error", ""),
        "faulted": faulted_text,
        "fatal": fatal if fatal is not None else prev.get("fatal", "none"),
        "uptime": uptime if uptime is not None else read_uptime(),
        "updated": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    fields["commission"] = commission_verdict(fields)
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
