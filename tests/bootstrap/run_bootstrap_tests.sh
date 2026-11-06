#!/usr/bin/env bash
# Execute the POSIX bootstrap resolver with mocked block-device commands.
# No real laptop disk is inspected, formatted or mounted.
set -u

ROOT_DIR=$(cd "$(dirname "$0")/../.." && pwd)
SCRIPT="${SCRIPT_OVERRIDE:-$ROOT_DIR/infra/bootstrap/data-volume.sh}"
TMP_ROOT=$(mktemp -d)
trap 'rm -rf "$TMP_ROOT"' EXIT
PASS=0
FAIL=0

fail() {
  echo "FAIL $1: $2"
  FAIL=$((FAIL + 1))
}

pass() {
  echo "PASS $1"
  PASS=$((PASS + 1))
}

make_mocks() {
  local dir="$1/mockbin"
  mkdir -p "$dir"
  cat > "$dir/lsblk" <<'MOCK'
#!/usr/bin/env bash
if [[ "$*" == *"PKNAME"* ]]; then
  cat "$MOCK_STATE/pkname" 2>/dev/null || true
  exit 0
fi
if [[ "$*" == *"-dn"* ]]; then
  n=$(cat "$MOCK_STATE/counter" 2>/dev/null || echo 0)
  n=$((n + 1))
  echo "$n" > "$MOCK_STATE/counter"
  delay=$(cat "$MOCK_STATE/delay" 2>/dev/null || echo 0)
  if [ "$n" -le "$delay" ] && [ "$delay" -gt 0 ]; then
    exit 0
  fi
  cat "$MOCK_STATE/disks"
  exit 0
fi
cat "$MOCK_STATE/disks"
MOCK
  cat > "$dir/findmnt" <<'MOCK'
#!/usr/bin/env bash
echo "findmnt $*" >> "$MOCK_STATE/log"
target=""
mode="target"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --target) mode=target; target=$2; shift 2 ;;
    --mountpoint) mode=mountpoint; target=$2; shift 2 ;;
    *) shift ;;
  esac
done
if [ "$mode" = "mountpoint" ]; then
  if [ "$target" = "$MOUNTPOINT" ] && [ -f "$MOCK_STATE/mounted_source" ]; then
    cat "$MOCK_STATE/mounted_source"
    exit 0
  fi
  exit 1
fi
# Real findmnt --target returns the containing filesystem for an ordinary
# directory. The resolver must not use this mode for exact mount checks.
echo "${MOCK_ROOT_SOURCE:-/dev/nvme1n1p2}"
exit 0
MOCK
  cat > "$dir/blkid" <<'MOCK'
#!/usr/bin/env bash
device="${!#}"
if [[ "$*" == *"-s TYPE"* ]]; then
  if [ -f "$MOCK_STATE/no_filesystem" ]; then
    exit 1
  fi
  cat "$MOCK_STATE/fs_type" 2>/dev/null || { echo "ext4"; exit 0; }
  exit 0
fi
if [[ "$*" == *"-s UUID"* ]]; then
  case "$(basename "$device")" in
    nvme*) echo "uuid-data-5678" ;;
    *) echo "uuid-root-1234" ;;
  esac
  exit 0
fi
exit 2
MOCK
  cat > "$dir/mount" <<'MOCK'
#!/usr/bin/env bash
echo "mount $*" >> "$MOCK_STATE/log"
if [ "${MOCK_MOUNT_EXIT:-0}" != "0" ]; then
  exit "${MOCK_MOUNT_EXIT}"
fi
echo "$1" > "$MOCK_STATE/mounted_source"
exit 0
MOCK
  cat > "$dir/mkfs.ext4" <<'MOCK'
#!/usr/bin/env bash
echo "mkfs.ext4 $*" >> "$MOCK_STATE/log"
exit 0
MOCK
  cat > "$dir/chown" <<'MOCK'
#!/usr/bin/env bash
echo "chown $*" >> "$MOCK_STATE/log"
exit 0
MOCK
  cat > "$dir/sleep" <<'MOCK'
#!/usr/bin/env bash
echo "sleep $*" >> "$MOCK_STATE/log"
exit 0
MOCK
  chmod +x "$dir"/*
}

setup() {
  CASE="$1"
  CASE_ROOT="$TMP_ROOT/$CASE"
  rm -rf "$CASE_ROOT"
  mkdir -p "$CASE_ROOT/root/etc/delivery-recovery" "$CASE_ROOT/root/srv/delivery-recovery/data" "$CASE_ROOT/state"
  make_mocks "$CASE_ROOT"
  export DR_ROOT_PREFIX="$CASE_ROOT/root"
  export MOCK_STATE="$CASE_ROOT/state"
  export MOUNTPOINT="$CASE_ROOT/root/srv/delivery-recovery/data"
  export PATH="$CASE_ROOT/mockbin:$PATH"
  export MOCK_ROOT_SOURCE="/dev/nvme1n1p2"
  export MOCK_MOUNT_EXIT=0
  printf 'nvme1n1 disk vol0aaaaaaaaaaaaaaaa\nnvme0n1 disk vol0123456789abcdef0\n' > "$MOCK_STATE/disks"
  echo "nvme1n1" > "$MOCK_STATE/pkname"
  echo "ext4" > "$MOCK_STATE/fs_type"
  echo 0 > "$MOCK_STATE/delay"
  echo 0 > "$MOCK_STATE/counter"
  : > "$MOCK_STATE/log"
  rm -f "$MOCK_STATE/mounted_source" "$MOCK_STATE/no_filesystem"
}

run_prepare() {
  bash "$SCRIPT" prepare \
    --expected-volume-id vol-0123456789abcdef0 \
    --mountpoint "$MOUNTPOINT" \
    --wait-seconds 6 > "$MOCK_STATE/out" 2>&1
  RC=$?
}

expect_success() {
  local name="$1"
  if [ "$RC" -ne 0 ]; then
    fail "$name" "prepare failed rc=$RC: $(cat "$MOCK_STATE/out")"
    return
  fi
  if ! grep -q "DEVICE=/dev/nvme0n1" "$DR_ROOT_PREFIX/etc/delivery-recovery/data-volume.env"; then
    fail "$name" "state did not bind the expected data device"
    return
  fi
  if ! grep -q "mount /dev/nvme0n1 $MOUNTPOINT" "$MOCK_STATE/log"; then
    fail "$name" "mount did not use the expected data device"
    return
  fi
  pass "$name"
}

expect_failure() {
  local name="$1"
  if [ "$RC" -eq 0 ]; then
    fail "$name" "expected failure but prepare succeeded"
    return
  fi
  if [ "$RC" -eq 127 ] || ! grep -q "data-volume:" "$MOCK_STATE/out"; then
    fail "$name" "failure was not a controlled data-volume error: $(cat "$MOCK_STATE/out")"
    return
  fi
  if grep -q "^mount " "$MOCK_STATE/log"; then
    fail "$name" "mount was attempted after a failed resolution"
    return
  fi
  pass "$name"
}

# 1. Partitioned root and reversed NVMe enumeration: data must be selected by
# stable EBS serial, never by first-disk order.
setup reversed_nvme_partitioned_root
run_prepare
expect_success reversed_nvme_partitioned_root

# 2. The data volume appears after the resolver starts.
setup delayed_attachment
echo 2 > "$MOCK_STATE/delay"
run_prepare
expect_success delayed_attachment

# 3. No volume with the expected serial.
setup missing_disk
printf 'nvme1n1 disk vol0aaaaaaaaaaaaaaaa\n' > "$MOCK_STATE/disks"
run_prepare
expect_failure missing_disk

# 4. Two disks claim the expected serial.
setup ambiguous_disk
printf 'nvme1n1 disk vol0aaaaaaaaaaaaaaaa\nnvme0n1 disk vol0123456789abcdef0\nnvme2n1 disk vol0123456789abcdef0\n' > "$MOCK_STATE/disks"
run_prepare
expect_failure ambiguous_disk

# 5. An existing ext4 filesystem is preserved.
setup existing_ext4
run_prepare
if [ "$RC" -ne 0 ]; then
  fail existing_ext4 "prepare failed rc=$RC: $(cat "$MOCK_STATE/out")"
elif grep -q "mkfs.ext4" "$MOCK_STATE/log"; then
  fail existing_ext4 "existing filesystem was reformatted"
elif grep -q -- "--target $MOUNTPOINT" "$MOCK_STATE/log"; then
  fail existing_ext4 "used findmnt --target instead of an exact mountpoint check"
else
  pass existing_ext4
fi

# 6. A blank volume is formatted once.
setup no_filesystem
touch "$MOCK_STATE/no_filesystem"
run_prepare
if [ "$RC" -ne 0 ]; then
  fail no_filesystem "prepare failed rc=$RC: $(cat "$MOCK_STATE/out")"
elif ! grep -q "mkfs.ext4 -L delivery-recovery-data /dev/nvme0n1" "$MOCK_STATE/log"; then
  fail no_filesystem "blank volume was not formatted on the resolved device"
else
  pass no_filesystem
fi

# 7. An existing non-ext4 filesystem fails closed without formatting.
setup existing_non_ext4
echo "xfs" > "$MOCK_STATE/fs_type"
run_prepare
if [ "$RC" -eq 0 ]; then
  fail existing_non_ext4 "expected refusal to reformat xfs"
elif [ "$RC" -eq 127 ] || ! grep -q "data-volume:" "$MOCK_STATE/out"; then
  fail existing_non_ext4 "failure was not a controlled data-volume error: $(cat "$MOCK_STATE/out")"
elif grep -q "mkfs.ext4" "$MOCK_STATE/log"; then
  fail existing_non_ext4 "non-ext4 filesystem was reformatted"
else
  pass existing_non_ext4
fi

# 7. A mount failure stops before app start.
setup failed_mount
export MOCK_MOUNT_EXIT=1
run_prepare
if [ "$RC" -eq 0 ]; then
  fail failed_mount "expected mount failure"
elif ! grep -q "data-volume:" "$MOCK_STATE/out"; then
  fail failed_mount "failure was not a controlled data-volume error: $(cat "$MOCK_STATE/out")"
elif ! grep -q "^mount " "$MOCK_STATE/log"; then
  fail failed_mount "mount was not attempted"
else
  pass failed_mount
fi

# 8. The expected serial is on the root disk: never format the root disk.
setup expected_serial_on_root
printf 'nvme1n1 disk vol0123456789abcdef0\n' > "$MOCK_STATE/disks"
run_prepare
expect_failure expected_serial_on_root

# 9. Verify survives a stop/start with a different NVMe enumeration but the
# same volume identity and filesystem UUID.
setup renamed_nvme_after_restart
run_prepare
echo "nvme3n1 disk vol0123456789abcdef0" > "$MOCK_STATE/disks"
echo "/dev/nvme3n1" > "$MOCK_STATE/mounted_source"
if bash "$SCRIPT" verify --mountpoint "$MOUNTPOINT" > "$MOCK_STATE/verify" 2>&1; then
  pass renamed_nvme_after_restart
else
  fail renamed_nvme_after_restart "verify rejected the same volume under a new NVMe name: $(cat "$MOCK_STATE/verify")"
fi

# 10. Verify catches a mounted source that is not the resolved data device.
setup verify_mismatch
run_prepare
echo "/dev/other" > "$MOCK_STATE/mounted_source"
if bash "$SCRIPT" verify --mountpoint "$MOUNTPOINT" > "$MOCK_STATE/verify" 2>&1; then
  fail verify_mismatch "verify accepted the wrong mounted source"
elif grep -q "data-volume:" "$MOCK_STATE/verify"; then
  pass verify_mismatch
else
  fail verify_mismatch "verify did not report a controlled mount error: $(cat "$MOCK_STATE/verify")"
fi

# 10. Verify fails if the mount disappears (restart safety).
setup verify_missing_mount
run_prepare
rm -f "$MOCK_STATE/mounted_source"
if bash "$SCRIPT" verify --mountpoint "$MOUNTPOINT" > "$MOCK_STATE/verify" 2>&1; then
  fail verify_missing_mount "verify accepted an unmounted data path"
elif grep -q "data-volume:" "$MOCK_STATE/verify"; then
  pass verify_missing_mount
else
  fail verify_missing_mount "verify did not report a controlled mount error: $(cat "$MOCK_STATE/verify")"
fi

echo "SUMMARY pass=$PASS fail=$FAIL"
[ "$FAIL" -eq 0 ]
