"""
Digital-first wiring tests: on headerless boards (Arduino UNO Q / VENTUNO Q)
the app must skip analog/LoRa/relay hardware entirely — while RS485, GPS, and
the supervisor still come up. Also covers the defensive driver imports: every
Pi-only driver module imports on any host and fails at *construction*, not
import, when its hardware library is absent.
"""

from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Driver modules: import anywhere, construct only with the hardware lib
# ---------------------------------------------------------------------------


class TestDriverImportGuards:
    def test_ads1115_raises_without_smbus2(self, monkeypatch, mock_hardware):
        import sensors.ads1115 as mod

        monkeypatch.setattr(mod, "smbus2", None)
        with pytest.raises(RuntimeError, match="smbus2 not installed"):
            mod.ADS1115()

    def test_relay_raises_without_gpio(self, monkeypatch, mock_hardware):
        import control.relay as mod

        monkeypatch.setattr(mod, "GPIO", None)
        with pytest.raises(RuntimeError, match="RPi.GPIO not installed"):
            mod.RelayController()

    def test_leds_raise_without_gpio(self, monkeypatch, mock_hardware):
        import control.led as mod

        monkeypatch.setattr(mod, "GPIO", None)
        with pytest.raises(RuntimeError, match="RPi.GPIO not installed"):
            mod.StatusLEDs()

    def test_fan_raises_without_gpio(self, monkeypatch, mock_hardware):
        import utils.watchdog as mod

        monkeypatch.setattr(mod, "GPIO", None)
        with pytest.raises(RuntimeError, match="RPi.GPIO not installed"):
            mod.FanController()

    def test_sx1262_raises_without_spi_stack(self, monkeypatch, mock_hardware):
        import radio.sx1262 as mod

        monkeypatch.setattr(mod, "spidev", None)
        with pytest.raises(RuntimeError, match="no direct SPI"):
            mod.SX1262()

    def test_gps_works_without_gpio(self, monkeypatch, mock_hardware):
        """USB GPS on a headerless board: serial works, EXTINT is a no-op."""
        import sensors.gps as mod

        monkeypatch.setattr(mod, "GPIO", None)
        gps = mod.GPS()
        gps.power_cycle()  # must not raise — pin isn't wired on this host
        gps.close()


# ---------------------------------------------------------------------------
# WQM1App.start() gating
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


def _started_app(monkeypatch, tmp_path, config_yaml: str):
    """Build WQM1App against a tmp config with every hardware class mocked,
    run start(), and return (main_module, app)."""
    import main
    from utils.config import ConfigManager

    cfg = tmp_path / "config.yaml"
    cfg.write_text(config_yaml)
    mgr = ConfigManager(str(cfg), str(tmp_path / "config.d" / "remote.yaml"))
    monkeypatch.setattr(main, "get_config_manager", lambda: mgr)

    for name in _HW_CLASSES:
        monkeypatch.setattr(main, name, MagicMock(name=name))
    main.WQM1Database.return_value.load_session.return_value = None
    # Keep the service-window socket out of the test environment.
    monkeypatch.setattr(main.WQM1App, "_start_cmd_listener", lambda self: None)

    app = main.WQM1App()
    app.start()
    return main, app


class TestHeaderlessBoardWiring:
    def test_uno_q_skips_analog_lora_relay(self, monkeypatch, tmp_path, mock_hardware):
        main, app = _started_app(monkeypatch, tmp_path, "board: arduino-uno-q\n")

        assert app._board.id == "arduino-uno-q"
        main.RelayController.assert_not_called()
        main.StatusLEDs.assert_not_called()
        main.FanController.assert_not_called()
        main.ADS1115.assert_not_called()
        main.SX1262.assert_not_called()
        main.HardwareWatchdog.assert_not_called()
        assert app._relays is None
        assert app._adc is None
        assert app._ph is None
        assert app._lorawan is None

    def test_uno_q_keeps_digital_stack(self, monkeypatch, tmp_path, mock_hardware):
        fake_bus = MagicMock(name="ModbusBus")
        fake_multi = MagicMock(name="HondeMultiSensor")
        monkeypatch.setattr("sensors.modbus.ModbusBus", MagicMock(return_value=fake_bus))
        monkeypatch.setattr("sensors.honde.HondeMultiSensor", MagicMock(return_value=fake_multi))
        main, app = _started_app(
            monkeypatch,
            tmp_path,
            "board: arduino-uno-q\nrs485_multi_enabled: true\n",
        )

        # RS485 probes, GPS, DB, and the supervisor all come up headerless.
        assert app._multi is fake_multi
        main.GPS.assert_called_once()
        main.WQM1Database.assert_called_once()
        assert app._supervisor is not None

    def test_ventuno_q_is_headerless_too(self, monkeypatch, tmp_path, mock_hardware):
        main, app = _started_app(monkeypatch, tmp_path, "board: arduino-ventuno-q\n")
        main.ADS1115.assert_not_called()
        main.SX1262.assert_not_called()


class TestPiBoardWiring:
    def test_pi_builds_full_hardware_stack(self, monkeypatch, tmp_path, mock_hardware):
        main, app = _started_app(monkeypatch, tmp_path, "board: rpi-zero-2w\n")

        assert app._board.id == "rpi-zero-2w"
        main.RelayController.assert_called_once()
        main.StatusLEDs.assert_called_once()
        main.FanController.assert_called_once()
        main.ADS1115.assert_called_once()
        main.SX1262.assert_called_once()
        main.GPS.assert_called_once()
        assert app._supervisor is not None
