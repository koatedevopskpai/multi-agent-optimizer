#!/bin/bash
# Switch a verified restored data set into the stable `current` directory on the
# already-mounted data filesystem. Never rename the mountpoint itself.
set -u

MODE="${1:-}"
DATA_ROOT="${DR_DATA_MOUNTPOINT:-/srv/delivery-recovery/data}"
DATA_SUBDIR="${DR_DATA_SUBDIR:-current}"
CURRENT="$DATA_ROOT/$DATA_SUBDIR"
VERSIONS="$DATA_ROOT/versions"
SERVICE_CONTROL="${DR_SERVICE_CONTROL:-/usr/local/lib/delivery-recovery/service-control.sh}"

fail() { echo "restore-switch: $*" >&2; exit 1; }

canonicalize() {
  realpath -m -- "$1" || fail "failed to canonicalize $1"
}

require_mount() {
  DATA_ROOT_REAL=$(canonicalize "$DATA_ROOT")
  findmnt -n -o TARGET --mountpoint "$DATA_ROOT_REAL" >/dev/null 2>&1 \
    || fail "$DATA_ROOT is not an exact mounted filesystem"
}

require_release_id() {
  case "$1" in
    ""|*/*|*..*) fail "release id must be a simple name" ;;
  esac
}

same_filesystem() {
  local left right
  left=$(stat -c %d "$1") || fail "cannot stat $1"
  right=$(stat -c %d "$2") || fail "cannot stat $2"
  [ "$left" = "$right" ] || fail "$1 and $2 are on different filesystems"
}

stop_verified() {
  "$SERVICE_CONTROL" stop || fail "service stop failed"
  status=$("$SERVICE_CONTROL" status) || fail "service status failed"
  [ "$status" != "running" ] || fail "service is still running"
}

case "$MODE" in
  switch)
    RESTORED="${2:-}"
    RELEASE_ID="${3:-}"
    [ -n "$RESTORED" ] && [ -n "$RELEASE_ID" ] \
      || fail "usage: restore-switch.sh switch RESTORED_DIR RELEASE_ID"
    require_release_id "$RELEASE_ID"
    require_mount
    CURRENT_REAL=$(canonicalize "$CURRENT")
    VERSIONS_REAL=$(canonicalize "$VERSIONS")
    RESTORED_REAL=$(canonicalize "$RESTORED")
    case "$RESTORED_REAL" in
      "$DATA_ROOT_REAL"/*) ;;
      *) fail "restored directory must be inside the mounted data root" ;;
    esac
    case "$RESTORED_REAL" in
      "$CURRENT_REAL"|"$CURRENT_REAL"/*) fail "cannot restore from within current" ;;
    esac
    [ ! -e "$VERSIONS_REAL/$RELEASE_ID" ] \
      || fail "release destination $RELEASE_ID already exists"
    [ -f "$RESTORED_REAL/application.db" ] && [ -f "$RESTORED_REAL/checkpoints.db" ] \
      || fail "restored directory lacks application.db/checkpoints.db"
    [ "$RESTORED_REAL" != "$CURRENT_REAL" ] || fail "restored path is already current"
    stop_verified
    same_filesystem "$DATA_ROOT_REAL" "$RESTORED_REAL"
    mkdir -p "$VERSIONS_REAL" || fail "failed to create versions directory"
    chown 10001:10001 "$RESTORED_REAL" || fail "failed to set restored ownership"
    chown 10001:10001 "$VERSIONS_REAL" || fail "failed to set versions ownership"
    if [ -e "$CURRENT_REAL" ]; then
      same_filesystem "$CURRENT_REAL" "$RESTORED_REAL"
      mv "$CURRENT_REAL" "$VERSIONS_REAL/$RELEASE_ID" \
        || fail "failed to move current to versions/$RELEASE_ID"
    fi
    if ! mv "$RESTORED_REAL" "$CURRENT_REAL"; then
      [ -e "$VERSIONS_REAL/$RELEASE_ID" ] && mv "$VERSIONS_REAL/$RELEASE_ID" "$CURRENT_REAL"
      fail "failed to move restored data into current"
    fi
    chown -R 10001:10001 "$CURRENT_REAL" || fail "failed to set current ownership"
    "$SERVICE_CONTROL" verify || fail "restored data mount verification failed"
    if ! "$SERVICE_CONTROL" start; then
      failed="$VERSIONS_REAL/failed-$RELEASE_ID"
      mv "$CURRENT_REAL" "$failed"
      [ -e "$VERSIONS_REAL/$RELEASE_ID" ] && mv "$VERSIONS_REAL/$RELEASE_ID" "$CURRENT_REAL"
      chown -R 10001:10001 "$CURRENT_REAL"
      fail "service failed to start; previous data restored"
    fi
    echo "restore-switch: activated $CURRENT from release $RELEASE_ID"
    ;;
  rollback)
    RELEASE_ID="${2:-}"
    [ -n "$RELEASE_ID" ] || fail "usage: restore-switch.sh rollback RELEASE_ID"
    require_release_id "$RELEASE_ID"
    require_mount
    CURRENT_REAL=$(canonicalize "$CURRENT")
    VERSIONS_REAL=$(canonicalize "$VERSIONS")
    [ -d "$VERSIONS_REAL/$RELEASE_ID" ] || fail "release $RELEASE_ID is unavailable"
    same_filesystem "$DATA_ROOT_REAL" "$VERSIONS_REAL/$RELEASE_ID"
    stop_verified
    failed="$VERSIONS_REAL/failed-$(date -u +%Y%m%dT%H%M%SZ)"
    if [ -e "$CURRENT_REAL" ]; then
      mv "$CURRENT_REAL" "$failed" || fail "failed to preserve current data"
    fi
    mv "$VERSIONS_REAL/$RELEASE_ID" "$CURRENT_REAL" || fail "failed to restore release"
    chown -R 10001:10001 "$CURRENT_REAL" || fail "failed to set current ownership"
    "$SERVICE_CONTROL" verify || fail "rollback mount verification failed"
    "$SERVICE_CONTROL" start || fail "rollback service start failed"
    echo "restore-switch: rolled back to $RELEASE_ID; failed copy at $failed"
    ;;
  *)
    fail "usage: restore-switch.sh {switch RESTORED_DIR RELEASE_ID|rollback RELEASE_ID}"
    ;;
esac
