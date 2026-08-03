"""
Host reboot — the unprivileged (firmware) half of the recovery path.

Covers `app.reboot`, its wiring into the firmware command socket, and the
guarantee that matters most: the relays are at fail-safe before anything asks
for a reboot, and stay there through shutdown.

The privileged half (the root OTA agent that executes the request) is covered
in test_ota_agent.py::TestHostReboot.
"""

import time
from unittest.mock import MagicMock

import pytest

from app.reboot import REBOOT_REQUEST_FLAG, relays_failsafe, request_host_reboot


@pytest.fixture
def flag(tmp_path):
    return tmp_path / "state" / "reboot-request"


class TestRelaysFailsafe:
    def test_all_coils_dropped(self):
        relays = MagicMock()
        assert relays_failsafe(relays) is True
        relays.all_off.assert_called_once_with()

    def test_headerless_board_has_nothing_to_drop(self):
        """No relay hardware (Arduino UNO Q et al) is already fail-safe."""
        assert relays_failsafe(None) is True

    def test_gpio_failure_is_reported_not_raised(self):
        relays = MagicMock()
        relays.all_off.side_effect = RuntimeError("GPIO busy")
        assert relays_failsafe(relays) is False


class TestRequestHostReboot:
    def test_relays_are_off_before_the_request_is_written(self, flag):
        """Ordering is the whole point: a coil must never be left energised
        with nobody evaluating rules."""
        relays = MagicMock()
        flag_existed_when_relays_dropped = {}

        def record():
            flag_existed_when_relays_dropped["value"] = flag.exists()

        relays.all_off.side_effect = record

        result = request_host_reboot(relays, flag)

        assert result == {"ok": True, "rebooting": True, "relaysSafe": True}
        relays.all_off.assert_called_once_with()
        assert flag_existed_when_relays_dropped["value"] is False
        assert flag.exists()

    def test_creates_the_state_directory(self, flag):
        assert not flag.parent.exists()
        assert request_host_reboot(MagicMock(), flag)["ok"] is True
        assert flag.is_file()

    def test_request_is_fresh_enough_for_the_agent_to_accept(self, flag):
        request_host_reboot(MagicMock(), flag)
        assert time.time() - flag.stat().st_mtime < 5

    def test_stale_leftover_flag_is_refreshed(self, flag):
        """A request the agent ignored as stale must not poison the next one:
        touching an existing flag has to move its mtime forward."""
        flag.parent.mkdir(parents=True)
        flag.touch()
        long_ago = time.time() - 86400
        import os

        os.utime(flag, (long_ago, long_ago))

        request_host_reboot(MagicMock(), flag)

        assert time.time() - flag.stat().st_mtime < 5

    def test_relay_failure_still_reboots_but_says_so(self, flag):
        """Refusing here would break the one recovery path that still works
        when SSH is dead — and GPIO drops the coils across a reboot anyway."""
        relays = MagicMock()
        relays.all_off.side_effect = RuntimeError("GPIO busy")

        result = request_host_reboot(relays, flag)

        assert result["ok"] is True
        assert result["relaysSafe"] is False
        assert flag.exists()

    def test_unwritable_state_dir_fails_loudly(self, tmp_path):
        relays = MagicMock()
        blocked = tmp_path / "not-a-dir"
        blocked.write_text("this is a file, not a directory\n")

        result = request_host_reboot(relays, blocked / "reboot-request")

        assert result["ok"] is False
        assert "could not request reboot" in result["error"]
        # Relays were still driven safe — a failed request leaves no surprises.
        relays.all_off.assert_called_once_with()

    def test_default_flag_is_the_path_the_agent_watches(self):
        from ota.agent import REBOOT_FLAG

        assert REBOOT_REQUEST_FLAG.name == REBOOT_FLAG
        assert REBOOT_REQUEST_FLAG.parent.name == "bluesignal"


# ---------------------------------------------------------------------------
# Command-socket wiring
# ---------------------------------------------------------------------------

_HW_CLASSES = [
    "RelayController",
    "StatusLEDs",
    "FanController",
    "ADS1115",
    "DS18B20",
    "PHSensor",
    "TDSSensor",
    "TurbiditySensor",
    "ORPSensor",
    "SX1262",
    "LoRaWANMAC",
    "GPS",
    "WQM1Database",
    "CalibrationManager",
    "HealthReporter",
    "HardwareWatchdog",
]


@pytest.fixture
def firmware(monkeypatch, tmp_path, mock_hardware):
    """A started WQM1App on a Pi board with every hardware class mocked and the
    reboot flag pointed at tmp_path."""
    import main
    from utils.config import ConfigManager

    cfg = tmp_path / "config.yaml"
    cfg.write_text("board: rpi-zero-2w\n")
    mgr = ConfigManager(str(cfg), str(tmp_path / "config.d" / "remote.yaml"))
    monkeypatch.setattr(main, "get_config_manager", lambda: mgr)
    for name in _HW_CLASSES:
        monkeypatch.setattr(main, name, MagicMock(name=name))
    main.WQM1Database.return_value.load_session.return_value = None
    monkeypatch.setattr(main.WQM1App, "_start_cmd_listener", lambda self: None)
    monkeypatch.setattr(main, "REBOOT_REQUEST_FLAG", tmp_path / "state" / "reboot-request")

    app = main.WQM1App()
    app.start()
    app._relays.reset_mock()  # start() forces the coils off; tests want what follows
    return main, app


class TestCommandSocketDispatch:
    def test_reboot_action_requests_a_reboot(self, firmware):
        main, app = firmware

        result = app._handle_cmd({"action": "reboot"})

        assert result["ok"] is True
        assert result["rebooting"] is True
        assert main.REBOOT_REQUEST_FLAG.exists()
        app._relays.all_off.assert_called_once_with()

    def test_reboot_is_distinct_from_restart(self, firmware):
        """`restart` bounces the service only — it must never touch the host."""
        main, app = firmware

        result = app._handle_cmd({"action": "restart"})

        assert result == {"ok": True, "restarting": True}
        assert not main.REBOOT_REQUEST_FLAG.exists()

    def test_unknown_action_still_rejected(self, firmware):
        _, app = firmware
        assert app._handle_cmd({"action": "reboot_now"})["ok"] is False


class TestShutdownFailsafe:
    def test_shutdown_drops_the_relays(self, firmware):
        """The reboot goes through systemd, which SIGTERMs us — so the coils
        get a second, independent trip to fail-safe on the way down."""
        _, app = firmware

        app._shutdown()

        app._relays.all_off.assert_called_once_with()

    def test_shutdown_survives_a_dead_relay_controller(self, firmware):
        _, app = firmware
        app._relays.all_off.side_effect = RuntimeError("GPIO gone")

        app._shutdown()  # must not raise — everything else still gets closed

        app._db.close.assert_called_once_with()
