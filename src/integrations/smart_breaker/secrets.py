"""
Where the Eaton credentials come from — in order of precedence.

1. ``smart_breaker_client_id`` / ``_client_secret`` / ``_subscription_key`` in
   the device config (what the Service Window's AWG page writes).
2. Environment variables ``ABLEEDGE_CLIENT_ID`` / ``ABLEEDGE_CLIENT_SECRET`` /
   ``ABLEEDGE_SUBSCRIPTION_KEY`` (a systemd drop-in, for instance).
3. Files ``client_id`` / ``client_secret`` / ``subscription_key`` under
   ``/etc/bluesignal/secrets/ableedge/`` (one value per file, trailing
   whitespace ignored).

Options 2 and 3 exist so the person holding the Eaton developer credentials
can install them on the unit without touching config.yaml — drop three files
in the secrets directory and restart the service. Values are never logged;
only which *source* supplied each one.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("wqm1.smart_breaker")

DEFAULT_SECRETS_DIR = "/etc/bluesignal/secrets/ableedge"  # nosec B105 - a path, not a secret

# field -> (config key, env var, file name)
_FIELDS: dict[str, tuple[str, str, str]] = {
    "client_id": ("smart_breaker_client_id", "ABLEEDGE_CLIENT_ID", "client_id"),
    "client_secret": ("smart_breaker_client_secret", "ABLEEDGE_CLIENT_SECRET", "client_secret"),
    "subscription_key": (
        "smart_breaker_subscription_key",
        "ABLEEDGE_SUBSCRIPTION_KEY",
        "subscription_key",
    ),
}


@dataclass(frozen=True)
class Credentials:
    client_id: str
    client_secret: str
    subscription_key: str
    # "config" | "env" | "file" | "" per field — safe to log and to show on
    # the AWG page as "set (file)". Never the value.
    sources: Mapping[str, str]

    @property
    def complete(self) -> bool:
        return bool(self.client_id and self.client_secret and self.subscription_key)

    @property
    def missing(self) -> list[str]:
        return [name for name in _FIELDS if not getattr(self, name)]


def resolve_credentials(
    settings: Any,
    environ: Mapping[str, str] | None = None,
    secrets_dir: str | Path | None = None,
) -> Credentials:
    """Config first, then env, then files. Missing values are empty strings."""
    env: Mapping[str, str] = environ if environ is not None else os.environ
    directory = Path(secrets_dir if secrets_dir is not None else DEFAULT_SECRETS_DIR)

    values: dict[str, str] = {}
    sources: dict[str, str] = {}
    for name, (config_key, env_name, file_name) in _FIELDS.items():
        value, source = _one(settings, config_key, env, env_name, directory / file_name)
        values[name] = value
        sources[name] = source
    return Credentials(sources=sources, **values)


def _one(
    settings: Any, config_key: str, env: Mapping[str, str], env_name: str, path: Path
) -> tuple[str, str]:
    from_config = str(_get(settings, config_key) or "").strip()
    if from_config:
        return from_config, "config"
    from_env = str(env.get(env_name) or "").strip()
    if from_env:
        return from_env, "env"
    try:
        if path.is_file():
            from_file = path.read_text(encoding="utf-8").strip()
            if from_file:
                return from_file, "file"
    except OSError as e:
        logger.warning("Could not read smart breaker secret file %s: %s", path.name, e)
    return "", ""


def _get(settings: Any, key: str) -> Any:
    if isinstance(settings, Mapping):
        return settings.get(key)
    return getattr(settings, key, None)
