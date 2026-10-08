"""
Orange Pi Zero 3W: detection, profile, and WQM1App wiring.

The HAT seats on the Orange Pi's Pi-layout header, so this host gets the
FULL direct-header stack — relays, LEDs, fan, ADS1115, SX1262, pulse meter —
but with the host's bus numbers, UART and (chip, line) map. These tests pin
that every constructor receives the host's facts rather than the Pi's.
"""

import logging
from unittest.mock import MagicMock

import pytest

from platform_support import PROFILES, detect_board, set_active_board
from platform_support.hostpins import orangepi_zero_3w_pins

BENCH = {12: "PD5", 15: "PD6", 22: "PD7", 27: "PE5", 28: "PE6", 29: "PE7", 31: "PE8"}


def _model_file(tmp_path, text):
    p = tmp_path / "model"
    p.write_bytes(text.encode() + b"\x00")
    return str(p)


class TestDetection:
    @pytest.mark.parametrize(
        "model",
        [
            "OrangePi Zero 3W",
            "Orange Pi Zero 3W",
            "OrangePi Zero3W",
            "orangepi-zero-3w",
            "Xunlong Orange Pi Zero 3W",
        ],
    )
    def test_model_strings(self, tmp_path, model):
        assert detect_board(model_path=_model_file(tmp_path, model)).id == "orangepi-zero-3w"

    def test_other_orange_pi_boards_are_not_claimed(self, tmp_path):
        # A different Orange Pi has a different pin table; the Pi fallback is
        # the existing rule for an unknown board, and RPi.GPIO will refuse
        # loudly at construction rather than drive the wrong lines.
        assert detect_board(model_path=_model_file(tmp_path, "OrangePi Zero3")).id == "rpi-zero-2w"
        assert detect_board(model_path=_model_file(tmp_path, "OrangePi 5")).id == "rpi-zero-2w"

    def test_profile_is_direct_header_on_a_gpiochip(self):
        p = PROFILES["orangepi-zero-3w"]
        assert p.has_direct_headers is True
        assert p.gpio_backend == "gpiochip"
        assert PROFILES["rpi-zero-2w"].gpio_backend == "rpi"
        assert PROFILES["arduino-uno-q"].gpio_backend == "none"
        assert PROFILES["generic-linux"].gpio_backend == "none"

    def test_override_pins_it(self, tmp_path):
        path = _model_file(tmp_path, "Raspberry Pi Zero 2 W")
        assert detect_board(override="orangepi-zero-3w", model_path=path).id == "orangepi-zero-3w"


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


def _sysfs(tmp_path):
    root = tmp_path / "gpio"
    root.mkdir(exist_ok=True)
    for base, n in ((0, 352), (352, 64)):
        d = root / f"gpiochip{base}"
        d.mkdir(exist_ok=True)
        (d / "base").write_text(str(base))
        (d / "ngpio").write_text(str(n))
    return str(root)


def _started_app(monkeypatch, tmp_path, config_yaml, overrides=None):
    import main
    import platform_support
    from utils.config import ConfigManager

    pins = orangepi_zero_3w_pins(overrides=overrides, sysfs_root=_sysfs(tmp_path))
    monkeypatch.setattr(platform_support, "pins_for_board", lambda board_id, **kw: pins)
    set_active_board(None)

    cfg = tmp_path / "config.yaml"
    cfg.write_text(config_yaml)
    mgr = ConfigManager(str(cfg), str(tmp_path / "config.d" / "remote.yaml"))
    monkeypatch.setattr(main, "get_config_manager", lambda: mgr)
    for name in _HW_CLASSES:
        monkeypatch.setattr(main, name, MagicMock(name=name))
    main.WQM1Database.return_value.load_session.return_value = None
    main.WQM1Database.return_value.get_meta.return_value = None
    monkeypatch.setattr(main.WQM1App, "_start_cmd_listener", lambda self: None)

    app = main.WQM1App()
    app.start()
    return main, app, pins


class TestOrangePiWiring:
    def test_full_stack_with_the_hosts_buses(self, monkeypatch, tmp_path, mock_hardware):
        main, app, pins = _started_app(
            monkeypatch, tmp_path, "board: orangepi-zero-3w\n", overrides={"pins": BENCH}
        )
        assert app._board.id == "orangepi-zero-3w"
        assert app._pins is pins
        main.RelayController.assert_called_once()
        main.StatusLEDs.assert_called_once()
        main.FanController.assert_called_once()
        main.ADS1115.assert_called_once_with(bus=0)
        main.GPS.assert_called_once()
        assert main.GPS.call_args.kwargs["port"] == "/dev/ttyS0"
        main.SX1262.assert_called_once_with(pins=pins)
        assert app._supervisor is not None

    def test_pulse_meter_gets_the_host_line(self, monkeypatch, tmp_path, mock_hardware):
        fake_meter = MagicMock(name="PulseFlowMeter")
        monkeypatch.setattr("sensors.flow.PulseFlowMeter", fake_meter)
        _main, app, _pins = _started_app(
            monkeypatch,
            tmp_path,
            "board: orangepi-zero-3w\nflow_pulse_enabled: true\nflow_pulse_gpio: 26\n",
            overrides={"pins": BENCH},
        )
        assert app._flow is fake_meter.return_value
        kw = fake_meter.call_args.kwargs
        assert kw["gpio"] == 26
        assert (kw["chip"], kw["line"]) == (0, 100)  # pin 37 → PD4

    def test_unresolved_nets_are_named_at_startup(
        self, monkeypatch, tmp_path, mock_hardware, caplog
    ):
        with caplog.at_level(logging.ERROR):
            _started_app(monkeypatch, tmp_path, "board: orangepi-zero-3w\n")
        msg = next(r.getMessage() for r in caplog.records if "no known line" in r.getMessage())
        assert "relay_3=BCM22(pin 15)" in msg
        assert "led_2=BCM25(pin 22)" in msg
        assert "lora_rst=BCM18(pin 12)" in msg
        assert "host-pins.py" in msg

    def test_pi_still_gets_pi_buses(self, monkeypatch, tmp_path, mock_hardware):
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
        app = main.WQM1App()
        app.start()
        main.ADS1115.assert_called_once_with(bus=1)
        assert main.GPS.call_args.kwargs["port"] == "/dev/serial0"
        assert app._pins.backend == "rpi"
        assert app._pins.unresolved({"relay_1": 17}) == []

    def test_headerless_board_has_no_pins(self, monkeypatch, tmp_path, mock_hardware):
        import main
        from utils.config import ConfigManager

        cfg = tmp_path / "config.yaml"
        cfg.write_text("board: arduino-uno-q\n")
        mgr = ConfigManager(str(cfg), str(tmp_path / "config.d" / "remote.yaml"))
        monkeypatch.setattr(main, "get_config_manager", lambda: mgr)
        for name in _HW_CLASSES:
            monkeypatch.setattr(main, name, MagicMock(name=name))
        main.WQM1Database.return_value.load_session.return_value = None
        monkeypatch.setattr(main.WQM1App, "_start_cmd_listener", lambda self: None)
        app = main.WQM1App()
        app.start()
        assert app._pins is None
        assert "port" not in main.GPS.call_args.kwargs


class TestDriversOnOrangePi:
    """The real driver classes, on the gpiochip backend, with a fake lgpio."""

    @pytest.fixture
    def opi(self, tmp_path):
        from tests.test_platform_gpio import FakeLgpio

        pins = orangepi_zero_3w_pins(overrides={"pins": BENCH}, sysfs_root=_sysfs(tmp_path))
        set_active_board(PROFILES["orangepi-zero-3w"], pins)
        lg = FakeLgpio()
        from platform_support.gpio import HostGpio

        return HostGpio(pins, rpi_module=None, lgpio_module=lg), lg

    def test_relays_drive_allwinner_lines(self, opi, mock_hardware):
        from control.relay import RelayController

        io, lg = opi
        relays = RelayController(io=io)
        relays.set(1, True)
        relays.set(4, True)
        assert ("write", 100, 32, 1) in lg.calls  # relay 1 → PB0
        assert ("write", 101, 2, 1) in lg.calls  # relay 4 → PL2 on the R_PIO chip
        assert not mock_hardware["gpio"].output.called
        relays.cleanup()

    def test_leds_fan_and_gps_extint(self, opi, mock_hardware):
        from control.led import StatusLEDs
        from sensors.gps import GPS
        from utils.watchdog import FanController

        io, lg = opi
        leds = StatusLEDs(io=io)
        leds.on(24)
        assert ("write", 101, 3, 1) in lg.calls  # LED1 → pin 18 → PL3
        fan = FanController(io=io)
        fan.update(cpu_temp=70.0)
        assert ("write", 100, 39, 1) in lg.calls  # FAN_EN → pin 40 → PB7
        gps = GPS(port="/dev/ttyS0", io=io)
        gps.power_cycle()
        assert ("write", 100, 38, 1) in lg.calls  # EXTINT → pin 35 → PB6
        assert not mock_hardware["gpio"].output.called
        leds.cleanup()
        fan.cleanup()
        gps.close()

    def test_sx1262_opens_spidev3_and_alerts_on_pd2(self, opi, mock_hardware):
        from radio.sx1262 import SX1262

        io, lg = opi
        radio = SX1262(pins=io.pins, io=io)
        mock_hardware["spi"].open.assert_called_once_with(3, 0)
        assert ("claim_out", 100, 101, 1) in lg.calls  # LORA_RST → pin 12 → PD5 (bench), high
        assert ("claim_in", 100, 40) in lg.calls  # LORA_BUSY → PB8
        lg.calls.clear()
        radio._wait_busy = lambda timeout_s=1.0: True
        radio.init()
        assert ("claim_alert", 100, 98, 1) in lg.calls  # DIO1 → PD2
        radio.close()
        assert ("free", 100, 98) in lg.calls
