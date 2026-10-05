"""
Host-pin map tests (src/platform_support/hostpins.py).

The HAT's nets are BCM numbers; a second host is one table from the Pi
header's physical pins to that SoC's pins, plus a resolver from a SoC pin
name to a kernel (chip, line). The invariants: the Pi map is the identity;
the Orange Pi map ships only what the vendor published and refuses to guess
the rest; the bench can fill the rest in from the board's own `gpio
readall`; and a conflict between the two sources is reported, not hidden.
"""

import logging
from types import SimpleNamespace

import pytest

from platform_support.hostpins import (
    ORANGEPI_ZERO_3W_PHYSICAL_TO_SOC,
    PHYSICAL_TO_BCM,
    PI_HEADER_BCM_TO_PHYSICAL,
    HostPins,
    UnresolvedPinError,
    headerless_pins,
    merge_physical_overrides,
    orangepi_zero_3w_pins,
    parse_gpio_readall,
    pins_for_board,
    resolve_soc_line,
    rpi_pins,
    soc_global_number,
    soc_name_from_global,
)

# The five header pins Orange Pi's published pinout does not name.
UNPUBLISHED_PHYSICAL = {12, 15, 22, 27, 28, 29, 31}
# What a bench `gpio readall` would fill in (invented values for the test —
# the real ones come off the board).
BENCH_PINS = {12: "PD5", 15: "PD6", 22: "PD7", 27: "PE5", 28: "PE6", 29: "PE7", 31: "PE8"}


def _sysfs(tmp_path, chips=((0, 352), (352, 64))):
    root = tmp_path / "gpio"
    root.mkdir()
    for base, ngpio in chips:
        d = root / f"gpiochip{base}"
        d.mkdir()
        (d / "base").write_text(f"{base}\n")
        (d / "ngpio").write_text(f"{ngpio}\n")
    return str(root)


class TestPiHeader:
    def test_every_bcm_lands_on_a_distinct_header_pin(self):
        physicals = list(PI_HEADER_BCM_TO_PHYSICAL.values())
        assert len(physicals) == len(set(physicals)) == 28
        assert all(1 <= p <= 40 for p in physicals)
        assert PHYSICAL_TO_BCM[40] == 21  # FAN_EN
        assert PHYSICAL_TO_BCM[36] == 16  # LORA_DIO1

    def test_rpi_map_is_the_identity(self):
        pins = rpi_pins()
        assert pins.backend == "rpi"
        assert pins.i2c_bus == 1 and (pins.spi_bus, pins.spi_device) == (0, 0)
        assert pins.gps_port == "/dev/serial0"
        for bcm in PI_HEADER_BCM_TO_PHYSICAL:
            assert pins.line(bcm) == (0, bcm)
        assert pins.unresolved({"relay_1": 17, "fan": 21}) == []

    def test_headerless_profile_resolves_nothing(self):
        pins = headerless_pins("arduino-uno-q")
        assert pins.backend == "none"
        with pytest.raises(UnresolvedPinError):
            pins.line(17)


class TestOrangePiPublishedTable:
    def test_covers_every_header_pin_exactly_once(self):
        assert set(ORANGEPI_ZERO_3W_PHYSICAL_TO_SOC) == set(PI_HEADER_BCM_TO_PHYSICAL.values())

    def test_published_names_are_unique_allwinner_pins(self):
        names = [n for n in ORANGEPI_ZERO_3W_PHYSICAL_TO_SOC.values() if n]
        assert len(names) == len(set(names))
        for n in names:
            soc_global_number(n)  # raises on a malformed name

    def test_the_unpublished_pins_are_none_not_guessed(self):
        unknown = {p for p, n in ORANGEPI_ZERO_3W_PHYSICAL_TO_SOC.items() if n is None}
        assert unknown == UNPUBLISHED_PHYSICAL

    def test_buses_sit_where_the_pi_puts_them(self):
        # The reason the HAT seats unchanged: I2C on 3/5, SPI on 19/21/23/24,
        # UART on 8/10 — the vendor's function pins at the Pi's positions.
        t = ORANGEPI_ZERO_3W_PHYSICAL_TO_SOC
        assert (t[3], t[5]) == ("PB3", "PB2")
        assert (t[19], t[21], t[23], t[24]) == ("PE2", "PE3", "PE1", "PE0")
        assert (t[8], t[10]) == ("PB9", "PB10")
        assert t[7] == "PB4"  # DS18B20 / w1-gpio


class TestSocNumbering:
    @pytest.mark.parametrize(
        ("name", "number"),
        [("PA0", 0), ("PB4", 36), ("PD4", 100), ("PE0", 128), ("PH5", 229), ("PL2", 354)],
    )
    def test_round_trip(self, name, number):
        assert soc_global_number(name) == number
        assert soc_name_from_global(number) == name

    @pytest.mark.parametrize("bad", ["GPIO4", "P4", "PB", "PB32", "PZ1", ""])
    def test_rejects_non_allwinner_names(self, bad):
        with pytest.raises(ValueError):
            soc_global_number(bad)


class FakeLgpioNames:
    """lgpio with gpio-line-names populated on two chips."""

    def __init__(self, names_by_chip):
        self._names = names_by_chip
        self.closed = []

    def gpiochip_open(self, chip):
        if chip not in self._names:
            raise OSError("no such chip")
        return chip

    def gpio_get_chip_info(self, handle):
        return [len(self._names[handle]), f"gpiochip{handle}", "sunxi"]

    def gpio_get_line_info(self, handle, line):
        return [line, 0, self._names[handle][line], ""]

    def gpiochip_close(self, handle):
        self.closed.append(handle)


class TestResolveSocLine:
    def test_kernel_line_names_win(self, tmp_path):
        lg = FakeLgpioNames({0: ["PA0", "PA1", "PB4"], 1: ["PL0", "PL1", "PL2"]})
        root = _sysfs(tmp_path)
        assert resolve_soc_line("PB4", lgpio_module=lg, sysfs_root=root) == (0, 2)
        assert resolve_soc_line("PL2", lgpio_module=lg, sysfs_root=root) == (1, 2)
        assert lg.closed  # handles are released

    def test_sysfs_chip_table_when_lines_are_unnamed(self, tmp_path):
        lg = FakeLgpioNames({0: ["", "", ""]})
        root = _sysfs(tmp_path)
        assert resolve_soc_line("PB4", lgpio_module=lg, sysfs_root=root) == (0, 36)
        assert resolve_soc_line("PL2", lgpio_module=lg, sysfs_root=root) == (1, 2)

    def test_sysfs_respects_real_chip_bases(self, tmp_path):
        # A kernel that places the R_PIO chip somewhere other than 352: the
        # line offset follows the chip's real base, not the convention.
        root = _sysfs(tmp_path, chips=((0, 320), (320, 64)))
        assert resolve_soc_line("PL2", lgpio_module=None, sysfs_root=root) == (1, 34)

    def test_sunxi_convention_is_the_last_resort_and_says_so(self, tmp_path, caplog):
        with caplog.at_level(logging.WARNING):
            assert resolve_soc_line("PB4", lgpio_module=None, sysfs_root=str(tmp_path / "x")) == (
                0,
                36,
            )
            assert resolve_soc_line("PL3", lgpio_module=None, sysfs_root=str(tmp_path / "x")) == (
                1,
                3,
            )
        assert "unverified" in caplog.text


READALL = """
 +------+-----+----------+--------+---+  OPi Zero3W +---+--------+----------+-----+------+
 | GPIO | wPi |   Name   |  Mode  | V | Physical | V |  Mode  |   Name   | wPi | GPIO |
 +------+-----+----------+--------+---+----++----+---+--------+----------+-----+------+
 |      |     |     3.3V |        |   |  1 || 2  |   |        | 5V       |     |      |
 |   35 |   0 |    SDA.0 |    OFF | 0 |  3 || 4  |   |        | 5V       |     |      |
 |   34 |   1 |    SCL.0 |    OFF | 0 |  5 || 6  |   |        | GND      |     |      |
 |   36 |   2 |      PB4 |    OFF | 0 |  7 || 8  | 0 | OFF    | TXD.0    | 3   | 41   |
 |      |     |      GND |        |   |  9 || 10 | 0 | OFF    | RXD.0    | 4   | 42   |
 |   32 |   5 |      PB0 |    OFF | 0 | 11 || 12 | 0 | OFF    | PD5      | 6   | 101  |
 |   33 |   7 |      PB1 |    OFF | 0 | 13 || 14 |   |        | GND      |     |      |
 |  102 |   8 |      PD6 |    OFF | 0 | 15 || 16 | 0 | OFF    | PL2      | 9   | 354  |
 |      |     |     3.3V |        |   | 17 || 18 | 0 | OFF    | PL3      | 10  | 355  |
 |  130 |  11 |   MOSI.3 |    OFF | 0 | 19 || 20 |   |        | GND      |     |      |
 |  131 |  12 |   MISO.3 |    OFF | 0 | 21 || 22 | 0 | OFF    | PD7      | 13  | 103  |
 |  129 |  14 |   SCLK.3 |    OFF | 0 | 23 || 24 | 0 | OFF    | CE.3     | 15  | 128  |
 |      |     |      GND |        |   | 25 || 26 | 0 | OFF    | PE4      | 16  | 132  |
 +------+-----+----------+--------+---+----++----+---+--------+----------+-----+------+
"""


class TestGpioReadallParser:
    def test_trusts_the_gpio_number_columns_only(self):
        parsed = parse_gpio_readall(READALL)
        # Function aliases in the Name column ("SDA.0", "CE.3") are ignored;
        # the number says what the pin is.
        assert parsed[3] == "PB3" and parsed[5] == "PB2"
        assert parsed[24] == "PE0" and parsed[19] == "PE2"
        assert parsed[12] == "PD5" and parsed[15] == "PD6" and parsed[22] == "PD7"
        assert parsed[16] == "PL2" and parsed[18] == "PL3"
        # Power/ground rows carry no number and are not pins.
        assert 1 not in parsed and 9 not in parsed and 17 not in parsed

    def test_published_pins_agree_with_the_sample(self):
        parsed = parse_gpio_readall(READALL)
        for physical, name in ORANGEPI_ZERO_3W_PHYSICAL_TO_SOC.items():
            if name and physical in parsed:
                assert parsed[physical] == name, physical

    def test_garbage_parses_to_nothing(self):
        assert parse_gpio_readall("hello\n| not | a | table |\n") == {}


class TestOverrides:
    def test_fills_unpublished_pins(self):
        table, conflicts = merge_physical_overrides(ORANGEPI_ZERO_3W_PHYSICAL_TO_SOC, BENCH_PINS)
        assert conflicts == []
        assert table[12] == "PD5" and table[31] == "PE8"
        assert table[7] == "PB4"  # untouched

    def test_conflict_with_the_published_pinout_is_reported_and_the_board_wins(self):
        table, conflicts = merge_physical_overrides(ORANGEPI_ZERO_3W_PHYSICAL_TO_SOC, {7: "PB5"})
        assert table[7] == "PB5"
        assert conflicts == ["pin 7: published PB4, board reports PB5"]

    def test_malformed_and_non_header_keys_are_refused(self):
        table, conflicts = merge_physical_overrides(
            ORANGEPI_ZERO_3W_PHYSICAL_TO_SOC, {12: "GPIO18", 99: "PA0", "x": "PA1", 15: None}
        )
        assert table[12] is None and 99 not in table and table[15] is None
        assert any("not an Allwinner pin name" in c for c in conflicts)


class TestOrangePiPins:
    def test_published_pins_resolve_and_unpublished_refuse(self, tmp_path):
        pins = orangepi_zero_3w_pins(sysfs_root=_sysfs(tmp_path))
        assert pins.backend == "gpiochip" and pins.board_id == "orangepi-zero-3w"
        assert pins.i2c_bus == 0 and (pins.spi_bus, pins.spi_device) == (3, 0)
        assert pins.gps_port == "/dev/ttyS0" and pins.w1_pin == "PB4"
        assert pins.line(17) == (0, 32)  # relay 1 → pin 11 → PB0
        assert pins.line(23) == (1, 2)  # relay 4 → pin 16 → PL2 (R_PIO)
        assert pins.line(16) == (0, 98)  # LORA_DIO1 → pin 36 → PD2
        assert pins.line(26) == (0, 100)  # flow pulse → pin 37 → PD4
        with pytest.raises(UnresolvedPinError, match="pin 12.*host-pins.py"):
            pins.line(18)  # LORA_RST → pin 12, unpublished
        nets = {"relay_3": 22, "led_2": 25, "lora_rst": 18, "fan": 21}
        assert pins.unresolved(nets) == [
            ("relay_3", 22, 15),
            ("led_2", 25, 22),
            ("lora_rst", 18, 12),
        ]

    def test_bench_overrides_complete_the_map(self, tmp_path):
        pins = orangepi_zero_3w_pins(
            overrides={"pins": BENCH_PINS, "i2c_bus": 2, "gps_port": "/dev/ttyS2"},
            sysfs_root=_sysfs(tmp_path),
        )
        assert pins.source == "profile+overrides"
        assert pins.line(18) == (0, 101)  # PD5
        assert pins.line(22) == (0, 102) and pins.line(25) == (0, 103)
        assert pins.i2c_bus == 2 and pins.gps_port == "/dev/ttyS2"
        hat = {"relay_3": 22, "led_2": 25, "lora_rst": 18, "lora_busy": 20, "fan": 21}
        assert pins.unresolved(hat) == []

    def test_pins_for_board_reads_the_overrides_file(self, tmp_path):
        import yaml

        path = tmp_path / "host-pins.yaml"
        path.write_text(yaml.safe_dump({"pins": {12: "PD5"}, "spi_bus": 1}))
        pins = pins_for_board(
            "orangepi-zero-3w",
            overrides_path=str(path),
            lgpio_module=None,
            sysfs_root=_sysfs(tmp_path),
        )
        assert pins.line(18) == (0, 101)
        assert pins.spi_bus == 1
        assert pins_for_board("rpi-zero-2w").backend == "rpi"
        assert pins_for_board("arduino-uno-q").backend == "none"

    def test_unreadable_overrides_file_is_ignored_not_fatal(self, tmp_path):
        path = tmp_path / "host-pins.yaml"
        path.write_text(": : not yaml : [")
        pins = pins_for_board(
            "orangepi-zero-3w",
            overrides_path=str(path),
            lgpio_module=None,
            sysfs_root=_sysfs(tmp_path),
        )
        assert pins.source == "profile"

    def test_hostpins_is_frozen(self):
        pins = rpi_pins()
        with pytest.raises(AttributeError):
            pins.i2c_bus = 7  # type: ignore[misc]
        assert isinstance(pins, HostPins)
        assert SimpleNamespace  # keep the import honest for the fake above
