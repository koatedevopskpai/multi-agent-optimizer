#!/bin/bash
# Resolve the Terraform-managed data EBS volume by its stable Nitro serial and
# mount it fail-closed. This script never uses first-disk ordering.
set -u

ROOT_PREFIX="${DR_ROOT_PREFIX:-}"
STATE_DIR="${ROOT_PREFIX}/etc/delivery-recovery"
STATE_FILE="${STATE_DIR}/data-volume.env"
FSTAB="${ROOT_PREFIX}/etc/fstab"
MODE=""
EXPECTED_VOLUME_ID=""
MOUNTPOINT=""
WAIT_SECONDS="${DR_WAIT_SECONDS:-180}"
POLL_SECONDS="${DR_POLL_SECONDS:-2}"

fail() {
  echo "data-volume: $*" >&2
  exit 1
}

normalize_volume_id() {
  echo "$1" | tr '[:upper:]' '[:lower:]' | tr -d ' -'
}

usage() {
  echo "usage: data-volume.sh prepare --expected-volume-id ID --mountpoint PATH [--wait-seconds N]" >&2
  echo "       data-volume.sh verify --mountpoint PATH" >&2
  exit 2
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    prepare|verify) MODE="$1"; shift ;;
    --expected-volume-id) EXPECTED_VOLUME_ID="$2"; shift 2 ;;
    --mountpoint) MOUNTPOINT="$2"; shift 2 ;;
    --wait-seconds) WAIT_SECONDS="$2"; shift 2 ;;
    *) usage ;;
  esac
done

[ -n "$MODE" ] && [ -n "$MOUNTPOINT" ] || usage

if [ "$MODE" = "prepare" ]; then
  [ -n "$EXPECTED_VOLUME_ID" ] || usage
  EXPECTED_VOLUME_ID_NORM=$(normalize_volume_id "$EXPECTED_VOLUME_ID")
  case "$EXPECTED_VOLUME_ID_NORM" in
    vol[0-9a-f]*) ;;
    *) fail "volume id must be a Terraform vol-... or Nitro vol... identifier" ;;
  esac
fi

root_source=$(findmnt -n -o SOURCE --target "${ROOT_PREFIX}/" 2>/dev/null) \
  || fail "root mount source was not found"
[ -n "$root_source" ] || fail "root mount source was empty"

root_disk=$(lsblk -no PKNAME "$root_source" 2>/dev/null | head -n 1)
if [ -z "$root_disk" ]; then
  root_disk=$(basename "$root_source")
fi
[ -n "$root_disk" ] || fail "root disk could not be determined"

resolve_disk() {
  local wants_wait="$1"
  local attempts=1
  if [ "$wants_wait" = "wait" ]; then
    attempts=$((WAIT_SECONDS / 2))
    [ "$attempts" -ge 1 ] || attempts=1
  fi
  local attempt=1
  local candidates
  local name type serial
  while [ "$attempt" -le "$attempts" ]; do
    candidates=()
    while read -r name type serial; do
      [ "$type" = "disk" ] || continue
      [ "$(normalize_volume_id "$serial")" = "$EXPECTED_VOLUME_ID_NORM" ] || continue
      candidates+=("$name")
    done < <(lsblk -dn -o NAME,TYPE,SERIAL 2>/dev/null || true)
    if [ "${#candidates[@]}" -eq 1 ]; then
      printf '/dev/%s\n' "${candidates[0]}"
      return 0
    fi
    if [ "${#candidates[@]}" -gt 1 ]; then
      fail "more than one disk matches volume id $EXPECTED_VOLUME_ID"
    fi
    if [ "$attempt" -lt "$attempts" ]; then
      sleep "$POLL_SECONDS"
    fi
    attempt=$((attempt + 1))
  done
  fail "expected data volume $EXPECTED_VOLUME_ID was not found"
}

if [ "$MODE" = "verify" ]; then
  [ -f "$STATE_FILE" ] || fail "data-volume state file is missing"
  state_mount=$(sed -n 's/^DATA_MOUNTPOINT=//p' "$STATE_FILE")
  state_device=$(sed -n 's/^DATA_DEVICE=//p' "$STATE_FILE")
  state_uuid=$(sed -n 's/^DATA_UUID=//p' "$STATE_FILE")
  EXPECTED_VOLUME_ID=$(sed -n 's/^DATA_VOLUME_ID=//p' "$STATE_FILE")
  EXPECTED_VOLUME_ID_NORM=$(normalize_volume_id "$EXPECTED_VOLUME_ID")
  [ "$state_mount" = "$MOUNTPOINT" ] \
    || fail "state mountpoint $state_mount does not match $MOUNTPOINT"
  [ -n "$state_device" ] && [ -n "$state_uuid" ] && [ -n "$EXPECTED_VOLUME_ID_NORM" ] \
    || fail "data-volume state is incomplete"
  current_device=$(resolve_disk no) || exit 1
  current_uuid=$(blkid -s UUID -o value "$current_device" 2>/dev/null || true)
  [ "$current_uuid" = "$state_uuid" ] \
    || fail "resolved volume filesystem UUID does not match state"
  mounted_source=$(findmnt -n -o SOURCE --mountpoint "$MOUNTPOINT" 2>/dev/null) \
    || fail "data mountpoint $MOUNTPOINT is not an exact mountpoint"
  mounted_uuid=$(blkid -s UUID -o value "$mounted_source" 2>/dev/null || true)
  [ "$mounted_uuid" = "$state_uuid" ] \
    || fail "mounted filesystem UUID does not match the approved data volume"
  echo "data-volume: verified volume $EXPECTED_VOLUME_ID and UUID at $MOUNTPOINT"
  exit 0
fi

device=$(resolve_disk wait) || exit 1
device_name=$(basename "$device")
if [ "$device_name" = "$root_disk" ]; then
  fail "expected volume $EXPECTED_VOLUME_ID resolves to the root disk"
fi

fs_type=$(blkid -o value -s TYPE "$device" 2>/dev/null || true)
if [ -z "$fs_type" ]; then
  mkfs.ext4 -L delivery-recovery-data "$device" >/dev/null \
    || fail "failed to format $device"
  fs_type="ext4"
elif [ "$fs_type" != "ext4" ]; then
  fail "refusing to reformat $device with existing filesystem $fs_type"
fi

uuid=$(blkid -s UUID -o value "$device" 2>/dev/null) \
  || fail "filesystem UUID was not found for $device"
[ -n "$uuid" ] || fail "filesystem UUID was empty for $device"

mkdir -p "$MOUNTPOINT" "$STATE_DIR" || fail "failed to create mount/state paths"

mounted_source=$(findmnt -n -o SOURCE --mountpoint "$MOUNTPOINT" 2>/dev/null || true)
if [ -n "$mounted_source" ]; then
  mounted_uuid=$(blkid -s UUID -o value "$mounted_source" 2>/dev/null || true)
  [ "$mounted_uuid" = "$uuid" ] \
    || fail "$MOUNTPOINT is already mounted from a different filesystem"
else
  mount "$device" "$MOUNTPOINT" || fail "mount failed for $device"
fi

mounted_source=$(findmnt -n -o SOURCE --mountpoint "$MOUNTPOINT" 2>/dev/null) \
  || fail "mountpoint $MOUNTPOINT is not an exact mountpoint after mount"
mounted_uuid=$(blkid -s UUID -o value "$mounted_source" 2>/dev/null || true)
[ "$mounted_uuid" = "$uuid" ] \
  || fail "mounted filesystem UUID does not match $device"

case "$MOUNTPOINT" in
  "/"|"") fail "refusing to use the root directory as the data mountpoint" ;;
esac

chown -R 10001:10001 "$MOUNTPOINT" || fail "failed to set data ownership"
touch "$FSTAB" || fail "failed to open fstab"
if ! grep -q "UUID=$uuid" "$FSTAB"; then
  echo "UUID=$uuid $MOUNTPOINT ext4 defaults 0 2" >> "$FSTAB" \
    || fail "failed to write fstab entry"
fi

cat > "$STATE_FILE" <<EOF
DATA_DEVICE=$device
DATA_VOLUME_ID=$EXPECTED_VOLUME_ID
DATA_MOUNTPOINT=$MOUNTPOINT
DATA_UUID=$uuid
EOF
[ -s "$STATE_FILE" ] || fail "failed to write data-volume state"
echo "data-volume: bound $device ($EXPECTED_VOLUME_ID) at $MOUNTPOINT"
exit 0
