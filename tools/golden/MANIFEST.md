# WQM-1 golden image

Two cards from one script and one overlay. Both are Raspberry Pi OS Lite
(Trixie). The 32-bit (armhf) card is the only one a Zero W can boot. The
64-bit (arm64) card is for the Zero 2 W, Pi 3, Pi 4 and Pi 5. Which board
gets which card: [docs/raspberry-pi.md](../../docs/raspberry-pi.md).

The board name is **not** written into the image. `build-golden.sh` runs
`setup.sh` inside a chroot that has no device tree, and passes
`WQM1_BOARD_MODEL=Raspberry Pi` only so the installer takes the
direct-header package path. That string is not stored. On a real boot
`wqm1-firstboot` logs `/proc/device-tree/model`, and the firmware resolves
the profile from the same file. A 64-bit userspace on a Zero W, and a
32-bit userspace on a Pi 5, are logged as the wrong card.

No Wi-Fi credentials are in `bootfs/` or `overlay/`. Wi-Fi is set later, on
the unit, from the setup hotspot. The overlay is not copied per arch.

`setup.sh` installs `hostapd` and `dnsmasq` into the image (dnsmasq may
already be present; the package install is idempotent). The distro
`hostapd.service` and `dnsmasq.service` are masked. The setup AP uses
`wqm1-setup-hostapd.service` and `wqm1-setup-dnsmasq.service`, which are
copied in and not enabled. `netctl` starts them only while the hotspot
is up.

## Base images

Both dated 2026-10-06. The sha256 is the official sidecar
(`*.img.xz.sha256` on downloads.raspberrypi.com), checked before the build.

Raspberry Pi OS Lite (32-bit), Trixie:

- File: `2026-10-06-raspios-trixie-armhf-lite.img.xz`
- URL: https://downloads.raspberrypi.com/raspios_lite_armhf/images/raspios_lite_armhf-2026-10-06/2026-10-06-raspios-trixie-armhf-lite.img.xz
- sha256: `2c302771f34c67a0b7ae7aa2e0dcdd3eac660504be59193c011ba79d412e1632`

Raspberry Pi OS Lite (64-bit), Trixie:

- File: `2026-10-06-raspios-trixie-arm64-lite.img.xz`
- URL: https://downloads.raspberrypi.com/raspios_lite_arm64/images/raspios_lite_arm64-2026-10-06/2026-10-06-raspios-trixie-arm64-lite.img.xz
- sha256: `483db18a48da399b5b7022ffae9a07bc6d99daab30e593aedac09b69ff422843`

Decompress before building. The script wants the raw `.img`.

## Build

On a Debian (or Ubuntu) x86_64 box, as root, with `qemu-user-static`,
`parted`, `mtools`, `zerofree` and `e2fsprogs`:

```bash
sudo bash tools/golden/build-golden.sh \
  /path/to/2026-10-06-raspios-trixie-armhf-lite.img \
  /path/to/this/checkout

sudo bash tools/golden/build-golden.sh --arch arm64 \
  /path/to/2026-10-06-raspios-trixie-arm64-lite.img \
  /path/to/this/checkout
```

`QEMU_CPU=arm1176` is set for the armhf build so pip selects ARMv6 wheels.
The arm64 build does not set it (`qemu-aarch64` binfmt). The script rewrites
apt sources to HTTPS for the build and restores them before the image is
sealed. Output defaults to `./wqm1-golden.img` for armhf and
`./wqm1-golden-arm64.img` for arm64 (override with `OUT=`).

An earlier offline build (2026-10-18, firmware `b2c9fcf`) hardcoded
`BOARD_MODEL` to "Raspberry Pi Zero W" because the chroot has no board.
This script does not. Images built by this script are not in git, and they
have not been booted on a Pi. **Unverified on hardware.** Treat any hash
from the earlier hardcoded-name build as a different tree.

## What the overlay does

| Path | Role |
|---|---|
| `overlay/usr/local/sbin/wqm1-firstboot` | Every boot: unblock Wi-Fi, set the regulatory domain to US, log the device-tree model, record which Pi this card was commissioned on, and set hostname `wqm1-xxxx` from the same id the firmware uses for the hotspot SSID. A card moved to another Pi is renamed. A power pull leaves a step marker; the next boot resumes that step. |
| `overlay/usr/local/sbin/wqm1-boot-stage` | Writes `BOOT: stage=boot` on the FAT status file before the hotspot. Replaces a stale `COMMISSION` line. |
| `overlay/usr/local/lib/wqm1-boot.sh` | Status file, step markers, and the card-binding file shared with `src/utils/cardmove.py`. |
| `overlay/usr/local/sbin/wqm1-journal-preserve` | Copies the previous boot's journal to `/var/lib/wqm1/journal-previous/previous.journal` before journald starts. |
| `overlay/etc/systemd/system/wqm1-firstboot.service` | Runs first-boot after NetworkManager and after the early boot-stage line, before the firmware and the setup hotspot. |
| `overlay/etc/systemd/system/wqm1-boot-stage.service` | Oneshot before NetworkManager. |
| `overlay/etc/systemd/journald.conf.d/wqm1.conf` | Persistent journal, 32M cap, flush every 30s. |
| `overlay/etc/systemd/system/var-log.mount` | tmpfs for `/var/log`. The journal bind-mount is the copy that stays on the card. |
| `overlay/etc/systemd/system/var-log-journal.mount` | Bind `/var/lib/wqm1/journal` onto `/var/log/journal`. |
| `overlay/etc/systemd/system/var-tmp.mount`, `var-cache.mount` | tmpfs for the other chatty paths. |
| `rootfs-resilience.sh` | `fsck.repair=yes`, root `noatime` and `errors=remount-ro`, `tune2fs -c 1 -C 1 -i 0 -e remount-ro`. Does not enable an overlayfs root: a tmpfs upper layer would drop OTA and identity, and a persistent upper layer breaks the OTA symlink rename. |
| `overlay/etc/cloud/cloud.cfg.d/99_wqm1.cfg` | cloud-init does not configure the network and does not set a hostname. |
| `overlay/etc/ssh/sshd_config.d/10-wqm1.conf` | If SSH is ever enabled, it is key-only. |
| `bootfs/user-data`, `meta-data`, `network-config` | cloud-init identity of a never-booted card. No users, no Wi-Fi. |
| `bootfs/config.txt`, `cmdline.txt` | The boot files the 2026-10-18 image carried (UART, I2C, SPI, 1-Wire, `disable-bt`). `setup.sh` applies the same overlays on a normal install. |
