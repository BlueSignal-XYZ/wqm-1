# Getting Started

Step-by-step instructions for deploying the WQM-1 firmware onto a
Raspberry Pi Zero 2W with the WQM-1 HAT.

## Prerequisites

**Hardware:**

- Raspberry Pi Zero 2W
- WQM-1 carrier board, attached to the Pi's GPIO header
- microSD card (16 GB or larger recommended)
- 24 V DC power supply wired to the WQM-1 screw terminal for production
  deployment. A USB-C 5 V / 2.5 A supply to the Pi is fine for initial
  dev/debug (flashing, first-boot configuration) but is not the intended
  field power source.
- A computer with an SD card reader for flashing

**Software (on your computer):**

- [Raspberry Pi Imager](https://www.raspberrypi.com/software/)
- An SSH client (built into macOS/Linux; use PuTTY on Windows)

**Network:**

- WiFi network name (SSID) and password — the Pi Zero 2W has built-in
  WiFi

**User account:**

- Any username works. `setup.sh` auto-detects the invoking user via
  `$SUDO_USER`/`logname`, so you do not need to use `pi` specifically.

**Optional:**

- A LoRaWAN network server account ([The Things Network](https://www.thethingsnetwork.org/) or
  [ChirpStack](https://www.chirpstack.io/)) if you plan to use LoRa uplink

---

## 1. Flash Raspberry Pi OS Lite

1. Open **Raspberry Pi Imager** on your computer.
2. Click **Choose Device** and select **Raspberry Pi Zero 2W**.
3. Click **Choose OS** and select **Raspberry Pi OS (other)** →
   **Raspberry Pi OS Lite (64-bit)** (Bookworm or later).
4. Insert your microSD card and click **Choose Storage** to select it.
5. Click **Next**, then click **Edit Settings** to configure:
   - **General** tab:
     - Set hostname: `wqm1`
     - Set username: `pi` and choose a password
     - Configure wireless LAN: enter your WiFi SSID, password, and
       country code
     - Set locale and timezone
   - **Services** tab:
     - Enable SSH with password authentication
6. Click **Save**, then **Yes** to apply OS customisation, then **Yes**
   to write.
7. Wait for the write and verification to complete.

---

## 2. First Boot

1. Remove the microSD card from your computer and insert it into the
   Pi Zero 2W (with the WQM-1 HAT attached).
2. Connect the USB-C power supply. The green LED will blink during boot.
3. Wait about 60–90 seconds for the first boot to complete.
4. Find the Pi on your network:
   ```
   ping wqm1.local
   ```
   If mDNS doesn't work, check your router's DHCP lease table for the
   Pi's IP address.
5. SSH into the Pi:
   ```
   ssh pi@wqm1.local
   ```

---

## 3. Install the Firmware

Once connected via SSH, clone the repository and run the setup script:

```bash
sudo apt-get update && sudo apt-get install -y git
git clone https://github.com/bluesignal-xyz/wqm-1.git
cd wqm-1
sudo bash setup.sh
```

> On Debian Trixie, `setup.sh` automatically handles package-name
> differences (`libgpiod3` vs `libgpiod2`) and Debian-managed pip
> packages (adds `--ignore-installed` so pip doesn't try to uninstall
> distro-shipped modules without RECORD files).

The setup script runs 7 stages automatically:

| Stage | What it does |
|-------|-------------|
| 1/7 | Installs system packages (i2c-tools, python3-pip, libgpiod2) |
| 2/7 | Installs Python dependencies from `requirements.txt` |
| 3/7 | Configures `/boot/config.txt` device-tree overlays (I2C, SPI, UART, 1-Wire) |
| 4/7 | Creates directories (`/opt/bluesignal`, `/var/lib/bluesignal`, `/var/log/bluesignal`) |
| 5/7 | Copies firmware to `/opt/bluesignal/src/` and installs default config |
| 6/7 | Installs and enables the `bluesignal-wqm` systemd service |
| 7/7 | Prints next steps |

---

## 4. Configure

Edit the configuration file installed by the setup script:

```bash
sudo nano /etc/bluesignal/config.yaml
```

At minimum, set your **LoRaWAN AppKey** if you are using LoRa:

```yaml
app_key: "your-32-character-hex-appkey-here"
```

You can get the AppKey from:

- **TTN Console:** Applications → your app → End Devices → your
  device → Overview → AppKey
- **ChirpStack:** Applications → your app → Devices → your device →
  Keys (OTAA)

Other common settings to review:

| Field | Default | Description |
|-------|---------|-------------|
| `sensor_read_s` | `60` | Sensor read interval (seconds) |
| `lora_tx_s` | `300` | LoRaWAN transmit interval (seconds) |
| `gps_fix_s` | `600` | GPS fix interval (seconds) |
| `db_max_rows` | `100000` | Max SQLite rows before rotation |

See `config/config.yaml.example` in the repository for the full list
of fields with comments.

---

## 5. Reboot and Verify Hardware

A reboot is required for the `/boot/config.txt` overlay changes to take
effect:

```bash
sudo reboot
```

After the Pi comes back up (~60 seconds), SSH in again and run the
**diagnostics script** to verify all hardware at once:

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
are non-fatal. Fix any **FAIL** items before starting the service — see
the [Troubleshooting](#troubleshooting) section.

You can also verify individual interfaces manually:

| Interface | Command | Expected |
|-----------|---------|----------|
| I2C | `i2cdetect -y 1` | `0x48` (ADS1115) |
| 1-Wire | `ls /sys/bus/w1/devices/` | `28-*` directory |
| GPS | `cat /dev/serial0` | NMEA sentences (`$GNGGA`, `$GNRMC`) |
| SPI | `ls /dev/spidev0.0` | File exists |

---

## 6. Start the Service

```bash
sudo systemctl start bluesignal-wqm
```

Check the status:

```bash
sudo systemctl status bluesignal-wqm
```

It should show `active (running)`. Watch the live logs:

```bash
journalctl -u bluesignal-wqm -f
```

You should see:

- Firmware version and device ID on startup
- Sensor readings every 60 seconds (by default)
- LoRaWAN join attempts (if `app_key` is configured)
- GPS fix attempts every 10 minutes

The service is already **enabled** (from setup.sh), so it will
start automatically on every boot.

To stop the service:

```bash
sudo systemctl stop bluesignal-wqm
```

---

## Troubleshooting

### `i2cdetect` shows no devices

- Make sure the WQM-1 HAT is firmly seated on the GPIO header.
- Verify `/boot/config.txt` (or `/boot/firmware/config.txt` on
  Bookworm/Trixie) contains `dtparam=i2c_arm=on`.
- `setup.sh` creates `/etc/modules-load.d/i2c-dev.conf` to auto-load
  the `i2c-dev` kernel module on boot. If `/dev/i2c-1` is missing,
  run `sudo modprobe i2c-dev` manually to load it immediately.
- Reboot after any config.txt changes.

### No 1-Wire devices in `/sys/bus/w1/devices/`

- Verify `dtoverlay=w1-gpio,gpiopin=4` is in config.txt.
- Check that the DS18B20 sensor is wired correctly to GPIO 4.
- Reboot after config.txt changes.

### GPS shows no data on `/dev/serial0`

- Verify `enable_uart=1` and `dtoverlay=disable-bt` are in config.txt.
  The `disable-bt` overlay frees the PL011 UART for GPS use.
- Reboot after config.txt changes.
- The GPS module may take several minutes to get a first fix outdoors.

### GPS: permission denied on `/dev/serial0`

`/dev/serial0` is owned by `root:dialout`, so the firmware user must be
a member of `dialout` to read NMEA data. `setup.sh` adds the invoking
user to `dialout` automatically. If you installed manually or added
another user later, run:

```bash
sudo usermod -aG dialout $USER
sudo reboot
```

A reboot (or re-login) is required for the group change to take effect.

### LoRa: "Failed to add edge detection"

This error comes from `RPi.GPIO`'s `add_event_detect()` and occurs on
kernel 6.6+ (including Debian Trixie's 6.12). Firmware v1.1.0+ uses
`lgpio` for the SX1262 DIO1 interrupt, which works on current kernels.
If you see this error, pull the latest firmware from the repo and
reinstall:

```bash
cd ~/wqm-1
git pull
sudo bash setup.sh
sudo systemctl restart bluesignal-wqm
```

### Service fails to start

Check the error:

```bash
journalctl -u bluesignal-wqm -e
```

Common causes:

- **YAML syntax error** in `/etc/bluesignal/config.yaml` — validate
  with `python3 -c "import yaml; yaml.safe_load(open('/etc/bluesignal/config.yaml'))"`
- **Missing Python package** — re-run
  `sudo pip3 install --break-system-packages -r /opt/bluesignal/requirements.txt`
- **Permission denied** — ensure the install directories are owned by
  the user `bluesignal-wqm.service` runs as (by default the user who
  invoked `setup.sh`):
  `sudo chown -R $USER:$USER /opt/bluesignal /var/lib/bluesignal /var/log/bluesignal`

### LoRaWAN join fails

- Verify the `app_key` in config.yaml matches your network server
  exactly (32 hex characters, no spaces or dashes).
- Make sure the LoRa antenna is connected to the WQM-1 HAT.
- Confirm you are within range of a LoRaWAN gateway.
- Check the logs for join-request/join-accept messages.

### "No config at /etc/bluesignal/config.yaml, using defaults"

This is informational, not an error. The firmware runs with built-in
defaults. However, you should set `app_key` for LoRaWAN to work.

---

## Next Steps

- [Sensor Calibration](sensor-calibration.md) — calibrate pH, TDS,
  turbidity, and ORP probes
- [Firmware Overview](firmware-overview.md) — architecture and data
  pipeline details
- [Hardware Overview](hardware-overview.md) — schematic, pinout, and
  BOM reference
- See `config/policies.yaml` for relay automation rules (dosing pumps,
  aerators, valves)

## First-boot setup (v2.2+)

New units are commissioned entirely from a phone browser — no SSH, no config
files. Power the unit, join its network, and open `http://<unit-ip>:8080`.
Until setup is completed the unit shows only the guided wizard:

1. **PIN** — replace the factory PIN (1234 is refused).
2. **Identity** — device ID + DevEUI with a QR code for your install records.
3. **Cloud** — paste the device API key from cloud.bluesignal.xyz.
4. **Sensor check** — live traffic-light cards; each problem card says what to
   check in plain language.
5. **Go / no-go** — a final checklist covering sensors, cloud, LoRa, GPS and
   storage. Finish restarts monitoring with everything applied.

The dashboard home page keeps the same traffic-light language afterwards:
green = fine, amber = worth a look, red = needs attention — every card carries
the likely cause and the next step. Status LEDs mirror the same states
(steady heartbeat = ok, error blinks = the dashboard has details).

## RS485 digital probes (v2.3+)

The unit reads Honde Tech digital probes — residual chlorine, digital ORP,
and the 5-in-1 (pH/EC/TDS/salinity/temperature) — over a shared RS485
Modbus bus through the supplied RS485→USB adapter.

### Wiring

The USB adapter carries **data only**. Every probe also needs power from
the unit's 12V rail:

| Probe wire | Goes to |
|---|---|
| Red | +12V rail |
| Black | Ground |
| Yellow | RS485 A (adapter A terminal) |
| Green / White | RS485 B (adapter B terminal) |

All probes share the same two A/B bus wires (multi-drop). Never let the
A/B lines touch the power wires — that permanently damages the probe's
transceiver.

### Addresses

Every probe ships at Modbus address **1**, so probes must be added one at
a time: connect the new probe, open **Service Window → RS485 Sensors →
Add a new probe**, and the wizard discovers it and assigns the next free
address before you connect the following one.

### Calibration cadence

| Probe | Method | Cadence |
|---|---|---|
| Residual chlorine | Guided zero/slope wizard, flow cell (15–30 L/h), lab-verified sample | Every 1–2 months |
| 5-in-1 pH | Guided buffer wizard (4.01 / 6.86 / 9.18) | Quarterly, or on drift alert |
| 5-in-1 EC | Slope against a known-conductivity solution | Quarterly, or on drift alert |
| Digital ORP | Factory calibrated | Replace electrode per vendor guidance |

Notes for the chlorine probe: activate a new probe in 3M KCL before first
use, keep sample pH between 5 and 9, and install it in the flow cell —
immersion installs read unstably and foul quickly.
