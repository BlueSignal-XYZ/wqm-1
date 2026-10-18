"""
GPIO facade tests (src/platform_support/gpio.py).

Two backends, one contract. The RPi backend must make exactly the RPi.GPIO
calls the drivers made before the facade existed — the relay/LED/fan tests
pin those call shapes, this file pins the facade's half. The gpiochip
backend must translate every BCM number through the host map and never
touch RPi.GPIO.
"""

from unittest.mock import MagicMock

import pytest

from platform_support.gpio import HostGpio, open_gpio
from platform_support.hostpins import (
    UnresolvedPinError,
    headerless_pins,
    orangepi_zero_3w_pins,
    rpi5_pins,
    rpi_pins,
)


class FakeLgpio:
    RISING_EDGE = 1

    def __init__(self):
        self.calls = []
        self.handles = {}
        self.callbacks = []

    def gpiochip_open(self, chip):
        self.calls.append(("open", chip))
        return 100 + chip

    def gpio_claim_output(self, h, line, level):
        self.calls.append(("claim_out", h, line, level))

    def gpio_claim_input(self, h, line):
        self.calls.append(("claim_in", h, line))

    def gpio_write(self, h, line, level):
        self.calls.append(("write", h, line, level))

    def gpio_read(self, h, line):
        self.calls.append(("read", h, line))
        return 1

    def gpio_claim_alert(self, h, line, edge):
        self.calls.append(("claim_alert", h, line, edge))

    def gpio_set_debounce_micros(self, h, line, us):
        self.calls.append(("debounce", h, line, us))

    def callback(self, h, line, edge, func):
        self.calls.append(("callback", h, line, edge))
        cb = MagicMock()
        self.callbacks.append((func, cb))
        return cb

    def gpio_free(self, h, line):
        self.calls.append(("free", h, line))

    def gpiochip_close(self, h):
        self.calls.append(("close", h))


def _opi_pins(tmp_path):
    root = tmp_path / "gpio"
    root.mkdir()
    for base, n in ((0, 352), (352, 64)):
        d = root / f"gpiochip{base}"
        d.mkdir()
        (d / "base").write_text(str(base))
        (d / "ngpio").write_text(str(n))
    return orangepi_zero_3w_pins(
        overrides={"pins": {12: "PD5", 15: "PD6", 22: "PD7"}}, sysfs_root=str(root)
    )


class TestRPiBackend:
    def test_calls_rpi_gpio_exactly_as_the_drivers_did(self):
        rpi = MagicMock()
        rpi.BCM, rpi.OUT, rpi.IN, rpi.HIGH, rpi.LOW = 11, 0, 1, 1, 0
        io = HostGpio(rpi_pins(), rpi_module=rpi)
        io.setup_output(17)
        io.setup_output(18, initial=True)
        io.setup_input(20)
        io.write(17, True)
        io.write(17, False)
        rpi.input.return_value = 1
        assert io.read(20) is True

        rpi.setmode.assert_called_once_with(11)
        rpi.setwarnings.assert_called_once_with(False)
        assert rpi.setup.call_args_list[0].args == (17, 0)
        assert rpi.setup.call_args_list[0].kwargs == {"initial": 0}
        assert rpi.setup.call_args_list[1].kwargs == {"initial": 1}
        assert rpi.setup.call_args_list[2].args == (20, 1)
        assert [c.args for c in rpi.output.call_args_list] == [(17, 1), (17, 0)]
        rpi.input.assert_called_once_with(20)

    def test_output_is_looked_up_at_call_time(self):
        # test_phase3_coverage swaps GPIO.output for a failing mock mid-test;
        # the facade must not have cached the bound method.
        rpi = MagicMock()
        io = HostGpio(rpi_pins(), rpi_module=rpi)
        rpi.output = MagicMock(side_effect=RuntimeError("pin busy"))
        with pytest.raises(RuntimeError, match="pin busy"):
            io.write(17, True)

    def test_refuses_without_rpi_gpio(self):
        with pytest.raises(RuntimeError, match="RPi.GPIO not installed"):
            HostGpio(rpi_pins(), rpi_module=None)

    def test_alerts_go_through_lgpio_on_chip_0_line_bcm(self):
        rpi = MagicMock()
        lg = FakeLgpio()
        io = HostGpio(rpi_pins(), rpi_module=rpi, lgpio_module=lg)
        cb = MagicMock()
        handle = io.claim_alert(16, cb, debounce_us=500)
        assert ("open", 0) in lg.calls
        assert ("claim_alert", 100, 16, 1) in lg.calls
        assert ("debounce", 100, 16, 500) in lg.calls
        assert ("callback", 100, 16, 1) in lg.calls
        handle.cancel()
        handle.cancel()  # idempotent
        assert lg.calls.count(("free", 100, 16)) == 1
        assert lg.callbacks[0][1].cancel.call_count == 1

    def test_release_and_close_are_no_ops_on_rpi(self):
        rpi = MagicMock()
        io = HostGpio(rpi_pins(), rpi_module=rpi)
        io.setup_output(17)
        io.release(17)
        io.close()
        rpi.cleanup.assert_not_called()


class TestGpiochipBackend:
    def test_translates_bcm_to_chip_and_line(self, tmp_path):
        lg = FakeLgpio()
        io = HostGpio(_opi_pins(tmp_path), rpi_module=None, lgpio_module=lg)
        assert io.backend == "gpiochip"
        io.setup_output(17)  # pin 11 → PB0 → chip 0 line 32
        io.setup_output(23, initial=True)  # pin 16 → PL2 → chip 1 line 2
        io.setup_input(20)  # pin 38 → PB8 → chip 0 line 40
        io.write(17, True)
        assert io.read(20) is True
        assert lg.calls[:2] == [("open", 0), ("claim_out", 100, 32, 0)]
        assert ("open", 1) in lg.calls and ("claim_out", 101, 2, 1) in lg.calls
        assert ("claim_in", 100, 40) in lg.calls
        assert ("write", 100, 32, 1) in lg.calls
        assert ("read", 100, 40) in lg.calls
        # One handle per chip, however many lines.
        assert lg.calls.count(("open", 0)) == 1

    def test_never_touches_rpi_gpio(self, tmp_path):
        rpi = MagicMock()
        io = HostGpio(_opi_pins(tmp_path), rpi_module=rpi, lgpio_module=FakeLgpio())
        io.setup_output(17)
        io.write(17, True)
        assert not rpi.method_calls

    def test_unresolved_net_is_refused_by_name(self, tmp_path):
        io = HostGpio(_opi_pins(tmp_path), rpi_module=None, lgpio_module=FakeLgpio())
        with pytest.raises(UnresolvedPinError, match="BCM 5 .*pin 29"):
            io.setup_input(5)  # ADS ALERT/RDY, unpublished and not overridden

    def test_alert_uses_the_host_line(self, tmp_path):
        lg = FakeLgpio()
        io = HostGpio(_opi_pins(tmp_path), rpi_module=None, lgpio_module=lg)
        handle = io.claim_alert(16, MagicMock())  # LORA_DIO1 → PD2 → line 98
        assert ("claim_alert", 100, 98, 1) in lg.calls
        handle.cancel()
        assert ("free", 100, 98) in lg.calls

    def test_release_and_close_free_lines_and_chips(self, tmp_path):
        lg = FakeLgpio()
        io = HostGpio(_opi_pins(tmp_path), rpi_module=None, lgpio_module=lg)
        io.setup_output(17)
        io.setup_output(23)
        io.release(17)
        assert ("free", 100, 32) in lg.calls
        io.close()
        assert ("free", 101, 2) in lg.calls
        assert ("close", 100) in lg.calls and ("close", 101) in lg.calls

    def test_refuses_without_lgpio(self, tmp_path):
        with pytest.raises(RuntimeError, match="lgpio not installed"):
            HostGpio(_opi_pins(tmp_path), rpi_module=MagicMock(), lgpio_module=None)


class TestHeadlessBackend:
    def test_headerless_profile_refuses(self):
        with pytest.raises(RuntimeError, match="no direct GPIO"):
            HostGpio(
                headerless_pins("arduino-uno-q"), rpi_module=MagicMock(), lgpio_module=MagicMock()
            )


class _ChipTable:
    """lgpio stand-in: chip index → kernel label, and GPIO{n} at offset n."""

    def __init__(self, labels: dict[int, str], line_name: str | None = None):
        self.labels = labels
        self.line_name = line_name
        self.closed: list[int] = []

    def gpiochip_open(self, chip: int) -> int:
        if chip not in self.labels:
            raise OSError(f"no gpiochip{chip}")
        return 100 + chip

    def gpiochip_close(self, handle: int) -> None:
        self.closed.append(handle)

    def gpio_get_chip_info(self, handle: int) -> tuple[int, str, str]:
        chip = handle - 100
        return (54, f"gpiochip{chip}", self.labels[chip])

    def gpio_get_line_info(self, handle: int, line: int) -> tuple[int, int, str, str]:
        if self.line_name is not None and line == 17:
            name = self.line_name
        elif line >= 2:
            name = f"GPIO{line}"
        else:
            name = "ID_SDA" if line == 0 else "ID_SCL"
        return (line, 0, name, "")


class TestPi5Backend:
    """Pi 5 selects lgpio on the RP1. Other Pis stay on RPi.GPIO."""

    def test_old_kernel_numbering_uses_chip_4_not_chip_0(self):
        pins = rpi5_pins(lgpio_module=_ChipTable({0: "gpio-brcmstb@107d508500", 4: "pinctrl-rp1"}))
        assert pins.backend == "rp1"
        assert pins.source == "profile+rp1-label"
        assert pins.line(17) == (4, 17)
        assert pins.line(4) == (4, 4)
        assert pins.i2c_bus == 1 and pins.spi_bus == 0

    def test_trixie_numbering_uses_chip_0_when_that_is_the_rp1(self):
        pins = rpi5_pins(lgpio_module=_ChipTable({0: "pinctrl-rp1", 10: "gpio-brcmstb"}))
        assert pins.line(17) == (0, 17)
        assert pins.line(21) == (0, 21)

    def test_does_not_guess_when_the_label_is_missing(self):
        with pytest.raises(RuntimeError, match="Refusing to guess"):
            rpi5_pins(lgpio_module=_ChipTable({0: "gpio-brcmstb", 4: "gpio-brcmstb"}))

    def test_refuses_a_mislabeled_chip_whose_line_names_are_not_the_header(self):
        with pytest.raises(RuntimeError, match="Refusing to drive"):
            rpi5_pins(lgpio_module=_ChipTable({0: "pinctrl-rp1"}, line_name="2712_BOOT_MISO"))

    def test_without_lgpio_lines_stay_unresolved(self):
        pins = rpi5_pins(None)
        assert pins.backend == "rp1"
        assert all(line is None for line in pins.lines.values())
        with pytest.raises(RuntimeError, match="Pi 5 RP1 GPIO"):
            HostGpio(pins, rpi_module=MagicMock(), lgpio_module=None)

    def test_setup_and_alerts_use_lgpio_on_the_resolved_chip(self):
        rpi = MagicMock()
        lg = FakeLgpio()
        pins = rpi5_pins(lgpio_module=_ChipTable({0: "gpio-brcmstb", 4: "pinctrl-rp1"}))
        io = HostGpio(pins, rpi_module=rpi, lgpio_module=lg)
        assert io.backend == "rp1"
        io.setup_output(17)
        io.setup_output(18, initial=True)
        io.setup_input(21)
        io.write(17, True)
        assert io.read(21) is True
        handle = io.claim_alert(16, MagicMock(), debounce_us=250)
        assert ("open", 4) in lg.calls
        assert ("claim_out", 104, 17, 0) in lg.calls
        assert ("claim_out", 104, 18, 1) in lg.calls
        assert ("write", 104, 17, 1) in lg.calls
        assert ("read", 104, 21) in lg.calls
        assert ("claim_alert", 104, 16, 1) in lg.calls
        assert ("debounce", 104, 16, 250) in lg.calls
        assert not rpi.method_calls
        io.release(17)
        assert ("free", 104, 17) in lg.calls
        handle.cancel()
        io.close()
        assert ("close", 104) in lg.calls

    def test_other_pis_still_call_rpi_gpio_on_chip_0(self):
        rpi = MagicMock()
        rpi.BCM = rpi.OUT = rpi.LOW = 0
        lg = FakeLgpio()
        pins = rpi_pins("rpi-4")
        assert pins.backend == "rpi"
        assert pins.line(17) == (0, 17)
        io = HostGpio(pins, rpi_module=rpi, lgpio_module=lg)
        io.setup_output(17)
        rpi.setmode.assert_called_once()
        rpi.setup.assert_called_once()
        assert lg.calls == []


class TestOpenGpio:
    def test_defaults_to_the_active_board(self, mock_hardware):
        # No board pinned → auto-detection. A dev host with no device tree
        # falls back to the Zero 2 W profile; a real Pi reports its own id.
        # Either way the header path is the Pi's, not a demotion.
        from platform_support.board import RPI_DIRECT_HEADER_IDS

        io = open_gpio()
        assert io.backend == "rpi"
        assert io.pins.board_id in RPI_DIRECT_HEADER_IDS

    def test_follows_set_active_board(self, tmp_path):
        from platform_support import PROFILES, set_active_board

        set_active_board(PROFILES["orangepi-zero-3w"], _opi_pins(tmp_path))
        io = open_gpio(lgpio_module=FakeLgpio())
        assert io.backend == "gpiochip"
        assert io.pins.line(17) == (0, 32)

    def test_explicit_pins_win(self):
        io = open_gpio(rpi_module=MagicMock(), pins=rpi_pins("custom"))
        assert io.pins.board_id == "custom"
