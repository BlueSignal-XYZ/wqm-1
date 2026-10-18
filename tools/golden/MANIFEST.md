# WQM-1 golden image

One 32-bit (armhf) Raspberry Pi OS Lite Trixie card for every supported Pi
(Zero W, Zero 2 W, Pi 3, Pi 4). The Pi 5 is not a target; see
[docs/raspberry-pi.md](../../docs/raspberry-pi.md).

The board name is **not** written into the image. `build-golden.sh` runs
`setup.sh` inside a chroot that has no device tree, and passes
`WQM1_BOARD_MODEL=Raspberry Pi` only so the installer takes the
direct-header package path. That string is not stored. On a real boot
`wqm1-firstboot` logs `/proc/device-tree/model`, and the firmware resolves
the profile from the same file.

No Wi-Fi credentials are in `bootfs/` or `overlay/`. Wi-Fi is set later, on
the unit, from the setup hotspot.

## Base image

Raspberry Pi OS Lite (32-bit), Trixie, 2026-10-06:

- File: `2026-10-06-raspios-trixie-armhf-lite.img.xz`
- URL: https://downloads.raspberrypi.org/raspios_lite_armhf/images/raspios_lite_armhf-2026-10-06/2026-10-06-raspios-trixie-armhf-lite.img.xz
- sha256: `2c302771f34c67a0b7ae7aa2e0dcdd3eac660504be59193c011ba79d412e1632`

Decompress it before building. The script wants the raw `.img`.

## Build

On a Debian (or Ubuntu) x86_64 box, as root, with `qemu-user-static`,
`parted`, `mtools`, `zerofree` and `e2fsprogs`:

```bash
sudo bash tools/golden/build-golden.sh \
  /path/to/2026-10-06-raspios-trixie-armhf-lite.img \
  /path/to/this/checkout
```

`QEMU_CPU=arm1176` is set so pip selects ARMv6 wheels. The script rewrites
apt sources to HTTPS for the build and restores them before the image is
sealed. Output defaults to `./wqm1-golden.img` (override with `OUT=`).

An earlier offline build (2026-10-18, firmware `b2c9fcf`) hardcoded
`BOARD_MODEL` to "Raspberry Pi Zero W" because the chroot has no board.
This script does not. A later qemu-user-static run of this script against
the base image above completed with exit 0; that image is not in git, and
it was not booted on a Pi. **Unverified on hardware.** Treat any hash from
the earlier hardcoded-name build as a different tree.

## What the overlay does

| Path | Role |
|---|---|
| `overlay/usr/local/sbin/wqm1-firstboot` | Every boot: unblock Wi-Fi, set the regulatory domain to US, log the device-tree model. First boot of a card: hostname `wqm1-xxxx` from the same serial the firmware uses. |
| `overlay/etc/systemd/system/wqm1-firstboot.service` | Runs that script before the firmware and the setup hotspot. |
| `overlay/etc/cloud/cloud.cfg.d/99_wqm1.cfg` | cloud-init does not configure the network and does not set a hostname. |
| `overlay/etc/ssh/sshd_config.d/10-wqm1.conf` | If SSH is ever enabled, it is key-only. |
| `bootfs/user-data`, `meta-data`, `network-config` | cloud-init identity of a never-booted card. No users, no Wi-Fi. |
| `bootfs/config.txt`, `cmdline.txt` | The boot files the 2026-10-18 image carried (UART, I2C, SPI, 1-Wire, `disable-bt`). `setup.sh` applies the same overlays on a normal install. |
