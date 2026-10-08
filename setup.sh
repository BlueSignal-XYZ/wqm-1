#!/bin/bash
# WQM-1 Firmware Setup Script
# Reference host: Raspberry Pi Zero 2W with Raspberry Pi OS Lite.
# Full direct-header stack also on the Orange Pi Zero 3W (Allwinner A733,
# Pi-layout 40-pin header; GPIO through lgpio on the kernel gpiochips).
# Runs digital-first on Debian hosts without direct header access
# (Arduino UNO Q / VENTUNO Q). See docs/platforms.md.
set -euo pipefail

INSTALL_DIR="/opt/bluesignal"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# The user who invoked sudo (or the login user if run directly). Falls back to
# "pi" only if neither SUDO_USER nor logname resolve — e.g. when running in a
# non-interactive environment on a system without a "pi" user.
INSTALL_USER="${SUDO_USER:-$(logname 2>/dev/null || echo pi)}"

# OTA-managed layout: every install lands in /opt/bluesignal/releases/<version>/
# and /opt/bluesignal/current is a symlink to the active release. The systemd
# units run from current/src, so the OTA agent can swap releases atomically by
# flipping the symlink.
FW_VERSION="$(cat "$SCRIPT_DIR/VERSION")"
RELEASE_DIR="$INSTALL_DIR/releases/$FW_VERSION"
CURRENT_LINK="$INSTALL_DIR/current"

echo "=== BlueSignal WQM-1 Setup (firmware v$FW_VERSION) ==="

# --- Host board detection (mirrors src/platform_support/board.py) ---
# Raspberry Pi hosts get the full direct-header stack (I2C/SPI/1-Wire/GPIO,
# boot overlays, RPi Python libs). The Orange Pi Zero 3W gets the same stack
# through the kernel's gpiochips (lgpio) with its own overlay file and
# console. Anything else — Arduino UNO Q / VENTUNO Q, generic Debian — runs
# digital-first: RS485-USB probes, USB GPS, Wi-Fi sync.
BOARD_MODEL="$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo unknown)"
BOARD_COMPACT="$(echo "$BOARD_MODEL" | tr '[:upper:]' '[:lower:]' | tr -d ' _-')"
IS_RPI=0
IS_OPI=0
if echo "$BOARD_MODEL" | grep -qi "raspberry pi"; then
    IS_RPI=1
    echo "Host: $BOARD_MODEL (direct-header install)"
elif echo "$BOARD_COMPACT" | grep -q "orangepizero3w"; then
    IS_OPI=1
    echo "Host: $BOARD_MODEL (direct-header install via gpiochip — see docs/platforms.md)"
else
    echo "Host: $BOARD_MODEL (digital-first install — analog/LoRa/relay skipped)"
fi

# --- System packages ---
echo "[1/9] Installing system packages..."
sudo apt-get update -qq

if [ "$IS_OPI" = "1" ]; then
    # i2c-tools for diagnostics; swig + python3-dev so pip can build lgpio
    # (there is no Debian package for it outside Raspberry Pi OS). No
    # RPi.GPIO here — it only knows Broadcom silicon.
    sudo apt-get install -y -qq \
        python3-pip python3-venv python3-dev \
        i2c-tools swig build-essential
    echo "i2c-dev" | sudo tee /etc/modules-load.d/i2c-dev.conf > /dev/null
    sudo modprobe i2c-dev 2>/dev/null || true
    # The service unit asks for these supplementary groups; Raspberry Pi OS
    # ships them, Orange Pi's Debian does not, and systemd refuses to start
    # a unit whose SupplementaryGroups= names a group that does not exist.
    for grp in gpio i2c spi dialout; do
        getent group "$grp" >/dev/null || sudo groupadd "$grp"
    done
    # Pi OS also ships the udev rules that hand the bus nodes to those
    # groups. Without them every node is root:root 0600 and the firmware
    # fails with EACCES on first open (the same shape as the serial0 trap).
    sudo tee /etc/udev/rules.d/90-bluesignal-host.rules > /dev/null <<'EOF'
# BlueSignal WQM-1: bus nodes owned by the groups the service unit joins.
SUBSYSTEM=="gpio", KERNEL=="gpiochip*", GROUP="gpio", MODE="0660"
SUBSYSTEM=="i2c-dev", GROUP="i2c", MODE="0660"
SUBSYSTEM=="spidev", GROUP="spi", MODE="0660"
EOF
    sudo udevadm control --reload-rules 2>/dev/null || true
    sudo udevadm trigger 2>/dev/null || true
elif [ "$IS_RPI" = "1" ]; then
    # Detect libgpiod version: libgpiod3 on Trixie (13+), libgpiod2 on Bookworm.
    if apt-cache show libgpiod3 &>/dev/null; then
        GPIOD_PKG="libgpiod3"
    else
        GPIOD_PKG="libgpiod2"
    fi

    # fake-hwclock + systemd-timesyncd: a Pi has no RTC. fake-hwclock restores
    # the last known time at boot (so the clock starts plausible rather than
    # at the epoch) and timesyncd disciplines it the moment a link exists.
    # GPS RMC is the fallback for a dark site (src/utils/clock.py).
    sudo apt-get install -y -qq \
        python3-pip python3-venv python3-dev \
        i2c-tools python3-smbus \
        swig liblgpio-dev \
        fake-hwclock systemd-timesyncd \
        "$GPIOD_PKG"
    sudo systemctl enable --now fake-hwclock 2>/dev/null || true
    sudo systemctl enable --now systemd-timesyncd 2>/dev/null || true

    # Ensure the i2c-dev module loads on boot. On Trixie, i2c_bcm2835 auto-loads
    # but i2c-dev does not, so /dev/i2c-1 never appears.
    echo "i2c-dev" | sudo tee /etc/modules-load.d/i2c-dev.conf > /dev/null
    sudo modprobe i2c-dev 2>/dev/null || true
else
    sudo apt-get install -y -qq python3-pip python3-venv python3-dev
fi

# --- Python dependencies ---
echo "[2/9] Installing Python packages..."
sudo pip3 install --break-system-packages --ignore-installed -r "$SCRIPT_DIR/requirements.txt"
if [ "$IS_RPI" = "1" ]; then
    sudo pip3 install --break-system-packages --ignore-installed -r "$SCRIPT_DIR/requirements-rpi.txt"
elif [ "$IS_OPI" = "1" ]; then
    sudo pip3 install --break-system-packages --ignore-installed -r "$SCRIPT_DIR/requirements-gpiochip.txt"
fi

# --- Boot overlays: Orange Pi (/boot/orangepiEnv.txt) ---
#
# Orange Pi's Debian enables header functions through U-Boot overlays named
# on the `overlays=` line of /boot/orangepiEnv.txt. The overlay NAMES differ
# per image, so nothing is written blind: each wanted overlay is enabled
# only if a matching .dtbo exists on this image, and what was and was not
# found is printed. A name this script cannot find has to be enabled by hand
# (orangepi-config → System → Hardware) — diagnostics.sh will say which bus
# is still missing after the reboot.
if [ "$IS_OPI" = "1" ]; then
echo "[3/9] Configuring /boot/orangepiEnv.txt..."
OPI_ENV="/boot/orangepiEnv.txt"
[ -f "/boot/armbianEnv.txt" ] && [ ! -f "$OPI_ENV" ] && OPI_ENV="/boot/armbianEnv.txt"
OPI_OVERLAY_DIR="$(find /boot -maxdepth 4 -type d -name overlay 2>/dev/null | head -1)"
opi_enable_overlay() {
    # $1 = label for the log, $2 = grep -E pattern for the .dtbo basename.
    local label="$1" pattern="$2" dtbo name
    [ -n "$OPI_OVERLAY_DIR" ] || { echo "  $label: no overlay directory under /boot — enable by hand"; return; }
    dtbo="$(find "$OPI_OVERLAY_DIR" -maxdepth 1 -name '*.dtbo' 2>/dev/null | xargs -n1 basename 2>/dev/null \
            | grep -E "$pattern" | sort | head -1)"
    if [ -z "$dtbo" ]; then
        echo "  $label: no overlay matching /$pattern/ on this image — enable by hand (orangepi-config)"
        return
    fi
    # U-Boot names overlays without the SoC prefix and the extension:
    # sun55i-a733-i2c0.dtbo → i2c0.
    name="$(echo "$dtbo" | sed -E 's/\.dtbo$//; s/^sun[0-9a-z]+-[0-9a-z]+-//')"
    if grep -qE "^overlays=.*(^| )${name}( |$)" "$OPI_ENV" 2>/dev/null; then
        echo "  $label: $name already enabled"
    elif grep -qE "^overlays=" "$OPI_ENV" 2>/dev/null; then
        sudo sed -i -E "s/^overlays=(.*)$/overlays=\1 ${name}/" "$OPI_ENV"
        echo "  $label: enabled $name"
    else
        echo "overlays=${name}" | sudo tee -a "$OPI_ENV" > /dev/null
        echo "  $label: enabled $name"
    fi
}
if [ -f "$OPI_ENV" ]; then
    sudo cp "$OPI_ENV" "${OPI_ENV}.bak.$(date +%s)" 2>/dev/null || true
    opi_enable_overlay "I2C (TWI0, pins 3/5 → ADS1115)"      '(^|-)(i2c0|twi0)(\.|-|$)'
    opi_enable_overlay "SPI (SPI3 spidev, pins 19-24 → LoRa)" '(^|-)spi3[-_a-z0-9]*spidev|(^|-)spidev3|(^|-)spi-spidev'
    opi_enable_overlay "1-Wire (w1-gpio on PB4, pin 7)"       '(^|-)w1[-_]gpio'
    # The w1-gpio overlay takes its pin as a parameter on most Allwinner
    # images; harmless if this image's overlay ignores it.
    grep -qE "^param_w1_pin=" "$OPI_ENV" || echo "param_w1_pin=PB4" | sudo tee -a "$OPI_ENV" > /dev/null
    grep -qE "^param_w1_pin_int_pullup=" "$OPI_ENV" || echo "param_w1_pin_int_pullup=1" | sudo tee -a "$OPI_ENV" > /dev/null
    # UART0 (pins 8/10) is where the GPS lands AND the board's debug
    # console. Move the kernel console off it; U-Boot's own output on that
    # UART during boot is the one thing this cannot change (see the trap in
    # docs/platforms.md).
    if grep -qE "^console=" "$OPI_ENV"; then
        sudo sed -i -E 's/^console=.*/console=display/' "$OPI_ENV"
    else
        echo "console=display" | sudo tee -a "$OPI_ENV" > /dev/null
    fi
    echo "  console: display (UART0 freed for the GPS)"
else
    echo "  $OPI_ENV not found — enable TWI0, SPI3 spidev and w1-gpio(PB4) by hand (orangepi-config)"
fi
sudo systemctl disable --now serial-getty@ttyS0.service 2>/dev/null || true
sudo systemctl mask serial-getty@ttyS0.service 2>/dev/null || true
fi  # IS_OPI

# --- /boot/config.txt overlays (Raspberry Pi only) ---
if [ "$IS_RPI" = "0" ]; then
[ "$IS_OPI" = "1" ] || echo "[3/9] Skipping /boot/config.txt overlays (non-Pi host)"
else
echo "[3/9] Configuring /boot/config.txt..."
CONFIG="/boot/config.txt"
[ -f "/boot/firmware/config.txt" ] && CONFIG="/boot/firmware/config.txt"
sudo cp "$CONFIG" "${CONFIG}.bak.$(date +%s)" 2>/dev/null || true

declare -a OVERLAYS=(
    "dtoverlay=disable-bt"
    "dtparam=i2c_arm=on"
    "dtparam=i2c_arm_baudrate=100000"
    "dtparam=spi=on"
    "enable_uart=1"
    "dtoverlay=w1-gpio,gpiopin=4"
    "gpu_mem=16"
    "dtparam=act_led_trigger=none"
    "dtparam=act_led_activelow=on"
)

for line in "${OVERLAYS[@]}"; do
    # Escape BRE metacharacters so overlay strings can be used safely in
    # grep/sed patterns (defensive — none of the current overlays contain
    # special chars, but future additions might).
    esc=$(printf '%s\n' "$line" | sed 's/[][\.*^$/]/\\&/g')

    if grep -qE "^[[:space:]]*${esc}[[:space:]]*$" "$CONFIG"; then
        # Already present and uncommented — nothing to do.
        :
    elif grep -qE "^[[:space:]]*#+[[:space:]]*${esc}[[:space:]]*$" "$CONFIG"; then
        # Commented-out version exists — uncomment it in place.
        sudo sed -i -E "s|^[[:space:]]*#+[[:space:]]*${esc}[[:space:]]*$|${line}|" "$CONFIG"
        echo "  Uncommented: $line"
    else
        echo "$line" | sudo tee -a "$CONFIG" > /dev/null
        echo "  Added: $line"
    fi
done

# Free the primary UART for the GPS. enable_uart=1 (above) is necessary but not
# sufficient: by default Raspberry Pi OS attaches a serial console (getty) to
# ttyAMA0, which leaves /dev/ttyAMA0 owned root:tty mode 0600. The firmware
# then can't open /dev/serial0 (a symlink to ttyAMA0) and GPS reads fail with
# EACCES. Disable the getty and strip console=serial0 / console=ttyAMA0 from
# the kernel cmdline so the udev rule reclaims the device as root:dialout 0660.
CMDLINE="/boot/cmdline.txt"
[ -f "/boot/firmware/cmdline.txt" ] && CMDLINE="/boot/firmware/cmdline.txt"
if [ -f "$CMDLINE" ]; then
    sudo cp "$CMDLINE" "${CMDLINE}.bak.$(date +%s)" 2>/dev/null || true
    # Anchor on (^|space), not space alone. On a stock Raspberry Pi cmdline.txt
    # `console=serial0,115200` is the FIRST token with no leading whitespace, so
    # a space-only pattern never matched it — this step has been silently doing
    # nothing on every install since it was written. The symptom is downstream
    # and looks unrelated: the kernel keeps the UART as a console, /dev/serial0
    # stays root:tty 0600 instead of root:dialout 0660, and the firmware fails
    # to open GPS with EACCES on every restart after the first boot.
    sudo sed -i -E 's/(^|[[:space:]])console=(serial0|ttyAMA0)[^[:space:]]*/ /g; s/^[[:space:]]+//; s/[[:space:]]+$//; s/[[:space:]]+/ /g' "$CMDLINE"
fi
# Mask, not just disable. A disabled unit can be pulled back in by anything
# that enables it (raspi-config, an OS update, a hand-run systemctl enable), and
# when it returns it takes the UART with it. Masking is what makes it stay off.
sudo systemctl disable --now serial-getty@ttyAMA0.service 2>/dev/null || true
sudo systemctl mask serial-getty@ttyAMA0.service 2>/dev/null || true
fi  # IS_RPI — end of Pi-only boot configuration

# --- Migrate legacy flat layout (pre-OTA) to releases/ + current symlink ---
echo "[4/9] Preparing install layout..."
if [ -d "$INSTALL_DIR/src" ] && [ ! -d "$INSTALL_DIR/releases" ]; then
    echo "  Legacy flat install detected — migrating to $INSTALL_DIR/releases/"
    LEGACY_DIR="$INSTALL_DIR/releases/legacy-1.1.0"
    sudo mkdir -p "$LEGACY_DIR"
    for item in src config scripts requirements.txt VERSION; do
        if [ -e "$INSTALL_DIR/$item" ]; then
            sudo mv "$INSTALL_DIR/$item" "$LEGACY_DIR/"
        fi
    done
    # Point current at the legacy tree so there is never a moment without a
    # resolvable install (the new release flips it below).
    sudo ln -s "$LEGACY_DIR" "$INSTALL_DIR/current.tmp"
    sudo mv -T "$INSTALL_DIR/current.tmp" "$CURRENT_LINK"
    echo "  Migrated old tree to $LEGACY_DIR"
fi

sudo mkdir -p "$RELEASE_DIR"/{config,scripts}
sudo mkdir -p /var/lib/bluesignal
sudo mkdir -p /var/log/bluesignal
sudo mkdir -p /etc/bluesignal

# --- Install firmware ---
echo "[5/9] Installing firmware to $RELEASE_DIR..."
sudo cp -r "$SCRIPT_DIR/src" "$RELEASE_DIR/"
sudo cp "$SCRIPT_DIR/requirements.txt" "$RELEASE_DIR/"
for extra in requirements-rpi.txt requirements-gpiochip.txt; do
    if [ -f "$SCRIPT_DIR/$extra" ]; then
        sudo cp "$SCRIPT_DIR/$extra" "$RELEASE_DIR/"
    fi
done
sudo cp "$SCRIPT_DIR/VERSION" "$RELEASE_DIR/"
sudo cp "$SCRIPT_DIR/setup.sh" "$RELEASE_DIR/"

# Install example config if none exists
if [ ! -f /etc/bluesignal/config.yaml ]; then
    sudo cp "$SCRIPT_DIR/config/config.yaml.example" /etc/bluesignal/config.yaml
    echo "  Installed default config to /etc/bluesignal/config.yaml"
fi

# Install policies, diagnostics, and the OTA verification public key.
# The live copy of policies.yaml is /etc/bluesignal/policies.yaml — seeded
# once, never overwritten — because the release tree is replaced on every
# upgrade and would take a customer's rules with it. The copy in the release
# tree is only the stock fallback for a unit that has no /etc copy.
sudo cp "$SCRIPT_DIR/config/policies.yaml" "$RELEASE_DIR/config/"
if [ ! -f /etc/bluesignal/policies.yaml ]; then
    sudo cp "$SCRIPT_DIR/config/policies.yaml" /etc/bluesignal/policies.yaml
    echo "  Installed default relay policies to /etc/bluesignal/policies.yaml"
fi
if [ -f "$SCRIPT_DIR/config/ota_public_key.pem" ]; then
    sudo cp "$SCRIPT_DIR/config/ota_public_key.pem" "$RELEASE_DIR/config/"
else
    echo "  WARNING: config/ota_public_key.pem not found — OTA updates will fail"
    echo "           verification until a public key is installed (see docs/ota-runbook.md)."
fi
sudo cp "$SCRIPT_DIR/scripts/diagnostics.sh" "$RELEASE_DIR/scripts/"
sudo chmod +x "$RELEASE_DIR/scripts/diagnostics.sh"
if [ -f "$SCRIPT_DIR/scripts/host-pins.py" ]; then
    sudo cp "$SCRIPT_DIR/scripts/host-pins.py" "$RELEASE_DIR/scripts/"
fi

# --- Host pins (Orange Pi): read the five unpublished header pins off the board ---
#
# The published Orange Pi Zero 3W pinout names the function pins; five plain
# GPIO header pins (12, 15, 22, 29, 31 — LORA_RST, relay 3, LED2, ADS ALERT,
# IO6) are not in it, and the firmware refuses to drive a net it cannot
# place. wiringOP's `gpio readall` on the board itself is the source; the
# result lands in /etc/bluesignal/host-pins.yaml, which the firmware reads.
if [ "$IS_OPI" = "1" ]; then
    if command -v gpio >/dev/null 2>&1; then
        if gpio readall > /tmp/wqm1-gpio-readall.txt 2>/dev/null; then
            PYTHONPATH="$RELEASE_DIR/src" sudo -E python3 "$RELEASE_DIR/scripts/host-pins.py" \
                --from-readall /tmp/wqm1-gpio-readall.txt || \
                echo "  WARNING: host pins still unresolved — see docs/platforms.md (the service will refuse to start relays/LEDs/LoRa until they are)"
        else
            echo "  WARNING: 'gpio readall' failed — run it by hand and pipe into scripts/host-pins.py --from-readall -"
        fi
    else
        echo "  WARNING: wiringOP 'gpio' not found — install it (Orange Pi images ship it) and run:"
        echo "           gpio readall | sudo python3 $RELEASE_DIR/scripts/host-pins.py --from-readall -"
    fi
fi

# Flip the current symlink atomically (symlink + rename, never a dead window).
sudo ln -s "$RELEASE_DIR" "$INSTALL_DIR/current.tmp"
sudo mv -T "$INSTALL_DIR/current.tmp" "$CURRENT_LINK"
echo "  current -> $RELEASE_DIR"

# /etc/bluesignal must be writable by the service user, not just readable.
# The service window provisions the device by rewriting config.yaml (AppKey,
# cloud_enabled, api_key, PIN). It runs as $INSTALL_USER, so a root-owned
# /etc/bluesignal makes every save fail with a 500 and the provisioning page
# is read-only in practice — which is the one thing it exists to do.
sudo chown -R "$INSTALL_USER:$INSTALL_USER" "$INSTALL_DIR" /var/lib/bluesignal /var/log/bluesignal /etc/bluesignal

# --- systemd services ---
echo "[6/9] Installing systemd services..."
# Substitute the actual install user into the service files at install time,
# and rewrite the packaged /opt/bluesignal/src paths to the OTA-managed
# /opt/bluesignal/current/src (the repo unit files keep the documented
# defaults; the rewrite happens only here).
sudo sed -e "s/^User=pi$/User=$INSTALL_USER/" \
         -e "s|/opt/bluesignal/src|/opt/bluesignal/current/src|g" \
    "$SCRIPT_DIR/systemd/bluesignal-wqm.service" \
    | sudo tee /etc/systemd/system/bluesignal-wqm.service > /dev/null
sudo sed -e "s/^User=pi$/User=$INSTALL_USER/" \
         -e "s|/opt/bluesignal/src|/opt/bluesignal/current/src|g" \
    "$SCRIPT_DIR/systemd/bluesignal-service-window.service" \
    | sudo tee /etc/systemd/system/bluesignal-service-window.service > /dev/null
# OTA agent runs as root (symlink flip + systemctl restart) — install verbatim.
sudo cp "$SCRIPT_DIR/systemd/bluesignal-ota.service" /etc/systemd/system/
if [ -f "$SCRIPT_DIR/systemd/bluesignal-provision.service" ]; then
    sudo cp "$SCRIPT_DIR/systemd/bluesignal-provision.service" /etc/systemd/system/
fi
# Setup access point (root, oneshot after NetworkManager): if no known Wi-Fi
# network associates within the grace period, raise the unit's own WPA2 AP so
# an installer with a phone can reach the Service Window with no site network
# — commissioning plan, PR 4. Never concurrent with a station link.
sudo cp "$SCRIPT_DIR/systemd/bluesignal-ap-fallback.service" /etc/systemd/system/
# Captive portal for that AP (site flow v2): NetworkManager's shared-mode
# dnsmasq reads this drop-in for the hotspot only, so every name a joined
# phone looks up answers 192.168.4.1, and DHCP option 114 (RFC 8910) hands
# out the setup URL. The phone's captive-network check then opens the setup
# page by itself. The port-80 half is a NAT rule the AP fallback installs.
sudo mkdir -p /etc/NetworkManager/dnsmasq-shared.d
printf '%s\n' \
    '# BlueSignal WQM-1 setup hotspot captive portal (setup.sh)' \
    'address=/#/192.168.4.1' \
    'dhcp-option=114,"http://192.168.4.1/setup/"' \
    | sudo tee /etc/NetworkManager/dnsmasq-shared.d/wqm1-captive.conf > /dev/null
# First-boot card consumer (root, oneshot before the firmware): moves the
# bench-written bluesignal-cloud.json off the FAT boot partition into
# /etc/bluesignal/config.yaml and deletes it — commissioning plan, PR 3/5.
sudo cp "$SCRIPT_DIR/systemd/bluesignal-card.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable bluesignal-wqm.service
sudo systemctl enable bluesignal-service-window.service
sudo systemctl enable bluesignal-ota.service
sudo systemctl enable bluesignal-ap-fallback.service
sudo systemctl enable bluesignal-card.service

# The firmware runs unprivileged but must be able to set the system clock
# from GPS on a dark site (src/utils/clock.py). One command, no arguments
# beyond the timestamp, for the service user only.
echo "$INSTALL_USER ALL=(root) NOPASSWD: /usr/bin/date -u -s *" \
    | sudo tee /etc/sudoers.d/bluesignal-clock > /dev/null
sudo chmod 440 /etc/sudoers.d/bluesignal-clock
sudo visudo -cf /etc/sudoers.d/bluesignal-clock > /dev/null || sudo rm -f /etc/sudoers.d/bluesignal-clock
# One-shot first-boot check (prints the provisioning hint to the journal
# until /etc/bluesignal/.provisioned exists). It was copied but never enabled.
sudo systemctl enable bluesignal-provision.service 2>/dev/null || true

# --- Service window + provisioning ---
echo "[7/9] Installing service window and provisioning tools..."

# /var/run is tmpfs and clears on reboot, so install a tmpfiles.d entry
# that recreates /var/run/bluesignal owned by the install user on every boot.
sudo tee /etc/tmpfiles.d/bluesignal.conf > /dev/null <<EOF
d /var/run/bluesignal 0755 $INSTALL_USER $INSTALL_USER -
EOF
sudo mkdir -p /var/run/bluesignal
if [ -f "$SCRIPT_DIR/scripts/provision.py" ]; then
    sudo cp "$SCRIPT_DIR/scripts/provision.py" "$RELEASE_DIR/scripts/"
fi
if [ -f "$SCRIPT_DIR/scripts/first-boot-check.sh" ]; then
    sudo cp "$SCRIPT_DIR/scripts/first-boot-check.sh" "$RELEASE_DIR/scripts/"
    sudo chmod +x "$RELEASE_DIR/scripts/first-boot-check.sh"
fi
sudo chown -R "$INSTALL_USER:$INSTALL_USER" /var/run/bluesignal "$RELEASE_DIR"

# GPS UART (/dev/serial0) requires dialout group membership.
sudo usermod -aG dialout "$INSTALL_USER"

# --- Restart services onto the new release ---
echo "[8/9] Restarting services..."
sudo systemctl restart bluesignal-wqm.service bluesignal-service-window.service 2>/dev/null \
    || echo "  Services not running yet — start them after configuration (step 6 below)."
sudo systemctl restart bluesignal-ota.service 2>/dev/null || true

echo "[9/9] Setup complete!"
echo ""
echo "Note: $INSTALL_USER was added to the 'dialout' group for GPS UART access."
echo "      A reboot (or re-login) is required for the group change to take effect."
if [ "$IS_OPI" = "1" ]; then
echo ""
echo "Orange Pi Zero 3W — read before the reboot (docs/platforms.md has the detail):"
echo "  * The GPS shares UART0 with U-Boot's console. If the board stops at the"
echo "    U-Boot prompt with the GPS connected, NMEA bytes aborted autoboot: set"
echo "    bootdelay=0 in the U-Boot environment, or power the GPS after boot."
echo "  * Bus nodes to expect after reboot: /dev/i2c-0, /dev/spidev3.0, /dev/ttyS0."
echo "    If a number differs on this image, record it in /etc/bluesignal/host-pins.yaml"
echo "    (i2c_bus / spi_bus / spi_device / gps_port) and restart the service."
fi
echo ""
echo "Next steps:"
echo "  1. Edit config:      sudo nano /etc/bluesignal/config.yaml"
echo "  2. Set LoRaWAN key:  app_key field (from TTN/Chirpstack)"
echo "  3. Review policies:  sudo nano /etc/bluesignal/policies.yaml"
echo "  4. Reboot:           sudo reboot"
echo "  5. Run diagnostics:  sudo bash /opt/bluesignal/current/scripts/diagnostics.sh"
echo "  6. Start service:    sudo systemctl start bluesignal-wqm"
echo "  7. View logs:        journalctl -u bluesignal-wqm -f"
echo ""
echo "Service Window:"
echo "  Web UI:              http://$(hostname).local:8080"
echo "  Default PIN:         1234 (change in /etc/bluesignal/config.yaml)"
echo ""
echo "Provisioning:"
echo "  CLI wizard:          sudo python3 /opt/bluesignal/current/scripts/provision.py"
echo "  Web wizard:          http://$(hostname).local:8080/provision"
echo ""
echo "OTA updates:"
echo "  Layout:              releases in $INSTALL_DIR/releases/, active = $CURRENT_LINK"
echo "  Agent logs:          journalctl -u bluesignal-ota -f"
echo "  Runbook:             docs/ota-runbook.md"
