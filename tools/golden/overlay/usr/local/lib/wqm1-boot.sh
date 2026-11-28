# Shared helpers for the golden-image boot scripts.
# Sourced, not executed. The first-boot shell and src/utils/cardmove.py
# write the same card-binding file; keep the four keys in this order.

status_dest() {
  if [ -n "${WQM1_STATUS_FILE:-}" ]; then
    mkdir -p "$(dirname "$WQM1_STATUS_FILE")" || return 1
    printf '%s' "$WQM1_STATUS_FILE"
    return 0
  fi
  local d
  for d in /boot/firmware /boot; do
    # A bare /boot on a build machine is not a Pi card. Require the FAT
    # boot files so a test run as root does not publish onto the host.
    if [ -d "$d" ] && [ -w "$d" ] && { [ -f "$d/cmdline.txt" ] || [ -f "$d/config.txt" ]; }; then
      printf '%s' "$d/wqm1-status.txt"
      return 0
    fi
  done
  return 1
}

state_file() {
  if [ -n "${WQM1_FIRSTBOOT_STATE:-}" ]; then
    mkdir -p "$(dirname "$WQM1_FIRSTBOOT_STATE")" || return 1
    printf '%s' "$WQM1_FIRSTBOOT_STATE"
    return 0
  fi
  if [ -e /proc/device-tree/model ]; then
    mkdir -p /var/lib/wqm1-firstboot || return 1
    printf '%s' /var/lib/wqm1-firstboot/state
    return 0
  fi
  return 1
}

card_file() {
  if [ -n "${WQM1_CARD_STATE:-}" ]; then
    mkdir -p "$(dirname "$WQM1_CARD_STATE")" || return 1
    printf '%s' "$WQM1_CARD_STATE"
    return 0
  fi
  if [ -e /proc/device-tree/model ]; then
    mkdir -p /var/lib/wqm1 || return 1
    printf '%s' /var/lib/wqm1/card-binding
    return 0
  fi
  return 1
}

atomic_file() {
  # stdin -> $1, fsync, rename. Returns non-zero when the directory
  # cannot be created; callers treat that as "not this machine".
  local dest="$1" tmp
  tmp="${dest}.tmp"
  mkdir -p "$(dirname "$dest")" || return 1
  cat > "$tmp" || return 1
  sync -f "$tmp" 2>/dev/null || true
  mv -f "$tmp" "$dest" || return 1
  sync -f "$(dirname "$dest")" 2>/dev/null || true
}

upsert_line() {
  local key="$1" value="$2" file tmp line found
  file="$(status_dest)" || return 0
  tmp="${file}.tmp.$$"
  found=0
  if [ -f "$file" ]; then
    : > "$tmp"
    while IFS= read -r line || [ -n "$line" ]; do
      case "$line" in
        "${key}:"*)
          printf '%s: %s\n' "$key" "$value" >> "$tmp"
          found=1
          ;;
        *)
          printf '%s\n' "$line" >> "$tmp"
          ;;
      esac
    done < "$file"
    if [ "$found" -eq 0 ]; then
      printf '%s: %s\n' "$key" "$value" >> "$tmp"
    fi
  else
    printf '%s: %s\n' "$key" "$value" > "$tmp"
  fi
  sync -f "$tmp" 2>/dev/null || true
  mv -f "$tmp" "$file"
  sync -f "$(dirname "$file")" 2>/dev/null || true
}

set_boot_stage() {
  upsert_line "BOOT" "stage=$1"
}

replace_status_with_stage() {
  # First writer of the boot. Drop yesterday's COMMISSION line so a card
  # that dies before the hotspot is not still labelled READY.
  local file
  file="$(status_dest)" || return 0
  printf 'BOOT: stage=%s\n' "$1" | atomic_file "$file" || return 0
}

mark_step() {
  local step="$1" phase="$2" file tmp s p
  file="$(state_file)" || return 0
  tmp="${file}.tmp"
  : > "$tmp"
  for s in board radio card hostname done; do
    if [ "$s" = "$step" ]; then
      p="$phase"
    elif [ -f "$file" ]; then
      p="$(awk -F= -v k="$s" '$1 == k { print substr($0, index($0, "=") + 1); exit }' "$file" 2>/dev/null || true)"
    else
      p=""
    fi
    if [ -n "${p:-}" ]; then
      printf '%s=%s\n' "$s" "$p" >> "$tmp"
    fi
  done
  sync -f "$tmp" 2>/dev/null || true
  mv -f "$tmp" "$file"
  sync -f "$(dirname "$file")" 2>/dev/null || true
}

step_was_interrupted() {
  local step="$1" file phase
  file="$(state_file)" || return 1
  [ -f "$file" ] || return 1
  phase="$(awk -F= -v k="$step" '$1 == k { print substr($0, index($0, "=") + 1); exit }' "$file" 2>/dev/null || true)"
  [ "$phase" = "started" ]
}

read_pi_serial() {
  local cpuinfo="${WQM1_CPUINFO:-/proc/cpuinfo}" raw ser
  raw="$(awk -F': *' '/^Serial/ { gsub(/[^0-9a-fA-F]/, "", $2); print tolower($2); exit }' "$cpuinfo" 2>/dev/null || true)"
  raw="${raw//[^0-9a-f]/}"
  if [ -z "$raw" ]; then
    printf '%s' ""
    return 0
  fi
  ser="${raw: -16}"
  while [ "${#ser}" -lt 16 ]; do
    ser="0${ser}"
  done
  printf '%s' "$ser"
}

binding_read() {
  local key="$1" file
  file="$(card_file)" || return 0
  [ -f "$file" ] || return 0
  awk -F= -v k="$key" '$1 == k { print substr($0, index($0, "=") + 1); exit }' "$file" 2>/dev/null || true
}

binding_write() {
  local file
  file="$(card_file)" || return 0
  {
    printf 'commissioned_serial=%s\n' "${1:-}"
    printf 'moved_from=%s\n' "${2:-}"
    printf 'moved_to=%s\n' "${3:-}"
    printf 'wifi_reconfirm=%s\n' "${4:-0}"
  } | atomic_file "$file"
}

observe_card() {
  # $1 serial, $2 site: 1 saved site wifi, 0 none, u could not ask.
  # Same rules as utils.cardmove.observe.
  local serial="$1" site="$2" comm recon
  serial="${serial:-}"
  if [ -z "$serial" ] || [ "$serial" = "0000000000000000" ]; then
    return 0
  fi
  comm="$(binding_read commissioned_serial)"
  recon="$(binding_read wifi_reconfirm)"
  recon="${recon:-0}"
  if [ -z "$comm" ]; then
    binding_write "$serial" "" "" 0
    return 0
  fi
  if [ "$comm" = "$serial" ]; then
    if [ "$recon" != "1" ]; then
      binding_write "$comm" "" "" 0
    fi
    return 0
  fi
  if [ "$site" != "0" ]; then
    binding_write "$comm" "$comm" "$serial" 1
  else
    binding_write "$serial" "$comm" "$serial" 0
  fi
}
