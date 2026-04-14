"""Tests for src/service_window/cmd_client.py — firmware IPC client."""

import json
import socket
import threading


def _start_fake_server(sock_path: str, response: dict, error: Exception | None = None):
    """Start a tiny Unix-socket echo server that returns `response` JSON."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(sock_path)
    s.listen(1)
    s.settimeout(5.0)

    def _run():
        try:
            conn, _ = s.accept()
            conn.recv(4096)
            if error is not None:
                conn.close()
                return
            conn.sendall(json.dumps(response).encode())
            conn.close()
        except Exception:
            pass
        finally:
            s.close()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


class TestSendCommand:
    def test_sends_and_receives_json(self, tmp_path, mock_hardware):
        from service_window.cmd_client import send_command

        sock_path = str(tmp_path / "cmd.sock")
        _start_fake_server(sock_path, {"ok": True, "channel": 1, "state": True})

        resp = send_command(sock_path, "relay_set", channel=1, state=True)
        assert resp == {"ok": True, "channel": 1, "state": True}

    def test_returns_error_on_missing_socket(self, tmp_path, mock_hardware):
        from service_window.cmd_client import send_command

        sock_path = str(tmp_path / "does_not_exist.sock")
        resp = send_command(sock_path, "relay_set", channel=1, state=True)
        assert resp["ok"] is False
        assert "error" in resp

    def test_returns_error_on_invalid_json_response(self, tmp_path, mock_hardware):
        from service_window.cmd_client import send_command

        sock_path = str(tmp_path / "cmd.sock")

        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(sock_path)
        s.listen(1)
        s.settimeout(5.0)

        def _run():
            try:
                conn, _ = s.accept()
                conn.recv(4096)
                conn.sendall(b"not-valid-json-{")
                conn.close()
            finally:
                s.close()

        threading.Thread(target=_run, daemon=True).start()
        resp = send_command(sock_path, "ping")
        assert resp["ok"] is False
        assert "error" in resp

    def test_forwards_kwargs_as_command_fields(self, tmp_path, mock_hardware):
        """The client must include kwargs in the JSON command body."""
        from service_window.cmd_client import send_command

        sock_path = str(tmp_path / "cmd.sock")
        received: dict = {}

        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(sock_path)
        s.listen(1)
        s.settimeout(5.0)

        def _run():
            try:
                conn, _ = s.accept()
                data = conn.recv(4096)
                # Remove optional trailing newline the client may append
                received.update(json.loads(data.decode().strip()))
                conn.sendall(json.dumps({"ok": True}).encode())
                conn.close()
            finally:
                s.close()

        threading.Thread(target=_run, daemon=True).start()
        send_command(sock_path, "relay_set", channel=3, state=False, duration=5)

        assert received["action"] == "relay_set"
        assert received["channel"] == 3
        assert received["state"] is False
        assert received["duration"] == 5
