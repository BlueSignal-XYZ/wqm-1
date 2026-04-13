#!/bin/bash
# WQM-1 Firmware Setup Script
# Run on a fresh Raspberry Pi Zero 2W with Raspberry Pi OS Lite
set -euo pipefail

INSTALL_DIR="/opt/bluesignal"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# The user who invoked sudo (or the login user if run directly). Falls back to
# "pi" only if neither SUDO_USER nor logname resolve — e.g. when running in a
# non-interactive environment on a system without a "pi" user.
INSTALL_USER="${SUDO_USER:-$(logname 2>/dev/null || echo pi)}"

echo "=== BlueSignal WQM-1 Setup ==="

# --- System packages ---
echo "[1/7] Installing system packages..."
sudo apt-get update -qq

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

# --- Python dependencies ---
echo "[2/7] Installing Python packages..."
sudo pip3 install --break-system-packages --ignore-installed -r "$SCRIPT_DIR/requirements.txt"

# --- /boot/config.txt overlays ---
echo "[3/7] Configuring /boot/config.txt..."
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

# --- Create directories ---
echo "[4/7] Creating directories..."
sudo mkdir -p "$INSTALL_DIR"/{src,config,scripts}
sudo mkdir -p /var/lib/bluesignal
sudo mkdir -p /var/log/bluesignal
sudo mkdir -p /etc/bluesignal

# --- Install firmware ---
echo "[5/7] Installing firmware..."
sudo cp -r "$SCRIPT_DIR"/src/* "$INSTALL_DIR/src/"
sudo cp "$SCRIPT_DIR/requirements.txt" "$INSTALL_DIR/"
sudo cp "$SCRIPT_DIR/VERSION" "$INSTALL_DIR/"

# Install example config if none exists
if [ ! -f /etc/bluesignal/config.yaml ]; then
    sudo cp "$SCRIPT_DIR/config/config.yaml.example" /etc/bluesignal/config.yaml
    echo "  Installed default config to /etc/bluesignal/config.yaml"
fi

# Install policies and diagnostics
sudo cp "$SCRIPT_DIR/config/policies.yaml" "$INSTALL_DIR/config/"
sudo cp "$SCRIPT_DIR/scripts/diagnostics.sh" "$INSTALL_DIR/scripts/"
sudo chmod +x "$INSTALL_DIR/scripts/diagnostics.sh"

sudo chown -R "$INSTALL_USER:$INSTALL_USER" "$INSTALL_DIR" /var/lib/bluesignal /var/log/bluesignal

# --- systemd services ---
echo "[6/8] Installing systemd services..."
# Substitute the actual install user into the service files at install time.
# The source files in the repo keep the documented default of User=pi.
sudo sed "s/^User=pi$/User=$INSTALL_USER/" "$SCRIPT_DIR/systemd/bluesignal-wqm.service" \
    | sudo tee /etc/systemd/system/bluesignal-wqm.service > /dev/null
sudo sed "s/^User=pi$/User=$INSTALL_USER/" "$SCRIPT_DIR/systemd/bluesignal-service-window.service" \
    | sudo tee /etc/systemd/system/bluesignal-service-window.service > /dev/null
if [ -f "$SCRIPT_DIR/systemd/bluesignal-provision.service" ]; then
    sudo cp "$SCRIPT_DIR/systemd/bluesignal-provision.service" /etc/systemd/system/
fi
sudo systemctl daemon-reload
sudo systemctl enable bluesignal-wqm.service
sudo systemctl enable bluesignal-service-window.service

# --- Service window + provisioning ---
echo "[7/8] Installing service window and provisioning tools..."
sudo mkdir -p "$INSTALL_DIR/scripts"

# /var/run is tmpfs and clears on reboot, so install a tmpfiles.d entry
# that recreates /var/run/bluesignal owned by the install user on every boot.
sudo tee /etc/tmpfiles.d/bluesignal.conf > /dev/null <<EOF
d /var/run/bluesignal 0755 $INSTALL_USER $INSTALL_USER -
EOF
sudo mkdir -p /var/run/bluesignal
if [ -f "$SCRIPT_DIR/scripts/provision.py" ]; then
    sudo cp "$SCRIPT_DIR/scripts/provision.py" "$INSTALL_DIR/scripts/"
fi
if [ -f "$SCRIPT_DIR/scripts/first-boot-check.sh" ]; then
    sudo cp "$SCRIPT_DIR/scripts/first-boot-check.sh" "$INSTALL_DIR/scripts/"
    sudo chmod +x "$INSTALL_DIR/scripts/first-boot-check.sh"
fi
sudo chown -R "$INSTALL_USER:$INSTALL_USER" /var/run/bluesignal

# GPS UART (/dev/serial0) requires dialout group membership.
sudo usermod -aG dialout "$INSTALL_USER"

echo "[8/8] Setup complete!"
echo ""
echo "Note: $INSTALL_USER was added to the 'dialout' group for GPS UART access."
echo "      A reboot (or re-login) is required for the group change to take effect."
echo ""
echo "Next steps:"
echo "  1. Edit config:      sudo nano /etc/bluesignal/config.yaml"
echo "  2. Set LoRaWAN key:  app_key field (from TTN/Chirpstack)"
echo "  3. Review policies:  sudo nano /opt/bluesignal/config/policies.yaml"
echo "  4. Reboot:           sudo reboot"
echo "  5. Run diagnostics:  sudo bash /opt/bluesignal/scripts/diagnostics.sh"
echo "  6. Start service:    sudo systemctl start bluesignal-wqm"
echo "  7. View logs:        journalctl -u bluesignal-wqm -f"
echo ""
echo "Service Window:"
echo "  Web UI:              http://$(hostname).local:8080"
echo "  Default PIN:         1234 (change in /etc/bluesignal/config.yaml)"
echo ""
echo "Provisioning:"
echo "  CLI wizard:          sudo python3 /opt/bluesignal/scripts/provision.py"
echo "  Web wizard:          http://$(hostname).local:8080/provision"
