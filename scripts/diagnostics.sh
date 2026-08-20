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

CONFIG_FILE="/etc/bluesignal/config.yaml"

# Read one scalar from config.yaml. The point of these checks is to test what
# the FIRMWARE will experience, not what root can do by hand — so anything the
# firmware reads from config has to be read here too.
cfg() {
    python3 - "$CONFIG_FILE" "$1" "$2" <<'PYEOF' 2>/dev/null || echo "$2"
import sys
try:
    import yaml
    c = yaml.safe_load(open(sys.argv[1])) or {}
    v = c.get(sys.argv[2], sys.argv[3])
    print("" if v is None else v)
except Exception:
    print(sys.argv[3])
PYEOF
}

# The user the firmware actually runs as. Checking a device node as root proves
# nothing: /dev/serial0 spent three hours readable by root and EACCES to the
# service, and the diagnostic reported a mild warning the whole time.
SERVICE_USER="$(systemctl show bluesignal-wqm -p User --value 2>/dev/null)"
[ -n "$SERVICE_USER" ] || SERVICE_USER="${SUDO_USER:-$(logname 2>/dev/null || echo pi)}"

# --- I2C: ADS1115 at 0x48 ---
if command -v i2cdetect &>/dev/null; then
    # On fresh Trixie boots i2c-dev may not be loaded yet; make sure it is
    # before probing the bus.
    sudo modprobe i2c-dev 2>/dev/null || true
    sleep 0.5
    # Capture first, then match. Piping i2cdetect straight into `grep -q` makes
    # the result depend on the pipeline's exit status under `set -o pipefail`,
    # so a probe that saw the chip could still be reported as a failure. The
    # scan is also re-tried: the firmware holds the same bus, and a probe that
    # lands mid-transaction can miss a device that is present.
    I2C_SCAN=""
    for _ in 1 2 3; do
        I2C_SCAN="$(i2cdetect -y 1 2>/dev/null || true)"
        case "$I2C_SCAN" in
            *" 48 "*|*" 48"|*"UU"*) break ;;
        esac
        sleep 0.5
    done
    case "$I2C_SCAN" in
        *" 48 "*|*" 48")
            pass "I2C:     ADS1115 found at 0x48" ;;
        *)
            fail "I2C:     ADS1115 not detected at 0x48 — check HAT seating"
            # Show what the bus actually held; "nothing at 0x48" and "nothing
            # at all" are different faults and the operator needs to tell them
            # apart without running the scan again by hand.
            if [ -n "$I2C_SCAN" ]; then
                echo "$I2C_SCAN" | sed 's/^/         /'
            else
                echo "         (i2cdetect produced no output — is /dev/i2c-1 present?)"
            fi ;;
    esac
else
    fail "I2C:     i2cdetect not installed (run setup.sh)"
fi

# --- 1-Wire: DS18B20 temperature sensor ---
W1_DIR="/sys/bus/w1/devices"
if [ -d "$W1_DIR" ]; then
    W1_DEVICE=$(find "$W1_DIR" -maxdepth 1 -name "28-*" -printf "%f\n" 2>/dev/null | head -1)
    TEMP_FITTED="$(cfg temperature_enabled true)"
    if [ -n "$W1_DEVICE" ]; then
        pass "1-Wire:  DS18B20 $W1_DEVICE"
    elif [ "$TEMP_FITTED" = "False" ] || [ "$TEMP_FITTED" = "false" ]; then
        # Declared not fitted, so its absence is the expected state, not a
        # fault. A FAIL that is always there is a FAIL nobody reads.
        info "1-Wire:  No DS18B20 — temperature declared not fitted, so this is expected"
    else
        fail "1-Wire:  No DS18B20 sensor found in $W1_DIR (declared fitted — check the 4.7k pull-up to 3.3V and wiring to GPIO 4)"
    fi
else
    fail "1-Wire:  $W1_DIR does not exist — check dtoverlay=w1-gpio in config.txt"
fi

# --- UART: GPS on /dev/serial0 ---
#
# Three things are checked, in the order that a real failure presents:
#   1. Can the SERVICE USER open the port? Root always can — that is precisely
#      how a unit ran for hours with the firmware getting EACCES while this
#      script reported a mild warning.
#   2. Does NMEA parse at the CONFIGURED baud? Reading at whatever the port
#      happens to be set to answers a question nobody asked.
#   3. If not, which baud does work? A mismatch is the most common GPS fault
#      and the fix is one config line, so the check names it rather than
#      leaving "no NMEA sentences" for someone to interpret.
if [ -e /dev/serial0 ]; then
    GPS_BAUD="$(cfg gps_baud 9600)"
    SERIAL_REAL="$(readlink -f /dev/serial0)"

    # 1. Permission, as the firmware's user rather than as root.
    if sudo -n -u "$SERVICE_USER" test -r "$SERIAL_REAL" 2>/dev/null; then
        GPS_READABLE=1
    else
        GPS_READABLE=0
    fi

    if [ "$GPS_READABLE" -eq 0 ]; then
        fail "GPS:     $SERIAL_REAL not readable by '$SERVICE_USER' ($(stat -c '%U:%G %a' "$SERIAL_REAL" 2>/dev/null))"
        echo "         The firmware runs as '$SERVICE_USER' and will fail with EACCES."
        echo "         Expected root:dialout 0660. root:tty 0600 means the kernel still"
        echo "         holds the UART as a console — remove console=serial0 from"
        echo "         /boot/firmware/cmdline.txt, mask serial-getty@ttyAMA0, and reboot."
    else
        # 2. Read at the configured baud.
        stty -F /dev/serial0 "$GPS_BAUD" raw -echo 2>/dev/null || true
        GPS_DATA=$(timeout 3 cat /dev/serial0 2>/dev/null || true)
        if echo "$GPS_DATA" | grep -qE '\$G[NPLA]'; then
            pass "GPS:     NMEA at ${GPS_BAUD} baud on /dev/serial0"
        else
            # 3. Sweep. Naming the working baud turns a vague warning into a fix.
            GPS_FOUND=""
            for b in 9600 38400 115200 19200 57600; do
                [ "$b" = "$GPS_BAUD" ] && continue
                stty -F /dev/serial0 "$b" raw -echo 2>/dev/null || continue
                if timeout 2 cat /dev/serial0 2>/dev/null | grep -qE '\$G[NPLA]'; then
                    GPS_FOUND="$b"
                    break
                fi
            done
            stty -F /dev/serial0 "$GPS_BAUD" raw -echo 2>/dev/null || true
            if [ -n "$GPS_FOUND" ]; then
                fail "GPS:     no NMEA at ${GPS_BAUD} baud — but valid NMEA at ${GPS_FOUND}"
                echo "         Set 'gps_baud: ${GPS_FOUND}' in $CONFIG_FILE and restart."
            elif [ -n "$GPS_DATA" ]; then
                warn "GPS:     bytes on /dev/serial0 but no NMEA at any common baud (module may need time)"
            else
                warn "GPS:     no data on /dev/serial0 (check antenna / wait for cold start)"
            fi
        fi
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
# Since the OTA releases/ layout, the active tree is /opt/bluesignal/current.
# The pre-OTA flat path is still checked so an un-migrated unit reports
# honestly rather than claiming automation is disabled when it is not.
POLICIES="/opt/bluesignal/current/config/policies.yaml"
[ -f "$POLICIES" ] || POLICIES="/opt/bluesignal/config/policies.yaml"
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
