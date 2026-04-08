#!/bin/bash
# WQM-1 First-Boot Provisioning Check
#
# Checks if device needs provisioning (default AppKey).
# Runs as a one-shot systemd service on first boot only.
set -uo pipefail

CONFIG="/etc/bluesignal/config.yaml"
PROVISIONED_FLAG="/etc/bluesignal/.provisioned"

# Already provisioned — exit silently
if [ -f "$PROVISIONED_FLAG" ]; then
    exit 0
fi

# Check if AppKey is the default placeholder
APP_KEY=""
if [ -f "$CONFIG" ]; then
    APP_KEY=$(python3 -c "
import yaml
c = yaml.safe_load(open('$CONFIG')) or {}
print(c.get('app_key', ''))
" 2>/dev/null)
fi

if [ "$APP_KEY" = "00000000000000000000000000000000" ] || [ -z "$APP_KEY" ]; then
    echo "================================================"
    echo " WQM-1 needs provisioning!"
    echo ""
    echo " Option A (CLI):"
    echo "   sudo python3 /opt/bluesignal/scripts/provision.py"
    echo ""
    echo " Option B (Web):"
    echo "   Visit: http://$(hostname).local:8080/provision"
    echo "   PIN: 1234 (change in $CONFIG)"
    echo "================================================"
fi
