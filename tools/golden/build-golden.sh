#!/bin/bash
# build-golden.sh — WQM-1 golden image, offline, on an x86_64 Linux box.
#
# One 32-bit (armhf) Raspberry Pi OS Lite Trixie card for every supported
# Pi. The board name is not written into the image. setup.sh runs here only
# to install the Pi package set (the same set for Zero W, Zero 2 W, Pi 3
# and Pi 4). WQM1_BOARD_MODEL is a chroot-only stand-in because this build
# has no device tree. On a real boot the firmware reads
# /proc/device-tree/model. See MANIFEST.md.
#
#   sudo bash build-golden.sh /path/to/2026-10-06-raspios-trixie-armhf-lite.img /path/to/wqm-1
#
# The host kernel may have no vfat, so the boot partition is edited with
# mtools. Apt sources are rewritten to HTTPS for the build and restored
# before the image is sealed.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "build-golden.sh must run as root" >&2
  exit 1
fi
if [ "$#" -ne 2 ]; then
  echo "usage: sudo bash build-golden.sh BASE.img WQM1_SRC" >&2
  exit 1
fi

BASE="$1"
SRC="$2"
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="${OUT:-$PWD/wqm1-golden.img}"
R="${R:-/mnt/wqm1root}"
# ARMv6: a wheel built for ARMv7 traps, and pip must pick linux_armv6l.
export QEMU_CPU=arm1176
L1=""
L2=""

if [ ! -e /proc/sys/fs/binfmt_misc/qemu-arm ]; then
  if [ ! -f /usr/lib/binfmt.d/qemu-arm.conf ]; then
    echo "qemu-arm binfmt is not registered and qemu-arm.conf is missing" >&2
    exit 1
  fi
  cat /usr/lib/binfmt.d/qemu-arm.conf > /proc/sys/fs/binfmt_misc/register
fi

cleanup() {
  ec=$?
  trap - EXIT
  set +e
  if [ -d "$R" ]; then
    if mountpoint -q "$R/sys"; then umount "$R/sys"; fi
    if mountpoint -q "$R/proc"; then umount "$R/proc"; fi
    if mountpoint -q "$R/dev/pts"; then umount "$R/dev/pts"; fi
    if mountpoint -q "$R/dev"; then umount "$R/dev"; fi
    if mountpoint -q "$R"; then umount "$R"; fi
  fi
  if [ -n "$L1" ]; then losetup -d "$L1"; fi
  if [ -n "$L2" ]; then losetup -d "$L2"; fi
  exit "$ec"
}
trap cleanup EXIT

cp "$BASE" "$OUT"
truncate -s +2G "$OUT"
parted -s "$OUT" resizepart 2 100%

part_field() {
  # $1 = partition number, $2 = awk field (2 start, 3 end). Trailing 's' stripped.
  parted -s "$OUT" unit s print | awk -v n="$1" -v f="$2" '$1 == n { gsub("s", "", $f); print $f }'
}
P1S="$(part_field 1 2)"
P1E="$(part_field 1 3)"
P2S="$(part_field 2 2)"
P2E="$(part_field 2 3)"

L1="$(losetup -f --show -o $((P1S * 512)) --sizelimit $(((P1E - P1S + 1) * 512)) "$OUT")"
L2="$(losetup -f --show -o $((P2S * 512)) --sizelimit $(((P2E - P2S + 1) * 512)) "$OUT")"

e2fsck -pf "$L2"
resize2fs "$L2"
mkdir -p "$R"
mount "$L2" "$R"
mtype -i "$L1" ::/config.txt > "$R/boot/firmware/config.txt"
mtype -i "$L1" ::/cmdline.txt > "$R/boot/firmware/cmdline.txt"
for d in dev dev/pts proc sys; do
  mount --bind "/$d" "$R/$d"
done
mv "$R/etc/resolv.conf" "$R/etc/resolv.conf.wqm1orig"
cp -L /etc/resolv.conf "$R/etc/resolv.conf"
printf '%s\n' '#!/bin/sh' 'exit 101' > "$R/usr/sbin/policy-rc.d"
chmod +x "$R/usr/sbin/policy-rc.d"
echo 'Defaults env_keep += "QEMU_CPU"' > "$R/etc/sudoers.d/zz-wqm1-build"
chmod 440 "$R/etc/sudoers.d/zz-wqm1-build"

APT_ORIG="$(mktemp -d)"
if [ -f "$R/etc/apt/sources.list" ]; then
  cp -a "$R/etc/apt/sources.list" "$APT_ORIG/"
fi
cp -a "$R/etc/apt/sources.list.d" "$APT_ORIG/"
sed -i \
  -e 's|http://raspbian.raspberrypi.com/raspbian/|https://archive.raspbian.org/raspbian/|' \
  -e 's|http://archive.raspberrypi.com/debian/|https://archive.raspberrypi.com/debian/|' \
  "$R"/etc/apt/sources.list.d/*.sources

# Firmware tree for this install only. Removed before the image is sealed.
cp -a "$SRC" "$R/opt/wqm-1-src"
chown -R 0:0 "$R/opt/wqm-1-src"
chroot "$R" usermod -s /bin/bash pi
chroot "$R" apt-get update
# "Raspberry Pi" only selects the direct-header package path. It is not a
# model, and setup.sh does not write it down.
chroot "$R" env \
  SUDO_USER=pi \
  DEBIAN_FRONTEND=noninteractive \
  LC_ALL=C.UTF-8 \
  QEMU_CPU=arm1176 \
  WQM1_BOARD_MODEL="Raspberry Pi" \
  bash -c 'cd /opt/wqm-1-src && bash ./setup.sh'
# The captive-portal redirect needs iptables. setup.sh installs it.
chroot "$R" dpkg -s iptables >/dev/null

install -m 755 "$HERE/overlay/usr/local/sbin/wqm1-firstboot" "$R/usr/local/sbin/"
install -m 644 "$HERE/overlay/etc/systemd/system/wqm1-firstboot.service" "$R/etc/systemd/system/"
install -m 644 "$HERE/overlay/etc/cloud/cloud.cfg.d/99_wqm1.cfg" "$R/etc/cloud/cloud.cfg.d/"
install -m 644 "$HERE/overlay/etc/ssh/sshd_config.d/10-wqm1.conf" "$R/etc/ssh/sshd_config.d/"
chroot "$R" systemctl enable wqm1-firstboot.service
chroot "$R" systemctl disable userconfig.service ssh.service
printf '%s\n' '[main]' 'WirelessEnabled=true' > "$R/var/lib/NetworkManager/NetworkManager.state"
if ! grep -q cfg80211.ieee80211_regdom "$R/boot/firmware/cmdline.txt"; then
  sed -i '1s/$/ cfg80211.ieee80211_regdom=US/' "$R/boot/firmware/cmdline.txt"
fi
chroot "$R" env \
  SUDO_USER=pi \
  DEBIAN_FRONTEND=noninteractive \
  LC_ALL=C.UTF-8 \
  QEMU_CPU=arm1176 \
  bash -c 'cd /opt/wqm-1-src && bash ./setup.sh --golden'

# dpkg's format string. Single quotes so bash does not expand it.
# shellcheck disable=SC2016
chroot "$R" dpkg-query -W -f='${Package}\t${Version}\t${Architecture}\n' > "$OUT.packages-dpkg.tsv"
chroot "$R" python3 -m pip list --path /usr/local/lib/python3.13/dist-packages --format=freeze \
  > "$OUT.packages-pip-local.txt"

rm -rf "$R/opt/wqm-1-src"
cp -a "$APT_ORIG/sources.list.d/." "$R/etc/apt/sources.list.d/"
if [ -f "$APT_ORIG/sources.list" ]; then
  cp -a "$APT_ORIG/sources.list" "$R/etc/apt/"
fi
rm -rf "$APT_ORIG"
rm -f "$R/usr/sbin/policy-rc.d" "$R/etc/sudoers.d/zz-wqm1-build" "$R/etc/resolv.conf"
mv "$R/etc/resolv.conf.wqm1orig" "$R/etc/resolv.conf"
chroot "$R" apt-get clean
rm -rf "$R"/var/lib/apt/lists/* "$R/root/.cache" "$R"/tmp/* "$R"/var/tmp/* \
  "$R"/var/log/journal/* "$R"/var/lib/cloud/* \
  "$R/root/.bash_history" "$R/home/pi/.bash_history"
find "$R/var/log" -type f \( -name '*.gz' -o -name '*.[0-9]' -o -name '*.old' \) -delete
find "$R/var/log" -type f -exec truncate -s 0 {} +
[ "$(cat "$R/etc/machine-id")" = "uninitialized" ]
[ -z "$(ls -A "$R/etc/NetworkManager/system-connections/")" ]
if compgen -G "$R/etc/ssh/ssh_host_*" > /dev/null; then
  echo "host keys in image" >&2
  exit 1
fi

for f in cmdline.txt config.txt; do
  mcopy -o -i "$L1" "$R/boot/firmware/$f" "::/$f"
done
for f in user-data meta-data network-config; do
  mcopy -o -i "$L1" "$HERE/bootfs/$f" "::/$f"
done
# /boot/firmware on the rootfs is the mount point. The files live on the FAT.
rm -f "$R"/boot/firmware/*
for d in sys proc dev/pts dev; do
  umount "$R/$d"
done
umount "$R"

# 600000 x 4 KiB rootfs (~2.3 GiB). The image is ~3.0 GB and grows on first boot.
NB=600000
e2fsck -fy "$L2"
resize2fs "$L2" "$NB"
zerofree "$L2"
e2fsck -fy "$L2"
losetup -d "$L1"
losetup -d "$L2"
L1=""
L2=""

echo yes | parted ---pretend-input-tty "$OUT" unit s resizepart 2 $((P2S + NB * 8 - 1))s
truncate -s $(((P2S + NB * 8) * 512)) "$OUT"
sha256sum "$OUT" > "$OUT.sha256"
xz -T0 -6 -k "$OUT"
sha256sum "$OUT.xz" > "$OUT.xz.sha256"
