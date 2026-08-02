#!/bin/bash
# WQM-1 Firmware Setup Script
# Reference host: Raspberry Pi Zero 2W with Raspberry Pi OS Lite.
# Also runs digital-first on Debian hosts without direct header access
# (Arduino UNO Q / VENTUNO Q) — see docs/platforms.md.
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
# boot overlays, RPi Python libs). Anything else — Arduino UNO Q / VENTUNO Q,
# generic Debian — runs digital-first: RS485-USB probes, USB GPS, Wi-Fi sync.
BOARD_MODEL="$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo unknown)"
if echo "$BOARD_MODEL" | grep -qi "raspberry pi"; then
    IS_RPI=1
    echo "Host: $BOARD_MODEL (direct-header install)"
else
    IS_RPI=0
    echo "Host: $BOARD_MODEL (digital-first install — analog/LoRa/relay skipped)"
fi

# --- System packages ---
echo "[1/9] Installing system packages..."
sudo apt-get update -qq

if [ "$IS_RPI" = "1" ]; then
    # Detect libgpiod version: libgpiod3 on Trixie (13+), libgpiod2 on Bookworm.
    if apt-cache show libgpiod3 &>/dev/null; then
        GPIOD_PKG="libgpiod3"
    else
        GPIOD_PKG="libgpiod2"
    fi

    sudo apt-get install -y -qq \
        python3-pip python3-venv python3-dev \
        i2c-tools python3-smbus \
        swig liblgpio-dev \
        "$GPIOD_PKG"

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
fi

# --- /boot/config.txt overlays (Raspberry Pi only) ---
if [ "$IS_RPI" = "0" ]; then
echo "[3/9] Skipping /boot/config.txt overlays (non-Pi host)"
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
    sudo sed -i -E 's/[[:space:]]+console=(serial0|ttyAMA0)[^[:space:]]*//g' "$CMDLINE"
fi
sudo systemctl disable --now serial-getty@ttyAMA0.service 2>/dev/null || true
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
if [ -f "$SCRIPT_DIR/requirements-rpi.txt" ]; then
    sudo cp "$SCRIPT_DIR/requirements-rpi.txt" "$RELEASE_DIR/"
fi
sudo cp "$SCRIPT_DIR/VERSION" "$RELEASE_DIR/"
sudo cp "$SCRIPT_DIR/setup.sh" "$RELEASE_DIR/"

# Install example config if none exists
if [ ! -f /etc/bluesignal/config.yaml ]; then
    sudo cp "$SCRIPT_DIR/config/config.yaml.example" /etc/bluesignal/config.yaml
    echo "  Installed default config to /etc/bluesignal/config.yaml"
fi

# Install policies, diagnostics, and the OTA verification public key
sudo cp "$SCRIPT_DIR/config/policies.yaml" "$RELEASE_DIR/config/"
if [ -f "$SCRIPT_DIR/config/ota_public_key.pem" ]; then
    sudo cp "$SCRIPT_DIR/config/ota_public_key.pem" "$RELEASE_DIR/config/"
else
    echo "  WARNING: config/ota_public_key.pem not found — OTA updates will fail"
    echo "           verification until a public key is installed (see docs/ota-runbook.md)."
fi
sudo cp "$SCRIPT_DIR/scripts/diagnostics.sh" "$RELEASE_DIR/scripts/"
sudo chmod +x "$RELEASE_DIR/scripts/diagnostics.sh"

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
sudo systemctl daemon-reload
sudo systemctl enable bluesignal-wqm.service
sudo systemctl enable bluesignal-service-window.service
sudo systemctl enable bluesignal-ota.service

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
echo ""
echo "Next steps:"
echo "  1. Edit config:      sudo nano /etc/bluesignal/config.yaml"
echo "  2. Set LoRaWAN key:  app_key field (from TTN/Chirpstack)"
echo "  3. Review policies:  sudo nano /opt/bluesignal/current/config/policies.yaml"
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
