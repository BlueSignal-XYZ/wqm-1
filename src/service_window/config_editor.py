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
    with open(p) as f:
        return yaml.safe_load(f) or {}


def update_config(path: str, updates: dict[str, Any]) -> None:
    """Merge updates into config file with atomic write."""
    current = read_config(path)
    current.update(updates)
    _atomic_yaml_write(path, current)


def _atomic_yaml_write(path: str, data: dict[str, Any]) -> None:
    """Write YAML file atomically (write to .tmp then rename)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    try:
        with open(tmp, "w") as f:
            yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)
        tmp.replace(p)
    except Exception:
        if tmp.exists():
            tmp.unlink()
        raise
