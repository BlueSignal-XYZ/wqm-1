"""
One Wi-Fi join at a time, with its result on disk (install plan §2).

``start_join`` takes the join lock, records ``joining``, and starts a worker
thread that runs ``netctl.join_network``. It returns at once. A second call
while a join runs gets ``busy`` and the running SSID, and starts nothing (I4).

The lock is an exclusive ``flock`` on ``join.lock`` in the persistent state
directory, held by the worker for the whole join. It works across processes
(the Service Window today, the port-80 setup page in §4) and needs no
cleanup after a crash: the kernel drops a dead process's lock.

``read_state`` reads ``join-state.json`` (mode 0600, written atomically). It
survives a restart, and two processes see the same answer. It never holds the
password (I2). The fields are ``state`` (idle | joining | joined | failed),
``ssid``, ``reason``, ``message``, ``at``, and, for the pages that show them,
``ap_restored``, ``ip``, ``rssi`` and ``forgotten``.

The worker waits ``START_DELAY_S`` before touching the radio, so the HTTP
answer that started the join reaches the phone before the setup network goes
down. Saved networks other than the joined one are forgotten only after the
join reached BlueSignal (spec rule 7).

On a unit the join runs as root, in ``utils.join_service``: stopping hostapd
and dropping 192.168.4.1 need root. The files here are then handed to the
owner of the state directory (the install user), so the port-80 page and the
Service Window, which run as that user, still read them.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from utils import netctl
from utils.atomic import atomic_write_text
from utils.device_keys import ensure_state_dir, state_dir

logger = logging.getLogger("wqm1.join")

STATE_NAME = "join-state.json"
LOCK_NAME = "join.lock"
FILE_MODE = 0o600
START_DELAY_S = 1.5
LOCK_RETRIES = 4
LOCK_RETRY_S = 0.02
STATES = ("idle", "joining", "joined", "failed")
# What may be written to the state file. The password is never one of them.
_FIELDS = ("state", "ssid", "reason", "message", "at", "ap_restored", "ip", "rssi", "forgotten")
INTERRUPTED_MESSAGE = "The last join did not finish. Try again."

_THREAD_LOCK = threading.Lock()
_sleep: Callable[[float], Any] = time.sleep


def state_path() -> Path:
    return state_dir() / STATE_NAME


def lock_path() -> Path:
    return state_dir() / LOCK_NAME


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _idle() -> dict[str, Any]:
    return {"state": "idle", "ssid": None, "reason": None, "message": None, "at": None}


def _state_owner() -> tuple[int, int] | None:
    """(uid, gid) of the state directory, or None when root owns it."""
    try:
        st = state_dir().stat()
    except OSError:
        return None
    return None if st.st_uid == 0 else (st.st_uid, st.st_gid)


def _hand_to_owner(path: Path) -> None:
    """When root wrote ``path``, give it to the state directory's owner.

    The join runs as root on a unit; the page and the Service Window read
    these files as the install user. A state directory owned by root (a test,
    a bench shell) is left alone.
    """
    try:
        if os.geteuid() != 0:
            return
    except AttributeError:
        return
    owner = _state_owner()
    if owner is None:
        return
    with contextlib.suppress(OSError):
        os.chown(path, owner[0], owner[1])


def _try_lock() -> int | None:
    """An fd holding the join lock, or None when another join holds it."""
    ensure_state_dir()
    fd = os.open(lock_path(), os.O_RDWR | os.O_CREAT, FILE_MODE)
    _hand_to_owner(lock_path())
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    except OSError:
        os.close(fd)
        raise
    return fd


def _release(fd: int) -> None:
    with contextlib.suppress(OSError):
        fcntl.flock(fd, fcntl.LOCK_UN)
    with contextlib.suppress(OSError):
        os.close(fd)


def _lock_held() -> bool:
    try:
        fd = _try_lock()
    except OSError:
        return False
    if fd is None:
        return True
    _release(fd)
    return False


def _write_state(fields: dict[str, Any]) -> None:
    clean = {key: fields.get(key) for key in _FIELDS if key in fields}
    clean.setdefault("at", _now())
    ensure_state_dir()
    try:
        atomic_write_text(state_path(), json.dumps(clean, sort_keys=True), mode=FILE_MODE)
    except OSError as e:
        logger.error("Could not record the join state: %s", e)
        return
    _hand_to_owner(state_path())


def read_state() -> dict[str, Any]:
    """The last join. ``idle`` when there has been none.

    A ``joining`` record with no join holding the lock means the process
    running it died (a power cut, a crash); it reads as ``failed`` so a page
    never waits on a join that is not running.
    """
    try:
        raw = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _idle()
    if not isinstance(raw, dict) or raw.get("state") not in STATES:
        return _idle()
    state = {key: raw.get(key) for key in _FIELDS if key in raw}
    for key in ("ssid", "reason", "message", "at"):
        state.setdefault(key, None)
    if state["state"] == "joining" and not _lock_held():
        state.update(state="failed", reason="interrupted", message=INTERRUPTED_MESSAGE)
    return state


def start_join(
    ssid: str,
    password: str | None,
    *,
    hidden: bool = False,
    ap_ssid: str | None = None,
    ap_passphrase: str | None = None,
    api_base: str | None = None,
    on_joined: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Start one join and return at once.

    Returns ``{"state": "joining", "ssid": ssid}``, or ``{"state": "busy",
    "ssid": <the running one>}`` when a join is already running.
    ``on_joined`` runs in the worker after a proven join (the Service Window
    records setup and restarts the firmware there).
    """
    with _THREAD_LOCK:
        fd = _try_lock()
        for _ in range(LOCK_RETRIES):
            if fd is not None:
                break
            # A page polling read_state takes the lock for an instant to
            # see whether a join runs. That is not a join; try again.
            time.sleep(LOCK_RETRY_S)
            fd = _try_lock()
        if fd is None:
            running = read_state()
            return {"state": "busy", "ssid": running.get("ssid") or ssid}
        _write_state({"state": "joining", "ssid": ssid, "reason": None, "message": None})
        worker = threading.Thread(
            target=_run,
            name="wqm1-wifi-join",
            args=(fd, ssid, password, hidden, ap_ssid, ap_passphrase, api_base, on_joined),
            daemon=True,
        )
        try:
            worker.start()
        except Exception:
            _release(fd)
            raise
    return {"state": "joining", "ssid": ssid}


def _failed(ssid: str, result: dict[str, Any], password: str | None) -> dict[str, Any]:
    """The failed state. The sentence falls back to the raw nmcli line, with
    the password removed even if a caller forgot to (I2)."""
    told = (
        result.get("message") or result.get("error") or netctl._join_message("no_association", [])
    )
    return {
        "state": "failed",
        "ssid": ssid,
        "reason": result.get("reason") or "no_association",
        "message": netctl._redact_secret(str(told), password),
        "ap_restored": bool(result.get("ap_restored")),
    }


def _record_attempt(ssid: str, reason: str, password: str | None) -> None:
    try:
        from diagnostics.boot_status import record_join_attempt

        record_join_attempt(ssid, reason, secret=password)
    except Exception as e:  # noqa: BLE001 - the status file never decides the join
        logger.error("Could not record the join attempt: %s", e)


def _restore_hotspot(ap_ssid: str | None, ap_passphrase: str | None) -> bool:
    """After an exception escaped the join, put the setup network back."""
    if not ap_ssid:
        return False
    try:
        if netctl.ap_active():
            return True
        return netctl._restore_ap(True, ap_ssid, ap_passphrase, netctl.DEFAULT_WIFI_IFACE, _sleep)
    except Exception:  # noqa: BLE001 - logged; the state still says failed
        logger.exception("Could not bring the setup network back after a join error")
        return False


def _run(
    fd: int,
    ssid: str,
    password: str | None,
    hidden: bool,
    ap_ssid: str | None,
    ap_passphrase: str | None,
    api_base: str | None,
    on_joined: Callable[[dict[str, Any]], None] | None,
) -> None:
    joined = False
    try:
        _sleep(START_DELAY_S)
        if ap_ssid is None:
            ap_ssid, ap_passphrase = netctl.ap_credentials()

        def _early(pending: dict[str, Any]) -> None:
            # Before the setup network comes back, so the page has the reason
            # the moment the phone rejoins.
            _write_state(_failed(ssid, pending, password))

        result = netctl.join_network(
            ssid,
            password or None,
            ap_ssid=ap_ssid,
            ap_passphrase=ap_passphrase,
            on_failure=_early,
            hidden=hidden,
            api_base=api_base,
        )
        # Joined only with a usable address: a link-local 169.254 lease on
        # an associated radio is not on the network.
        if result.get("connected") and netctl.usable_ipv4(str(result.get("ip") or "")):
            joined = True
            forgotten = netctl.forget_saved_wifi(ssid).get("forgotten", [])
            joined_result = {**result, "forgotten": forgotten}
            # The follow-up (record setup, restart the firmware) runs before
            # the state says joined, so a page that reads "joined" finds
            # setup already recorded.
            if on_joined is not None:
                try:
                    on_joined(joined_result)
                except Exception:  # noqa: BLE001 - joined is joined; log the follow-up
                    logger.exception("Joined %s, but the follow-up failed", ssid)
            _write_state(
                {
                    "state": "joined",
                    "ssid": ssid,
                    "reason": None,
                    "message": None,
                    "ip": result.get("ip"),
                    "rssi": result.get("rssi"),
                    "forgotten": forgotten,
                }
            )
            _record_attempt(ssid, "joined", password)
        else:
            failed = _failed(ssid, result, password)
            _write_state(failed)
            _record_attempt(ssid, str(failed["reason"]), password)
    except Exception:
        logger.exception("Join %s failed in the background", ssid)
        if not joined:
            restored = _restore_hotspot(ap_ssid, ap_passphrase)
            _write_state(
                {
                    "state": "failed",
                    "ssid": ssid,
                    "reason": "no_association",
                    "message": netctl._join_message("no_association", []),
                    "ap_restored": restored,
                }
            )
            _record_attempt(ssid, "no_association", password)
    finally:
        _release(fd)
