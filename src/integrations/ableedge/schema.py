"""
Load-control config schema (AbleEdge-only).

``vendor`` is the installer switch:

* ``none`` — no AWG/circuit actuation (default)
* ``ableedge`` — Eaton AbleEdge Smart Breaker API
* ``relay_only`` — G5Q-14 fallback relay only (explicitly *not* AbleEdge)

Span, Lumin, and Savant are refused (vendor lock, 2026-09-05). Circuit
ampacity is installer input only — never invented, never used to fabricate
telemetry.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("wqm1.ableedge")

VENDORS = frozenset({"ableedge", "none", "relay_only"})
FAIL_SAFES = frozenset({"off", "last", "on"})
# Jacques 2026-09-05: Eaton AbleEdge ONLY. These names are accepted nowhere.
LOCKED_VENDORS = frozenset({"span", "lumin", "savant"})

DEFAULT_API_BASE = "https://api.em.eaton.com"
DEFAULT_SECRETS_DIR = "/etc/bluesignal/secrets/ableedge"
DEFAULT_CLIENT_ID_ENV = "ABLEEDGE_CLIENT_ID"
DEFAULT_CLIENT_SECRET_ENV = "ABLEEDGE_CLIENT_SECRET"
DEFAULT_SUBSCRIPTION_KEY_ENV = "ABLEEDGE_SUBSCRIPTION_KEY"
DEFAULT_POLL_S = 30
MIN_POLL_S = 5
MAX_POLL_S = 3600

# Filenames inside secrets_dir — values never belong in config.yaml.
SECRET_FILES = {
    "client_id": "client_id",
    "client_secret": "client_secret",
    "subscription_key": "subscription_key",
}


@dataclass(frozen=True)
class CredentialRefs:
    """Names of env vars / files that hold secrets. Never the secrets themselves."""

    client_id_env: str = DEFAULT_CLIENT_ID_ENV
    client_secret_env: str = DEFAULT_CLIENT_SECRET_ENV
    subscription_key_env: str = DEFAULT_SUBSCRIPTION_KEY_ENV
    secrets_dir: str = DEFAULT_SECRETS_DIR


@dataclass(frozen=True)
class LoadControlConfig:
    """Parsed ``load_control:`` block from config.yaml."""

    vendor: str = "none"
    site_id: str = ""
    device_id: str = ""
    circuit_id: str = ""
    circuit_ampacity_a: float | None = None
    poll_s: int = DEFAULT_POLL_S
    fail_safe: str = "off"
    fallback_relay: int | None = None
    api_base: str = DEFAULT_API_BASE
    backend: str = "http"
    credentials: CredentialRefs = field(default_factory=CredentialRefs)

    @property
    def bound_circuit_id(self) -> str:
        """Circuit UUID, falling back to the AbleEdge device UUID."""
        return self.circuit_id or self.device_id


def default_load_control() -> dict[str, Any]:
    """Default YAML-shaped mapping stored on Settings."""
    return {
        "vendor": "none",
        "site_id": "",
        "device_id": "",
        "circuit_id": "",
        "circuit_ampacity_a": None,
        "poll_s": DEFAULT_POLL_S,
        "fail_safe": "off",
        "fallback_relay": None,
        "api_base": DEFAULT_API_BASE,
        "backend": "http",
        "credentials": {
            "client_id_env": DEFAULT_CLIENT_ID_ENV,
            "client_secret_env": DEFAULT_CLIENT_SECRET_ENV,
            "subscription_key_env": DEFAULT_SUBSCRIPTION_KEY_ENV,
            "secrets_dir": DEFAULT_SECRETS_DIR,
        },
    }


def parse_load_control(raw: Any) -> LoadControlConfig:
    """
    Validate a ``load_control`` mapping.

    Unknown or locked vendors become ``none`` so a typo cannot silently talk
    to a different panel vendor. Invalid ampacity is dropped (None) rather
    than replaced with a guessed breaker size.
    """
    if raw is None:
        return LoadControlConfig()
    if not isinstance(raw, dict):
        logger.warning("load_control is not a mapping — using vendor=none")
        return LoadControlConfig()

    vendor = str(raw.get("vendor") or "none").strip().lower()
    if vendor in LOCKED_VENDORS:
        logger.error(
            "load_control.vendor=%s is locked out (Eaton AbleEdge only) — using none",
            vendor,
        )
        vendor = "none"
    elif vendor not in VENDORS:
        logger.warning("load_control.vendor=%s is not supported — using none", vendor)
        vendor = "none"

    fail_safe = str(raw.get("fail_safe") or "off").strip().lower()
    if fail_safe not in FAIL_SAFES:
        logger.warning("load_control.fail_safe=%s invalid — using off", fail_safe)
        fail_safe = "off"

    poll_s = _as_int(raw.get("poll_s"), DEFAULT_POLL_S)
    if poll_s < MIN_POLL_S or poll_s > MAX_POLL_S:
        logger.warning("load_control.poll_s=%s out of range — using %d", poll_s, DEFAULT_POLL_S)
        poll_s = DEFAULT_POLL_S

    ampacity = _as_optional_positive_float(raw.get("circuit_ampacity_a"))
    fallback = _as_optional_relay(raw.get("fallback_relay"))

    backend = str(raw.get("backend") or "http").strip().lower()
    if backend not in {"http", "mock"}:
        logger.warning("load_control.backend=%s invalid — using http", backend)
        backend = "http"

    api_base = str(raw.get("api_base") or DEFAULT_API_BASE).strip()
    if not api_base.lower().startswith(("https://", "http://")):
        logger.warning("load_control.api_base is not HTTP(S) — using default")
        api_base = DEFAULT_API_BASE

    creds_in = raw.get("credentials")
    creds_raw: dict[str, Any] = creds_in if isinstance(creds_in, dict) else {}
    # Refuse inline secret values — only env/file *names* are accepted.
    for banned in ("client_id", "client_secret", "subscription_key", "api_key", "secret"):
        if banned in creds_raw:
            logger.error(
                "load_control.credentials.%s is a value, not a ref — ignored. "
                "Use env/file names only.",
                banned,
            )

    credentials = CredentialRefs(
        client_id_env=_as_name(creds_raw.get("client_id_env"), DEFAULT_CLIENT_ID_ENV),
        client_secret_env=_as_name(creds_raw.get("client_secret_env"), DEFAULT_CLIENT_SECRET_ENV),
        subscription_key_env=_as_name(
            creds_raw.get("subscription_key_env"), DEFAULT_SUBSCRIPTION_KEY_ENV
        ),
        secrets_dir=_as_name(creds_raw.get("secrets_dir"), DEFAULT_SECRETS_DIR),
    )

    return LoadControlConfig(
        vendor=vendor,
        site_id=str(raw.get("site_id") or "").strip(),
        device_id=str(raw.get("device_id") or "").strip(),
        circuit_id=str(raw.get("circuit_id") or "").strip(),
        circuit_ampacity_a=ampacity,
        poll_s=poll_s,
        fail_safe=fail_safe,
        fallback_relay=fallback,
        api_base=api_base.rstrip("/"),
        backend=backend,
        credentials=credentials,
    )


def _as_name(value: Any, default: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default


def _as_int(value: Any, default: int) -> int:
    if isinstance(value, bool) or value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_optional_positive_float(value: Any) -> float | None:
    """Installer-supplied ampacity only. Refuse guessed / non-positive values."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        logger.warning("load_control.circuit_ampacity_a is not a number — ignored")
        return None
    if parsed <= 0:
        logger.warning("load_control.circuit_ampacity_a must be > 0 — ignored")
        return None
    return parsed


def _as_optional_relay(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    try:
        channel = int(value)
    except (TypeError, ValueError):
        return None
    if 1 <= channel <= 4:
        return channel
    logger.warning("load_control.fallback_relay must be 1-4 — ignored")
    return None
