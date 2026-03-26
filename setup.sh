#!/bin/bash
# WQM-1 Firmware Setup Script
# Run on a fresh Raspberry Pi Zero 2W with Raspberry Pi OS Lite
set -euo pipefail

INSTALL_DIR="/opt/bluesignal"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== BlueSignal WQM-1 Setup ==="

# --- System packages ---
echo "[1/7] Installing system packages..."
sudo apt-get update -qq
sudo apt-get install -y -qq \
    python3-pip python3-venv python3-dev \
    i2c-tools python3-smbus \
    libgpiod2

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
sudo mkdir -p "$INSTALL_DIR"/{src,config}
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

sudo chown -R pi:pi "$INSTALL_DIR" /var/lib/bluesignal /var/log/bluesignal

# --- systemd service ---
echo "[6/7] Installing systemd service..."
sudo cp "$SCRIPT_DIR/systemd/bluesignal-wqm.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable bluesignal-wqm.service

echo "[7/7] Setup complete!"
echo ""
echo "Next steps:"
echo "  1. Edit config:    sudo nano /etc/bluesignal/config.yaml"
echo "  2. Set LoRaWAN key: app_key field (from TTN/Chirpstack)"
echo "  3. Reboot:         sudo reboot"
echo "  4. After reboot, verify hardware:"
echo "     - I2C:  i2cdetect -y 1  (should show 0x48)"
echo "     - 1W:   ls /sys/bus/w1/devices/"
echo "     - GPS:  cat /dev/serial0"
echo "  5. Start service:  sudo systemctl start bluesignal-wqm"
echo "  6. View logs:      journalctl -u bluesignal-wqm -f"
