# Host Platforms

The WQM-1 firmware runs on more than one Linux host board. The reference
platform is the **Raspberry Pi Zero 2 W** with the WQM-1 HAT. The same
full stack runs on the **Orange Pi Zero 3W** (Allwinner A733) through the
kernel's gpiochips, and the firmware runs **digital-first** on the
**Arduino UNO Q** and the upcoming **Arduino VENTUNO Q**.

## Why boards differ

On a Raspberry Pi, Linux owns the 40-pin header directly: the ADS1115 ADC is
on kernel I2C (`smbus2`), the DS18B20 on `w1-gpio`, the SX1262 LoRa radio on
`spidev`, and relays/LEDs/fan on `RPi.GPIO`.

The Orange Pi Zero 3W copies the Pi Zero's form factor and its 40-pin header
layout — power and ground in the Pi's positions, I2C on pins 3/5, SPI on
pins 19/21/23/24, UART on pins 8/10 — so the HAT seats on it unchanged and
every bus the HAT uses is a kernel bus there too. What differs is the GPIO
layer: the lines are Allwinner pins (`PB0`, `PL2`, …) on two gpiochips, and
`RPi.GPIO` only knows Broadcom silicon. The firmware drives them through
`lgpio` instead, with each of the HAT's BCM-numbered nets translated to the
host's (chip, line) by `src/platform_support/hostpins.py`.

The Arduino UNO Q (Qualcomm Dragonwing QRB2210 + STM32U585) and VENTUNO Q
(Dragonwing IQ-8275 + STM32H5) are dual-brain boards: Debian runs on the
Qualcomm MPU, but the Arduino headers — analog pins, Qwiic I2C, SPI, GPIO —
belong to the **STM32 MCU**, reachable from Linux only through Arduino's
Bridge RPC. The Qualcomm's own exposed lines are camera/audio-dedicated and
reserved in the device tree; they are not general-purpose Linux GPIO.

So on the Arduino Q family the firmware cannot reach header peripherals, but
everything that speaks USB or the network works unchanged.

## Support matrix

| Capability | Raspberry Pi Zero 2 W | Orange Pi Zero 3W | Arduino UNO Q / VENTUNO Q |
|---|---|---|---|
| RS485 Modbus probes (pH, EC, TDS, salinity, temp, chlorine, ORP) via USB adapter | ✅ | ✅ | ✅ |
| Cloud sync, heartbeat, remote config, commands (Wi-Fi/Ethernet HTTP) | ✅ | ✅ | ✅ |
| OTA updates (separate agent service) | ✅ | ✅ | ✅ |
| Service Window (installer web UI) | ✅ | ✅ | ✅ |
| GPS | ✅ UART header (+ EXTINT power-cycle) | ✅ UART0 header (+ EXTINT) — see the console trap below | ✅ USB GPS (no EXTINT) |
| Analog probes via ADS1115 (BNC pH/TDS/turbidity) | ✅ | ✅ TWI0 | ❌ needs Bridge companion |
| DS18B20 temperature (1-Wire) | ✅ | ✅ w1-gpio overlay on PB4 | ❌ needs Bridge companion (5-in-1 RS485 probe covers temp) |
| LoRaWAN (SX1262 on SPI) | ✅ | ✅ SPI3 | ❌ needs Bridge companion |
| Relays / dosing control | ✅ | ✅ (relay 3's pin read off the board, see below) | ❌ needs Bridge companion |
| Flow pulse meter (lgpio edge count) | ✅ | ✅ | ❌ |
| Status LEDs, fan, hardware watchdog | ✅ | ✅ (LED2's pin read off the board) | ❌ (systemd watchdog still active) |
| Verified on hardware | ✅ field units | **❌ not yet — bench pass pending** | ✅ bench |

**Digital-first** means a headerless host still delivers the full measurement
parameter set: the Honde RS485 probes (5-in-1 pH/EC/TDS/salinity/temp,
chlorine, digital ORP) connect through the RS485→USB adapter and cover every
channel the analog stack measures — plus chlorine, conductivity, and salinity,
which the analog stack never had.

## How detection works

- `src/platform_support/board.py` reads `/proc/device-tree/model` at startup
  and resolves a `BoardProfile`. The `board` config setting (default `auto`)
  can pin a profile explicitly (`rpi-zero-2w`, `orangepi-zero-3w`,
  `arduino-uno-q`, `arduino-ventuno-q`, `generic-linux`).
- The Orange Pi match is on the compacted model string (`orangepizero3w`),
  because Orange Pi images have printed the same board as "OrangePi Zero3",
  "Orange Pi Zero 3" and "orangepi-zero3" across releases. **Other Orange Pi
  boards are deliberately not claimed** — a different board has a different
  pin table, and driving the Zero 3W's lines on it would energise the wrong
  pins.
- Unrecognized or missing model strings fall back to the **Raspberry Pi**
  profile on purpose: every field unit today is a Pi, and an OTA update must
  never demote a working analog deployment to digital-only because a model
  string changed shape.
- `main.py` calls `set_active_board()` with the resolved profile, then gates
  hardware construction on `profile.has_direct_headers`: relays, LEDs, fan,
  hardware watchdog, ADS1115 + analog sensors, the pulse flow meter and the
  SX1262/LoRaWAN stack are skipped on headerless boards. RS485, GPS, cloud
  sync, and the Service Window are wired unconditionally.
- Every driver drives its lines through `src/platform_support/gpio.py`, one
  facade with two backends: `rpi` (RPi.GPIO for setup/output/input plus
  lgpio for edge alerts — exactly the calls the drivers made before the
  facade existed, so a field unit sees no change) and `gpiochip` (lgpio on
  the kernel's gpiochips, BCM numbers translated through the host map).
- The hardware driver modules import their Pi-only libraries defensively,
  so the process starts cleanly on hosts where `RPi.GPIO`/`smbus2`/`spidev`
  are not installed, and refuse at *construction* with a message naming
  what is missing.
- Dependencies are split: `requirements.txt` is universal;
  `requirements-rpi.txt` holds the Pi drivers (installed by `setup.sh` on a
  Raspberry Pi); `requirements-gpiochip.txt` holds the Orange Pi set — the
  same `smbus2`/`spidev`/`w1thermsensor` plus `lgpio`, and no `RPi.GPIO`.
- The heartbeat reports the profile id as `hostBoard`, stored by the cloud at
  `devices/{id}/hardware/hostBoard` beside `piSerial`.

## Orange Pi Zero 3W

**Status: the firmware, installer and diagnostics are built and tested
against the published pinout; no Orange Pi Zero 3W has run this firmware on
a bench yet.** Everything below the pin table is what the bench pass has to
confirm. Do not list the board on a customer-facing page until it has.

### Why it works at all

The HAT was designed for the Pi header, so every net in the firmware is a
Pi BCM number. The physical pin a BCM number lands on is fixed by the
header, and the Orange Pi Zero 3W copies that header. Compatibility is
therefore one table — physical pin → Allwinner pin — plus a resolver from
an Allwinner pin name to a kernel (chip, line):

| HAT net | BCM | Header pin | Orange Pi Zero 3W pin | Source |
|---|---|---|---|---|
| I2C SDA (ADS1115) | 2 | 3 | PB3 — TWI0_SDA | published |
| I2C SCL (ADS1115) | 3 | 5 | PB2 — TWI0_SCK | published |
| DS18B20 1-Wire | 4 | 7 | PB4 | published |
| GPS RX (host TX) | 14 | 8 | PB9 — UART0_TX | published |
| GPS TX (host RX) | 15 | 10 | PB10 — UART0_RX | published |
| Relay 1 | 17 | 11 | PB0 | published |
| LORA_RST | 18 | 12 | **read off the board** | `gpio readall` |
| Relay 2 | 27 | 13 | PB1 | published |
| Relay 3 | 22 | 15 | **read off the board** | `gpio readall` |
| Relay 4 | 23 | 16 | PL2 (R_PIO, second gpiochip) | published |
| LED1 heartbeat | 24 | 18 | PL3 (R_PIO) | published |
| LoRa MOSI | 10 | 19 | PE2 — SPI3_MOSI | published |
| LoRa MISO | 9 | 21 | PE3 — SPI3_MISO | published |
| LED2 LoRa TX | 25 | 22 | **read off the board** | `gpio readall` |
| LoRa SCLK | 11 | 23 | PE1 — SPI3_CLK | published |
| LoRa CS | 8 | 24 | PE0 — SPI3_CS0 | published |
| Expansion IO7 | 7 | 26 | PE4 — SPI3_CS1 | published |
| Expansion IO0 / IO1 | 0 / 1 | 27 / 28 | read off the board | `gpio readall` |
| ADS1115 ALERT/RDY (unused) | 5 | 29 | read off the board | `gpio readall` |
| Expansion IO6 | 6 | 31 | read off the board | `gpio readall` |
| LED3 GPS fix | 12 | 32 | PD1 | published |
| LED4 error | 13 | 33 | PD3 | published |
| GPS EXTINT | 19 | 35 | PB6 | published |
| LORA_DIO1 | 16 | 36 | PD2 | published |
| Flow pulse (default) | 26 | 37 | PD4 | published |
| LORA_BUSY | 20 | 38 | PB8 | published |
| FAN_EN | 21 | 40 | PB7 | published |

"Published" is Orange Pi's own pinout for the board (orangepi.org product
page and the vendor pinout tables it links). The published text names the
I2C/SPI/UART/PWM pins and leaves the plain-GPIO pins out; **the firmware
ships `None` for those and refuses to drive a net it cannot place** rather
than guess from a different Orange Pi model. `scripts/host-pins.py` fills
them from the board's own `gpio readall` (wiringOP, preinstalled on every
Orange Pi image) into `/etc/bluesignal/host-pins.yaml`; `setup.sh` runs it
automatically when `gpio` is on the path, and `diagnostics.sh` reports any
net still unresolved as a FAIL. A value the board reports that contradicts
the published table is printed as a CONFLICT and the board's value is
taken — the board is the authority, but a conflict means one source is
wrong, so look at it.

Resolution from an Allwinner pin name to a kernel line tries, in order: the
kernel's own `gpio-line-names` (through lgpio), the sysfs chip table
(`/sys/class/gpio/gpiochip*/base`), and last the sunxi convention (PA–PK on
`gpiochip0` from base 0, PL/PM on `gpiochip1` from base 352) — that last one
is logged as unverified when it is used.

Bus numbers the firmware assumes, and the override for each in
`host-pins.yaml`: `/dev/i2c-0` for TWI0 (`i2c_bus`), `/dev/spidev3.0` for
SPI3 (`spi_bus`, `spi_device`), `/dev/ttyS0` for UART0 (`gps_port`). The BSP
usually numbers the nodes after the controller, not always; `i2cdetect -l`
and `ls /dev/spidev* /dev/ttyS*` after the first reboot are the check.

### Setting up an Orange Pi Zero 3W

1. Flash Orange Pi's **Debian** image for the Zero 3W (Ubuntu also works;
   Armbian support for the A733 did not exist at the time of writing) and
   get the board on the network. Seat the HAT; power it from the HAT's 24 V
   terminal as on the Pi (the Type-C 5 V input runs the board, LoRa and GPS
   only).
2. Copy the firmware release to the board and run `sudo bash setup.sh`. The
   script detects the board, installs `requirements-gpiochip.txt` (builds
   `lgpio` from source — `swig` and `python3-dev` are installed first),
   creates the `gpio`/`i2c`/`spi`/`dialout` groups the service unit joins
   and the udev rules that hand the bus nodes to them (Orange Pi's Debian
   ships neither), enables the TWI0, SPI3-spidev and w1-gpio overlays in
   `/boot/orangepiEnv.txt` **only where a matching `.dtbo` exists on the
   image** (it prints what it found; anything it could not find is enabled
   by hand in `orangepi-config → System → Hardware`), sets
   `console=display` and masks `serial-getty@ttyS0` so UART0 is the GPS's,
   and runs `gpio readall` through `scripts/host-pins.py`.
3. Reboot, then `sudo bash /opt/bluesignal/current/scripts/diagnostics.sh`.
   It checks the Orange Pi's nodes (`/dev/i2c-0`, `/dev/spidev3.0`,
   `/dev/ttyS0`, the w1 bus) as the service user, and prints a `Pins:` row
   that must read PASS before the service can drive relays, LEDs or LoRa.
4. Commission through the Service Window exactly as on a Pi.

### Traps the bench pass must clear, in order

1. **U-Boot's console is UART0, and the GPS transmits into it.** U-Boot
   aborts autoboot on any byte received on its console during the boot
   delay; a GPS streaming NMEA into pin 10 may hold the board at the U-Boot
   prompt forever. `setup.sh` moves the *kernel* console off UART0; it
   cannot change U-Boot's. If the board does not reach Linux with the GPS
   connected, set `bootdelay=0` (or `-2`) in the U-Boot environment
   (`fw_setenv`, or the U-Boot shell once), or power the GPS from a relay
   after boot. Confirm one way or the other and record it here.
2. **The five unpublished pins.** Run `gpio readall` on the board, feed it to
   `scripts/host-pins.py --from-readall -`, and check the `Pins:` diagnostic
   row. Then update `ORANGEPI_ZERO_3W_PHYSICAL_TO_SOC` in
   `src/platform_support/hostpins.py` with the values the board reported so
   the next unit needs no override file.
3. **Overlay names and bus numbers.** Confirm the `.dtbo` names `setup.sh`
   enabled and that `/dev/i2c-0`, `/dev/spidev3.0` and `/dev/ttyS0` are the
   nodes that appear; record any difference in `host-pins.yaml` and in this
   document.
4. **`lgpio` builds.** pip builds it from source; if the build fails on this
   image, build the `lg` library from source (abyz.me.uk/lg) and `pip
   install lgpio` again.
5. **The R_PIO chip.** Relay 4 (PL2) and LED1 (PL3) are on the second
   gpiochip. `cat /sys/class/gpio/gpiochip*/base` tells you where the kernel
   put it; the resolver reads that file, and logs "unverified" only when it
   had to assume 352.
6. **Acceptance**, same as a blue-board Pi unit: `diagnostics.sh` zero FAIL,
   first reading in Cloud, each relay clicks from the Service Window, LED
   startup sweep, fan toggles at 60/55 °C, LoRa join, a 5-gallon bucket
   test on the pulse meter.

### Identity on an Allwinner host

The arm64 kernel prints no `Serial` line in `/proc/cpuinfo`.
`utils/identity.py` falls back to `/proc/device-tree/serial-number` (set by
U-Boot from the SoC's fused ID) and then the SID eFuse via nvmem, and logs
which source it used. Under label-as-identity (commissioning plan, PR 1)
the printed label wins anyway; the hardware serial is reported as
`piSerial` in the heartbeat, which keeps its name because the cloud field
already has it.

## Setting up an Arduino UNO Q

1. Flash/boot the board's Debian image and get it on the network.
2. Copy the firmware release to the board and run `sudo bash setup.sh` —
   the script detects the non-Pi host, skips boot overlays and Pi packages,
   and installs universal Python dependencies only.
3. Plug the RS485→USB adapter (probes powered from the 12 V rail as usual)
   and, optionally, a USB GPS.
4. In `/etc/bluesignal/config.yaml`, enable the RS485 probes
   (`rs485_multi_enabled`, `rs485_chlorine_enabled`, `rs485_orp_enabled`) and
   point `rs485_port` at the adapter (usually `/dev/ttyUSB0`). Leave
   `board: auto` — detection recognizes the Qualcomm device tree.
5. Commission through the Service Window as on a Pi. LoRa/relay/analog pages
   simply report those subsystems as unavailable on this host.

## Future work: Bridge companion sketch

Header peripherals on the Arduino Q family (analog probes, LoRa module,
relays, LEDs) require a companion sketch running on the STM32 MCU, exposed to
Linux over Arduino Bridge RPC, with a firmware-side driver that mimics the
existing driver interfaces. That work is scoped out of the current release;
this document and `platform_support/board.py` are the anchor points for it.
