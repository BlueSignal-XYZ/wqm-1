"""
Safe YAML config editing with atomic writes.
"""

from pathlib import Path
from typing import Any

import yaml


def read_config(path: str) -> dict[str, Any]:
    """Read YAML config file, returning empty dict if missing.

    The open() is guarded as well as the exists() check: the setup funnel calls
    this on EVERY request, and on a factory-fresh unit config.yaml does not
    exist until provisioning writes it (atomically, via rename — so a reader
    can also hit the gap mid-swap). Without the catch, a fresh unit 500s on
    every page instead of funneling into /setup, which is the one moment the
    funnel exists for. Found because the test suite patches Path.exists
    globally and drove a request straight through the guard.
    """
    p = Path(path)
    if not p.exists():
        return {}
    try:
        with p.open() as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


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


def update_config_section(path: str, section: str, updates: dict[str, Any]) -> None:
    """Merge updates into a nested mapping (e.g. service_window) atomically."""
    current = read_config(path)
    existing = current.get(section)
    merged = dict(existing) if isinstance(existing, dict) else {}
    merged.update(updates)
    current[section] = merged
    _atomic_yaml_write(path, current)


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
