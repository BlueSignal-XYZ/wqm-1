# Host Platforms

The WQM-1 firmware runs on more than one Linux host board. The reference
platform is the **Raspberry Pi Zero 2 W** with the WQM-1 HAT; the firmware
also runs **digital-first** on the **Arduino UNO Q** and the upcoming
**Arduino VENTUNO Q**.

## Why boards differ

On a Raspberry Pi, Linux owns the 40-pin header directly: the ADS1115 ADC is
on kernel I2C (`smbus2`), the DS18B20 on `w1-gpio`, the SX1262 LoRa radio on
`spidev`, and relays/LEDs/fan on `RPi.GPIO`.

The Arduino UNO Q (Qualcomm Dragonwing QRB2210 + STM32U585) and VENTUNO Q
(Dragonwing IQ-8275 + STM32H5) are dual-brain boards: Debian runs on the
Qualcomm MPU, but the Arduino headers — analog pins, Qwiic I2C, SPI, GPIO —
belong to the **STM32 MCU**, reachable from Linux only through Arduino's
Bridge RPC. The Qualcomm's own exposed lines are camera/audio-dedicated and
reserved in the device tree; they are not general-purpose Linux GPIO.

So on the Arduino Q family the firmware cannot reach header peripherals, but
everything that speaks USB or the network works unchanged.

## Support matrix

| Capability | Raspberry Pi Zero 2 W | Arduino UNO Q / VENTUNO Q |
|---|---|---|
| RS485 Modbus probes (pH, EC, TDS, salinity, temp, chlorine, ORP) via USB adapter | ✅ | ✅ |
| Cloud sync, heartbeat, remote config, commands (Wi-Fi/Ethernet HTTP) | ✅ | ✅ |
| OTA updates (separate agent service) | ✅ | ✅ |
| Service Window (installer web UI) | ✅ | ✅ |
| GPS | ✅ UART header (+ EXTINT power-cycle) | ✅ USB GPS (no EXTINT) |
| Analog probes via ADS1115 (BNC pH/TDS/turbidity/ORP) | ✅ | ❌ needs Bridge companion |
| DS18B20 temperature (1-Wire) | ✅ | ❌ needs Bridge companion (5-in-1 RS485 probe covers temp) |
| LoRaWAN (SX1262 on SPI) | ✅ | ❌ needs Bridge companion |
| Relays / dosing control | ✅ | ❌ needs Bridge companion |
| Status LEDs, fan, hardware watchdog | ✅ | ❌ (systemd watchdog still active) |

**Digital-first** means a headerless host still delivers the full measurement
parameter set: the Honde RS485 probes (5-in-1 pH/EC/TDS/salinity/temp,
chlorine, digital ORP) connect through the RS485→USB adapter and cover every
channel the analog stack measures — plus chlorine, conductivity, and salinity,
which the analog stack never had.

## How detection works

- `src/platform_support/board.py` reads `/proc/device-tree/model` at startup
  and resolves a `BoardProfile`. The `board` config setting (default `auto`)
  can pin a profile explicitly (`rpi-zero-2w`, `arduino-uno-q`,
  `arduino-ventuno-q`, `generic-linux`).
- Unrecognized or missing model strings fall back to the **Raspberry Pi**
  profile on purpose: every field unit today is a Pi, and an OTA update must
  never demote a working analog deployment to digital-only because a model
  string changed shape.
- `main.py` gates hardware construction on `profile.has_direct_headers`:
  relays, LEDs, fan, hardware watchdog, ADS1115 + analog sensors, and the
  SX1262/LoRaWAN stack are skipped on headerless boards. RS485, GPS, cloud
  sync, and the Service Window are wired unconditionally.
- The hardware driver modules (`sensors/ads1115.py`, `control/relay.py`,
  `control/led.py`, `utils/watchdog.py`, `radio/sx1262.py`, `sensors/gps.py`)
  import their Pi-only libraries defensively, so the process starts cleanly
  on hosts where `RPi.GPIO`/`smbus2`/`spidev`/`lgpio` are not installed.
- Dependencies are split: `requirements.txt` is universal;
  `requirements-rpi.txt` holds the Pi-only drivers and is installed by
  `setup.sh` only when the host is a Raspberry Pi.

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
