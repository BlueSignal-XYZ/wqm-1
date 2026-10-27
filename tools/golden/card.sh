#!/bin/bash
# card.sh — label and check a built WQM-1 card image without mounting it.
#
#   bash tools/golden/card.sh label IMG LABEL_FILE   # writes wqm1-card.txt
#   bash tools/golden/card.sh check IMG armhf|arm64  # exits 1 on a problem
#
# Reads the boot (FAT) partition with mtools and the root (ext4) partition
# with debugfs, so it needs no root and no loop device. The card-image
# workflow runs both on every image before it can be downloaded.
#
# `check` fails on anything that stops a Pi from booting the card or that
# should never ship on one: a missing kernel or boot file for the boards
# the image is for, a root= that does not match the card's own partition
# id, saved Wi-Fi, a burn-in flag, an SSH flag, or a card that has already
# booted once.
set -euo pipefail

usage() {
  echo "usage: card.sh label IMG LABEL_FILE | card.sh check IMG armhf|arm64" >&2
  exit 2
}

[ "$#" -ge 3 ] || usage
cmd="$1"
img="$2"
[ -f "$img" ] || { echo "no such image: $img" >&2; exit 2; }

# partition N start/end in 512-byte sectors.
part_field() {
  parted -s "$img" unit s print | awk -v n="$1" -v f="$2" '$1 == n { gsub("s", "", $f); print $f }'
}
p1="$(part_field 1 2)"
p2="$(part_field 2 2)"
p2_end="$(part_field 2 3)"
if [ -z "$p1" ] || [ -z "$p2" ]; then
  echo "FAIL: no Raspberry Pi partition table (want a FAT boot and an ext4 root)" >&2
  exit 1
fi
FAT="${img}@@$((p1 * 512))"
export MTOOLS_SKIP_CHECK=1

case "$cmd" in
  label)
    [ -f "$3" ] || { echo "no such label: $3" >&2; exit 2; }
    mcopy -o -i "$FAT" "$3" ::/wqm1-card.txt
    echo "labelled: $(head -n 2 "$3" | tail -n 1)"
    exit 0
    ;;
  check)
    arch="$3"
    case "$arch" in armhf | arm64) ;; *) usage ;; esac
    ;;
  *) usage ;;
esac

fails=0
ok() { echo "ok:   $*"; }
bad() { echo "FAIL: $*"; fails=$((fails + 1)); }

files="$(mdir -b -i "$FAT" ::/ | sed 's|^::/||')"
has() { grep -xF "$1" <<<"$files" >/dev/null; }
fat_cat() { mtype -i "$FAT" "::/$1"; }

need() {
  if has "$1"; then ok "$1 ($2)"; else bad "$1 missing: $2"; fi
}

# Boot files. A Zero W, Zero 2 W or Pi 3 reads bootcode.bin from the card
# before anything else; without it the board does nothing at all.
need bootcode.bin "first-stage loader for Pi Zero W, Zero 2 W and 3"
need start.elf "GPU firmware"
need fixup.dat "GPU firmware"
need start_cd.elf "GPU firmware for gpu_mem=16, which config.txt sets"
need fixup_cd.dat "GPU firmware for gpu_mem=16"
need config.txt "boot settings"
need cmdline.txt "kernel command line"
if [ "$arch" = "armhf" ]; then
  need kernel.img "the only kernel a Pi Zero W runs"
  need kernel7.img "32-bit kernel for Pi Zero 2 W and 3"
  need bcm2708-rpi-zero-w.dtb "Pi Zero W device tree"
  need bcm2710-rpi-zero-2-w.dtb "Pi Zero 2 W device tree"
else
  need kernel8.img "64-bit kernel for Pi Zero 2 W, 3 and 4"
  need kernel_2712.img "Pi 5 kernel"
  need bcm2710-rpi-zero-2-w.dtb "Pi Zero 2 W device tree"
  need bcm2712-rpi-5-b.dtb "Pi 5 device tree"
  if has kernel.img; then
    bad "kernel.img on a 64-bit card: a Pi Zero W would start and then fail"
  fi
fi

# root= must name this card's own root partition. The disk identifier is
# the 4 bytes at offset 440 of the partition table, little-endian.
disk_id="$(od -An -tx4 -j440 -N4 "$img" | tr -d ' \n')"
cmdline="$(fat_cat cmdline.txt | tr -d '\r')"
if [ "$(printf '%s\n' "$cmdline" | wc -l)" -ne 1 ]; then
  bad "cmdline.txt must be one line"
fi
if grep -E "(^| )root=PARTUUID=${disk_id}-02( |$)" <<<"$cmdline" >/dev/null; then
  ok "cmdline.txt root=PARTUUID=${disk_id}-02 (this card's root)"
else
  bad "cmdline.txt root does not match this card (disk id ${disk_id}): ${cmdline}"
fi

# Nothing that belongs to one site or one bench run.
for f in user-data network-config meta-data; do
  if ! has "$f"; then
    bad "$f missing (cloud-init would fall back to the image defaults)"
    continue
  fi
  # Read the file first: grep -q exits at its first match, and under
  # pipefail a writer killed by that SIGPIPE turns a hit into a miss.
  body="$(fat_cat "$f" | grep -vE '^[[:space:]]*#' || true)"
  if grep -iE '(ssid|psk|password|passwd|wifis:|access-points)' <<<"$body" >/dev/null; then
    bad "$f has Wi-Fi or a password in it"
  else
    ok "$f has no Wi-Fi and no password"
  fi
done
for f in wpa_supplicant.conf ssh ssh.txt userconf userconf.txt burnin wqm1-status.txt; do
  if has "$f"; then
    case "$f" in
      burnin) bad "burnin flag on the image (add it per card on the bench, not in the image)" ;;
      wqm1-status.txt) bad "wqm1-status.txt present: this card has already booted" ;;
      *) bad "$f on the image" ;;
    esac
  fi
done

# The root file system: never booted, and holds the firmware. debugfs
# reads a file system image, not a partition at an offset, so the root
# partition is cut out once.
tmp_root="$(mktemp)"
trap 'rm -f "$tmp_root"' EXIT
dd if="$img" of="$tmp_root" bs=512 skip="$p2" count=$((p2_end - p2 + 1)) status=none
machine_id="$(debugfs -R 'cat /etc/machine-id' "$tmp_root" 2>/dev/null | tr -d '\n')"
if [ "$machine_id" = "uninitialized" ]; then
  ok "/etc/machine-id uninitialized (never booted)"
else
  bad "/etc/machine-id is '${machine_id}': the card is not a never-booted copy"
fi
fw_version="$(debugfs -R 'cat /opt/bluesignal/current/VERSION' "$tmp_root" 2>/dev/null | tr -d '\n')"
if [ -n "$fw_version" ]; then
  ok "firmware ${fw_version} installed at /opt/bluesignal/current"
else
  bad "no firmware at /opt/bluesignal/current"
fi

if has wqm1-card.txt; then
  echo "----- wqm1-card.txt"
  fat_cat wqm1-card.txt | tr -d '\r'
  echo "-----"
else
  echo "note: no wqm1-card.txt label (card.sh label writes it)"
fi

if [ "$fails" -gt 0 ]; then
  echo "card check: ${fails} problem(s)"
  exit 1
fi
echo "card check: ok"
