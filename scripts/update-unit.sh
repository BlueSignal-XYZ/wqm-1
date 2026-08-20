#!/bin/bash
# WQM-1 in-place firmware update — run ON the unit.
#
#   sudo bash scripts/update-unit.sh [git-ref]
#
# With a git-ref it fetches and checks that out first; without one it installs
# whatever is in the current working tree. Either way it captures the unit's
# state before and after, so an update that silently did nothing is visible
# rather than assumed.
#
# Safe to re-run. setup.sh preserves /etc/bluesignal/config.yaml, installs the
# new tree as releases/<version>, and flips the `current` symlink atomically.
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_DIR="/opt/bluesignal"
CONFIG="/etc/bluesignal/config.yaml"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="/var/backups/bluesignal/$STAMP"
REF="${1:-}"

say()  { echo "  $*"; }
head2() { echo; echo "=== $* ==="; }

if [ "$(id -u)" -ne 0 ]; then
    echo "Run with sudo: sudo bash scripts/update-unit.sh ${REF:-}" >&2
    exit 1
fi

db_path() {
    python3 - "$CONFIG" <<'PY' 2>/dev/null || echo "/var/lib/bluesignal/wqm1.db"
import sys
try:
    import yaml
    c = yaml.safe_load(open(sys.argv[1])) or {}
    print(c.get("db_path") or "/var/lib/bluesignal/wqm1.db")
except Exception:
    print("/var/lib/bluesignal/wqm1.db")
PY
}

row_counts() {
    local db="$1"
    [ -f "$db" ] || { echo "(no database at $db)"; return; }
    sqlite3 "$db" \
      "SELECT sync_state || ': ' || COUNT(*) FROM readings GROUP BY sync_state;" 2>/dev/null \
      || echo "(sqlite3 not installed — skipping row counts)"
}

DB="$(db_path)"

head2 "Before"
OLD_VERSION="$(cat "$INSTALL_DIR/current/VERSION" 2>/dev/null || echo unknown)"
say "Installed version : $OLD_VERSION"
say "Repo checkout     : $REPO_DIR"
say "Database          : $DB"
say "Reading rows by sync_state:"
row_counts "$DB" | sed 's/^/    /'
systemctl is-active --quiet bluesignal-wqm && say "Service           : running" || say "Service           : NOT running"

head2 "Backing up"
mkdir -p "$BACKUP_DIR"
if [ -f "$CONFIG" ]; then
    cp "$CONFIG" "$BACKUP_DIR/config.yaml" && say "config.yaml → $BACKUP_DIR/config.yaml"
else
    say "No $CONFIG to back up."
fi
if [ -f "$DB" ]; then
    # Use sqlite3's own backup so a copy taken mid-write is still consistent.
    if command -v sqlite3 >/dev/null 2>&1; then
        sqlite3 "$DB" ".backup '$BACKUP_DIR/wqm1.db'" && say "database → $BACKUP_DIR/wqm1.db"
    else
        cp "$DB" "$BACKUP_DIR/wqm1.db" && say "database (plain copy) → $BACKUP_DIR/wqm1.db"
    fi
fi

if [ -n "$REF" ]; then
    head2 "Fetching $REF"
    git -C "$REPO_DIR" fetch origin "$REF" || { echo "fetch failed" >&2; exit 1; }
    git -C "$REPO_DIR" checkout -q FETCH_HEAD || { echo "checkout failed" >&2; exit 1; }
    say "Now at $(git -C "$REPO_DIR" rev-parse --short HEAD) ($REF)"
else
    say "No git ref given — installing the working tree as-is."
fi

NEW_VERSION="$(cat "$REPO_DIR/VERSION" 2>/dev/null || echo unknown)"
head2 "Installing $OLD_VERSION → $NEW_VERSION"
if [ "$OLD_VERSION" = "$NEW_VERSION" ]; then
    say "WARNING: the tree carries the same version as what is installed."
    say "The install will still happen, but the dashboard will not show a change."
fi
bash "$REPO_DIR/setup.sh" || { echo "setup.sh failed — the previous release is still active" >&2; exit 1; }

head2 "After"
RUNNING_VERSION="$(cat "$INSTALL_DIR/current/VERSION" 2>/dev/null || echo unknown)"
say "Installed version : $RUNNING_VERSION"
if [ "$RUNNING_VERSION" != "$NEW_VERSION" ]; then
    say "WARNING: expected $NEW_VERSION but current/ reports $RUNNING_VERSION."
fi
for svc in bluesignal-wqm bluesignal-service-window bluesignal-ota; do
    if systemctl is-active --quiet "$svc"; then say "$svc: running"; else say "$svc: NOT running"; fi
done
say "Reading rows by sync_state:"
row_counts "$DB" | sed 's/^/    /'

head2 "Diagnostics"
bash "$INSTALL_DIR/current/scripts/diagnostics.sh"
DIAG_RC=$?

head2 "Done"
say "Backup:  $BACKUP_DIR"
say "Logs:    journalctl -u bluesignal-wqm -f"
say "Rejects: journalctl -u bluesignal-wqm --since '-1h' | grep -i 'permanently rejected'"
say "A reboot is worth doing if this is the unit's first move onto the releases/ layout."
exit $DIAG_RC
