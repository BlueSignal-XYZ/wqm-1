"""
Safe YAML config editing with atomic writes.
"""

from pathlib import Path
from typing import Any

import yaml


def read_config(path: str) -> dict[str, Any]:
    """Read YAML config file, returning empty dict if missing."""
    p = Path(path)
    if not p.exists():
        return {}
    with p.open() as f:
        return yaml.safe_load(f) or {}


class ConfigWriteError(RuntimeError):
    """Raised when the config file cannot be written.

    Exists so the service window can tell the operator *why* a save failed
    instead of returning a bare 500. The overwhelmingly common cause is
    /etc/bluesignal being root-owned while the service runs unprivileged, which
    looks identical to "the device rejected my key" from the browser.
    """


def update_config(path: str, updates: dict[str, Any]) -> None:
    """Merge updates into config file with atomic write."""
    current = read_config(path)
    current.update(updates)
    try:
        _atomic_yaml_write(path, current)
    except PermissionError as exc:
        p = Path(path)
        raise ConfigWriteError(
            f"Cannot write {p}: permission denied. The service window runs "
            f"unprivileged and needs write access to {p.parent}. Fix with: "
            f"sudo chown -R $USER:$USER {p.parent}"
        ) from exc
    except OSError as exc:
        raise ConfigWriteError(f"Cannot write {path}: {exc}") from exc


def _atomic_yaml_write(path: str, data: dict[str, Any]) -> None:
    """Write YAML file atomically (write to .tmp then rename)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    try:
        with tmp.open("w") as f:
            yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)
        tmp.replace(p)
    except Exception:
        if tmp.exists():
            tmp.unlink()
        raise
