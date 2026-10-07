#!/bin/bash
# WQM-1 Hardware Diagnostics
# Run after setup.sh + reboot to verify all subsystems.
# Usage: sudo bash /opt/bluesignal/current/scripts/diagnostics.sh [--relays]
#
#   --relays   Also click each of the four relays ON for 1 s, then OFF, so you
#              can hear them. Off by default: whatever is wired to a relay
#              output runs for that second. Stops the firmware service for the
#              test and starts it again afterwards.
set -uo pipefail

RUN_RELAYS=0
for arg in "$@"; do
    case "$arg" in
        --relays) RUN_RELAYS=1 ;;
        -h|--help) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Unknown option: $arg (try --help)" >&2; exit 2 ;;
    esac
done

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

# Can the service user read AND write this device node? Every bus the firmware
# drives needs both — a read-only /dev/i2c-1 fails an ADS1115 conversion just
# as surely as a missing chip, and it presents as a sensor fault rather than a
# permission one. Returns 0 (ok) when SERVICE_USER is root or unresolvable,
# because then there is nothing to distinguish.
svc_can_rw() {
    local node="$1"
    [ -e "$node" ] || return 0
    [ "$SERVICE_USER" = "root" ] && return 0
    id -u "$SERVICE_USER" >/dev/null 2>&1 || return 0
    sudo -n -u "$SERVICE_USER" test -r "$node" 2>/dev/null &&
        sudo -n -u "$SERVICE_USER" test -w "$node" 2>/dev/null
}

# One line explaining a node the service cannot use, with its actual ownership
# and the group that would fix it.
svc_perm_fail() {
    local node="$1" group="$2"
    fail "$3 $node not read/writable by '$SERVICE_USER' ($(stat -c '%U:%G %a' "$node" 2>/dev/null))"
    echo "         The firmware runs as '$SERVICE_USER' and will fail with EACCES."
    echo "         Fix: sudo usermod -aG $group $SERVICE_USER  (then restart the service)"
}

# --- Identity: what to type into Cloud ---
#
# Printed first because it is the one thing the bench needs to carry away:
# the device id the firmware reports under (from the Pi serial — NOT the
# printed WQM- label on 2.3.0 firmware) and the DevEUI the claim asks for.
# Read from the installed firmware's own identity module so it can never
# disagree with what the unit actually posts.
ID_SRC=""
for d in /opt/bluesignal/current/src "$(cd "$(dirname "$0")" && pwd)/../src"; do
    [ -f "$d/utils/identity.py" ] && { ID_SRC="$d"; break; }
done
if [ -n "$ID_SRC" ]; then
    IDENTITY="$(python3 - "$ID_SRC" <<'PYEOF' 2>/dev/null
import sys
sys.path.insert(0, sys.argv[1])
from utils.identity import get_dev_eui, get_device_id, get_pi_serial
print(get_device_id())
print(get_dev_eui().hex().upper())
print(get_pi_serial())
PYEOF
)"
    DEVICE_ID="$(echo "$IDENTITY" | sed -n 1p)"
    DEV_EUI="$(echo "$IDENTITY" | sed -n 2p)"
    PI_SERIAL="$(echo "$IDENTITY" | sed -n 3p)"
    if [ -n "$DEVICE_ID" ]; then
        info "Device ID: $DEVICE_ID   (enter this in Cloud commissioning)"
        info "DevEUI:    $DEV_EUI"
        info "Pi serial: $PI_SERIAL"
        if [ "$PI_SERIAL" = "0000000000000000" ]; then
            fail "Identity: Pi serial unreadable — every unit would share one device id"
        fi
    else
        warn "Identity: could not read device id from $ID_SRC/utils/identity.py"
    fi
else
    warn "Identity: firmware not installed — run setup.sh"
fi
API_KEY_SET="$(cfg api_key '')"
if [ -n "$API_KEY_SET" ]; then
    info "Cloud key: set in $CONFIG_FILE (…${API_KEY_SET: -4})"
else
    info "Cloud key: not set yet — claim in Cloud, then paste it at http://$(hostname).local:8080"
fi
echo ""

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
            # Root found the chip. That is only half the answer: the firmware
            # opens /dev/i2c-1 as SERVICE_USER, and a node root can drive is
            # not necessarily one the service can.
            if svc_can_rw /dev/i2c-1; then
                pass "I2C:     ADS1115 found at 0x48"
            else
                svc_perm_fail /dev/i2c-1 i2c "I2C:    "
                echo "         (the ADS1115 IS present at 0x48 — this is permissions, not hardware)"
            fi ;;
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
    GPS_BAUD="$(cfg gps_baud 38400)"
    SERIAL_REAL="$(readlink -f /dev/serial0)"

    # 1. Permission, as the firmware's user rather than as root. Write matters
    #    as much as read: pyserial sets termios on open, which needs both.
    if svc_can_rw "$SERIAL_REAL"; then
        GPS_READABLE=1
    else
        GPS_READABLE=0
    fi

    if [ "$GPS_READABLE" -eq 0 ]; then
        svc_perm_fail "$SERIAL_REAL" dialout "GPS:    "
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
            for b in 38400 9600 115200 19200 57600; do
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
    if svc_can_rw /dev/spidev0.0; then
        pass "SPI:     /dev/spidev0.0"
    else
        svc_perm_fail /dev/spidev0.0 spi "SPI:    "
        echo "         The LoRa radio will never key up; joins fail with no radio error."
    fi
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

# --- Store-and-forward buffer ---
#
# The check that did not exist on 2026-08-03, and whose absence let a unit
# reject 15,409 readings over sixteen days while every other line here said
# PASS. `failed_permanent` is terminal — those rows are never retried — so a
# growing count is not a backlog that will clear itself, it is data that is
# gone. It has to be loud.
WQM_DB="/var/lib/bluesignal/wqm1.db"
if [ -f "$WQM_DB" ] && command -v sqlite3 &>/dev/null; then
    # Read-only so a running firmware is never blocked by the diagnostic.
    BUF_SYNCED=$(sqlite3 "file:$WQM_DB?mode=ro" "SELECT COUNT(*) FROM readings WHERE sync_state='synced';" 2>/dev/null || echo "")
    BUF_PENDING=$(sqlite3 "file:$WQM_DB?mode=ro" "SELECT COUNT(*) FROM readings WHERE sync_state='pending';" 2>/dev/null || echo "")
    BUF_FAILED=$(sqlite3 "file:$WQM_DB?mode=ro" "SELECT COUNT(*) FROM readings WHERE sync_state='failed_permanent';" 2>/dev/null || echo "")
    if [ -n "$BUF_FAILED" ]; then
        info "Buffer:  $BUF_SYNCED synced, $BUF_PENDING pending, $BUF_FAILED permanently rejected"
        if [ "$BUF_FAILED" -gt 100 ]; then
            fail "Buffer:  $BUF_FAILED reading(s) permanently rejected by the cloud"
            echo "         These are never retried — the measurements are lost."
            echo "         Most common cause: rows with no sensor values at all, from"
            echo "         probes that are enabled in config but not fitted (or not"
            echo "         reading). Check which probes are declared:"
            echo "           grep -E '_enabled' $CONFIG_FILE"
            echo "         and what the server said:"
            echo "           journalctl -u bluesignal-wqm | grep -i 'permanently rejected' | tail -5"
        elif [ "$BUF_FAILED" -gt 0 ]; then
            warn "Buffer:  $BUF_FAILED reading(s) permanently rejected (watch it — the count only grows)"
        fi
        # A backlog that never drains is a sync fault, not a buffer size.
        if [ -n "$BUF_PENDING" ] && [ "$BUF_PENDING" -gt 500 ]; then
            warn "Buffer:  $BUF_PENDING readings pending upload — check connectivity and cloud auth"
        fi
    fi
elif [ -f "$WQM_DB" ]; then
    warn "Buffer:  sqlite3 not installed — cannot check the reading buffer (apt install sqlite3)"
fi

# --- Relay click test (opt-in: --relays) ---
#
# Hearing the coils is the only bench check that proves the whole relay path:
# GPIO -> LTV-354T opto -> S8050 -> coil. A relay on a HAT powered from the
# Pi's USB alone will NOT click — the coils run from the 24 V input — so a
# silent relay on USB power is expected, not a fault.
#
# Pins are the BCM numbers in src/utils/config.py RELAY_PINS (active-high);
# tests/test_diagnostics_relays.py keeps the two lists equal.
RELAY_TEST_PINS=(17 27 22 23)
if [ "$RUN_RELAYS" = "1" ]; then
    echo ""
    echo "--- Relay click test ---"
    echo "Each relay switches ON for 1 second, then OFF: listen for two clicks."
    echo "Anything wired to a relay output WILL run for that second."
    echo "The HAT must be on 24 V — the coils do not click on USB power alone."
    INTERACTIVE=0
    if [ -t 0 ]; then
        INTERACTIVE=1
        read -r -p "Press Enter to start (Ctrl+C to cancel) " _
    fi

    # The firmware owns these GPIO lines while it runs. Stop it so the test
    # is the only thing driving them, and always start it again — on success,
    # on failure and on Ctrl+C. At start the firmware forces every relay OFF.
    RELAY_SVC_WAS_ACTIVE=0
    if systemctl is-active --quiet bluesignal-wqm; then
        RELAY_SVC_WAS_ACTIVE=1
        systemctl stop bluesignal-wqm
    fi
    relay_test_restore() {
        if [ "$RELAY_SVC_WAS_ACTIVE" = "1" ]; then
            systemctl start bluesignal-wqm && echo "         (firmware service started again)"
            RELAY_SVC_WAS_ACTIVE=0
        fi
    }
    trap relay_test_restore EXIT
    trap 'relay_test_restore; exit 130' INT TERM

    RELAY_N=0
    for pin in "${RELAY_TEST_PINS[@]}"; do
        RELAY_N=$((RELAY_N + 1))
        echo "Relay $RELAY_N (GPIO $pin): ON..."
        if ! RELAY_ERR=$(python3 - "$pin" 2>&1 <<'PYEOF'
import sys, time
import RPi.GPIO as GPIO
pin = int(sys.argv[1])
GPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)
GPIO.setup(pin, GPIO.OUT, initial=GPIO.LOW)
try:
    GPIO.output(pin, GPIO.HIGH)
    time.sleep(1.0)
finally:
    GPIO.output(pin, GPIO.LOW)
    GPIO.cleanup(pin)
PYEOF
        ); then
            fail "Relay $RELAY_N: could not drive GPIO $pin — $(echo "$RELAY_ERR" | tail -1)"
            continue
        fi
        echo "Relay $RELAY_N (GPIO $pin): OFF"
        if [ "$INTERACTIVE" = "1" ]; then
            read -r -p "  Heard relay $RELAY_N click on and off? [Y/n] " heard
            case "$heard" in
                n|N|no|NO)
                    fail "Relay $RELAY_N: no click heard (GPIO $pin)"
                    echo "         On 24 V? Then check the relay is seated and the opto/transistor"
                    echo "         for channel $RELAY_N. Re-test one channel at a time with --relays." ;;
                *) pass "Relay $RELAY_N: clicked (GPIO $pin)" ;;
            esac
        else
            info "Relay $RELAY_N: switched GPIO $pin on and off — listen for the click"
        fi
        sleep 1
    done
    relay_test_restore
    trap - EXIT INT TERM
    echo ""
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
