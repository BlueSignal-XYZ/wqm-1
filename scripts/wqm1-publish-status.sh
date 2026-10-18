#!/bin/bash
# Copy the firmware's status snapshot onto the FAT boot partition.
#
# The firmware runs unprivileged and cannot write /boot/firmware. setup.sh
# grants this one script, with no arguments, via sudo. It copies
# /var/lib/bluesignal/wqm1-status.txt and nothing else.
set -euo pipefail

SRC=/var/lib/bluesignal/wqm1-status.txt

if [ ! -f "$SRC" ]; then
  echo "wqm1-publish-status: no status file yet" >&2
  exit 1
fi

# The writer already redacts these. Refuse anyway: this script is the
# thing that puts bytes on a partition anyone with the card can read.
if grep -E -q 'api_key|app_key|passphrase|PRIVATE KEY' "$SRC"; then
  echo "wqm1-publish-status: refusing to publish a file that contains a secret" >&2
  exit 1
fi

published=0
for dest_dir in /boot/firmware /boot; do
  if [ -d "$dest_dir" ] && [ -w "$dest_dir" ]; then
    tmp="$dest_dir/wqm1-status.txt.tmp"
    dest="$dest_dir/wqm1-status.txt"
    cp "$SRC" "$tmp"
    mv "$tmp" "$dest"
    published=1
    break
  fi
done

if [ "$published" -ne 1 ]; then
  echo "wqm1-publish-status: boot partition is not mounted writable" >&2
  exit 1
fi
