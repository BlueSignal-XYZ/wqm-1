"""diagnostics.sh --relays: the bench click test.

Runs the real script with stubbed system tools so the rules hold on any host:

1. The four pins it drives are RELAY_PINS, in order — a drifted list clicks the
   wrong coil and a technician signs off a relay that was never tested.
2. Each pin goes HIGH then LOW, and the GPIO is released.
3. The firmware service is stopped for the test and ALWAYS started again —
   including when a relay cannot be driven.
4. Without --relays no relay is touched.
"""

import os
import re
import subprocess
from pathlib import Path

from utils.config import RELAY_PINS

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "diagnostics.sh"

FAKE_GPIO = """
import os
BCM = "BCM"; OUT = "OUT"; HIGH = 1; LOW = 0
_log = os.environ["GPIO_LOG"]
def _w(line):
    with open(_log, "a") as f:
        f.write(line + "\\n")
def setmode(m): pass
def setwarnings(w): pass
def setup(pin, mode, initial=0):
    if os.environ.get("GPIO_FAIL_PIN") == str(pin):
        raise RuntimeError("GPIO busy")
    _w(f"setup {pin}")
def output(pin, v): _w(f"output {pin} {v}")
def cleanup(pin=None): _w(f"cleanup {pin}")
"""


def _run(tmp_path, args, extra_env=None):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    syslog = tmp_path / "systemctl.log"
    # systemctl: the service starts out active; every call is recorded.
    (bindir / "systemctl").write_text(
        "#!/bin/bash\n"
        f'echo "$@" >> "{syslog}"\n'
        'case "$1" in is-active|is-enabled) exit 0 ;; esac\n'
        "exit 0\n"
    )
    # Hide hardware tools so the other checks fail fast instead of probing.
    (bindir / "sleep").write_text("#!/bin/bash\nexit 0\n")
    for tool in ("systemctl", "sleep"):
        (bindir / tool).chmod(0o755)
    pkg = tmp_path / "py" / "RPi"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "GPIO.py").write_text(FAKE_GPIO)
    gpio_log = tmp_path / "gpio.log"
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "PYTHONPATH": str(tmp_path / "py"),
        "GPIO_LOG": str(gpio_log),
        **(extra_env or {}),
    }
    result = subprocess.run(
        ["bash", str(SCRIPT), *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    gpio = gpio_log.read_text().splitlines() if gpio_log.exists() else []
    calls = syslog.read_text().splitlines() if syslog.exists() else []
    return result, gpio, calls


def test_pin_list_matches_relay_pins():
    text = SCRIPT.read_text()
    m = re.search(r"RELAY_TEST_PINS=\(([\d ]+)\)", text)
    assert m, "RELAY_TEST_PINS missing from diagnostics.sh"
    assert tuple(int(p) for p in m.group(1).split()) == tuple(RELAY_PINS)


def test_relays_click_in_order_and_service_restarts(tmp_path):
    result, gpio, calls = _run(tmp_path, ["--relays"])
    expected = []
    for pin in RELAY_PINS:
        expected += [f"setup {pin}", f"output {pin} 1", f"output {pin} 0", f"cleanup {pin}"]
    assert gpio == expected, result.stdout + result.stderr
    stops = [i for i, c in enumerate(calls) if c.startswith("stop bluesignal-wqm")]
    starts = [i for i, c in enumerate(calls) if c.startswith("start bluesignal-wqm")]
    assert len(stops) == 1 and len(starts) == 1
    assert stops[0] < starts[0]
    for n in range(1, 5):
        assert f"Relay {n}: switched GPIO" in result.stdout


def test_service_restarts_even_when_a_relay_cannot_be_driven(tmp_path):
    result, gpio, calls = _run(tmp_path, ["--relays"], {"GPIO_FAIL_PIN": str(RELAY_PINS[1])})
    assert any(c.startswith("start bluesignal-wqm") for c in calls)
    assert f"Relay 2: could not drive GPIO {RELAY_PINS[1]}" in result.stdout
    # The other three still clicked.
    assert sum(1 for line in gpio if line.endswith(" 1")) == 3


def test_no_relay_touched_without_flag(tmp_path):
    result, gpio, calls = _run(tmp_path, [])
    assert gpio == []
    assert not any(c.startswith(("stop ", "start ")) for c in calls)


def test_unknown_option_is_refused(tmp_path):
    result, _, _ = _run(tmp_path, ["--relay"])
    assert result.returncode == 2


def test_identity_block_prints_device_id_and_dev_eui(tmp_path):
    """The bench must leave the first pass knowing what to type into Cloud.

    Uses the script's repo-relative fallback (no /opt install here), so the
    printed id comes from the same identity module the firmware posts under.
    """
    from utils.identity import get_dev_eui, get_device_id

    result, _, _ = _run(tmp_path, [])
    assert f"Device ID: {get_device_id()}" in result.stdout
    assert f"DevEUI:    {get_dev_eui().hex().upper()}" in result.stdout
    assert "Cloud key:" in result.stdout
