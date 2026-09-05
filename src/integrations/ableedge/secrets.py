"""
Resolve AbleEdge credential *references* (env vars / files). Never hardcode.

On-device pattern (same idea as the device API key living outside git):

1. Environment variable named in ``CredentialRefs`` (systemd drop-in, etc.)
2. Else a file under ``secrets_dir`` (``client_id``, ``client_secret``,
   ``subscription_key``)

Production tokens may later live only in Cloud Functions; this module is the
on-device half. Values are never logged.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from integrations.ableedge.schema import SECRET_FILES, CredentialRefs

logger = logging.getLogger("wqm1.ableedge")


@dataclass(frozen=True)
class AbleEdgeSecrets:
    client_id: str
    client_secret: str
    subscription_key: str

    @property
    def complete(self) -> bool:
        return bool(self.client_id and self.client_secret and self.subscription_key)


def resolve_secrets(
    refs: CredentialRefs,
    environ: Mapping[str, str] | None = None,
    *,
    secrets_dir: str | Path | None = None,
) -> AbleEdgeSecrets:
    """Load secrets from env, then files. Missing values are empty strings."""
    env: Mapping[str, str] = environ if environ is not None else os.environ
    directory = Path(secrets_dir) if secrets_dir is not None else Path(refs.secrets_dir)

    client_id = _one(env, refs.client_id_env, directory / SECRET_FILES["client_id"])
    client_secret = _one(env, refs.client_secret_env, directory / SECRET_FILES["client_secret"])
    subscription_key = _one(
        env, refs.subscription_key_env, directory / SECRET_FILES["subscription_key"]
    )
    loaded = AbleEdgeSecrets(client_id, client_secret, subscription_key)
    if not loaded.complete:
        missing = [
            name
            for name, ok in (
                ("client_id", bool(client_id)),
                ("client_secret", bool(client_secret)),
                ("subscription_key", bool(subscription_key)),
            )
            if not ok
        ]
        logger.info(
            "AbleEdge secrets incomplete (%s) — live API calls are blocked. "
            "Set the env refs or files under %s.",
            ", ".join(missing),
            directory,
        )
    else:
        logger.info("AbleEdge credential refs resolved (values not logged)")
    return loaded


def _one(env: Mapping[str, str], env_name: str, path: Path) -> str:
    value = str(env.get(env_name) or "").strip()
    if value:
        return value
    try:
        if path.is_file():
            return path.read_text(encoding="utf-8").strip()
    except OSError as e:
        logger.warning("Could not read AbleEdge secret file %s: %s", path.name, e)
    return ""
