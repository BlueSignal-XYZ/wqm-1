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
    "$GPIOD_PKG"

# --- Python dependencies ---
echo "[2/7] Installing Python packages..."
sudo pip3 install --break-system-packages -r "$SCRIPT_DIR/requirements.txt"

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
    if ! grep -qF "$line" "$CONFIG"; then
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
sudo cp "$SCRIPT_DIR/systemd/bluesignal-wqm.service" /etc/systemd/system/
sudo cp "$SCRIPT_DIR/systemd/bluesignal-service-window.service" /etc/systemd/system/
if [ -f "$SCRIPT_DIR/systemd/bluesignal-provision.service" ]; then
    sudo cp "$SCRIPT_DIR/systemd/bluesignal-provision.service" /etc/systemd/system/
fi
sudo systemctl daemon-reload
sudo systemctl enable bluesignal-wqm.service
sudo systemctl enable bluesignal-service-window.service

# --- Service window + provisioning ---
echo "[7/8] Installing service window and provisioning tools..."
sudo mkdir -p "$INSTALL_DIR/scripts"
sudo mkdir -p /var/run/bluesignal
if [ -f "$SCRIPT_DIR/scripts/provision.py" ]; then
    sudo cp "$SCRIPT_DIR/scripts/provision.py" "$INSTALL_DIR/scripts/"
fi
if [ -f "$SCRIPT_DIR/scripts/first-boot-check.sh" ]; then
    sudo cp "$SCRIPT_DIR/scripts/first-boot-check.sh" "$INSTALL_DIR/scripts/"
    sudo chmod +x "$INSTALL_DIR/scripts/first-boot-check.sh"
fi
sudo chown -R "$INSTALL_USER:$INSTALL_USER" /var/run/bluesignal

echo "[8/8] Setup complete!"
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
