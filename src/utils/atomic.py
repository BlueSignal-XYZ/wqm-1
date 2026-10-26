"""Durable replace for config and identity files.

A power cut in the middle of ``write_text`` leaves a half-written file that
the next boot then trusts. The sequence here is the usual one: write a temp
file in the same directory, ``fsync`` it, then ``rename`` over the
destination. A second ``fsync`` on the directory makes the rename itself
durable on ext4. The temp name keeps the historical suffix (``data.json``
becomes ``data.tmp``) so a crash still leaves one predictable leftover.

vfat (the boot partition) does not make rename atomic. Callers that publish
there still use this helper: a short temp file plus ``fsync`` is the best
that filesystem can do.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path


def atomic_write_text(
    path: Path | str,
    text: str,
    *,
    mode: int | None = None,
    create_parent: bool = True,
) -> None:
    """Write ``text`` to ``path`` via a temp file, fsync, and rename."""
    atomic_write_bytes(path, text.encode("utf-8"), mode=mode, create_parent=create_parent)


def atomic_write_bytes(
    path: Path | str,
    data: bytes,
    *,
    mode: int | None = None,
    create_parent: bool = True,
) -> None:
    """Write ``data`` to ``path`` via a temp file, fsync, and rename."""
    dest = Path(path)
    if create_parent:
        dest.parent.mkdir(parents=True, exist_ok=True)
    elif not dest.parent.is_dir():
        raise FileNotFoundError(dest.parent)
    # The whole name, so config.yaml and config.json never share a temp file.
    tmp = dest.with_name(dest.name + ".tmp")
    try:
        # builtins.open, so a test that patches it still sees the write fail.
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            # vfat often rejects chmod. The bytes are what matter.
            with contextlib.suppress(OSError):
                os.chmod(tmp, mode)
        tmp.replace(dest)
        _fsync_dir(dest.parent)
    except Exception:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def _fsync_dir(directory: Path) -> None:
    """Make a rename durable. EINVAL on vfat is ignored."""
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        fd = os.open(directory, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        return
    finally:
        os.close(fd)
