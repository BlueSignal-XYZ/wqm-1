#!/bin/bash
# Root-filesystem durability for the golden image.
#
# fsck.repair=yes on the kernel command line tells systemd-fsck to fix what
# it finds. tune2fs -c 1 -C 1 makes that check run on every boot, including
# the first, which is what a dirty inode-bitmap checksum needs: the journal
# replay alone left the filesystem marked clean. -e remount-ro stops a
# later boot from writing through an error. -i 0 turns the time interval
# off so the mount count is the only trigger.
#
# An overlayfs read-only root is not applied. The upper layer would have to
# be persistent for OTA (the current symlink and releases/) and for identity
# provisioning, which puts the writes back on the card and breaks rename
# across the overlay boundary. See docs/commissioning-audit.md.
set -u

tune2fs_args() {
  printf '%s\n' "-c 1 -C 1 -i 0 -e remount-ro"
}

apply_cmdline() {
  local file="$1" line
  if [ ! -f "$file" ]; then
    echo "apply-cmdline: $file missing" >&2
    return 1
  fi
  line="$(tr -d '\n' < "$file")"
  case " $line " in
    *" fsck.repair=yes "*) ;;
    *) line="$line fsck.repair=yes" ;;
  esac
  printf '%s\n' "$line" > "$file"
}

apply_fstab() {
  local file="$1" line out opts pass
  if [ ! -f "$file" ]; then
    echo "apply-fstab: $file missing" >&2
    return 1
  fi
  out="$(mktemp)"
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      "" | \#*)
        printf '%s\n' "$line" >> "$out"
        continue
        ;;
    esac
    # shellcheck disable=SC2086
    set -- $line
    if [ "${2:-}" = "/" ]; then
      opts="${4:-defaults}"
      case ",$opts," in
        *,noatime,*) ;;
        *) opts="${opts},noatime" ;;
      esac
      case ",$opts," in
        *,errors=*) ;;
        *) opts="${opts},errors=remount-ro" ;;
      esac
      pass="${6:-0}"
      if [ "$pass" = "0" ]; then
        pass=1
      fi
      printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$1" "$2" "${3:-ext4}" "$opts" "${5:-0}" "$pass" >> "$out"
    else
      printf '%s\n' "$line" >> "$out"
    fi
  done < "$file"
  cat "$out" > "$file"
  rm -f "$out"
}

dry_run() {
  local cmdline="" fstab=""
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --cmdline)
        cmdline="$2"
        shift 2
        ;;
      --fstab)
        fstab="$2"
        shift 2
        ;;
      *)
        echo "unknown dry-run arg: $1" >&2
        return 1
        ;;
    esac
  done
  if [ -z "$cmdline" ] || [ -z "$fstab" ]; then
    echo "dry-run needs --cmdline FILE --fstab FILE" >&2
    return 1
  fi
  apply_cmdline "$cmdline"
  apply_fstab "$fstab"
  echo "cmdline=$(tr -d '\n' < "$cmdline")"
  echo "fstab_root=$(awk '$2 == "/" { print; exit }' "$fstab")"
  echo "tune2fs $(tune2fs_args) <root-device>"
  echo "overlayfs=disabled"
  echo "journald=persistent SyncIntervalSec=30s SystemMaxUse=32M"
  echo "previous_journal=/var/lib/wqm1/journal-previous/previous.journal"
}

usage() {
  echo "usage: rootfs-resilience.sh apply-cmdline FILE | apply-fstab FILE | tune2fs-args | --dry-run --cmdline FILE --fstab FILE" >&2
}

if [ "$#" -lt 1 ]; then
  usage
  exit 1
fi

case "$1" in
  apply-cmdline)
    apply_cmdline "$2"
    ;;
  apply-fstab)
    apply_fstab "$2"
    ;;
  tune2fs-args)
    tune2fs_args
    ;;
  --dry-run)
    shift
    dry_run "$@"
    ;;
  *)
    usage
    exit 1
    ;;
esac
