#!/bin/bash
# build-golden.sh — WQM-1 golden image, offline, on an x86_64 Linux box.
#
# One Raspberry Pi OS Lite Trixie card, either 32-bit (armhf, the default)
# or 64-bit (arm64). The overlay is the same either way. The board name is
# not written into the image. setup.sh runs here only to install the Pi
# package set (the same set for every Pi). WQM1_BOARD_MODEL is a
# chroot-only stand-in because this build has no device tree. On a real
# boot the firmware reads /proc/device-tree/model. See MANIFEST.md.
#
#   sudo bash build-golden.sh BASE.img /path/to/wqm-1
#   sudo bash build-golden.sh --arch arm64 BASE.img /path/to/wqm-1
#
# armhf is the only card a Zero W can boot. QEMU_CPU=arm1176 is set for
# that build so pip selects ARMv6 wheels. The arm64 build does not set it.
#
# The host kernel may have no vfat, so the boot partition is edited with
# mtools. Apt sources are rewritten to HTTPS for the build and restored
# before the image is sealed.
set -euo pipefail

usage() {
  echo "usage: sudo bash build-golden.sh [--arch armhf|arm64] BASE.img WQM1_SRC" >&2
  echo "  armhf (default) writes ./wqm1-golden.img ; arm64 writes ./wqm1-golden-arm64.img" >&2
  echo "  override the output path with OUT=" >&2
}

if [ "$(id -u)" -ne 0 ]; then
  echo "build-golden.sh must run as root" >&2
  exit 1
fi

ARCH="${ARCH:-armhf}"
POSITIONAL=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --arch)
      if [ "$#" -lt 2 ]; then
        usage
        exit 1
      fi
      ARCH="$2"
      shift 2
      ;;
    --arch=*)
      ARCH="${1#--arch=}"
      shift
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    --)
      shift
      while [ "$#" -gt 0 ]; do
        POSITIONAL+=("$1")
        shift
      done
      ;;
    -*)
      echo "unknown option: $1" >&2
      usage
      exit 1
      ;;
    *)
      POSITIONAL+=("$1")
      shift
      ;;
  esac
done
if [ "${#POSITIONAL[@]}" -gt 0 ]; then
  set -- "${POSITIONAL[@]}"
else
  set --
fi

case "$ARCH" in
  armhf | arm64) ;;
  *)
    echo "arch must be armhf or arm64 (got ${ARCH})" >&2
    usage
    exit 1
    ;;
esac

if [ "$#" -ne 2 ]; then
  usage
  exit 1
fi

BASE="$1"
SRC="$2"
HERE="$(cd "$(dirname "$0")" && pwd)"
if [ "$ARCH" = "arm64" ]; then
  OUT="${OUT:-$PWD/wqm1-golden-arm64.img}"
  BINFMT="qemu-aarch64"
  unset QEMU_CPU || true
else
  OUT="${OUT:-$PWD/wqm1-golden.img}"
  BINFMT="qemu-arm"
  # ARMv6: a wheel built for ARMv7 traps, and pip must pick linux_armv6l.
  export QEMU_CPU=arm1176
fi
# Per-arch mount point so a late cleanup from one build cannot unmount the other.
R="${R:-/mnt/wqm1root-$ARCH}"
L1=""
L2=""

BINFMT_CONF="/usr/lib/binfmt.d/${BINFMT}.conf"
if [ ! -e "/proc/sys/fs/binfmt_misc/${BINFMT}" ]; then
  if [ ! -f "$BINFMT_CONF" ]; then
    echo "$BINFMT binfmt is not registered and $BINFMT_CONF is missing" >&2
    exit 1
  fi
  # The kernel's binfmt_misc directory can exist before the filesystem is
  # mounted. register is what shows it is actually mounted.
  if [ ! -e /proc/sys/fs/binfmt_misc/register ]; then
    mount -t binfmt_misc binfmt_misc /proc/sys/fs/binfmt_misc
  fi
  cat "$BINFMT_CONF" > /proc/sys/fs/binfmt_misc/register
fi

# $@ is the env command (including the program). armhf passes
# QEMU_CPU=arm1176 through sudo; arm64 must not, or the aarch64
# interpreter is asked to emulate an ARMv6 CPU.
chroot_env() {
  if [ "$ARCH" = "armhf" ]; then
    chroot "$R" env \
      SUDO_USER=pi \
      DEBIAN_FRONTEND=noninteractive \
      LC_ALL=C.UTF-8 \
      QEMU_CPU=arm1176 \
      "$@"
  else
    chroot "$R" env \
      SUDO_USER=pi \
      DEBIAN_FRONTEND=noninteractive \
      LC_ALL=C.UTF-8 \
      "$@"
  fi
}

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
apt_files=()
for f in "$R"/etc/apt/sources.list.d/*.sources "$R"/etc/apt/sources.list; do
  if [ -f "$f" ]; then
    apt_files+=("$f")
  fi
done
if [ "${#apt_files[@]}" -eq 0 ]; then
  echo "no apt sources in the base image" >&2
  exit 1
fi
sed -i \
  -e 's|http://raspbian.raspberrypi.com/raspbian/|https://archive.raspbian.org/raspbian/|' \
  -e 's|http://archive.raspberrypi.com/debian/|https://archive.raspberrypi.com/debian/|' \
  -e 's|http://deb.debian.org/|https://deb.debian.org/|' \
  -e 's|http://security.debian.org/|https://security.debian.org/|' \
  "${apt_files[@]}"

# Firmware tree for this install only. Removed before the image is sealed.
cp -a "$SRC" "$R/opt/wqm-1-src"
chown -R 0:0 "$R/opt/wqm-1-src"
chroot "$R" usermod -s /bin/bash pi
chroot "$R" apt-get update
# "Raspberry Pi" only selects the direct-header package path. It is not a
# model, and setup.sh does not write it down.
chroot_env \
  WQM1_BOARD_MODEL="Raspberry Pi" \
  bash -c 'cd /opt/wqm-1-src && bash ./setup.sh'
# iptables deletes a legacy captive NAT rule. setup.sh installs it.
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
chroot_env \
  bash -c 'cd /opt/wqm-1-src && bash ./setup.sh --golden'

# dpkg's format string. Single quotes so bash does not expand it.
# shellcheck disable=SC2016
chroot "$R" dpkg-query -W -f='${Package}\t${Version}\t${Architecture}\n' > "$OUT.packages-dpkg.tsv"
py_local="$(chroot "$R" python3 -c 'import sys; print("/usr/local/lib/python%d.%d/dist-packages" % sys.version_info[:2])')"
chroot "$R" python3 -m pip list --path "$py_local" --format=freeze \
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

# 600000 x 4 KiB rootfs (~2.3 GiB) is what the armhf card fits, and the
# image is ~3.0 GB and grows on first boot. The arm64 root is larger;
# resize2fs -P is the smallest size that filesystem will accept.
NB=600000
e2fsck -fy "$L2"
MIN_BLOCKS="$(resize2fs -P "$L2" 2>/dev/null | awk -F': ' '/minimum size/ {print $2}')"
if [ -n "${MIN_BLOCKS:-}" ] && [ "$MIN_BLOCKS" -gt "$NB" ]; then
  NB=$((MIN_BLOCKS + 16384))
fi
echo "rootfs blocks: $NB (minimum ${MIN_BLOCKS:-unknown})"
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
