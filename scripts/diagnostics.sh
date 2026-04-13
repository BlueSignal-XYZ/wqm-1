#!/bin/bash
# WQM-1 Hardware Diagnostics
# Run after setup.sh + reboot to verify all subsystems.
# Usage: sudo bash /opt/bluesignal/scripts/diagnostics.sh
set -uo pipefail

PASS=0
WARN=0
FAIL=0

pass()  { echo "[PASS] $1"; PASS=$((PASS + 1)); }
warn()  { echo "[WARN] $1"; WARN=$((WARN + 1)); }
fail()  { echo "[FAIL] $1"; FAIL=$((FAIL + 1)); }
info()  { echo "[INFO] $1"; }

echo "=== WQM-1 Hardware Diagnostics ==="
echo ""

# --- I2C: ADS1115 at 0x48 ---
if command -v i2cdetect &>/dev/null; then
    # On fresh Trixie boots i2c-dev may not be loaded yet; make sure it is
    # before probing the bus.
    sudo modprobe i2c-dev 2>/dev/null || true
    sleep 0.5
    if i2cdetect -y 1 2>/dev/null | grep -q "48"; then
        pass "I2C:     ADS1115 found at 0x48"
    else
        fail "I2C:     ADS1115 not detected at 0x48 — check HAT seating"
    fi
else
    fail "I2C:     i2cdetect not installed (run setup.sh)"
fi

# --- 1-Wire: DS18B20 temperature sensor ---
W1_DIR="/sys/bus/w1/devices"
if [ -d "$W1_DIR" ]; then
    W1_DEVICE=$(find "$W1_DIR" -maxdepth 1 -name "28-*" -printf "%f\n" 2>/dev/null | head -1)
    if [ -n "$W1_DEVICE" ]; then
        pass "1-Wire:  DS18B20 $W1_DEVICE"
    else
        fail "1-Wire:  No DS18B20 sensor found in $W1_DIR"
    fi
else
    fail "1-Wire:  $W1_DIR does not exist — check dtoverlay=w1-gpio in config.txt"
fi

# --- UART: GPS data on /dev/serial0 ---
if [ -e /dev/serial0 ]; then
    # Read 2 seconds of GPS data, check for NMEA sentences
    GPS_DATA=$(timeout 3 cat /dev/serial0 2>/dev/null || true)
    if echo "$GPS_DATA" | grep -qE '^\$G[NPLA]'; then
        pass "GPS:     NMEA data on /dev/serial0"
    elif [ -n "$GPS_DATA" ]; then
        warn "GPS:     Data on /dev/serial0 but no NMEA sentences (module may need time)"
    else
        warn "GPS:     No data on /dev/serial0 (check antenna / wait for cold start)"
    fi
else
    fail "GPS:     /dev/serial0 does not exist — check enable_uart=1 and dtoverlay=disable-bt"
fi

# --- SPI: LoRa radio ---
if [ -e /dev/spidev0.0 ]; then
    pass "SPI:     /dev/spidev0.0"
else
    fail "SPI:     /dev/spidev0.0 missing — check dtparam=spi=on in config.txt"
fi

# --- Config file ---
CONFIG="/etc/bluesignal/config.yaml"
if [ -f "$CONFIG" ]; then
    if python3 -c "import yaml; yaml.safe_load(open('$CONFIG'))" 2>/dev/null; then
        pass "Config:  $CONFIG (valid YAML)"
    else
        fail "Config:  $CONFIG has YAML syntax errors"
    fi
else
    warn "Config:  $CONFIG not found (firmware will use defaults)"
fi

# --- LoRaWAN app_key ---
if [ -f "$CONFIG" ]; then
    APP_KEY=$(python3 -c "
import yaml
c = yaml.safe_load(open('$CONFIG')) or {}
print(c.get('app_key', ''))
" 2>/dev/null)
    if [ -z "$APP_KEY" ] || [ "$APP_KEY" = "00000000000000000000000000000000" ]; then
        warn "LoRaWAN: app_key is default (LoRa will not connect)"
    else
        pass "LoRaWAN: app_key configured"
    fi
fi

# --- Policies file ---
POLICIES="/opt/bluesignal/config/policies.yaml"
if [ -f "$POLICIES" ]; then
    RULE_COUNT=$(python3 -c "
import yaml
p = yaml.safe_load(open('$POLICIES')) or {}
print(len(p.get('rules', [])))
" 2>/dev/null)
    pass "Policies: $POLICIES ($RULE_COUNT rules loaded)"
else
    info "Policies: No policies.yaml (automation rules disabled)"
fi

# --- systemd service ---
if systemctl is-enabled bluesignal-wqm &>/dev/null; then
    if systemctl is-active bluesignal-wqm &>/dev/null; then
        pass "Service: bluesignal-wqm enabled and running"
    else
        pass "Service: bluesignal-wqm enabled (not yet started)"
    fi
else
    fail "Service: bluesignal-wqm not enabled (run setup.sh)"
fi

# --- Disk space ---
DISK_FREE=$(df -h /var/lib/bluesignal 2>/dev/null | awk 'NR==2{print $4}')
if [ -n "$DISK_FREE" ]; then
    info "Disk:    $DISK_FREE free on /var/lib/bluesignal"
fi

# --- CPU temperature ---
if [ -f /sys/class/thermal/thermal_zone0/temp ]; then
    CPU_TEMP_RAW=$(cat /sys/class/thermal/thermal_zone0/temp)
    CPU_TEMP=$(echo "scale=1; $CPU_TEMP_RAW / 1000" | bc 2>/dev/null || echo "N/A")
    info "CPU:     ${CPU_TEMP}°C"
fi

# --- Memory ---
MEM_FREE=$(free -m | awk 'NR==2{printf "%dMB / %dMB", $7, $2}')
info "Memory:  $MEM_FREE available"

# --- Summary ---
echo ""
echo "=== $PASS passed, $WARN warning(s), $FAIL failed ==="

if [ "$FAIL" -gt 0 ]; then
    echo ""
    echo "Fix the failures above, then run this script again."
    exit 1
fi
exit 0
