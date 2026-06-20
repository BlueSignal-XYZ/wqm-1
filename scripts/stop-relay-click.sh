#!/usr/bin/env bash
#
# stop-relay-click.sh - Stop the hourly WQM-1 relay "click".
#
# WHAT CAUSES THE CLICK
#   A stuck or misplaced sensor (e.g. TDS pegged near 0 ppm, turbidity pegged at
#   3000 NTU because a probe is out of the water or disconnected) keeps a relay
#   automation rule continuously satisfied. With the default policies.yaml this
#   is Relay 3 (the aerator/air-pump channel): `turbidity > 500` and `tds < 50`
#   both stay TRUE, so the rules engine keeps the relay energised. It runs until
#   it spends its `max_on_minutes_per_hour` budget (default 10 min), clicks OFF
#   ~10-12 min past the hour, then re-engages the instant the budget resets at
#   the top of the next clock hour (the counter is bucketed by the UTC hour).
#   Result: an audible relay click every hour on the hour, plus an OFF click
#   roughly 11 minutes later.
#
# WHAT THIS SCRIPT DOES
#   Sets `manual.override: true` in policies.yaml and restarts the firmware
#   service. The rules engine then short-circuits before evaluating any rule,
#   and the service forces every relay OFF at startup. Sensor reads, logging,
#   LoRaWAN and cloud upload are unaffected - only relay actuation stops.
#
#   This treats the symptom so the unit goes quiet immediately. The root cause
#   is physical: reseat / clean / resubmerge the TDS and turbidity probes. Once
#   the readings are back in range you can re-enable automation with --enable.
#
# USAGE  (run on EACH unit)
#   sudo ./stop-relay-click.sh            # disable relay automation (stop click)
#   sudo ./stop-relay-click.sh --enable   # re-enable automation (after fixing probes)
#
#   Options:
#     --policies=/path/to/policies.yaml   override config path
#     --service=name.service              override systemd unit name
#     -h | --help                         show this header
#
# Idempotent and reversible. A timestamped backup of policies.yaml is written
# before any change.
#
set -euo pipefail

POLICIES="${POLICIES:-/opt/bluesignal/config/policies.yaml}"
SERVICE="${SERVICE:-bluesignal-wqm.service}"
MODE="disable"

for arg in "$@"; do
  case "$arg" in
    --enable)      MODE="enable" ;;
    --disable)     MODE="disable" ;;
    --policies=*)  POLICIES="${arg#*=}" ;;
    --service=*)   SERVICE="${arg#*=}" ;;
    -h|--help)     sed -n '2,/^set -euo/p' "$0" | sed 's/^#\{0,1\} \{0,1\}//; $d'; exit 0 ;;
    *) echo "Unknown argument: $arg (try --help)" >&2; exit 2 ;;
  esac
done

# Root needed to edit /opt and restart the service.
SUDO=""
if [ "$(id -u)" -ne 0 ]; then SUDO="sudo"; fi

# Locate policies.yaml if the default path is absent.
if [ ! -f "$POLICIES" ]; then
  for candidate in /opt/bluesignal/config/policies.yaml /etc/bluesignal/policies.yaml; do
    if [ -f "$candidate" ]; then POLICIES="$candidate"; break; fi
  done
fi
if [ ! -f "$POLICIES" ]; then
  echo "ERROR: policies.yaml not found (looked at: $POLICIES)." >&2
  echo "       Pass the path explicitly: $0 --policies=/path/to/policies.yaml" >&2
  exit 1
fi

want_override="true"
[ "$MODE" = "enable" ] && want_override="false"

echo "==> Host:        $(hostname)"
echo "==> policies:    $POLICIES"
echo "==> service:     $SERVICE"
echo "==> mode:        $MODE  (manual.override -> $want_override)"

# Timestamped backup before touching anything.
STAMP="$(date +%Y%m%d-%H%M%S)"
BAK="${POLICIES}.bak.${STAMP}"
$SUDO cp -a "$POLICIES" "$BAK"
echo "==> backup:      $BAK"

# Edit + validate with python3 (PyYAML is a firmware dependency, so it is present).
# Comment-preserving: we flip the existing `override:` line in place, or insert
# one under `manual:`, or append a `manual:` block - whichever applies. The result
# is parsed back and asserted before it is written, so a malformed edit aborts
# without modifying the live file.
$SUDO python3 - "$POLICIES" "$want_override" <<'PY'
import re, sys, yaml

path, want = sys.argv[1], (sys.argv[2] == "true")
val = "true" if want else "false"

with open(path) as f:
    text = f.read()

m = re.search(r'(?m)^manual:[ \t]*\n((?:[ \t]+.*\n?)*)', text)
if m:
    block = m.group(0)
    if re.search(r'(?m)^[ \t]+override[ \t]*:', block):
        new_block = re.sub(
            r'(?m)^([ \t]+override[ \t]*:[ \t]*).*$',
            lambda g: g.group(1) + val,
            block,
        )
    else:
        new_block = re.sub(
            r'(?m)^(manual:[ \t]*\n)',
            r'\g<1>  override: ' + val + '\n',
            block,
            count=1,
        )
    text = text[:m.start()] + new_block + text[m.end():]
else:
    if not text.endswith("\n"):
        text += "\n"
    text += "\nmanual:\n  override: %s\n" % val

data = yaml.safe_load(text) or {}
got = bool(data.get("manual", {}).get("override", False))
assert got is want, "validation failed: manual.override=%r, wanted %r" % (got, want)

with open(path, "w") as f:
    f.write(text)
print("    manual.override is now %s" % val)
PY

# Firmware reads policies only at startup, so a restart is required. The restart
# re-inits GPIO low and calls all_off(), so any latched relay drops immediately.
echo "==> restarting $SERVICE ..."
$SUDO systemctl restart "$SERVICE"
sleep 3

if $SUDO systemctl is-active --quiet "$SERVICE"; then
  echo "==> $SERVICE is active"
else
  echo "WARN: $SERVICE is not active - check: journalctl -u $SERVICE -n 50" >&2
fi

echo "==> firmware policy log line:"
if ! $SUDO journalctl -u "$SERVICE" --since "1 min ago" --no-pager 2>/dev/null \
      | grep -i "Policies loaded" | tail -1; then
  echo "    (none yet - check manually: journalctl -u $SERVICE -n 50)"
fi

echo
if [ "$MODE" = "disable" ]; then
  cat <<EOF
DONE - relay automation is OFF on this unit. The hourly click stops now.
Sensor monitoring, LoRaWAN and cloud upload continue normally.

Root cause is physical: a misplaced/disconnected probe (TDS ~0 ppm, turbidity
3000 NTU). Reseat and clean the TDS + turbidity probes when you can. Automation
stays disabled until you re-run with --enable.

  Revert:  sudo $0 --enable
  Backup:  $BAK
EOF
else
  cat <<EOF
DONE - relay automation is RE-ENABLED on this unit.
Confirm the TDS and turbidity probes are seated and reading in range first, or
the hourly click will return.

  Backup:  $BAK
EOF
fi
