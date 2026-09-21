#!/usr/bin/env python3
"""
First-boot card consumer (commissioning plan, PR 3 / PR 5).

The bench writes two files onto the card's FAT boot partition
(``marketplace/scripts/commission-device.cjs --label … --commit``):

* ``bluesignal-identity.json`` — serial, DevEUI, AP passphrase. Read in
  place by ``utils.identity`` on every boot; it stays on the card so a
  warranty Pi swap keeps the identity.
* ``bluesignal-cloud.json`` — the API key, AppKey, JoinEUI and cloud URLs.
  Secrets, and the FAT partition is world-readable, so this file is moved
  INTO ``/etc/bluesignal/config.yaml`` and deleted on the first boot that
  sees it. That is this script, run as root by ``bluesignal-card.service``
  before the firmware starts.

Nobody in the field types a DevEUI, an AppKey, or 64 hex characters of API
key at a well head: the bench writes two files, the unit consumes one.

Refuses a card whose ``serial`` is not this unit's own — a cloud file
copied onto the wrong card would otherwise bind unit B's key to unit A.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

_SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(_SRC))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [card] %(message)s")
logger = logging.getLogger("wqm1.card")

CARD_PATHS = ("/boot/firmware/bluesignal-cloud.json", "/boot/bluesignal-cloud.json")
DEFAULT_CONFIG = "/etc/bluesignal/config.yaml"

_API_KEY_RE = re.compile(r"^[0-9a-zA-Z]{16,128}$")
_HEX32_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_HEX16_RE = re.compile(r"^[0-9a-fA-F]{16}$")
_URL_RE = re.compile(r"^https?://[^\s]+$")


def validate(card: dict[str, Any], device_id: str) -> dict[str, Any]:
    """Return the config.yaml updates a card file authorises, or raise
    ValueError naming the first field that does not pass."""
    serial = str(card.get("serial") or "")
    if serial != device_id:
        raise ValueError(f"card is for {serial or '<no serial>'}, this unit is {device_id}")
    api_key = str(card.get("api_key") or "")
    if not _API_KEY_RE.match(api_key):
        raise ValueError("api_key is malformed")
    updates: dict[str, Any] = {"api_key": api_key, "cloud_enabled": True}
    app_key = card.get("app_key")
    if app_key is not None:
        if not _HEX32_RE.match(str(app_key)):
            raise ValueError("app_key is not 32 hex characters")
        updates["app_key"] = str(app_key).lower()
    join_eui = card.get("join_eui")
    if join_eui is not None:
        if not _HEX16_RE.match(str(join_eui)):
            raise ValueError("join_eui is not 16 hex characters")
        updates["app_eui"] = str(join_eui).upper()
    for key in ("cloud_api_base", "cloud_ingest_url"):
        val = card.get(key)
        if val is not None:
            if not _URL_RE.match(str(val)):
                raise ValueError(f"{key} is not an http(s) URL")
            updates[key] = str(val)
    return updates


def consume(
    paths: tuple[str, ...] = CARD_PATHS,
    config_path: str = DEFAULT_CONFIG,
    device_id: str | None = None,
) -> dict[str, Any]:
    """Consume the first card file found. Returns what happened."""
    from service_window.config_editor import update_config

    found = next((p for p in paths if os.path.exists(p)), None)
    if found is None:
        return {"action": "none", "reason": "no card file"}
    if device_id is None:
        from utils.identity import get_device_id

        device_id = get_device_id()
    try:
        card = json.loads(Path(found).read_text())
        if not isinstance(card, dict):
            raise ValueError("card is not a JSON object")
        updates = validate(card, device_id)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        # Left in place so the fault is visible on the card, not silently
        # eaten. The unit still boots; it simply has no cloud credentials.
        logger.error("Refusing %s: %s", found, e)
        return {"action": "refused", "path": found, "reason": str(e)}

    # Preserve the config file's owner: this runs as root and the atomic
    # writer replaces the file, and the Service Window (unprivileged) must
    # still be able to write it afterwards.
    owner: tuple[int, int] | None = None
    try:
        st = os.stat(config_path)
        owner = (st.st_uid, st.st_gid)
    except OSError:
        pass
    update_config(config_path, updates)
    if owner is not None:
        try:
            os.chown(config_path, *owner)
        except OSError as e:
            logger.warning("could not restore owner of %s: %s", config_path, e)
    try:
        os.remove(found)
    except OSError as e:
        logger.error("Card consumed but %s could not be deleted: %s", found, e)
        return {"action": "consumed_not_deleted", "path": found, "keys": sorted(updates)}
    logger.info("Consumed %s into %s (%s)", found, config_path, ", ".join(sorted(updates)))
    return {"action": "consumed", "path": found, "keys": sorted(updates)}


def main() -> int:
    result = consume()
    return 0 if result["action"] in ("none", "consumed") else 1


if __name__ == "__main__":
    sys.exit(main())
