#!/usr/bin/env bash
# Build the OTA firmware tarball the device extracts into
# /opt/bluesignal/releases/<version>/.
#
# Layout: repo files at the TOP LEVEL of the tarball (no wrapping
# directory) — the agent extracts straight into the release tree, so
# src/, VERSION, systemd/ and the rest sit at the archive root. Keep
# this list in step with ci.yml / release.yml callers and with
# setup.sh's per-release install layout.
#
# Bytecode: compileall with the python that runs this script, then pack
# __pycache__ (do not --exclude it). CI's python is 3.11; Trixie on the
# Pi is compiled again by setup.sh / the golden chroot so first boot
# matches the image interpreter. Hash-based pyc survive tar mtimes.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
VERSION="$(cat VERSION)"
OUT="${1:-$ROOT/wqm1-firmware-${VERSION}.tar.gz}"
PYTHON="${PYTHON:-python3}"

STAGE="$(mktemp -d)"
cleanup() { rm -rf "$STAGE"; }
trap cleanup EXIT

for item in src config scripts systemd requirements.txt requirements-rpi.txt VERSION setup.sh; do
    cp -a "$ROOT/$item" "$STAGE/"
done

"$PYTHON" -m compileall -q -f --invalidation-mode=checked-hash "$STAGE/src"

tar -C "$STAGE" -czf "$OUT" \
    src config scripts systemd \
    requirements.txt requirements-rpi.txt VERSION setup.sh

ls -l "$OUT"
