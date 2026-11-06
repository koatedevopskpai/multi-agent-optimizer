#!/usr/bin/env bash
# Exercise restore-switch on a genuine disposable tmpfs mount.
set -u

ROOT_DIR=$(cd "$(dirname "$0")/../.." && pwd)
SCRIPT="$ROOT_DIR/infra/bootstrap/restore-switch.sh"
MNT=$(mktemp -d /tmp/p4-restore-switch.XXXXXX)
MOCK_DIR=$(mktemp -d /tmp/p4-restore-mock.XXXXXX)
trap 'umount "$MNT" 2>/dev/null || true; rm -rf "$MNT" "$MOCK_DIR"' EXIT
mount -t tmpfs -o size=32m tmpfs "$MNT" || { echo "FAIL mount"; exit 2; }
mkdir -p "$MNT/current" "$MNT/versions" "$MNT/restore/run-1/restored"
echo old > "$MNT/current/application.db"
echo old > "$MNT/current/checkpoints.db"
echo new > "$MNT/restore/run-1/restored/application.db"
echo new > "$MNT/restore/run-1/restored/checkpoints.db"

cat > "$MOCK_DIR/service-control" <<'MOCK'
#!/usr/bin/env bash
echo "service-control $*" >> "$MOCK_DIR/log"
case "$1" in
  status) cat "$MOCK_DIR/state" 2>/dev/null || echo stopped ;;
  stop) echo stopped > "$MOCK_DIR/state" ;;
  start)
    if [ "${START_EXIT:-0}" != "0" ]; then exit "$START_EXIT"; fi
    echo running > "$MOCK_DIR/state"
    ;;
  verify) exit 0 ;;
esac
MOCK
chmod +x "$MOCK_DIR/service-control"
echo running > "$MOCK_DIR/state"
: > "$MOCK_DIR/log"
export MOCK_DIR
export DR_DATA_MOUNTPOINT="$MNT"
export DR_DATA_SUBDIR=current
export DR_SERVICE_CONTROL="$MOCK_DIR/service-control"
PASS=0; FAIL=0
fail(){ echo "FAIL $1: $2"; FAIL=$((FAIL+1)); }
pass(){ echo "PASS $1"; PASS=$((PASS+1)); }

# 1. Switch keeps the mountpoint stable and activates the restored directory.
if bash "$SCRIPT" switch "$MNT/restore/run-1/restored" rel-1 > "$MOCK_DIR/out" 2>&1; then
  if [ "$(cat "$MNT/current/application.db")" = "new" ] && \
     [ "$(cat "$MNT/versions/rel-1/application.db")" = "old" ] && \
     findmnt -n -o TARGET --mountpoint "$MNT" >/dev/null 2>&1; then
    pass switch_stable_mount
  else
    fail switch_stable_mount "data/versions/mount state unexpected"
  fi
else
  fail switch_stable_mount "$(cat "$MOCK_DIR/out")"
fi

# 2. Rollback swaps the previous release back.
if bash "$SCRIPT" rollback rel-1 > "$MOCK_DIR/out" 2>&1; then
  if [ "$(cat "$MNT/current/application.db")" = "old" ]; then
    pass rollback_previous
  else
    fail rollback_previous "current was not restored"
  fi
else
  fail rollback_previous "$(cat "$MOCK_DIR/out")"
fi

# 3. Refuse a restored path outside the data filesystem.
echo outside > "$MOCK_DIR/outside.db"
mkdir -p "$MOCK_DIR/outside"
echo new > "$MOCK_DIR/outside/application.db"
echo new > "$MOCK_DIR/outside/checkpoints.db"
if bash "$SCRIPT" switch "$MOCK_DIR/outside" rel-x > "$MOCK_DIR/out" 2>&1; then
  fail same_filesystem_required "accepted a restored path outside the data mount"
else
  pass same_filesystem_required
fi

# 4. Refuse a plain directory that is not an exact mountpoint.
PLAIN=$(mktemp -d /tmp/p4-plain.XXXXXX)
mkdir -p "$PLAIN/current" "$PLAIN/restore/new"
echo old > "$PLAIN/current/application.db"; echo old > "$PLAIN/current/checkpoints.db"
echo new > "$PLAIN/restore/new/application.db"; echo new > "$PLAIN/restore/new/checkpoints.db"
if DR_DATA_MOUNTPOINT="$PLAIN" bash "$SCRIPT" switch "$PLAIN/restore/new" rel-y > "$MOCK_DIR/out" 2>&1; then
  fail mountpoint_required "accepted a plain directory as the data mount"
else
  pass mountpoint_required
fi
rm -rf "$PLAIN"

# 5. Start failure rolls the previous data back.
echo running > "$MOCK_DIR/state"
mkdir -p "$MNT/restore/fail/restored"
echo new > "$MNT/restore/fail/restored/application.db"; echo new > "$MNT/restore/fail/restored/checkpoints.db"
if START_EXIT=1 bash "$SCRIPT" switch "$MNT/restore/fail/restored" rel-fail > "$MOCK_DIR/out" 2>&1; then
  fail start_failure_rollback "switch succeeded despite failed start"
elif [ "$(cat "$MNT/current/application.db")" = "old" ]; then
  pass start_failure_rollback
else
  fail start_failure_rollback "previous data was not restored"
fi

# 6. Reject traversal and symlink escape outside the mounted data root.
mkdir -p "$MNT/restore/escape"
echo new > "$MNT/restore/escape/application.db"; echo new > "$MNT/restore/escape/checkpoints.db"
if bash "$SCRIPT" switch "$MNT/restore/../../escape" rel-trav > "$MOCK_DIR/out" 2>&1; then
  fail traversal_rejected "accepted .. path traversal"
else
  pass traversal_rejected
fi
ln -sfn "$MOCK_DIR/outside" "$MNT/restore/link"
if bash "$SCRIPT" switch "$MNT/restore/link" rel-link > "$MOCK_DIR/out" 2>&1; then
  fail symlink_escape_rejected "accepted symlink escape"
else
  pass symlink_escape_rejected
fi

# 7. Reject restoring from within current and reusing a release destination.
mkdir -p "$MNT/current/nested"
echo old > "$MNT/current/nested/application.db"; echo old > "$MNT/current/nested/checkpoints.db"
if bash "$SCRIPT" switch "$MNT/current/nested" rel-nested > "$MOCK_DIR/out" 2>&1; then
  fail current_nested_rejected "accepted a restore from inside current"
else
  pass current_nested_rejected
fi
mkdir -p "$MNT/versions/rel-dup" "$MNT/restore/dup"
echo new > "$MNT/restore/dup/application.db"; echo new > "$MNT/restore/dup/checkpoints.db"
if bash "$SCRIPT" switch "$MNT/restore/dup" rel-dup > "$MOCK_DIR/out" 2>&1; then
  fail existing_release_rejected "reused an existing release destination"
else
  pass existing_release_rejected
fi

echo "SUMMARY pass=$PASS fail=$FAIL"
[ "$FAIL" -eq 0 ]
