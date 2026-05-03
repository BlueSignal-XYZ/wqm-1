[![CERN-OHL-S-2.0](https://img.shields.io/badge/Hardware_License-CERN--OHL--S--2.0-blue)](LICENSE-HARDWARE)
[![GPL-3.0](https://img.shields.io/badge/Firmware_License-GPL--3.0-blue)](LICENSE-FIRMWARE)
[![Made with KiCad](https://img.shields.io/badge/Made_with-KiCad-orange)](https://www.kicad.org/)

# WQM-1 — Open Source Water Quality Monitor

> Six-channel water quality monitoring on a Raspberry Pi Zero 2W. Open hardware. Open firmware. Built by [BlueSignal](https://bluesignal.xyz).

## What It Is

The WQM-1 is a Raspberry Pi Zero 2W carrier board (120 × 105 mm) designed for continuous, autonomous water quality monitoring. It combines precision analog sensing with long-range wireless connectivity in a field-deployable package.

**Key hardware:**

- **ADS1115** 16-bit ADC (I²C) — four analog channels
- **SX1262 LoRa radio** — up to 15 km range via LoRaWAN
- **u-blox GPS** — georeferenced readings out of the box
- **LMR51450 buck converter** — wide-input 24 V DC power (screw terminal)
- **4× G5Q-14 optoisolated relays** — control dosing pumps, aerators, and valves

**Monitored parameters:**

- pH
- TDS (Total Dissolved Solids)
- Turbidity
- ORP (Oxidation-Reduction Potential)
- Temperature
- GPS location

**Data pipeline:**

Sensors → ADS1115 (I²C) → Pi Zero 2W → SQLite WAL buffer → LoRaWAN (SX1262, Cayenne LPP, AES-128)

**Supported platforms:**

Tested on Debian Trixie (13), kernel 6.12, Pi Zero 2W (aarch64). `setup.sh` handles Bookworm and Trixie automatically.

## Applications

- **Aquaculture** — real-time pond and tank monitoring
- **Algae control** — early detection of harmful algal blooms
- **Stormwater MS4 compliance** — automated NPDES permit reporting
- **Residential well & cistern monitoring** — peace of mind for private water supplies
- **Environmental research** — low-cost, distributed sensor networks

## Buy or Build

You can build the WQM-1 yourself from the files in this repository, or buy it assembled, tested, and provisioned from [bluesignal.xyz](https://bluesignal.xyz).

The dev kit ships with cloud monitoring via [cloud.bluesignal.xyz](https://cloud.bluesignal.xyz) and optional integration with the [WaterQuality.Trading](https://waterquality.trading) marketplace.

## Repository Layout

```
wqm-1/
├── src/                   # Firmware source (Python)
│   ├── main.py            # Entry point
│   ├── sensors/           # Sensor drivers (ADS1115, GPS, pH, TDS, etc.)
│   ├── radio/             # LoRaWAN + SX1262 driver
│   ├── control/           # Relays, LEDs, automation rules
│   ├── storage/           # SQLite database
│   ├── calibration/       # Sensor calibration
│   └── utils/             # Config, health, identity, watchdog
├── config/                # Example configs (pinmap, policies, config.yaml)
├── hardware/
│   ├── bom/               # Bill of Materials
│   └── fab/               # Gerbers and schematics
├── firmware/              # Pre-built firmware artefacts
├── images/                # README and docs images
├── tests/                 # Test suite
├── systemd/               # systemd service unit files
├── scripts/               # Diagnostics and utility scripts
├── docs/                  # Project documentation
├── setup.sh               # Automated Pi setup script
├── requirements.txt       # Runtime Python dependencies
├── requirements-dev.txt   # Dev/test dependencies
├── pyproject.toml         # Python project metadata & tool config
└── .github/               # CI workflows
```

## Prerequisites

The base **Raspberry Pi OS Lite (Trixie)** image does not ship with `git`. Before cloning this repository on a fresh Pi, install it:

```bash
sudo apt-get update
sudo apt-get install -y git
```

## Getting Started

Full instructions for deploying the WQM-1 firmware onto a Raspberry Pi
Zero 2W with the WQM-1 HAT attached.

### Prerequisites

**Hardware:**

- Raspberry Pi Zero 2W
- WQM-1 HAT (PCBA revision Fin\_3), attached to the Pi's 40-pin GPIO
  header
- microSD card (16 GB or larger recommended)
- USB-C power supply (5 V / 2.5 A minimum)
- A computer with an SD card reader for flashing

**Software (on your computer):**

- [Raspberry Pi Imager](https://www.raspberrypi.com/software/)
- An SSH client (built into macOS and Linux; use
  [PuTTY](https://www.putty.org/) on Windows)

**Network:**

- WiFi network name (SSID) and password — the Pi Zero 2W has built-in
  WiFi

**Optional:**

- A LoRaWAN network server account
  ([The Things Network](https://www.thethingsnetwork.org/) or
  [ChirpStack](https://www.chirpstack.io/)) if you plan to use LoRa
  uplink

---

### Step 1 — Flash Raspberry Pi OS Lite

1. Open **Raspberry Pi Imager** on your computer.
2. Click **Choose Device** → select **Raspberry Pi Zero 2W**.
3. Click **Choose OS** → **Raspberry Pi OS (other)** →
   **Raspberry Pi OS Lite (64-bit)** (Bookworm or later).
   > **Important:** Choose the **Lite** image, not Desktop. The WQM-1
   > runs headless — no monitor or keyboard needed.
4. Insert your microSD card and click **Choose Storage** to select it.
5. Click **Next**, then click **Edit Settings** to pre-configure the OS:
   - **General** tab:
     - Set hostname: `wqm1`
     - Set username: `pi` and choose a strong password
     - Configure wireless LAN: enter your WiFi SSID, password, and
       country code
     - Set locale and timezone
   - **Services** tab:
     - Enable SSH with **password authentication**
6. Click **Save**, then **Yes** to apply OS customisation, then **Yes**
   to write the image.
7. Wait for the write and verification to complete, then eject the card.

---

### Step 2 — First Boot & SSH

1. Insert the microSD card into the Pi Zero 2W (with the WQM-1 HAT
   already attached).
2. Connect the USB-C power supply. The green activity LED will blink
   during boot.
3. Wait **60–90 seconds** for the first boot to finish (the Pi expands
   the filesystem and applies your WiFi/SSH settings).
4. Find the Pi on your network:
   ```bash
   ping wqm1.local
   ```
   If mDNS does not work on your network, check your router's DHCP
   lease table for the Pi's IP address.
5. SSH into the Pi:
   ```bash
   ssh pi@wqm1.local
   ```

---

### Step 3 — Install the Firmware

Once connected via SSH, install Git, clone the repository, and run the
automated setup script:

```bash
sudo apt-get update && sudo apt-get install -y git
git clone https://github.com/bluesignal-xyz/wqm-1.git
cd wqm-1
sudo bash setup.sh
```

The setup script runs **7 stages** automatically:

| Stage | What it does |
|-------|-------------|
| 1/7 | Installs system packages (`i2c-tools`, `python3-pip`, `python3-venv`, `libgpiod2`) |
| 2/7 | Installs Python dependencies from `requirements.txt` |
| 3/7 | Configures `/boot/config.txt` device-tree overlays (I2C, SPI, UART, 1-Wire, disable Bluetooth, reduce GPU memory) |
| 4/7 | Creates directories (`/opt/bluesignal`, `/var/lib/bluesignal`, `/var/log/bluesignal`, `/etc/bluesignal`) |
| 5/7 | Copies firmware source to `/opt/bluesignal/src/` and installs default config to `/etc/bluesignal/config.yaml` |
| 6/7 | Installs and enables the `bluesignal-wqm` systemd service |
| 7/7 | Prints next steps |

---

### Step 4 — Configure

Edit the configuration file that the setup script installed:

```bash
sudo nano /etc/bluesignal/config.yaml
```

At minimum, set your **LoRaWAN AppKey** if you plan to transmit data
over LoRa:

```yaml
app_key: "your-32-character-hex-appkey-here"
```

Where to find your AppKey:

- **TTN Console:** Applications → your app → End Devices → your device
  → Overview → AppKey
- **ChirpStack:** Applications → your app → Devices → your device →
  Keys (OTAA)

Key settings you may want to adjust:

| Field | Default | Description |
|-------|---------|-------------|
| `sensor_read_s` | `60` | How often to read all sensors (seconds) |
| `lora_tx_s` | `300` | How often to transmit via LoRaWAN (seconds) |
| `gps_fix_s` | `600` | How often to attempt a GPS fix (seconds) |
| `gps_fix_timeout_s` | `60` | Max seconds to wait for GPS lock |
| `db_max_rows` | `100000` | Max SQLite rows before automatic rotation |
| `fan_on_temp_c` | `60.0` | CPU temperature threshold to turn fan on |
| `fan_off_temp_c` | `55.0` | CPU temperature threshold to turn fan off |

See [`config/config.yaml.example`](config/config.yaml.example) for the
full list of fields with comments.

---

### Step 5 — Reboot & Verify Hardware

A reboot is **required** for the `/boot/config.txt` overlay changes
(I2C, SPI, UART, 1-Wire) to take effect:

```bash
sudo reboot
```

After the Pi comes back up (~60 seconds), SSH in again and run the
**diagnostics script** to verify all hardware in one shot:

```bash
sudo bash /opt/bluesignal/scripts/diagnostics.sh
```

Example output:

```
=== WQM-1 Hardware Diagnostics ===

[PASS] I2C:     ADS1115 found at 0x48
[PASS] 1-Wire:  DS18B20 28-0300a279f2e8
[PASS] GPS:     NMEA data on /dev/serial0
[PASS] SPI:     /dev/spidev0.0
[PASS] Config:  /etc/bluesignal/config.yaml (valid YAML)
[WARN] LoRaWAN: app_key is default (LoRa will not connect)
[PASS] Policies: /opt/bluesignal/config/policies.yaml (17 rules loaded)
[PASS] Service: bluesignal-wqm enabled (not yet started)
[INFO] Disk:    2.1 GB free on /var/lib/bluesignal
[INFO] CPU:     42.3°C

=== 7 passed, 1 warning(s), 0 failed ===
```

All items should show **PASS**. Warnings (like an unconfigured AppKey)
are non-fatal. Any **FAIL** items need to be resolved before starting
the service — see the [Troubleshooting](#troubleshooting) section below.

You can also verify individual interfaces manually:

| Interface | Command | Expected |
|-----------|---------|----------|
| I2C | `i2cdetect -y 1` | `0x48` (ADS1115) |
| 1-Wire | `ls /sys/bus/w1/devices/` | `28-*` directory |
| GPS | `cat /dev/serial0` | NMEA sentences (`$GNGGA`, `$GNRMC`) |
| SPI | `ls /dev/spidev0.0` | File exists |

---

### Step 6 — Start the Service

Start the WQM-1 firmware:

```bash
sudo systemctl start bluesignal-wqm
```

Check the status:

```bash
sudo systemctl status bluesignal-wqm
```

It should show **`active (running)`**. Watch the live logs:

```bash
journalctl -u bluesignal-wqm -f
```

What you should see in the logs:

- Firmware version and device ID on startup
- Sensor readings every 60 seconds (by default)
- LoRaWAN join attempts (if `app_key` is configured)
- GPS fix attempts every 10 minutes

The service is already **enabled** (from `setup.sh`), so it will
auto-start on every boot. To stop the service:

```bash
sudo systemctl stop bluesignal-wqm
```

---

### Troubleshooting

<details>
<summary><strong><code>i2cdetect</code> shows no devices</strong></summary>

- Make sure the WQM-1 HAT is firmly seated on the GPIO header.
- Verify `/boot/config.txt` (or `/boot/firmware/config.txt` on
  Bookworm) contains `dtparam=i2c_arm=on`.
- Reboot after any `config.txt` changes.

</details>

<details>
<summary><strong>No 1-Wire devices in <code>/sys/bus/w1/devices/</code></strong></summary>

- Verify `dtoverlay=w1-gpio,gpiopin=4` is in `config.txt`.
- Check that the DS18B20 sensor is wired correctly to GPIO 4.
- Reboot after `config.txt` changes.

</details>

<details>
<summary><strong>GPS shows no data on <code>/dev/serial0</code></strong></summary>

- Verify both `enable_uart=1` and `dtoverlay=disable-bt` are in
  `config.txt`. The `disable-bt` overlay frees the PL011 UART for GPS.
- If the firmware logs `GPS UART open failed: [Errno 13] ... Permission denied: '/dev/serial0'`,
  the **serial console is holding the UART**. Check with
  `ls -lL /dev/serial0` — if you see `root tty` mode `0600`, the console
  has it. `setup.sh` disables this automatically; if you upgraded from
  an older install, run:
  ```bash
  sudo systemctl disable --now serial-getty@ttyAMA0.service
  CMDLINE=/boot/firmware/cmdline.txt; [ -f /boot/cmdline.txt ] && CMDLINE=/boot/cmdline.txt
  sudo sed -i -E 's/[[:space:]]+console=(serial0|ttyAMA0)[^[:space:]]*//g' "$CMDLINE"
  sudo reboot
  ```
  After reboot, `/dev/ttyAMA0` should be `root dialout` mode `0660`.
- Reboot after `config.txt` changes.
- The GPS module may need several minutes to acquire a first fix
  outdoors.

</details>

<details>
<summary><strong>Service fails to start</strong></summary>

Check the error:

```bash
journalctl -u bluesignal-wqm -e
```

Common causes:

- **YAML syntax error** in `/etc/bluesignal/config.yaml` — validate
  with:
  ```bash
  python3 -c "import yaml; yaml.safe_load(open('/etc/bluesignal/config.yaml'))"
  ```
- **Missing Python package** — re-run:
  ```bash
  sudo pip3 install --break-system-packages -r /opt/bluesignal/requirements.txt
  ```
- **Permission denied** — ensure directories are owned by `pi`:
  ```bash
  sudo chown -R pi:pi /opt/bluesignal /var/lib/bluesignal /var/log/bluesignal
  ```
- **Permission denied on `/dev/gpiochip0`, `/dev/i2c-1`, `/dev/spidev0.0`,
  or `/var/run/bluesignal`** — the unit is missing
  `SupplementaryGroups=` and/or `RuntimeDirectory=`. Confirm the
  installed unit at `/etc/systemd/system/bluesignal-wqm.service`
  contains:
  ```ini
  SupplementaryGroups=dialout gpio i2c spi
  RuntimeDirectory=bluesignal
  RuntimeDirectoryMode=0755
  ```
  If not, re-run `setup.sh` (it copies the unit from the repo) and then
  `sudo systemctl daemon-reload && sudo systemctl restart bluesignal-wqm`.

</details>

<details>
<summary><strong>LoRaWAN join fails</strong></summary>

- Verify the `app_key` in `config.yaml` matches your network server
  **exactly** (32 hex characters, no spaces or dashes).
- Make sure the LoRa antenna is connected to the WQM-1 HAT's U.FL
  connector.
- Confirm you are within range of a LoRaWAN gateway.
- Check the logs for join-request / join-accept messages.

</details>

<details>
<summary><strong>"No config at /etc/bluesignal/config.yaml, using defaults"</strong></summary>

This is informational, not an error. The firmware runs fine with
built-in defaults. However, you must set `app_key` for LoRaWAN to work.

</details>

For more detail, see [docs/getting-started.md](docs/getting-started.md).

## Contributing

Contributions are welcome! Please read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request.

## License

This project uses a **dual license** model:

| Component | License | File |
|-----------|---------|------|
| Hardware (schematics, PCB, BOM) | [CERN Open Hardware Licence v2 — Strongly Reciprocal](https://ohwr.org/cern_ohl_s_v2.txt) | [LICENSE-HARDWARE](LICENSE-HARDWARE) |
| Firmware (source code, scripts) | [GNU General Public License v3.0](https://www.gnu.org/licenses/gpl-3.0.en.html) | [LICENSE-FIRMWARE](LICENSE-FIRMWARE) |

**BlueSignal** builds the hardware. [**WaterQuality.Trading**](https://waterquality.trading) is the marketplace.

## Security

Found a vulnerability? See [SECURITY.md](SECURITY.md).
