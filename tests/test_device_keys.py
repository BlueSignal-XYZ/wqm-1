"""Device key creation under concurrent first-boot processes (install plan §1).

The firmware and the Service Window both call ``ensure_keypair()`` at first
boot. Before the cross-process lock, both wrote the same ``<key>.tmp``: one
raised ``FileNotFoundError`` or returned a public key that was not the one
on disk, and registration then signed with a key the unit did not hold.
"""

from __future__ import annotations

import multiprocessing
import os
from pathlib import Path

import pytest

from utils import device_keys

PROCESSES = 8
RUNS = 50


def _create(state_dir: str, barrier, results) -> None:
    os.environ["BLUESIGNAL_STATE_DIR"] = state_dir
    try:
        barrier.wait(timeout=30)
        results.put(("ok", device_keys.ensure_keypair().hex()))
    except Exception as e:  # noqa: BLE001 - the parent asserts on the type
        results.put(("error", f"{type(e).__name__}: {e}"))


def _race_once(state_dir: Path) -> list[tuple[str, str]]:
    ctx = multiprocessing.get_context("fork")
    barrier = ctx.Barrier(PROCESSES)
    results = ctx.Queue()
    procs = [
        ctx.Process(target=_create, args=(str(state_dir), barrier, results))
        for _ in range(PROCESSES)
    ]
    for p in procs:
        p.start()
    out = [results.get(timeout=60) for _ in procs]
    for p in procs:
        p.join(timeout=30)
    return out


def _disk_public_key(state_dir: Path) -> str:
    seed = (state_dir / device_keys.PRIVATE_KEY_NAME).read_bytes()
    return device_keys._key_from_seed(seed).public_key().export_key(format="raw").hex()


def test_concurrent_first_boot_processes_all_return_the_key_on_disk(tmp_path):
    for run in range(RUNS):
        state_dir = tmp_path / f"run-{run}"
        out = _race_once(state_dir)
        errors = [detail for status, detail in out if status != "ok"]
        assert errors == [], f"run {run}: {errors}"
        keys = {detail for _, detail in out}
        assert len(keys) == 1, f"run {run}: {len(keys)} different public keys"
        assert keys == {_disk_public_key(state_dir)}, f"run {run}: key not on disk"


def test_an_existing_key_is_returned_without_a_rewrite(tmp_path, monkeypatch):
    monkeypatch.setenv("BLUESIGNAL_STATE_DIR", str(tmp_path / "state"))
    first = device_keys.ensure_keypair()
    path = device_keys.private_key_path()
    stat = path.stat()

    second = device_keys.ensure_keypair()

    assert second == first
    assert path.stat().st_mtime_ns == stat.st_mtime_ns
    assert path.stat().st_ino == stat.st_ino


def test_the_lock_file_is_kept_so_every_process_locks_one_inode(tmp_path, monkeypatch):
    monkeypatch.setenv("BLUESIGNAL_STATE_DIR", str(tmp_path / "state"))
    device_keys.ensure_keypair()
    lock = device_keys.private_key_path().with_name(device_keys.PRIVATE_KEY_NAME + ".lock")
    assert lock.exists()
    inode = lock.stat().st_ino
    assert lock.stat().st_mode & 0o777 == device_keys.FILE_MODE

    device_keys.ensure_keypair()
    device_keys.sign_bytes(b"message")

    assert lock.stat().st_ino == inode


def test_a_complete_tmp_is_still_promoted_under_the_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("BLUESIGNAL_STATE_DIR", str(tmp_path / "state"))
    device_keys.ensure_state_dir()
    path = device_keys.private_key_path()
    seed = bytes(range(device_keys.SEED_BYTES))
    path.with_name(path.name + ".tmp").write_bytes(seed)

    pub = device_keys.ensure_keypair()

    assert path.read_bytes() == seed
    assert pub == device_keys._key_from_seed(seed).public_key().export_key(format="raw")


@pytest.mark.parametrize("call", ["ensure_keypair", "sign_bytes"])
def test_both_entry_points_take_the_key_lock(tmp_path, monkeypatch, call):
    monkeypatch.setenv("BLUESIGNAL_STATE_DIR", str(tmp_path / "state"))
    taken: list[Path] = []
    real = device_keys._key_file_lock

    def spy(path):
        taken.append(path)
        return real(path)

    monkeypatch.setattr(device_keys, "_key_file_lock", spy)
    if call == "ensure_keypair":
        device_keys.ensure_keypair()
    else:
        device_keys.sign_bytes(b"message")

    assert taken == [device_keys.private_key_path()]
