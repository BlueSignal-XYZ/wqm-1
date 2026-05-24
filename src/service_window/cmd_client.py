"""
Unix socket client for sending commands to the firmware process.

The firmware runs a command listener on /var/run/bluesignal/cmd.sock.
This client sends JSON commands and receives JSON responses.
"""

import json
import socket


def send_command(sock_path: str, action: str, **kwargs: object) -> dict[str, object]:
    """
    Send a command to the firmware via Unix socket.

    Args:
        sock_path: Path to the Unix domain socket.
        action: Command action (e.g. "relay_set").
        **kwargs: Additional command parameters.

    Returns:
        Response dict from firmware.
    """
    cmd = {"action": action, **kwargs}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(5.0)
            s.connect(sock_path)
            s.sendall(json.dumps(cmd).encode() + b"\n")
            data = s.recv(4096)
            return json.loads(data.decode())
    except (OSError, json.JSONDecodeError) as e:
        return {"ok": False, "error": str(e)}
