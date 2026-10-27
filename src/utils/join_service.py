"""
The Wi-Fi join and rescan, run as root for the port-80 page (install plan §4).

The page (``utils.captive_portal``) runs as the install user so the process
that parses what a phone sends has no privilege beyond binding port 80. A
join cannot run there: it stops hostapd, drops 192.168.4.1 and hands
``wlan0`` back to NetworkManager, and a rescan while the setup network is up
is ``iw dev wlan0 scan ap-force``. All of that needs root.

This service is that root half. It accepts exactly two requests, ``join`` and
``rescan``, on a Unix socket only root and the install user can open (mode
0600, owned by the install user, and a peer-uid check). It listens on no
network port. Each request is one JSON line of at most ``MAX_REQUEST`` bytes
and gets one JSON line back.

The password arrives over the socket, stays in memory, and goes to nmcli as
one argv element through ``utils.join`` and ``netctl.join_network``. Nothing
here writes or logs it (I2).

The join itself is ``utils.join.start_join``: one at a time across processes
(I4), the result in ``join-state.json``, which the page reads directly.

The simulator (``WQM1_VIRTUAL_NET``) and a root caller skip the socket and
call the same functions in their own process; there is no radio to protect.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import pwd
import signal
import socket
import struct
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger("wqm1.join_service")

SOCKET_PATH = Path("/run/bluesignal/wifi-join.sock")
SOCKET_MODE = 0o600
MAX_REQUEST = 4096
READ_TIMEOUT_S = 2.0
JOIN_REPLY_TIMEOUT_S = 10.0
# A rescan in AP mode takes several seconds on a Pi Zero.
RESCAN_REPLY_TIMEOUT_S = 30.0
UNIT_NAME = "bluesignal-wifi-join.service"
# Requests answered at once, each on its own thread, so a join is never
# queued behind a rescan that takes 20 s. The callers are the install user
# and root; four is more than the one page ever needs.
MAX_CONNECTIONS = 4
# One scan at a time; a second Rescan waits for the first and reads its list.
_RESCAN_LOCK = threading.Lock()

UNAVAILABLE = {"error": "unavailable"}
BAD_REQUEST = {"error": "bad_request"}


def socket_path() -> Path:
    override = os.environ.get("WQM1_JOIN_SOCKET")
    return Path(override) if override else SOCKET_PATH


def _owner() -> tuple[int, int] | None:
    """(uid, gid) of the install user, or None when it cannot be told.

    The state directory is ``chown``-ed to the install user by ``setup.sh``;
    its owner is the user the page, the Service Window and the firmware run
    as. A root-owned directory falls back to the Service Window unit's
    ``User=``.
    """
    from utils.device_keys import state_dir

    with contextlib.suppress(OSError):
        st = state_dir().stat()
        if st.st_uid != 0:
            return st.st_uid, st.st_gid
    from utils.captive_portal import _service_user

    with contextlib.suppress(KeyError):
        entry = pwd.getpwnam(_service_user())
        if entry.pw_uid != 0:
            return entry.pw_uid, entry.pw_gid
    return None


def handle_request(request: Any) -> dict[str, Any]:
    """Answer one request. Pure apart from the join and the scan it starts."""
    from utils import join, netctl
    from utils.wifi_setup import password_problem_code

    if not isinstance(request, dict):
        return dict(BAD_REQUEST)
    op = request.get("op")
    if op == "rescan":
        if join._lock_held():
            # A join owns the radio; the cached list stays.
            return {"ok": False, "count": 0}
        with _RESCAN_LOCK:
            return netctl.refresh_scan_cache()
    if op != "join":
        return dict(BAD_REQUEST)
    ssid = request.get("ssid")
    password = request.get("password")
    hidden = request.get("hidden") is True
    if not isinstance(ssid, str) or not netctl.valid_ssid(ssid):
        return {"error": "network_name_bad"}
    if password is not None and not isinstance(password, str):
        return dict(BAD_REQUEST)
    if password:
        # The page checked this already; a local client might not have.
        problem = password_problem_code(password, secured=True)
        if problem:
            return {"error": problem}
    return join.start_join(ssid, password or None, hidden=hidden)


def _use_socket() -> bool:
    """The socket is how the install user reaches root. Nobody else needs it."""
    from utils import netsim

    if netsim.is_virtual():
        return False
    try:
        return os.geteuid() != 0
    except AttributeError:
        return False


def _call(request: dict[str, Any], timeout: float) -> dict[str, Any]:
    if not _use_socket():
        return handle_request(request)
    data = (json.dumps(request) + "\n").encode("utf-8")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(timeout)
            conn.connect(str(socket_path()))
            conn.sendall(data)
            reply = _read_line(conn)
    except OSError as e:
        logger.warning("Wi-Fi join service not reachable: %s", e)
        return dict(UNAVAILABLE)
    if reply is None:
        return dict(UNAVAILABLE)
    try:
        answer = json.loads(reply)
    except ValueError:
        return dict(UNAVAILABLE)
    return answer if isinstance(answer, dict) else dict(UNAVAILABLE)


def request_join(ssid: str, password: str | None, *, hidden: bool = False) -> dict[str, Any]:
    """Start a join. ``{"state": "joining" | "busy", "ssid"}`` or ``{"error"}``."""
    return _call(
        {"op": "join", "ssid": ssid, "password": password, "hidden": bool(hidden)},
        JOIN_REPLY_TIMEOUT_S,
    )


def request_rescan() -> dict[str, Any]:
    """Refresh the cached scan. ``{"ok": bool, "count"}`` or ``{"error"}``."""
    return _call({"op": "rescan"}, RESCAN_REPLY_TIMEOUT_S)


def _read_line(conn: socket.socket) -> bytes | None:
    """One newline-terminated line, at most ``MAX_REQUEST`` bytes."""
    buf = b""
    while b"\n" not in buf:
        chunk = conn.recv(MAX_REQUEST + 1 - len(buf))
        if not chunk:
            break
        buf += chunk
        if len(buf) > MAX_REQUEST:
            return None
    line, sep, _rest = buf.partition(b"\n")
    return line if sep else None


def peer_uid(conn: socket.socket) -> int | None:
    """The connecting process's uid (Linux ``SO_PEERCRED``), or None."""
    opt = getattr(socket, "SO_PEERCRED", None)
    if opt is None:
        return None
    try:
        raw = conn.getsockopt(socket.SOL_SOCKET, opt, struct.calcsize("3i"))
    except OSError:
        return None
    _pid, uid, _gid = struct.unpack("3i", raw)
    return uid


def _allowed(uid: int | None, owner: tuple[int, int] | None) -> bool:
    if uid is None:
        return False
    return uid == 0 or (owner is not None and uid == owner[0])


def serve_one(conn: socket.socket, owner: tuple[int, int] | None) -> None:
    """Read one request, answer it, close. Never raises."""
    try:
        if not _allowed(peer_uid(conn), owner):
            logger.warning("Refused a join request from another user")
            return
        conn.settimeout(READ_TIMEOUT_S)
        line = _read_line(conn)
        if line is None:
            answer: dict[str, Any] = dict(BAD_REQUEST)
        else:
            try:
                request = json.loads(line)
            except ValueError:
                request = None
            answer = handle_request(request)
            if isinstance(request, dict) and request.get("op") == "join":
                # The SSID only. The request also holds the password (I2).
                logger.info(
                    "Join %s requested: %s", request.get("ssid"), answer.get("state") or answer
                )
        conn.sendall((json.dumps(answer) + "\n").encode("utf-8"))
    except Exception:  # noqa: BLE001 - one bad request never stops the service
        logger.exception("Join request failed")
    finally:
        with contextlib.suppress(OSError):
            conn.close()


def bind(path: Path, owner: tuple[int, int] | None) -> socket.socket:
    """A listening socket at ``path``: 0600, owned by the install user."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(FileNotFoundError):
        path.unlink()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(str(path))
        os.chmod(path, SOCKET_MODE)
        if owner is not None:
            os.chown(path, owner[0], owner[1])
        sock.listen(4)
    except OSError:
        sock.close()
        raise
    return sock


def recover_interrupted_join() -> bool:
    """After a crash mid-join, put the setup network back (I6).

    A join that died with the setup network down and no station leaves the
    unit unreachable until a reboot. The join lock is free once this process
    starts, so a ``joining`` record means that join is not running. A record
    from an earlier boot (a power cut mid-join) is the AP fallback's to
    decide at boot, not this; the kernel's boot id says which boot wrote it,
    whatever the clock did since.
    """
    from utils import join, netctl

    try:
        raw = json.loads(join.state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(raw, dict) or raw.get("state") != "joining":
        return False
    now_boot = join.current_boot_id()
    if not now_boot or raw.get("boot_id") != now_boot:
        return False
    if join._lock_held():
        # Another process (the Service Window) is running that join.
        return False
    if netctl.ap_active() or netctl.station_connected():
        return False
    ssid, passphrase = netctl.ap_credentials()
    logger.warning("A join was interrupted; raising the setup network again")
    return bool(netctl.start_ap(ssid, passphrase).get("ok"))


def serve(
    path: Path | None = None,
    stop: Callable[[], bool] | None = None,
    poll_s: float = 0.5,
    owner: tuple[int, int] | None = None,
) -> None:
    """Accept requests until ``stop()``. The socket is recreated if removed."""
    target = path or socket_path()
    who = owner if owner is not None else _owner()
    stopped = stop if stop is not None else (lambda: False)
    slots = threading.BoundedSemaphore(MAX_CONNECTIONS)

    def _serve_and_release(conn: socket.socket) -> None:
        try:
            serve_one(conn, who)
        finally:
            slots.release()

    sock = bind(target, who)
    try:
        while not stopped():
            if not target.exists():
                sock.close()
                sock = bind(target, who)
            sock.settimeout(poll_s)
            try:
                conn, _addr = sock.accept()
            except TimeoutError:
                continue
            except OSError as e:
                logger.warning("Join socket accept failed: %s", e)
                continue
            if not slots.acquire(blocking=False):
                # Every slot is busy: answer now rather than queue the caller.
                with contextlib.suppress(OSError):
                    conn.sendall((json.dumps(UNAVAILABLE) + "\n").encode("utf-8"))
                    conn.close()
                continue
            threading.Thread(
                target=_serve_and_release, args=(conn,), name="wifi-join-req", daemon=True
            ).start()
    finally:
        sock.close()
        with contextlib.suppress(OSError):
            target.unlink()


def ensure_join_service() -> dict[str, Any]:
    """Install and start this unit on an image that has not re-run setup.sh."""
    from utils.captive_portal import ensure_packaged_unit

    return ensure_packaged_unit(UNIT_NAME, as_service_user=False)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s [wifi-join] %(message)s"
    )
    halt = False

    def _stop(_signum: int, _frame: object) -> None:
        nonlocal halt
        halt = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        recover_interrupted_join()
    except Exception:  # noqa: BLE001 - the service still has to answer
        logger.exception("Could not check for an interrupted join")
    serve(stop=lambda: halt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
