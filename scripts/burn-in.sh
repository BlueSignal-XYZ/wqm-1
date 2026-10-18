#!/bin/bash
# Bench burn-in for a WQM-1 on the HAT. Does nothing unless an empty file
# named "burnin" is on the FAT boot partition (drop it on the card from a
# Mac, then boot). Removes that file at the start so the next boot is a
# normal one, and writes wqm1-burnin.txt next to where the file was.
#
# Ten minutes, one check every 30 seconds: hotspot still active, ADC
# answers, no current undervoltage, commissioning services not restarting.
# Override the directories in tests with WQM1_BURNIN_BOOT_DIRS (colon
# separated). Duration and interval are WQM1_BURNIN_DURATION / _INTERVAL
# read by diagnostics.burnin.
set -u

IFS=':' read -r -a boot_dirs <<< "${WQM1_BURNIN_BOOT_DIRS:-/boot/firmware:/boot}"
flag=""
boot=""
for dir in "${boot_dirs[@]}"; do
  if [ -n "$dir" ] && [ -f "$dir/burnin" ]; then
    flag="$dir/burnin"
    boot="$dir"
    break
  fi
done

if [ -z "$flag" ]; then
  echo "burn-in: no burnin flag on the boot partition; skipping"
  exit 0
fi

rm -f "$flag"
export WQM1_BURNIN_FILE="${WQM1_BURNIN_FILE:-$boot/wqm1-burnin.txt}"
if [ -z "${PYTHONPATH:-}" ]; then
  export PYTHONPATH=/opt/bluesignal/current/src
fi

echo "burn-in: flag consumed, writing $WQM1_BURNIN_FILE"
exec /usr/bin/python3 -c 'from diagnostics.burnin import main; raise SystemExit(main())'
