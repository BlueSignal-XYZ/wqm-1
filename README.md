[![CERN-OHL-S-2.0](https://img.shields.io/badge/Hardware_License-CERN--OHL--S--2.0-blue)](LICENSE-HARDWARE)
[![GPL-3.0](https://img.shields.io/badge/Firmware_License-GPL--3.0-blue)](LICENSE-FIRMWARE)
[![Made with KiCad](https://img.shields.io/badge/Made_with-KiCad-orange)](https://www.kicad.org/)

# WQM-1 — Open Source Water Quality Monitor

> Continuous water quality monitoring on a Raspberry Pi Zero 2W — pH, TDS, turbidity and temperature, with LoRaWAN and GPS. Open hardware. Open firmware. Built by [BlueSignal](https://bluesignal.xyz).

## What It Is

The WQM-1 is a Raspberry Pi Zero 2W carrier board (120 × 105 mm) designed for continuous, autonomous water quality monitoring. It combines precision analog sensing with long-range wireless connectivity in a field-deployable package.

**Key hardware:**

- **ADS1115** 16-bit ADC (I²C) — four analog channels
- **SX1262 LoRa radio** — up to 15 km range via LoRaWAN
- **u-blox GPS** — georeferenced readings out of the box
- **LMR51450 buck converter** — wide-input 24 V DC power (screw terminal)
- **4× G5Q-14 optoisolated relays** — control dosing pumps, aerators, and valves

**Monitored parameters:**

- pH (BNC, analog)
- TDS (Total Dissolved Solids)
- Turbidity
- Temperature (DS18B20, 1-Wire)
- GPS location

Optional, over a shared RS485 Modbus bus (Honde Tech digital probes):

- Residual chlorine
- ORP — **digital only.** On PCBA rev Fin_3 the BNC front end is pH-only and
  AIN3 is spare, so there is no analog ORP channel. Earlier documentation
  showed ORP sharing the pH BNC; that circuit does not exist.
- 5-in-1: pH, EC/conductivity, TDS, salinity, temperature

**A probe is only read once it is declared fitted** — in the first-boot wizard's
Sensors step, or via `ph_enabled` / `tds_enabled` / `turbidity_enabled` /
`temperature_enabled` in `config.yaml`. An undeclared channel is never sampled,
which is what stops an open input being published as a measurement.

**Data pipeline:**

Sensors → ADS1115 (I²C) → Pi Zero 2W → SQLite WAL buffer → LoRaWAN (SX1262,
Cayenne LPP, AES-128) and/or HTTPS to the cloud over WiFi.

The two transports coexist. The SQLite buffer is store-and-forward: readings
survive an outage and upload when the link returns.

**Optional AWG load control:** the unit can ask an existing Eaton AbleEdge
smart breaker to energise or de-energise the site's AWG circuit (the four
G5Q-14 relays stay as the local interlock; the unit is not a breaker). Off
unless bound from the Service Window's **AWG circuit** page — see
[docs/smart-breaker-integration.md](docs/smart-breaker-integration.md) and the
[installer guide](docs/smart-breaker-installer-guide.md).

**Supported platforms:**

Tested on Debian Trixie (13), kernels 6.12–6.18, Pi Zero 2W (aarch64).
`setup.sh` handles Bookworm and Trixie automatically. A digital-first subset
(RS485 probes + USB GPS + WiFi; no analog, LoRa or relays) runs on Arduino
UNO Q / VENTUNO Q — see [docs/platforms.md](docs/platforms.md).

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
│   ├── main.py            # Entry point — wires subsystems, builds workers
│   ├── app/               # Worker loop (sampling, radio, GPS, sync, heartbeat)
│   ├── sensors/           # Drivers: ADS1115, DS18B20, pH, TDS, turbidity, Honde RS485
│   ├── sensing/           # Drift/stuck/spike detection, adaptive cadence
│   ├── radio/             # SX1262 driver + LoRaWAN MAC
│   ├── cloud/             # HTTPS store-and-forward client
│   ├── control/           # Relays, LEDs, automation rules
│   ├── integrations/      # Talks TO customer-owned systems (Eaton AbleEdge smart breaker)
│   ├── storage/           # SQLite buffer + migrations
│   ├── service_window/    # Flask UI: first-boot wizard, calibration, diagnostics
│   ├── ota/               # Signed over-the-air update agent
│   ├── diagnostics/       # Plain-language health explanations
│   ├── calibration/       # Sensor calibration
│   ├── platform_support/  # Host detection (Pi vs digital-first boards)
│   └── utils/             # Config, health, identity, netinfo, watchdog
├── config/                # Example configs (pinmap, policies, config.yaml)
├── hardware/
│   ├── bom/               # Bill of Materials
│   └── fab/               # Gerbers and schematics
├── firmware/              # Pre-built firmware artefacts
├── images/                # README and docs images
├── tests/                 # Test suite
├── systemd/               # systemd service unit files
├── scripts/               # diagnostics.sh, update-unit.sh, provision.py,
│                          #   first-boot-check.sh, ota-generate-keys.py
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

The setup script runs **9 stages** automatically:

| Stage | What it does |
|-------|-------------|
| 1/9 | Installs system packages (`i2c-tools`, `python3-pip`, `python3-venv`, `libgpiod2/3`) |
| 2/9 | Installs Python dependencies from `requirements.txt` (+ `requirements-rpi.txt` on a Pi) |
| 3/9 | Configures `/boot/config.txt` device-tree overlays (I2C, SPI, UART, 1-Wire, disable Bluetooth, reduce GPU memory) and frees the UART from the serial console |
| 4/9 | Prepares the install layout, migrating a pre-OTA flat install to `releases/` + `current` |
| 5/9 | Installs this version as `/opt/bluesignal/releases/<version>/` and flips the `current` symlink atomically |
| 6/9 | Installs and enables the `bluesignal-wqm`, `bluesignal-service-window` and `bluesignal-ota` systemd services |
| 7/9 | Installs the Service Window and provisioning tools |
| 8/9 | Restarts the services onto the new release |
| 9/9 | Prints next steps |

**Re-running `setup.sh` is how you upgrade.** It preserves
`/etc/bluesignal/config.yaml`, installs alongside the previous release rather
than over it, and restarts the services itself. `scripts/update-unit.sh` wraps
it with before/after state, a config and database backup, and a diagnostics run
— see [Upgrading a unit](#upgrading-a-unit).

---

### Step 3b — Commission from a phone (no SSH)

Everything in Step 4 can be done from a phone browser instead, and on a new
unit that is the intended path. Power the unit, join its network, and open
`http://wqm1.local:8080`. Until setup is finished the unit serves only the
wizard:

| Step | What it settles |
|------|-----------------|
| PIN | Replaces the factory PIN (1234 is refused) |
| Identity | Device ID + DevEUI, with a QR code for your install records |
| Network | WiFi signal **at the final mounting position, enclosure closed** |
| Cloud | The device API key, verified against the cloud before you leave |
| Sensors | **Which probes are physically fitted** — nothing is sampled until declared |
| Finish | Go/no-go over sensors, cloud, LoRa, GPS and storage |

The wizard gates the UI only while the PIN is still `1234` and setup has never
been completed. After that the same pages are reachable individually.

> **WiFi credentials can only be set when the card is imaged.** The Service
> Window is served over the unit's own network connection, so it cannot join a
> new SSID from its own UI. Decide the site's network before you flash.

For a field install — what to prepare, the on-site order, a go/no-go list and
the traps — see [docs/install-day-runbook.md](docs/install-day-runbook.md).

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

Optional AWG compressor / load control talks to the site's existing
**Eaton AbleEdge** smart breaker over HTTP (not a breaker SKU; G5Q-14
relays stay as fallback / interlock). The `smart_breaker_*` keys are set
from the Service Window's AWG circuit page; the installer binding,
fail-safe, and the Eaton developer-app checklist are in
[docs/smart-breaker-installer-guide.md](docs/smart-breaker-installer-guide.md)
and [docs/smart-breaker-integration.md](docs/smart-breaker-integration.md).
Live API smoke is blocked until the Eaton credentials are installed on the
unit — never commit them.

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
sudo bash /opt/bluesignal/current/scripts/diagnostics.sh
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
[PASS] Policies: /opt/bluesignal/current/config/policies.yaml (15 rules loaded)
[PASS] Service: bluesignal-wqm enabled (not yet started)
[INFO] Disk:    2.1 GB free on /var/lib/bluesignal
[INFO] CPU:     42.3°C
[INFO] Memory:  230MB / 463MB available

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

### Upgrading a unit

Firmware installs are versioned. Each release lands in
`/opt/bluesignal/releases/<version>/` and `/opt/bluesignal/current` is a symlink
to the active one, flipped atomically, so an upgrade never leaves a moment with
no firmware and the previous release stays on disk.

Two ways to upgrade:

**Signed OTA (routine).** The `bluesignal-ota` agent polls for releases, checks
an Ed25519 signature over the manifest and the tarball's hash, applies, self-
tests and rolls back on failure. This is the normal path once a unit is in the
field — see [docs/ota-runbook.md](docs/ota-runbook.md).

**Over SSH (bootstrap and break-glass).** `scripts/update-unit.sh` wraps
`setup.sh` with the parts a remote upgrade needs:

```bash
sudo bash scripts/update-unit.sh [git-ref]
```

It records the installed version, service state and reading rows by sync state
before and after; backs up `config.yaml` and takes a consistent `sqlite3
.backup` of the database ahead of any schema migration; optionally fetches a
given ref; warns rather than proceeding quietly when the tree's version matches
what is already installed; and finishes by running diagnostics and exiting on
their status.

After the first bootstrap, **SSH is break-glass only.** An unsigned change made
over SSH is invisible to the audit trail and is overwritten by the next release.

---

### Day-2 Operations

Once the firmware is installed and running, here is the everyday command
reference for inspecting the device over SSH.

#### Service control

```bash
sudo systemctl status  bluesignal-wqm        # is it alive?
sudo systemctl restart bluesignal-wqm
sudo systemctl stop    bluesignal-wqm
sudo systemctl start   bluesignal-wqm
sudo systemctl enable  bluesignal-wqm        # auto-start on boot (default)
journalctl -u bluesignal-wqm -f              # live logs
journalctl -u bluesignal-wqm -b 0            # logs since current boot
journalctl -u bluesignal-wqm --since "10 min ago"
```

#### Hardware sanity checks

```bash
sudo bash /opt/bluesignal/current/scripts/diagnostics.sh   # full one-shot probe
i2cdetect -y 1                                     # ADS1115 → 0x48
ls /sys/bus/w1/devices/                            # DS18B20 → 28-*
ls /dev/spidev0.0                                  # SX1262 SPI bus
vcgencmd measure_temp                              # CPU temp
```

#### GPS — see what it's reading

```bash
# Watch fix events as they happen
journalctl -u bluesignal-wqm -f | grep -i gps
# Expect every gps_fix_s seconds: "GPS fix: 47.654321, -122.123456"

# Query stored fixes from the SQLite buffer
sudo sqlite3 /var/lib/bluesignal/wqm1.db \
  "SELECT timestamp, lat, lon, alt_m FROM readings WHERE lat IS NOT NULL ORDER BY id DESC LIMIT 10;"

# Raw NMEA stream (must stop the service - it holds the port)
sudo systemctl stop bluesignal-wqm
sudo stty -F /dev/serial0 38400 raw -echo
sudo timeout 10 cat /dev/serial0
sudo systemctl start bluesignal-wqm
# $...,A,...,A,...  → fix acquired (A = active)
# $...,V,...,V,...  → no fix yet (V = void)
```

**How long should the first GPS fix take?** A u-blox cold start
(no almanac, no time, no ephemeris) is **30–60 seconds with a clear
view of the sky**. Indoors near a window is typically 2–10 minutes.
In the centre of a building, often never. The default `gps_fix_s` is
600s and `gps_fix_timeout_s` is 60s, so the first one or two attempts
on a fresh power-up may show "GPS fix timeout" before locking — that
is expected, not a fault. Once the module has an almanac stored,
warm starts drop to ~25 s and hot starts to a few seconds.

#### LoRa / LoRaWAN — see what's going on

LoRaWAN is a star network: the device only talks to a gateway, which
forwards everything to a network server (TTN, ChirpStack, etc.). All
"communication" with the device happens via that server.

```bash
# Watch radio activity in real time
journalctl -u bluesignal-wqm -f | grep -iE 'lora|sx1262|join|uplink|tx complete|fcnt'
# On join:    "Sending JoinRequest" → "JoinAccept received" → "Joined network"
# On uplink:  "LoRa TX complete (N bytes)" + "FCntUp=N"

# Check current join status from the DB
sudo sqlite3 /var/lib/bluesignal/wqm1.db \
  "SELECT joined, fcnt_up, fcnt_down, updated_at FROM lorawan_session;"
# joined=1 → registered. fcnt_up climbs once per uplink.
```

**To see uplinks land**, log into your network server console — TTN
Console → Application → Device → "Live data", or ChirpStack →
Applications → Device → "LoRaWAN frames".

**To send a downlink to the device**, schedule it from the network
server (TTN: device page → "Messaging" → "Downlink"). The firmware
receives it on the next uplink's RX1/RX2 window and applies any
matching policy from `config/policies.yaml`.

#### Service Window (web UI)

A Flask UI on port `8080` exposes calibration, relay control,
LoRaWAN AppKey configuration, sensor data, and diagnostics.

```bash
sudo systemctl status bluesignal-service-window
sudo systemctl enable --now bluesignal-service-window   # if not running
```

Then from another machine on the same network:

```
http://<pi-ip>:8080/                   # dashboard (or the first-boot wizard)
http://<pi-ip>:8080/setup/             # first-boot wizard, if not yet completed
http://<pi-ip>:8080/sensors/           # live sensor readings
http://<pi-ip>:8080/sensors/data.json  # raw JSON
http://<pi-ip>:8080/lora/              # set AppKey, view join state
http://<pi-ip>:8080/relays/            # manual relay control
http://<pi-ip>:8080/calibration/       # pH/TDS/turbidity calibration
http://<pi-ip>:8080/rs485/             # add and address RS485 digital probes
http://<pi-ip>:8080/awg/               # bind / switch the AWG smart-breaker circuit (optional)
http://<pi-ip>:8080/settings/          # config, PIN, remote reboot
http://<pi-ip>:8080/diagnostics/       # run hardware probe
```

#### Configuration

```bash
sudo nano /etc/bluesignal/config.yaml         # main config (AppKey, intervals, fan thresholds)
sudo nano /opt/bluesignal/current/config/policies.yaml # relay rules
sudo systemctl restart bluesignal-wqm          # apply changes
```

#### Storage

```bash
ls -lh /var/lib/bluesignal/                            # database files
sudo sqlite3 /var/lib/bluesignal/wqm1.db '.tables'
sudo sqlite3 /var/lib/bluesignal/wqm1.db \
  'SELECT * FROM readings ORDER BY id DESC LIMIT 10;'  # latest readings
df -h /var/lib/bluesignal                              # disk space
ls -lh /var/log/bluesignal/                            # log files
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
  sudo systemctl mask serial-getty@ttyAMA0.service
  # Prefer /boot/firmware — on Bookworm/Trixie the real file lives there and
  # /boot/cmdline.txt is a stub that only says so, which means a naive
  # `[ -f /boot/cmdline.txt ]` test picks the placeholder and edits nothing.
  CMDLINE=/boot/cmdline.txt; [ -f /boot/firmware/cmdline.txt ] && CMDLINE=/boot/firmware/cmdline.txt
  sudo sed -i -E 's/(^|[[:space:]])console=(serial0|ttyAMA0)[^[:space:]]*/ /g; s/^[[:space:]]+//; s/[[:space:]]+$//; s/[[:space:]]+/ /g' "$CMDLINE"
  sudo reboot
  ```
  After reboot, `/dev/ttyAMA0` should be `root dialout` mode `0660`.
- Reboot after `config.txt` changes.
- The GPS module may need several minutes to acquire a first fix
  outdoors.
- **Firmware logs `GPS UART opened` but no `GPS fix:` ever appears, and
  raw bytes look garbled.** This is a baud-rate mismatch. The default
  is 38400, which is what the module fitted on the WQM-1 uses — so on our
  hardware this should not happen. Other u-blox variants ship at 9600 or
  115200. `diagnostics.sh` sweeps automatically and names the working rate;
  to sweep by hand:
  ```bash
  sudo systemctl stop bluesignal-wqm
  for baud in 9600 115200 19200 57600 4800; do
    echo "=== $baud ==="
    sudo stty -F /dev/serial0 $baud raw -echo
    sudo timeout 2 cat /dev/serial0 | head -c 600
    echo
  done
  sudo systemctl start bluesignal-wqm
  ```
  The baud that prints clean `$GNRMC,...,$GNGGA,...` lines is the
  correct one. Set it in `/etc/bluesignal/config.yaml`:
  ```yaml
  gps_baud: 38400
  ```
  Then `sudo systemctl restart bluesignal-wqm`.

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
  sudo pip3 install --break-system-packages -r /opt/bluesignal/current/requirements.txt
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

Read the device's own log first — it distinguishes the two failures that
look identical from the console:

```bash
journalctl -u bluesignal-wqm | grep -iE "OTAA join attempt|JoinRequest|JoinAccept"
```

- **`JoinRequest TX failed`** — the packet never left the radio. Check SPI and
  that the SX1262 initialised (`LoRa init failed` earlier in the log).
- **`No JoinAccept received`** — the packet went out and nothing answered.
  Now check the network server console.

Then, on the network server:

- **No activity at all for the device** (TTN: "No activity yet") means **no
  gateway ever heard it.** No credential change will fix that — it is antenna
  or coverage. Confirm the antenna is actually attached to the U.FL connector
  (transmitting into an unterminated connector is also hard on the PA), get it
  outside the enclosure and vertical, and check the gateway map for the site.
- **Join requests arriving but rejected** means coverage is fine and the
  credentials disagree. Verify all three match the network server exactly:
  `app_key` (32 hex), `app_eui`/JoinEUI (16 hex — the firmware defaults to
  all-zeros, which is valid only if the device is registered that way), and the
  DevEUI, which the firmware derives as `0018B200` + the last 8 hex of the Pi's
  serial and cannot be overridden.
- Confirm the frequency plan matches: the firmware is **US915**, RX2 at
  923.3 MHz, which pairs with TTN's "United States 902–928 MHz, FSB 2".

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
