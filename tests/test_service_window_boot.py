"""Service Window first-boot: no firmware wait, shipped bytecode, unit verify.

A phone on WQM1-xxxx was waiting 2–3 minutes on a Pi Zero W because
bluesignal-service-window.service ordered After= bluesignal-wqm.service
(Type=notify, READY=1 after workers start) and the tree had no .pyc.
"""

from __future__ import annotations

import configparser
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from ota.agent import validate_tar_members

ROOT = Path(__file__).resolve().parent.parent

SYSTEMD = ROOT / "systemd"
GOLDEN_UNIT = (
    ROOT / "tools" / "golden" / "overlay" / "etc" / "systemd" / "system" / "wqm1-firstboot.service"
)
PACKAGER = ROOT / "scripts" / "build-ota-bundle.sh"
_DEP_KEYS = ("After", "Requires", "Wants", "BindsTo", "PartOf", "Requisite")


def _directive_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line and not line.lstrip().startswith("#")]


def _directives(text: str) -> str:
    return "\n".join(_directive_lines(text))


def _dep_values(text: str, key: str) -> set[str]:
    found: set[str] = set()
    prefix = f"{key}="
    for line in _directive_lines(text):
        if line.startswith(prefix):
            found.update(line.split("=", 1)[1].split())
    return found


def _install_units(dest: Path, install_user: str = "installer") -> Path:
    """Rewrite units the way setup.sh installs them under /etc/systemd/system."""
    etc = dest / "etc" / "systemd" / "system"
    etc.mkdir(parents=True)
    for unit in sorted(SYSTEMD.glob("*.service")):
        text = unit.read_text(encoding="utf-8")
        text = re.sub(r"^User=pi$", f"User={install_user}", text, flags=re.M)
        text = text.replace("/opt/bluesignal/src", "/opt/bluesignal/current/src")
        (etc / unit.name).write_text(text, encoding="utf-8")
    return etc


def _parse_unit(path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    parser.read(path)
    return parser


def test_installed_service_window_does_not_order_after_firmware(tmp_path: Path) -> None:
    """Installed-image layout: /opt/bluesignal/releases/<ver> + current, units in /etc."""
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    release = tmp_path / "opt" / "bluesignal" / "releases" / version
    release.mkdir(parents=True)
    (release / "VERSION").write_text(version + "\n", encoding="utf-8")
    app = release / "src" / "service_window" / "app.py"
    app.parent.mkdir(parents=True)
    app.write_text("# stub\n", encoding="utf-8")
    current = tmp_path / "opt" / "bluesignal" / "current"
    current.symlink_to(release)
    assert current.resolve() == release.resolve()
    assert (current / "src" / "service_window" / "app.py").is_file()

    etc = _install_units(tmp_path)
    window = (etc / "bluesignal-service-window.service").read_text(encoding="utf-8")

    assert "/opt/bluesignal/current/src" in window
    assert "User=installer" in window
    assert "User=pi" not in _directives(window)
    assert "bluesignal-wqm" not in _directives(window)
    for key in _DEP_KEYS:
        assert "bluesignal-wqm.service" not in _dep_values(window, key), key
    assert "network.target" in _dep_values(window, "After")
    assert "hostapd" not in _directives(window).lower()

    parsed_fw = _parse_unit(etc / "bluesignal-wqm.service")
    assert parsed_fw.get("Service", "Type") == "notify"
    assert parsed_fw.get("Service", "Nice") == "10"
    assert parsed_fw.get("Service", "StartupCPUWeight") == "20"
    parsed_sw = _parse_unit(etc / "bluesignal-service-window.service")
    assert parsed_sw.get("Service", "CPUWeight") == "200"
    assert parsed_fw.get("Unit", "After") == "network.target"


def test_repo_service_window_unit_does_not_wait_on_firmware() -> None:
    text = (SYSTEMD / "bluesignal-service-window.service").read_text(encoding="utf-8")
    assert "bluesignal-wqm" not in _directives(text)
    assert "network.target" in _dep_values(text, "After")
    assert "Requires=" not in _directives(text)


def test_all_units_systemd_analyze_verify() -> None:
    exe = shutil.which("systemd-analyze")
    if not exe:
        pytest.fail("systemd-analyze is required to verify unit files")
    units = sorted(SYSTEMD.glob("*.service"))
    assert units
    args = [exe, "verify", *[str(p) for p in units], str(GOLDEN_UNIT)]
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    combined = result.stdout + result.stderr
    assert "Unknown key" not in combined, combined
    # Missing sibling units (NetworkManager.service) are expected off-image.
    assert "Failed to parse" not in combined, combined


def test_ota_bundle_contains_service_window_pyc_and_current_layout(tmp_path: Path) -> None:
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    bundle = tmp_path / f"wqm1-firmware-{version}.tar.gz"
    packed = subprocess.run(
        ["bash", str(PACKAGER), str(bundle)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert packed.returncode == 0, packed.stdout + packed.stderr
    assert bundle.is_file()
    assert validate_tar_members(bundle) is None

    with tarfile.open(bundle, "r:gz") as tar:
        names = tar.getnames()
        tar.extractall(tmp_path / "staging", filter="data")

    setup_pyc = [
        name for name in names if name.startswith("src/service_window/") and name.endswith(".pyc")
    ]
    tag = f"cpython-{sys.version_info.major}{sys.version_info.minor}"
    assert any("/app.cpython-" in name for name in setup_pyc), setup_pyc[:12]
    assert any(tag in name and name.endswith(".pyc") for name in names)
    assert "src/service_window/app.py" in names
    assert "systemd/bluesignal-service-window.service" in names
    assert "VERSION" in names

    root = tmp_path / "opt" / "bluesignal"
    release = root / "releases" / version
    release.mkdir(parents=True)
    staging = tmp_path / "staging"
    for child in staging.iterdir():
        shutil.move(str(child), str(release / child.name))
    current = root / "current"
    current.symlink_to(release)
    assert (current / "VERSION").read_text(encoding="utf-8").strip() == version
    pyc_dir = current / "src" / "service_window" / "__pycache__"
    found = list(pyc_dir.glob("app.cpython-*.pyc")) if pyc_dir.is_dir() else []
    assert found, found


def test_ota_compileall_after_extract_in_installed_layout(tmp_path: Path) -> None:
    """releases/<ver> + current, then device-python compileall — not the CI tag."""
    from ota.agent import compile_release_bytecode

    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    root = tmp_path / "opt" / "bluesignal"
    release = root / "releases" / version
    app = release / "src" / "service_window"
    app.mkdir(parents=True)
    (app / "app.py").write_text("READY = True\n", encoding="utf-8")
    (release / "VERSION").write_text(version + "\n", encoding="utf-8")
    stale = app / "__pycache__"
    stale.mkdir()
    (stale / "app.cpython-311.pyc").write_bytes(b"wrong-tag")
    current = root / "current"
    current.symlink_to(release)

    assert compile_release_bytecode(release, run_cmd=subprocess.run, timeout_s=30)
    tag = f"cpython-{sys.version_info.major}{sys.version_info.minor}"
    found = list((app / "__pycache__").glob(f"app.{tag}*.pyc"))
    assert found, list((app / "__pycache__").iterdir())
    assert current.resolve() == release.resolve()
    assert (current / "src" / "service_window" / "app.py").is_file()


def test_setup_sh_precompiles_src_into_the_release() -> None:
    setup = (ROOT / "setup.sh").read_text(encoding="utf-8")
    assert "python3 -m compileall" in setup
    assert "--invalidation-mode=checked-hash" in setup
    assert '"$RELEASE_DIR/src"' in setup


def test_validate_tar_members_allows_pycache(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    pyc_dir = tree / "src" / "service_window" / "__pycache__"
    pyc_dir.mkdir(parents=True)
    (tree / "src" / "service_window" / "app.py").write_text("x = 1\n", encoding="utf-8")
    (pyc_dir / "app.cpython-311.pyc").write_bytes(b"\x00pyc")
    (tree / "VERSION").write_text("2.4.0\n", encoding="utf-8")
    bundle = tmp_path / "wqm1-firmware-2.4.0.tar.gz"
    with tarfile.open(bundle, "w:gz") as tar:
        tar.add(tree / "src", arcname="src")
        tar.add(tree / "VERSION", arcname="VERSION")
    assert validate_tar_members(bundle) is None
