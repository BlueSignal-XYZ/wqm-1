"""The downloadable card image: base list, label, card check and workflow.

The image build needs root, qemu and the Raspberry Pi OS download, so it
runs in .github/workflows/card-image.yml. These tests hold what that
workflow relies on: the base-image list agrees with MANIFEST.md, the label
names the boards and the setup network from the firmware itself, the card
check refuses a card a Pi cannot boot or that carries a secret, and the
workflow checks a card before anyone can download it.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "tools" / "golden"
WORKFLOW = ROOT / ".github" / "workflows" / "card-image.yml"


# --- base-images.txt -------------------------------------------------------


def _base_images() -> dict[str, tuple[str, str]]:
    out = {}
    for line in (GOLDEN / "base-images.txt").read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        arch, url, sha = line.split()
        out[arch] = (url, sha)
    return out


def test_base_images_name_both_arches_with_a_checksum():
    images = _base_images()
    assert set(images) == {"armhf", "arm64"}
    for arch, (url, sha) in images.items():
        assert url.startswith("https://downloads.raspberrypi.com/"), arch
        assert url.endswith(".img.xz"), arch
        assert arch in url, arch
        assert re.fullmatch(r"[0-9a-f]{64}", sha), arch


def test_manifest_and_base_images_agree():
    manifest = (GOLDEN / "MANIFEST.md").read_text()
    for arch, (url, sha) in _base_images().items():
        assert url in manifest, f"{arch} URL differs between base-images.txt and MANIFEST.md"
        assert sha in manifest, f"{arch} sha256 differs between base-images.txt and MANIFEST.md"


# --- card-label.py ---------------------------------------------------------


def _fake_tree(tmp_path: Path, *, shared_ssid: bool, version: str = "9.9.9") -> Path:
    tree = tmp_path / "fw"
    (tree / "src" / "utils").mkdir(parents=True)
    (tree / "src" / "utils" / "__init__.py").write_text("")
    if shared_ssid:
        body = (
            'SETUP_SSID = "BlueSignal Setup"\n\n'
            "def ap_name(device_id=None):\n    return SETUP_SSID\n"
        )
    else:
        body = 'def ap_name(device_id=None):\n    return f"WQM1-{device_id[-4:].upper()}"\n'
    (tree / "src" / "utils" / "identity.py").write_text(body)
    (tree / "VERSION").write_text(version + "\n")
    return tree


def _label(tree: Path, arch: str, *extra: str) -> str:
    out = subprocess.run(
        [sys.executable, str(GOLDEN / "card-label.py"), str(tree), arch, "0123456789abcdef"]
        + list(extra),
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout


def test_label_names_the_open_setup_network_from_the_firmware(tmp_path):
    text = _label(_fake_tree(tmp_path, shared_ssid=True), "armhf")
    assert "Setup network: BlueSignal Setup, no password\n" in text
    assert "Firmware: 9.9.9, commit 0123456\n" in text


def test_label_on_an_older_firmware_says_the_name_is_per_unit(tmp_path):
    text = _label(_fake_tree(tmp_path, shared_ssid=False), "armhf")
    assert "Setup network: WQM1- plus the last four of the unit, with a password\n" in text


def test_label_says_which_boards_boot_the_card(tmp_path):
    tree = _fake_tree(tmp_path, shared_ssid=True)
    armhf = _label(tree, "armhf")
    assert "Boots: Raspberry Pi Zero W, Zero 2 W, 3, 4\n" in armhf
    assert "Does not boot: Raspberry Pi 5\n" in armhf
    arm64 = _label(tree, "arm64")
    assert "Boots: Raspberry Pi Zero 2 W, 3, 4, 5\n" in arm64
    assert "Does not boot: Raspberry Pi Zero W\n" in arm64


def test_label_records_the_build_that_made_it(tmp_path):
    url = "https://github.com/BlueSignal-XYZ/wqm-1/actions/runs/1"
    text = _label(_fake_tree(tmp_path, shared_ssid=True), "armhf", "--run", url)
    assert f"Build: {url}\n" in text


def test_label_reads_this_repos_firmware(tmp_path):
    text = _label(ROOT, "armhf")
    version = (ROOT / "VERSION").read_text().strip()
    assert f"Firmware: {version}, " in text
    assert text.startswith("WQM-1 card\n")


# --- card.sh check ---------------------------------------------------------

_CARD_TOOLS = ("parted", "mformat", "mcopy", "mdir", "mtype", "debugfs", "mke2fs", "od")
# Skipped on a laptop without the tools; never in CI, which installs them
# (ci.yml "Install card-image tools"). A skip there would read as coverage.
needs_card_tools = pytest.mark.skipif(
    not all(shutil.which(t) for t in _CARD_TOOLS) and not os.environ.get("CI"),
    reason="needs parted, mtools and e2fsprogs (apt install mtools parted e2fsprogs)",
)


def test_ci_has_the_card_tools():
    if os.environ.get("CI"):
        missing = [t for t in _CARD_TOOLS if not shutil.which(t)]
        assert not missing, f"CI must install {missing}: the card.sh tests would not run"
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    runs = " ".join(step.get("run", "") for step in workflow["jobs"]["test"]["steps"])
    assert "mtools" in runs and "parted" in runs and "e2fsprogs" in runs


_ARMHF_BOOT = (
    "bootcode.bin",
    "start.elf",
    "fixup.dat",
    "start_cd.elf",
    "fixup_cd.dat",
    "config.txt",
    "kernel.img",
    "kernel7.img",
    "bcm2708-rpi-zero-w.dtb",
    "bcm2710-rpi-zero-2-w.dtb",
)
_CLOUD_INIT = {
    "user-data": "#cloud-config\n# users:\n#   passwd: example\n",
    "meta-data": "dsmode: local\ninstance_id: wqm1-golden-test\n",
    "network-config": (
        "#network:\n#  wifis:\n#      access-points:\n#        x:\n#          password: y\n"
    ),
}

FAT_START, FAT_SECTORS = 2048, 32768
ROOT_START, ROOT_SECTORS = FAT_START + FAT_SECTORS, 32768


def _run(*args: str, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), capture_output=True, text=True, **kw)


def _make_card(
    tmp_path: Path,
    *,
    boot: tuple[str, ...] = _ARMHF_BOOT,
    extra: dict[str, str] | None = None,
    machine_id: str = "uninitialized",
    firmware: bool = True,
    root_suffix: str = "-02",
) -> Path:
    env = {**os.environ, "MTOOLS_SKIP_CHECK": "1"}
    tmp_path.mkdir(parents=True, exist_ok=True)
    img = tmp_path / "card.img"
    with img.open("wb") as fh:
        fh.truncate((ROOT_START + ROOT_SECTORS + 2048) * 512)
    _run(
        "parted", "-s", str(img), "unit", "s", "mklabel", "msdos",
        "mkpart", "primary", "fat32", str(FAT_START), str(FAT_START + FAT_SECTORS - 1),
        "mkpart", "primary", "ext4", str(ROOT_START), str(ROOT_START + ROOT_SECTORS - 1),
        check=True,
    )  # fmt: skip
    disk_id = img.read_bytes()[440:444][::-1].hex()

    files = {name: name for name in boot}
    files.update(_CLOUD_INIT)
    files["cmdline.txt"] = (
        f"console=tty1 root=PARTUUID={disk_id}{root_suffix} rootfstype=ext4 rootwait\n"
    )
    files.update(extra or {})
    fat = tmp_path / "fat.img"
    geometry = ("-t", "16", "-h", "64", "-s", "32")  # 16 x 64 x 32 sectors = 16 MiB
    _run("mformat", "-C", "-i", str(fat), *geometry, "::", env=env, check=True)
    src = tmp_path / "fatfiles"
    src.mkdir()
    for name, body in files.items():
        (src / name).write_text(body)
        _run("mcopy", "-i", str(fat), str(src / name), f"::/{name}", env=env, check=True)

    rootdir = tmp_path / "rootdir"
    (rootdir / "etc").mkdir(parents=True)
    (rootdir / "etc" / "machine-id").write_text(machine_id + "\n")
    if firmware:
        release = rootdir / "opt" / "bluesignal" / "releases" / "2.4.0"
        release.mkdir(parents=True)
        (release / "VERSION").write_text("2.4.0\n")
        # setup.sh makes current an absolute link; check it is followed
        # inside the image, not on the machine running the check.
        (rootdir / "opt" / "bluesignal" / "current").symlink_to("/opt/bluesignal/releases/2.4.0")
    root = tmp_path / "root.img"
    _run("mke2fs", "-q", "-F", "-t", "ext4", "-d", str(rootdir), str(root), "16M", check=True)

    with img.open("r+b") as fh:
        fh.seek(FAT_START * 512)
        fh.write(fat.read_bytes())
        fh.seek(ROOT_START * 512)
        fh.write(root.read_bytes())
    return img


def _check(img: Path, arch: str = "armhf") -> subprocess.CompletedProcess:
    return _run("bash", str(GOLDEN / "card.sh"), "check", str(img), arch)


@needs_card_tools
def test_a_good_card_passes(tmp_path):
    result = _check(_make_card(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "card check: ok" in result.stdout
    assert "firmware 2.4.0 installed" in result.stdout


@needs_card_tools
def test_a_card_without_the_zero_w_kernel_fails(tmp_path):
    boot = tuple(f for f in _ARMHF_BOOT if f != "kernel.img")
    result = _check(_make_card(tmp_path, boot=boot))
    assert result.returncode == 1
    assert "FAIL: kernel.img missing" in result.stdout


@needs_card_tools
def test_a_card_without_bootcode_fails(tmp_path):
    boot = tuple(f for f in _ARMHF_BOOT if f != "bootcode.bin")
    result = _check(_make_card(tmp_path, boot=boot))
    assert result.returncode == 1
    assert "FAIL: bootcode.bin missing" in result.stdout


@needs_card_tools
def test_a_root_that_is_not_this_cards_partition_fails(tmp_path):
    result = _check(_make_card(tmp_path, root_suffix="-03"))
    assert result.returncode == 1
    assert "cmdline.txt root does not match this card" in result.stdout


@needs_card_tools
@pytest.mark.parametrize(
    ("name", "body"),
    [
        ("network-config", "network:\n  wifis:\n    wlan0:\n      access-points:\n        Home:\n"),
        ("user-data", "#cloud-config\nusers:\n  - name: pi\n    plain_text_passwd: hunter2\n"),
    ],
)
def test_a_card_with_wifi_or_a_password_fails(tmp_path, name, body):
    result = _check(_make_card(tmp_path, extra={name: body}))
    assert result.returncode == 1
    assert f"FAIL: {name} has Wi-Fi or a password in it" in result.stdout


@needs_card_tools
def test_a_password_early_in_a_large_file_is_still_found(tmp_path):
    """#171 review: grep -q exits at the first match; under pipefail the
    writer's SIGPIPE turned that hit into "no Wi-Fi". A large file with
    the secret on line one is where the old pipeline lost it."""
    body = "network:\n  wifis:\n    wlan0:\n      access-points:\n        Home:\n"
    body += "          password: hunter22\n" + "      dhcp4: true\n" * 60000
    result = _check(_make_card(tmp_path, extra={"network-config": body}))
    assert result.returncode == 1
    assert "FAIL: network-config has Wi-Fi or a password in it" in result.stdout


@needs_card_tools
@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("burnin", "burnin flag on the image"),
        ("ssh", "ssh on the image"),
        ("wpa_supplicant.conf", "wpa_supplicant.conf on the image"),
        ("wqm1-status.txt", "this card has already booted"),
    ],
)
def test_a_card_with_a_bench_or_site_file_fails(tmp_path, name, message):
    result = _check(_make_card(tmp_path, extra={name: "x\n"}))
    assert result.returncode == 1
    assert message in result.stdout


@needs_card_tools
def test_a_card_that_has_booted_fails(tmp_path):
    result = _check(_make_card(tmp_path, machine_id="0123456789abcdef0123456789abcdef"))
    assert result.returncode == 1
    assert "not a never-booted copy" in result.stdout


@needs_card_tools
def test_a_card_without_firmware_fails(tmp_path):
    result = _check(_make_card(tmp_path, firmware=False))
    assert result.returncode == 1
    assert "no firmware at /opt/bluesignal/current" in result.stdout


@needs_card_tools
def test_a_64_bit_card_with_a_zero_w_kernel_fails(tmp_path):
    boot = tuple(
        f for f in _ARMHF_BOOT if f not in ("kernel.img", "kernel7.img", "bcm2708-rpi-zero-w.dtb")
    ) + ("kernel8.img", "kernel_2712.img", "bcm2712-rpi-5-b.dtb")
    good = _check(_make_card(tmp_path / "a", boot=boot), "arm64")
    assert good.returncode == 0, good.stdout
    bad = _check(_make_card(tmp_path / "b", boot=(*boot, "kernel.img")), "arm64")
    assert bad.returncode == 1
    assert "kernel.img on a 64-bit card" in bad.stdout


@needs_card_tools
def test_label_is_written_onto_the_card_and_shown_by_check(tmp_path):
    img = _make_card(tmp_path)
    label = tmp_path / "wqm1-card.txt"
    label.write_text("WQM-1 card\nFirmware: 2.4.0, commit 0123456\n")
    written = _run("bash", str(GOLDEN / "card.sh"), "label", str(img), str(label))
    assert written.returncode == 0, written.stderr
    result = _check(img)
    assert result.returncode == 0, result.stdout
    assert "Firmware: 2.4.0, commit 0123456" in result.stdout


# --- the workflow ------------------------------------------------------------


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def _steps(job: str) -> list[dict]:
    return _workflow()["jobs"][job]["steps"]


def _step_index(steps: list[dict], needle: str) -> int:
    for i, step in enumerate(steps):
        if needle in (step.get("run") or "") or needle in (step.get("uses") or ""):
            return i
    raise AssertionError(f"no step with {needle!r}")


def test_workflow_checks_the_card_before_anyone_can_download_it():
    steps = _steps("build")
    check = _step_index(steps, "card.sh check")
    upload = _step_index(steps, "actions/upload-artifact")
    assert check < upload
    assert steps[upload]["with"]["if-no-files-found"] == "error"


def test_workflow_checks_the_base_image_against_the_list():
    steps = _steps("build")
    download = steps[_step_index(steps, "base-images.txt")]["run"]
    assert "sha256sum -c" in download
    build = _step_index(steps, "build-golden.sh")
    assert _step_index(steps, "base-images.txt") < build


def test_workflow_writes_only_when_published_by_hand():
    workflow = _workflow()
    assert workflow["permissions"] == {"contents": "read"}
    assert "permissions" not in workflow["jobs"]["build"]
    publish = workflow["jobs"]["publish"]
    assert publish["if"].startswith("github.event_name == 'workflow_dispatch'")
    assert "inputs.publish" in publish["if"]
    assert publish["permissions"]["contents"] == "write"


def test_workflow_release_tag_cannot_start_a_firmware_release():
    run = _steps("publish")[-1]["run"]
    assert 'tag="card-' in run
    release = yaml.safe_load((ROOT / ".github" / "workflows" / "release.yml").read_text())
    # PyYAML reads the bare key `on` as True.
    trigger = release.get("on", release.get(True))
    assert trigger["push"]["tags"] == ["v*"]


def test_the_upload_path_has_no_dot_dot():
    """upload-artifact refuses a path with "..": the first real run built and
    checked a good card, then failed at the upload."""
    job = _workflow()["jobs"]["build"]
    assert "WORK" not in (job.get("env") or {})
    first = job["steps"][0]["run"]
    assert first.strip() == 'echo "WORK=$RUNNER_TEMP/card-work" >> "$GITHUB_ENV"'
    assert ".." not in first


def test_a_pr_build_is_named_after_the_branch_commit():
    """#171 review: github.sha on a pull_request is GitHub's merge commit,
    which is on no branch; the card would carry a commit nobody can find."""
    checkouts = [s for s in _steps("build") if "actions/checkout" in (s.get("uses") or "")]
    assert len(checkouts) == 2
    for step in checkouts:
        assert "github.event.pull_request.head.sha" in step["with"]["ref"]


def test_flashing_page_names_the_files_the_workflow_makes():
    page = (ROOT / "docs" / "flash-a-card.md").read_text()
    name_step = _steps("build")[_step_index(_steps("build"), 'base="wqm1-pi-zero-')]["run"]
    assert 'base="wqm1-pi-zero-${short}"' in name_step
    assert 'base="wqm1-pi-5-${short}"' in name_step
    assert "wqm1-pi-zero-<commit>.img.xz" in page
    assert "wqm1-pi-5-<commit>.img.xz" in page
    assert "Use custom" in page
    assert "customisation" in page
