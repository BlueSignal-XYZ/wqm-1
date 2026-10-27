"""Write the setup-AP failure clue onto the boot partition.

``bluesignal-ap-fallback.service`` lists this as ``OnFailure=``. The
journal can be lost when the power cable is pulled; ``/boot/firmware``
is the FAT partition a laptop can read. The passphrase is removed
before the write.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import subprocess  # nosec B404 - fixed argv, journalctl of one unit
from collections.abc import Iterable
from pathlib import Path

from utils.netctl import ap_credentials

logger = logging.getLogger("wqm1.ap_fail")

AP_FALLBACK_UNIT = "bluesignal-ap-fallback.service"
JOURNAL_LINES = 40
_BOOT_PATHS = (
    Path("/boot/firmware/wqm1-apfail.txt"),
    Path("/boot/wqm1-apfail.txt"),
)
_SECRET_ASSIGN = re.compile(r"(?i)\b(wpa_passphrase|passphrase|psk)\s*[=:]\s*\S+")


def strip_passphrases(text: str, secrets: Iterable[str]) -> str:
    """Remove known passphrases and ``wpa_passphrase=`` / ``psk=`` values."""
    redacted = text
    for secret in secrets:
        if secret and len(secret) >= 8:
            redacted = redacted.replace(secret, "[redacted]")
    return _SECRET_ASSIGN.sub(r"\1=[redacted]", redacted)


def known_passphrases() -> list[str]:
    """Passphrases that must not land on the boot partition.

    The setup network is open since install plan §5, so normally there is
    none; a hostapd config written with one (an older release, a bench
    override) is still read so nothing it holds reaches the card. A missing
    file is not a reason to skip the clue.
    """
    found: list[str] = []
    try:
        _ssid, secret = ap_credentials()
    except Exception:  # noqa: BLE001 — still write the journal clue
        logger.debug("AP credentials unavailable for redaction", exc_info=True)
    else:
        if secret:
            found.append(secret)
    conf = Path(os.environ.get("WQM1_HOSTAPD_CONF", "/run/wqm1/hostapd.conf"))
    try:
        for line in conf.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("wpa_passphrase="):
                value = line.split("=", 1)[1].strip()
                if value:
                    found.append(value)
    except OSError:
        pass
    return found


def write_clue(path: Path, text: str) -> bool:
    """Atomic write: fsync the temp file, rename, fsync the directory, sync.

    Directory fsync can fail on FAT. The file is already renamed, and
    ``os.sync`` is what flushes the card.
    """
    parent = path.parent
    if not parent.is_dir() or not os.access(parent, os.W_OK):
        return False
    tmp = path.with_name(path.name + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.chmod(tmp, 0o644)
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except OSError as e:
        logger.debug("apfail write %s failed: %s", path, e)
        with contextlib.suppress(OSError):
            tmp.unlink()
        return False
    try:
        dir_fd = os.open(parent, os.O_RDONLY)
    except OSError as e:
        logger.debug("apfail dir open %s failed: %s", parent, e)
        dir_fd = -1
    if dir_fd >= 0:
        try:
            os.fsync(dir_fd)
        except OSError as e:
            logger.debug("apfail dir fsync %s failed: %s", parent, e)
        finally:
            os.close(dir_fd)
    try:
        os.sync()
    except OSError as e:
        logger.debug("apfail sync failed: %s", e)
    return True


def journal_tail(unit: str = AP_FALLBACK_UNIT, lines: int = JOURNAL_LINES) -> str:
    """Last ``lines`` journal lines for ``unit``, or a short error string."""
    exe = shutil.which("journalctl")
    if not exe:
        return "journalctl not found\n"
    try:
        out = subprocess.run(  # nosec B603 - resolved path, fixed argv, no shell
            [exe, "-u", unit, "-n", str(lines), "--no-pager"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return f"journalctl failed: {e}\n"
    text = out.stdout or ""
    if out.returncode != 0 and not text:
        text = out.stderr or f"journalctl rc {out.returncode}\n"
    kept = text.splitlines()[-lines:]
    body = "\n".join(kept)
    if body and not body.endswith("\n"):
        body += "\n"
    return body


def publish_clue(text: str) -> bool:
    """Write ``text`` to the boot partition, or to ``WQM1_APFAIL_FILE``."""
    env = os.environ.get("WQM1_APFAIL_FILE")
    if env:
        parent = Path(env).parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            return False
        return write_clue(Path(env), text)
    return any(write_clue(path, text) for path in _BOOT_PATHS)


def main() -> int:
    text = strip_passphrases(journal_tail(), known_passphrases())
    if not publish_clue(text):
        logger.error("Could not write wqm1-apfail.txt")
        return 1
    return 0
