"""The setup AP must find its units on an installed image, not only in a checkout.

Bench image b881e8a installed wqm1-setup-hostapd.service into
/etc/systemd/system and never copied systemd/ into the release tree.
``ensure_ap_units`` only looked next to the source file, reported the
unit missing, and hostapd was never started. These tests fail on that
layout and pass once the installed units and the release copy both count.
"""

from __future__ import annotations

import ast
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

_PROBE = """
import sys
from pathlib import Path

release = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(release / "src"))
import utils.netctl as netctl

loaded = Path(netctl.__file__).resolve()
if not loaded.is_relative_to(release):
    raise SystemExit(f"imported {loaded}, not the installed tree")
missing = []
for name in sys.argv[2:]:
    found = netctl._packaged_unit(name)
    if found is None or not found.is_file():
        missing.append(name)
        continue
    if not found.resolve().is_relative_to(release):
        missing.append(f"{name} -> {found}")
if missing:
    raise SystemExit("unresolved: " + ", ".join(missing))
print("found", len(sys.argv) - 2)
"""


def _service_constants(tree: ast.AST) -> dict[str, str]:
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
            value = node.value
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            continue
        if not value.value.endswith(".service"):
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                found[target.id] = value.value
    return found


def _segments(node: ast.AST) -> list[ast.AST]:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return _segments(node.left) + _segments(node.right)
    return [node]


def _segment_text(node: ast.AST, constants: dict[str, str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    if isinstance(node, ast.Call):
        for arg in node.args:
            text = _segment_text(arg, constants)
            if text and ("systemd" in text or text.endswith(".service")):
                return text
    return None


def _is_release_systemd_dir(text: str) -> bool:
    if "/etc/systemd" in text:
        return False
    return text == "systemd" or text.endswith("/systemd")


def _arg_names(fn: ast.FunctionDef) -> list[str]:
    return [arg.arg for arg in fn.args.args]


def _loop_tuple_services(fn: ast.FunctionDef, varname: str, constants: dict[str, str]) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(fn):
        if not isinstance(node, ast.For) or not isinstance(node.target, ast.Name):
            continue
        if node.target.id != varname:
            continue
        elts: list[ast.expr] = []
        if isinstance(node.iter, ast.Tuple | ast.List | ast.Set):
            elts = list(node.iter.elts)
        for elt in elts:
            if isinstance(elt, ast.Name) and elt.id in constants:
                found.add(constants[elt.id])
            elif (
                isinstance(elt, ast.Constant)
                and isinstance(elt.value, str)
                and elt.value.endswith(".service")
            ):
                found.add(elt.value)
    return found


def _resolve_service_expr(
    expr: ast.AST, constants: dict[str, str], fn: ast.FunctionDef | None
) -> set[str]:
    if (
        isinstance(expr, ast.Constant)
        and isinstance(expr.value, str)
        and expr.value.endswith(".service")
    ):
        return {expr.value}
    if isinstance(expr, ast.Name) and expr.id in constants:
        return {constants[expr.id]}
    if isinstance(expr, ast.Name) and fn is not None:
        return _loop_tuple_services(fn, expr.id, constants)
    return set()


def _enclosing_function(parents: dict[ast.AST, ast.AST], node: ast.AST) -> ast.FunctionDef | None:
    current: ast.AST | None = node
    while current is not None:
        current = parents.get(current)
        if isinstance(current, ast.FunctionDef):
            return current
    return None


def release_unit_names(src: Path) -> set[str]:
    """Unit filenames runtime code resolves under the release's systemd/ directory."""
    found: set[str] = set()
    for path in src.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        constants = _service_constants(tree)
        parents: dict[ast.AST, ast.AST] = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node
        functions = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)]
        lookup_params: dict[str, set[str]] = {}
        for fn in functions:
            params = set(_arg_names(fn))
            used: set[str] = set()
            for node in ast.walk(fn):
                if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Div):
                    continue
                segs = _segments(node)
                if len(segs) < 2:
                    continue
                directory = any(
                    _is_release_systemd_dir(_segment_text(seg, constants) or "")
                    for seg in segs[:-1]
                )
                if not directory:
                    continue
                last = segs[-1]
                text = _segment_text(last, constants)
                if text and text.endswith(".service"):
                    found.add(text)
                if isinstance(last, ast.Name) and last.id in params:
                    used.add(last.id)
            if used:
                lookup_params[fn.name] = used
        by_name = {fn.name: fn for fn in functions}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            params = lookup_params.get(node.func.id)
            if not params:
                continue
            fn = by_name[node.func.id]
            enclosing = _enclosing_function(parents, node)
            for param, arg in zip(_arg_names(fn), node.args, strict=False):
                if param not in params:
                    continue
                found.update(_resolve_service_expr(arg, constants, enclosing))
    return found


def _compound_closer(stripped: str) -> str | None:
    head = stripped.split(" ", 1)[0] if stripped else ""
    if head in {"for", "while", "until"}:
        return "done"
    if head == "if":
        return "fi"
    return None


_SHELL_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")
_FOR_NAME = re.compile(r"^for\s+([A-Za-z_][A-Za-z0-9_]*)\s+in\b")


def _shell_vars(line: str) -> set[str]:
    return {braced or bare for braced, bare in _SHELL_VAR.findall(line)}


def _line_is_release_local(stripped: str) -> bool:
    """True when a setup.sh line cannot write outside the release tree."""
    if not stripped or stripped.startswith("#"):
        return True
    forbidden = ("/etc", "/var", "/boot", "/usr", "/opt", "sudoers")
    if any(token in stripped for token in forbidden):
        return False
    head = stripped.split(" ", 1)[0]
    if head in {"do", "then", "else", "elif", "fi", "done", "for", "if", "while", "until"}:
        return True
    if "$RELEASE_DIR" in stripped or "$SCRIPT_DIR" in stripped:
        return True
    return head == "echo"


def _take_compound(lines: list[str], start: int) -> tuple[list[str], int]:
    block = [lines[start]]
    stack = [_compound_closer(lines[start].strip())]
    index = start + 1
    while index < len(lines) and stack:
        stripped = lines[index].strip()
        opened = _compound_closer(stripped)
        head = stripped.split(" ", 1)[0] if stripped else ""
        if opened:
            stack.append(opened)
        elif stack[-1] is not None and head == stack[-1]:
            stack.pop()
        block.append(lines[index])
        index += 1
    return block, index


def _block_only_fills_the_release(block: list[str]) -> bool:
    """Keep a compound statement only when every path it writes is the release."""
    allowed = {"SCRIPT_DIR", "RELEASE_DIR"}
    for line in block:
        match = _FOR_NAME.match(line.strip())
        if match:
            allowed.add(match.group(1))
    if not any("$RELEASE_DIR" in line for line in block):
        return False
    for line in block:
        stripped = line.strip()
        if not _line_is_release_local(stripped):
            return False
        if not _shell_vars(stripped) <= allowed:
            return False
    return True


def extract_release_copies(setup_text: str) -> str:
    """The setup.sh statements that fill RELEASE_DIR, and nothing else."""
    lines = setup_text.splitlines()
    kept: list[str] = []
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if _compound_closer(stripped):
            block, index = _take_compound(lines, index)
            if _block_only_fills_the_release(block):
                kept.extend(block)
            continue
        if (
            "$RELEASE_DIR" in stripped
            and _line_is_release_local(stripped)
            and _shell_vars(stripped) <= {"SCRIPT_DIR", "RELEASE_DIR"}
        ):
            kept.append(lines[index])
        index += 1
    return "\n".join(kept) + "\n"


def materialize_release(repo: Path, release: Path) -> str:
    """Apply exactly the RELEASE_DIR copies from setup.sh."""
    script = extract_release_copies((repo / "setup.sh").read_text(encoding="utf-8"))
    release.mkdir(parents=True, exist_ok=True)
    full = "\n".join(
        [
            "set -euo pipefail",
            'sudo() { "$@"; }',
            f"SCRIPT_DIR={shlex.quote(str(repo))}",
            f"RELEASE_DIR={shlex.quote(str(release))}",
            script,
        ]
    )
    result = subprocess.run(
        ["bash", "-s"],
        input=full,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"{full}\n--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
        )
    return script


def test_ensure_ap_units_accepts_installed_units(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from utils import netctl

    monkeypatch.setattr(netctl.os, "geteuid", lambda: 0)
    monkeypatch.setattr(netctl, "_packaged_unit", lambda _name: None)
    monkeypatch.setattr(netctl, "SYSTEMD_DIR", tmp_path)
    calls: list[tuple[str, ...]] = []

    def _systemctl(*args: str, timeout: float = 30.0) -> tuple[int, str, str]:
        del timeout
        calls.append(args)
        return 0, "", ""

    monkeypatch.setattr(netctl, "_systemctl", _systemctl)
    for name in (netctl.HOSTAPD_UNIT, netctl.DNSMASQ_UNIT):
        (tmp_path / name).write_text("[Unit]\nDescription=installed\n", encoding="utf-8")

    installed = netctl.ensure_ap_units()
    assert installed["ok"] is True
    assert calls == []

    for path in tmp_path.iterdir():
        path.unlink()
    missing = netctl.ensure_ap_units()
    assert missing["ok"] is False
    assert "missing" in str(missing["error"])
    assert netctl.HOSTAPD_UNIT in str(missing["error"])


def test_installed_layout_finds_ap_units(tmp_path: Path) -> None:
    units = sorted(release_unit_names(ROOT / "src"))
    assert "wqm1-setup-hostapd.service" in units
    assert "wqm1-setup-dnsmasq.service" in units
    release = tmp_path / "opt" / "bluesignal" / "releases" / "X"
    script = materialize_release(ROOT, release)
    assert 'cp -r "$SCRIPT_DIR/src"' in script or "cp -r" in script
    assert (release / "src" / "utils" / "netctl.py").is_file()
    probe = tmp_path / "probe_units.py"
    probe.write_text(_PROBE, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(probe), str(release), *units],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "found" in result.stdout


_LAYOUT_PROBE = """
import os
import sys
from pathlib import Path

current = Path(sys.argv[1])
release = Path(sys.argv[2]).resolve()
sys.path.insert(0, str(current / "src"))
os.environ["WQM1_CARD_STATE"] = sys.argv[3]
os.environ["WQM1_STATUS_FILE"] = sys.argv[4]

import utils.cardmove as cardmove
import utils.netctl as netctl
from diagnostics.boot_status import write_boot_status

loaded = Path(netctl.__file__).resolve()
if not loaded.is_relative_to(release):
    raise SystemExit(f"imported {loaded}, not the release behind current")
unit = netctl._packaged_unit("wqm1-setup-hostapd.service")
if unit is None or not unit.resolve().is_relative_to(release):
    raise SystemExit(f"setup AP unit resolved to {unit}")

first = cardmove.observe("1000000086544fd8", site_wifi=False)
if first.commissioned_serial != "1000000086544fd8" or first.moved:
    raise SystemExit(f"first boot was not recorded: {first}")
moved = cardmove.observe("00000000aabbccdd", site_wifi=True)
if not moved.wifi_reconfirm or moved.moved_from != "1000000086544fd8":
    raise SystemExit(f"move was not latched: {moved}")

text = write_boot_status(
    hotspot="up",
    portal="up (80)",
    adc="ok",
    fatal="none",
    wlan0="up",
    station="",
    power="undervoltage seen (1)",
    board="Raspberry Pi Zero W",
    revision="9000c1",
    kernel="test",
    wifi="BCM43430/1",
    i2c="ok",
    onewire="not fitted",
    gps="absent",
    uptime="10",
)
if not text.startswith("COMMISSION: FAIL: weak power supply or cable\\n"):
    raise SystemExit(text.splitlines()[0])
if "card: moved from 1000000086544fd8 to 00000000aabbccdd" not in text:
    raise SystemExit("card line missing")
print("layout-ok")
"""


def test_resilience_runs_from_the_current_symlink(tmp_path: Path) -> None:
    """Code under releases/<ver> reached only through the current symlink.

    A host-side path that follows the absolute symlink leaves the image.
    This imports the installed tree the way a unit does.
    """
    release = tmp_path / "opt" / "bluesignal" / "releases" / "2.4.0"
    materialize_release(ROOT, release)
    current = tmp_path / "opt" / "bluesignal" / "current"
    current.symlink_to(release)
    binding = tmp_path / "card-binding"
    status = tmp_path / "wqm1-status.txt"
    probe = tmp_path / "probe_layout.py"
    probe.write_text(_LAYOUT_PROBE, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(probe), str(current), str(release), str(binding), str(status)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "layout-ok" in result.stdout
    assert "weak power supply or cable" in status.read_text(encoding="utf-8")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (
        ("hostname", "#!/bin/sh\nprintf '%s\\n' 'raspberrypi'\n"),
        ("hostnamectl", "#!/bin/sh\nexit 0\n"),
        ("systemctl", "#!/bin/sh\nexit 0\n"),
        ("rfkill", "#!/bin/sh\nexit 0\n"),
        ("iw", "#!/bin/sh\nexit 0\n"),
        ("nmcli", "#!/bin/sh\nexit 0\n"),
    ):
        path = bin_dir / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
    cpu = tmp_path / "cpuinfo"
    cpu.write_text("Serial\t\t: 1000000086544fd8\n", encoding="utf-8")
    boot_status = tmp_path / "boot-status.txt"
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "WQM1_FIRMWARE_SRC": str(current / "src"),
        "WQM1_HOSTS_FILE": str(tmp_path / "hosts"),
        "WQM1_HOSTNAME_FILE": str(tmp_path / "hostname-file"),
        "WQM1_CPUINFO": str(cpu),
        "WQM1_STATUS_FILE": str(boot_status),
        "WQM1_FIRSTBOOT_STATE": str(tmp_path / "firstboot-state"),
        "WQM1_CARD_STATE": str(tmp_path / "firstboot-card"),
    }
    (tmp_path / "hosts").write_text("127.0.1.1\traspberrypi\n", encoding="utf-8")
    script = ROOT / "tools" / "golden" / "overlay" / "usr" / "local" / "sbin" / "wqm1-firstboot"
    boot = subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert boot.returncode == 0, boot.stderr
    assert "firmware id unavailable" not in boot.stdout
    assert "BOOT: stage=done" in boot_status.read_text(encoding="utf-8")


def test_workflows_pack_systemd_into_the_ota_bundle() -> None:
    members = "src config scripts systemd requirements.txt requirements-rpi.txt VERSION setup.sh"
    for name in ("ci.yml", "release.yml"):
        text = (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
        assert members in text, name


def test_status_and_apfail_writes_fsync(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    from diagnostics.ap_fail import write_clue
    from diagnostics.boot_status import write_boot_status

    events: list[str] = []

    def _fsync(_fd: int) -> None:
        events.append("fsync")

    def _sync() -> None:
        events.append("sync")

    monkeypatch.setattr(os, "fsync", _fsync)
    monkeypatch.setattr(os, "sync", _sync)
    status = tmp_path / "wqm1-status.txt"
    monkeypatch.setenv("WQM1_STATUS_FILE", str(status))
    write_boot_status(
        hotspot="starting",
        ssid="WQM1-TEST",
        portal="down: starting",
        board="bench",
        i2c="none",
        wlan0="down",
        station="",
        adc="ok",
        adc_error="",
        onewire="absent",
        gps="absent",
        wifi="unknown",
        power="ok",
        kernel="test",
        revision="test",
        uptime="1s",
        faulted=[],
        fatal="none",
    )
    assert events.count("fsync") >= 2
    assert status.is_file()
    assert "hotspot: starting" in status.read_text(encoding="utf-8")

    events.clear()
    clue = tmp_path / "wqm1-apfail.txt"
    assert write_clue(clue, "hostapd did not start\n") is True
    assert events.count("fsync") >= 2
    assert "sync" in events
    assert clue.read_text(encoding="utf-8") == "hostapd did not start\n"

    publish = (ROOT / "scripts" / "wqm1-publish-status.sh").read_text(encoding="utf-8")
    stripped = [
        line.strip()
        for line in publish.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert stripped[stripped.index('mv "$tmp" "$dest"') + 1] == "sync"


def test_apfail_strips_passphrases(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    from diagnostics import ap_fail

    raw = (
        "wpa_passphrase=river-cedar-42\n"
        "Error: psk river-cedar-42 rejected\n"
        "passphrase=river-cedar-42\n"
    )
    text = ap_fail.strip_passphrases(raw, ["river-cedar-42"])
    assert "river-cedar-42" not in text
    assert "wpa_passphrase=[redacted]" in text
    assert "psk [redacted] rejected" in text

    monkeypatch.setattr(os, "sync", lambda: None)
    dest = tmp_path / "wqm1-apfail.txt"
    monkeypatch.setenv("WQM1_APFAIL_FILE", str(dest))
    monkeypatch.setattr(ap_fail, "journal_tail", lambda: raw)
    monkeypatch.setattr(ap_fail, "known_passphrases", lambda: ["river-cedar-42"])
    assert ap_fail.main() == 0
    written = dest.read_text(encoding="utf-8")
    assert "river-cedar-42" not in written
    assert "wpa_passphrase=[redacted]" in written


def test_known_passphrases_come_from_identity_and_hostapd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from diagnostics import ap_fail

    monkeypatch.setattr(ap_fail, "ap_credentials", lambda: ("WQM1-TEST", "river-cedar-42"))
    conf = tmp_path / "hostapd.conf"
    conf.write_text("ssid=WQM1-TEST\nwpa_passphrase=other-secret-99\n", encoding="utf-8")
    monkeypatch.setenv("WQM1_HOSTAPD_CONF", str(conf))
    assert ap_fail.known_passphrases() == ["river-cedar-42", "other-secret-99"]

    def _unavailable() -> tuple[str, str]:
        raise OSError("identity unreadable")

    monkeypatch.setattr(ap_fail, "ap_credentials", _unavailable)
    monkeypatch.setenv("WQM1_HOSTAPD_CONF", str(tmp_path / "missing.conf"))
    assert ap_fail.known_passphrases() == []


def test_apfail_write_refuses_a_missing_directory(tmp_path: Path) -> None:
    from diagnostics.ap_fail import write_clue

    assert write_clue(tmp_path / "absent" / "wqm1-apfail.txt", "clue\n") is False


def test_apfail_keeps_the_last_40_journal_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    from diagnostics import ap_fail

    blob = "".join(f"line {index}\n" for index in range(100))

    class _Done:
        returncode = 0
        stdout = blob
        stderr = ""

    monkeypatch.setattr(ap_fail.shutil, "which", lambda _name: "/bin/journalctl")
    monkeypatch.setattr(ap_fail.subprocess, "run", lambda *_a, **_k: _Done())
    text = ap_fail.journal_tail()
    lines = text.splitlines()
    assert len(lines) == 40
    assert lines[0] == "line 60"
    assert lines[-1] == "line 99"


def test_ap_fallback_writes_hotspot_starting_before_the_radio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib.util

    from utils import netctl

    spec = importlib.util.spec_from_file_location(
        "ap_fallback_starting", ROOT / "scripts" / "wqm1-ap-fallback.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    status = tmp_path / "wqm1-status.txt"
    monkeypatch.setenv("WQM1_STATUS_FILE", str(status))
    seen: dict[str, str] = {}

    def _stop(*_args: object, **_kwargs: object) -> dict[str, str]:
        seen["text"] = status.read_text(encoding="utf-8")
        return {"mode": "station", "ip": "192.168.4.20"}

    monkeypatch.setattr(netctl, "ensure_reachable", _stop)
    assert mod.main() == 0
    assert "hotspot: starting" in seen["text"]
    assert "hotspot: station" in status.read_text(encoding="utf-8")


def test_ap_fallback_onfailure_unit_writes_the_boot_clue() -> None:
    unit = (ROOT / "systemd" / "bluesignal-ap-fallback.service").read_text(encoding="utf-8")
    directives = "\n".join(
        line for line in unit.splitlines() if line and not line.lstrip().startswith("#")
    )
    assert "OnFailure=wqm1-apfail.service" in directives
    fail = (ROOT / "systemd" / "wqm1-apfail.service").read_text(encoding="utf-8")
    assert "/opt/bluesignal/current/scripts/wqm1-apfail.py" in fail
    clue = (ROOT / "src" / "diagnostics" / "ap_fail.py").read_text(encoding="utf-8")
    assert "wqm1-apfail.txt" in clue
    exe = shutil.which("systemd-analyze")
    if not exe:
        return
    result = subprocess.run(
        [
            exe,
            "verify",
            str(ROOT / "systemd" / "bluesignal-ap-fallback.service"),
            str(ROOT / "systemd" / "wqm1-apfail.service"),
            str(ROOT / "systemd" / "wqm1-setup-hostapd.service"),
            str(ROOT / "systemd" / "wqm1-setup-dnsmasq.service"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    combined = result.stdout + result.stderr
    assert "Unknown key" not in combined
    assert "StartLimitIntervalSec" not in combined


def test_journal_tail_reports_a_missing_journalctl(monkeypatch: pytest.MonkeyPatch) -> None:
    from diagnostics import ap_fail

    monkeypatch.setattr(ap_fail.shutil, "which", lambda _name: None)
    assert ap_fail.journal_tail() == "journalctl not found\n"
