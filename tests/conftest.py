"""
Shared fixtures for firmware tests.

All hardware is mocked at the sys.modules level so tests run on any
platform without RPi.GPIO, smbus2, spidev, or w1thermsensor installed.
"""

import sys
from collections.abc import Callable
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Install mock hardware modules before any firmware code is imported.
# This runs at collection time, before tests execute.
# ---------------------------------------------------------------------------

# RPi.GPIO
_gpio = MagicMock()
_gpio.BCM = 11
_gpio.OUT = 0
_gpio.IN = 1
_gpio.HIGH = 1
_gpio.LOW = 0
_gpio.RISING = 31
_rpi = MagicMock()
_rpi.GPIO = _gpio
sys.modules["RPi"] = _rpi
sys.modules["RPi.GPIO"] = _gpio

# smbus2
_smbus2 = MagicMock()
_bus = MagicMock()
_bus.read_word_data.return_value = 0x8583
_bus.read_i2c_block_data.return_value = [0x85, 0x83]
_smbus2.SMBus.return_value = _bus
sys.modules["smbus2"] = _smbus2

# lgpio (sx1262 DIO1 edge detection; imported defensively since HW1)
_lgpio = MagicMock()
sys.modules["lgpio"] = _lgpio

# spidev
_spidev = MagicMock()
_spi = MagicMock()
_spi.xfer2.return_value = [0] * 10
_spidev.SpiDev.return_value = _spi
sys.modules["spidev"] = _spidev

# serial (pyserial)
_serial_mod = MagicMock()
_serial_inst = MagicMock()
_serial_inst.is_open = True
_serial_inst.readline.return_value = b""
_serial_mod.Serial.return_value = _serial_inst
_serial_mod.EIGHTBITS = 8
_serial_mod.PARITY_NONE = "N"
_serial_mod.STOPBITS_ONE = 1
sys.modules["serial"] = _serial_mod

# w1thermsensor
_w1 = MagicMock()
_w1_sensor = MagicMock()
_w1_sensor.id = "28-0000abcdef"
_w1_sensor.get_temperature.return_value = 22.5
_w1.W1ThermSensor.return_value = _w1_sensor
_w1.NoSensorFoundError = type("NoSensorFoundError", (Exception,), {})
sys.modules["w1thermsensor"] = _w1


@pytest.fixture(autouse=True)
def _reset_active_board():
    """A test that pins a board profile must not leak it into the next one."""
    yield
    try:
        from platform_support import set_active_board

        set_active_board(None)
    except Exception:  # noqa: BLE001 — platform_support may be unimportable mid-refactor
        pass


@pytest.fixture(autouse=True)
def _no_real_bluesignal_probe(monkeypatch, tmp_path):
    """A join proves itself by reaching BlueSignal over HTTPS (install plan
    §2). No test may make that request: BlueSignal answers, the clock is
    synced, and no HTTP Date is read, unless a test says otherwise."""
    try:
        from utils import netctl
    except Exception:  # noqa: BLE001 — netctl may be unimportable mid-refactor
        yield
        return
    monkeypatch.setattr(netctl, "_https_status", lambda url, timeout=None: "ok")
    monkeypatch.setattr(netctl, "_http_date", lambda url, timeout=None: None)
    monkeypatch.setattr(netctl, "_ntp_synced", lambda: True)
    # The 1.5 s head start lets a real phone get the HTTP answer before the
    # setup network drops. Tests run the join at once; test_join pins it.
    from utils import join

    monkeypatch.setattr(join, "START_DELAY_S", 0.0)
    # One clock step per boot is recorded under /run; never the real one.
    monkeypatch.setenv("WQM1_CLOCK_STEP_MARKER", str(tmp_path / "clock-stepped"))
    yield


@pytest.fixture(autouse=True)
def mock_hardware():
    """Provide access to mock hardware objects and reset between tests."""
    _gpio.reset_mock()
    # reset_mock() leaves side_effect in place, so a test that makes a pin
    # write fail would otherwise leak that failure into every later test.
    _gpio.output.side_effect = None
    _smbus2.reset_mock()
    _bus.reset_mock()
    _smbus2.SMBus.side_effect = None
    _smbus2.SMBus.return_value = _bus
    _bus.read_word_data.side_effect = None
    _bus.read_word_data.return_value = 0x8583
    _bus.read_i2c_block_data.return_value = [0x85, 0x83]
    _bus.read_i2c_block_data.side_effect = None
    _spidev.reset_mock()
    _spi.reset_mock()
    _spi.xfer2.return_value = [0] * 10
    _spidev.SpiDev.return_value = _spi
    _serial_mod.reset_mock()
    _serial_inst.reset_mock()
    _serial_inst.is_open = True
    _serial_inst.readline.return_value = b""
    _serial_mod.Serial.side_effect = None
    _serial_mod.Serial.return_value = _serial_inst
    _serial_mod.EIGHTBITS = 8
    _serial_mod.PARITY_NONE = "N"
    _serial_mod.STOPBITS_ONE = 1

    yield {
        "gpio": _gpio,
        "smbus2": _smbus2,
        "bus": _bus,
        "spidev": _spidev,
        "spi": _spi,
        "serial_mod": _serial_mod,
        "serial": _serial_inst,
        "w1": _w1,
        "w1_sensor": _w1_sensor,
    }


@pytest.fixture
def fixed_clock():
    """
    Factory for a frozen UTC clock to inject as ``RulesEngine(clock=...)``.

    Schedule windows and hourly on-time budgets are evaluated against this
    instead of wall time, so a test asserting "inside the window" cannot be
    falsified by the minute it happens to run at.
    """

    def _make(hour: int, minute: int = 0) -> Callable[[], datetime]:
        moment = datetime(2026, 1, 1, hour, minute, tzinfo=UTC)
        return lambda: moment

    return _make


@pytest.fixture(autouse=True)
def no_outbound_http(monkeypatch):
    """
    Nothing in the suite may talk to the real cloud.

    The setup wizard's cloud step verifies a pasted key by calling the live
    API. Without this fixture, `pytest` fires real requests at production
    Cloud Functions — slow, flaky offline, and noise in prod logs from CI.
    Tests that exercise the probe monkeypatch `urlopen` themselves, which
    takes precedence over this default.
    """
    import utils.netinfo as netinfo

    def blocked(*_a, **_k):
        raise netinfo.urllib.error.URLError("outbound HTTP blocked in tests")

    monkeypatch.setattr(netinfo.urllib.request, "urlopen", blocked)
    import urllib.request as urllib_request

    monkeypatch.setattr(urllib_request, "urlopen", blocked)


@pytest.fixture(autouse=True)
def _no_real_network_mutation(monkeypatch, tmp_path):
    """No test may reconfigure the machine running it.

    The setup wizard now forgets saved Wi-Fi profiles when it finishes and the
    AP fallback installs a NAT rule (site flow v2). On a developer laptop with
    NetworkManager, an unpatched test walking the wizard would delete that
    laptop's saved networks. Every ``utils.netctl`` subprocess answers
    "binary not found" unless a test installs its own fake (test_netctl.py
    does, and a later setattr wins). The virtual radio is cleared too, so a
    shell that exported WQM1_VIRTUAL_NET cannot change what a test sees.
    """
    from utils import netctl

    monkeypatch.setattr(netctl, "_run", lambda argv, timeout=15.0: (127, "", "blocked in tests"))
    # A successful join asks whether a sign-in page ate the internet. That
    # probe is a real HTTP GET. The suite must not make it.
    monkeypatch.setattr(netctl, "probe_sign_in_page", lambda *a, **k: "unknown")
    monkeypatch.delenv("WQM1_VIRTUAL_NET", raising=False)
    # hostapd.conf holds the setup passphrase. Tests must not write it under /run.
    monkeypatch.setenv("WQM1_HOSTAPD_CONF", str(tmp_path / "hostapd.conf"))
    monkeypatch.setenv("WQM1_DNSMASQ_CONF", str(tmp_path / "dnsmasq-ap.conf"))
    monkeypatch.setenv("BLUESIGNAL_STATE_DIR", str(tmp_path / "var-lib-bluesignal"))
    monkeypatch.delenv("BLUESIGNAL_ROOT", raising=False)
    try:
        from service_window import captive

        captive.reset_cache()
    except ImportError:
        pass


@pytest.fixture(autouse=True)
def _no_real_status_files(monkeypatch, tmp_path):
    """No test may read or write the unit's real status or card files.

    ``wqm1-status.txt`` lives at fixed paths (``/var/lib/bluesignal`` and the
    boot partition). Run as root — a bench Pi, a container — the netctl and
    AP-layout tests wrote those real files, and a later setup-wizard test
    read the leftover ``onewire`` line as a missing probe and refused Finish.
    On a non-root CI runner the write fails quietly, so the leak only shows
    where it does the most harm. A test that needs its own paths patches
    them again; the later setattr wins.
    """
    from diagnostics import ap_fail, boot_status

    status_dir = tmp_path / "status-files"
    monkeypatch.setattr(boot_status, "_STATE_PATH", status_dir / "var-lib" / "wqm1-status.txt")
    boot_paths = (
        status_dir / "boot-firmware" / "wqm1-status.txt",
        status_dir / "boot" / "wqm1-status.txt",
    )
    monkeypatch.setattr(boot_status, "_BOOT_PATHS", boot_paths)
    monkeypatch.setattr(
        ap_fail,
        "_BOOT_PATHS",
        (status_dir / "boot-firmware" / "wqm1-apfail.txt", status_dir / "boot" / "wqm1-apfail.txt"),
    )
    monkeypatch.delenv("WQM1_STATUS_FILE", raising=False)
    monkeypatch.setenv("WQM1_CARD_STATE", str(status_dir / "card-binding"))
    # The writer falls back to `sudo -n wqm1-publish-status.sh` when no boot
    # path is writable. Point it at a script that does not exist.
    monkeypatch.setenv("WQM1_STATUS_PUBLISH", str(status_dir / "no-publish-helper"))


@pytest.fixture(autouse=True)
def _join_register_kicks(monkeypatch):
    """Wait for background self-register attempts before the env is restored.

    The setup wizard starts a ``wqm1-register-kick`` daemon thread after a
    join. If it outlives its test, it runs after monkeypatch has put
    BLUESIGNAL_STATE_DIR back, and writes ``device-register.json`` into the
    real ``/var/lib/bluesignal`` (as root) — intermittently, depending on
    timing. This fixture depends on ``monkeypatch`` so it tears down first.
    """
    import threading

    before = set(threading.enumerate())
    yield
    for thread in threading.enumerate():
        if thread.name in {"wqm1-register-kick", "wqm1-wifi-join"} and thread not in before:
            thread.join(timeout=5)
