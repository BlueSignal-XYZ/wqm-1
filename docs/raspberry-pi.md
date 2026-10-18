# Raspberry Pi hosts

Firmware 2.4.0 is not tied to the Pi Zero 2 W. Two master cards, both
Raspberry Pi OS Lite (Trixie), and neither card has a board name baked in.
The firmware reads the model at boot from `/proc/device-tree/model` (and, if
that file is missing, from `/proc/cpuinfo`). Build steps:
[`tools/golden/`](../tools/golden/MANIFEST.md).

## Which card

| Pi | Card |
|---|---|
| Zero W, Zero WH | **32-bit (armhf) only.** The 64-bit card has no kernel this board can boot. |
| Zero 2 W | 32-bit (armhf) or 64-bit (arm64) |
| 3B, 3B+, 3A+ | 32-bit (armhf) or 64-bit (arm64) |
| 4 Model B, and the 400 | 32-bit (armhf) or 64-bit (arm64) |
| 5, 500 | **64-bit (arm64) only.** The 32-bit card cannot run the Pi 5 kernel. |

The 32-bit card is the one a Zero W must use, and that build is the same
card as before: same overlay, same hotspot, same first boot. The 64-bit
card is that overlay on the official arm64 Lite image. If a 64-bit userspace
does come up on a Zero W, first boot logs an error and the firmware logs
the same. A 32-bit userspace on a Pi 5 logs the mirror of that error.

**Nothing in this page was measured on a WQM-1 with a Pi 3, Pi 4, Pi 5 or a
cable.** The Zero 2 W remains the reference host that field units already
run. Rows below say **unverified** wherever the number or the behaviour has
not been seen on our hardware.

## What each profile does

| Pi | Profile id | Boot | Header stack | This firmware |
|---|---|---|---|---|
| Zero W, Zero WH | `rpi-zero-w` | armhf card: `kernel.img`, 32-bit ARMv6 | BCM2835, `RPi.GPIO`, same overlays as the Zero 2 W | Supported in software. **Hardware unverified** on a cabled WQM-1. |
| Zero 2 W | `rpi-zero-2w` | Either card. On armhf, `kernel7.img` (32-bit) unless `arm_64bit=1` | Reference. Profile id, GPIO backend and pin map are unchanged. | No intended behaviour change. **This card is unverified** against a field unit. |
| 3B, 3B+, 3A+ | `rpi-3` | Either card. On armhf, `kernel7.img`, 32-bit | Same 40-pin BCM numbering and `RPi.GPIO` | Supported in software. **Hardware unverified.** |
| 4 Model B, and the 400 | `rpi-4` | Either card. The armhf image boots a 64-bit `kernel8.img` with a 32-bit userland (`arm_64bit` defaults to 1). From kernel 6.18 Raspberry Pi Ltd does not ship a 32-bit kernel for the BCM2711. | Same 40-pin BCM numbering and `RPi.GPIO` | Supported in software. **Hardware unverified.** The 400 is the same profile because the header GPIO matches; it is not a cabled target we have checked. |
| 5, 500 | `rpi-5` | arm64 card. The BCM2712 cannot run a 32-bit kernel. `kernel_2712.img` is 16K pages and does not run armhf userland. | The header is the RP1. `lgpio` opens the gpiochip labelled `pinctrl-rp1` (gpiochip0 on Trixie, gpiochip4 on older kernels) and drives line N as BCM N. `RPi.GPIO` is not called. I2C, SPI and 1-Wire stay on the kernel overlays `setup.sh` already writes. | Supported in software. **Hardware unverified** on a cabled WQM-1. 5 V rail caution below. |

A Pi we do not name (Pi 2, a Compute Module, a model string that only says
"Raspberry Pi") still resolves to `rpi-zero-2w`. That fallback is deliberate:
an over-the-air update must not turn a working analog unit into a
digital-only one because a model string changed shape.

The kernel notes are from Raspberry Pi Ltd's boot and kernel documentation,
not from a boot on our bench.

## Setup hotspot

The setup AP is NetworkManager's hotspot on **`wlan0`**, and it is never up
at the same time as a station link. `brcmfmac` is what Raspberry Pi OS binds
to the onboard radio of each supported Pi. The chip is not the same part
(Zero W and Pi 3B are an older Broadcom SDIO radio; Pi 3B+, Pi 3A+ and Pi 4
are a CYW43455; the Zero 2 W is its own module on the same driver). The
firmware never branches on the chip. `nmcli` is asked for an AP on `wlan0`
either way.

`bluesignal-ap-fallback.service` raises that hotspot. It does not wait on
`bluesignal-wqm` and it does not wait on `network-online.target`. An
unconfigured unit has no connection for that target to report, and this
service is what creates the network. The script unblocks the radio (a Pi
leaves Wi-Fi soft-blocked until a regulatory domain is set, so `wlan0`
stays down), skips the station grace period when no Wi-Fi profile is saved,
and retries the hotspot if the interface is still unavailable. A failed
sensor init must not stop it. The golden image's `wqm1-firstboot` does the
radio unblock as well; the fallback does not depend on that script, because
`setup.sh` does not install it.

**Pi 3, Pi 4 and Pi 5 hotspot behaviour is unverified on hardware.** The
Zero 2 W path is the one field units already use.

The captive-portal redirect is an `iptables` NAT rule. Trixie Lite does not
install `iptables` (it ships nftables). `setup.sh` installs it. Without that
package the rule fails open and the phone does not open the setup page by
itself.

## The 40-pin cable

Pi 3 and Pi 4 do not sit on the Zero mounting holes. They connect to P2, the
board's 2×20 2.54 mm header, with an extension cable. Zero and Zero 2 W can
still stack; the electrical rules are the same.

Straight-through means pin 1 to pin 1 and pin 40 to pin 40.

- The cable is **1:1**. It is not a crossover, and neither connector is
  rotated 180°.
- The ribbon's pin-1 stripe (the coloured edge) goes to **pin 1 at both
  ends**.
- On the Pi, pin 1 is the **square pad** at one end of the header. On a Zero
  or Zero 2 W that end is the one by the SD card. On a Pi 3 or Pi 4 it is the
  same end of the 40-pin header, the corner the silkscreen marks.
- On the WQM-1, P2 pin 1 is **3.3 V** and pin 2 is **5 V**. That is the
  standard Pi numbering, from the Fin_3 schematic (`BST` power sheet,
  2026-03-24, connector P2). The stripe meets that pin.

Which way the Pi faces: the header pins point up out of the Pi's component
side, and the cable plugs onto those pins. Stacked, the Pi is under the
board with its component side toward the underside of the WQM-1 and pin 1 on
pin 1 — that is the orientation the header was drawn for. With a cable the
Pi sits beside the board instead of under it. It can be turned in the plane
(USB sockets toward whichever edge the enclosure allows) **as long as both
connectors stay pin-1 to pin-1**. Turning one connector around puts 5 V onto
a ground or a GPIO and will damage the Pi.

Which enclosure edge the USB sockets should point at is **not in the
schematic. Unverified.** Do not infer it from a photo that is not in this
repo.

## 5 V power

The 24 V input feeds U16, an **LMR51450**, which makes the 5 V rail. The
Fin_3 schematic annotates that rail **"+5V output 4A max"**. The BOM
describes the same IC as a **4.5 A** buck. The rail is shared: Pi header
pins 2 and 4, the AMS1117 3.3 V regulator (LoRa, GPS, LEDs), and the
turbidity probe's 5 V. The 24 V input fuse F6 is **1.5 A** (the whole board,
relays included).

**None of those currents has been metered on a WQM-1. Unverified.** The 4 A
figure is a schematic note, not a measurement. Do not connect a USB supply
on the Pi at the same time as the 24 V input; the two meet on this rail.

Raspberry Pi Ltd's published figures (typical bare-board active current, and
the PSU capacity they recommend) are quoted so a reader can compare them
with that 4 A note. They are **not** measurements on a WQM-1, and they
include no HAT:

| Pi | Typical bare-board active | Recommended PSU | Against the 4 A shared rail |
|---|---|---|---|
| Zero W | 150 mA | 1.2 A | Under the note on paper. **Unverified** on this board. |
| Zero 2 W | 350 mA | 2 A | Under the note on paper. **Unverified** as a metered WQM-1 load. |
| 3B | 400 mA | 2.5 A | Under the note on paper. **Unverified.** |
| 3A+ | 350 mA | 2.5 A | Under the note on paper. **Unverified.** |
| 3B+ | 500 mA | 2.5 A | Under the note on paper. **Unverified.** |
| 4B | 600 mA | 3.0 A | The recommended PSU is under 4 A, but the rail is shared and boot / Wi-Fi peaks are **unverified**. Do not hang USB devices off the Pi; the budget above does not include them. |
| 5 | 800 mA | 5.0 A | The recommended PSU is **above** the schematic's 4 A note once USB loads are included. A headless unit with nothing on the Pi's USB ports is nearer the 800 mA column, which is under the note on paper, before the shared 3.3 V rail and the turbidity probe. **Not metered on a WQM-1. Unverified.** Do not hang USB devices off the Pi. |

A WQM-1 install is headless: no HDMI, keyboard or mouse. Expect something
nearer the bare-board column than a desktop. That sentence is a reading of
the table, not a measurement.

The Pi 5 row is a caution, not a bench result. Software on the arm64 card
will drive the header. That does not mean the shared 5 V rail has been shown
to hold a Pi 5. If a later meter reading shows the rail sagging, treat the
Pi 5 as unsupported until the power path is changed.

Source for the Pi columns: Raspberry Pi Ltd, "Power supply" / typical power
requirements (their documentation, not a WQM-1 bench log). Source for the
4 A note and the 4.5 A IC line: `hardware/fab/PCBA_Fin_3.pdf` and
`hardware/bom/BOM WQM1.pdf`, both dated 2026-03-24.
