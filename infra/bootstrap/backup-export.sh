#!/bin/bash
# Quiesce the service, create a consistent backup, export it, then resume.
# If the stop fails, abort before copying either SQLite store.
set -u

RUN_ID="${1:-}"
[ -n "$RUN_ID" ] || { echo "backup-export: run id is required" >&2; exit 2; }

STATE_DIR="${DR_STATE_DIR:-/etc/delivery-recovery}"
ENV_FILE="${DR_ENV_FILE:-$STATE_DIR/service.env}"
BACKUP_ROOT="${DR_BACKUP_ROOT:-/srv/delivery-recovery/backups}"
DATA_MOUNTPOINT="${DR_DATA_MOUNTPOINT:-/srv/delivery-recovery/data}"
DATA_DIR="${DR_DATA_DIR:-$DATA_MOUNTPOINT/current}"
SERVICE_CONTROL="${DR_SERVICE_CONTROL:-/usr/local/lib/delivery-recovery/service-control.sh}"

fail() { echo "backup-export: $*" >&2; exit 1; }

[ -f "$ENV_FILE" ] || fail "environment file $ENV_FILE is missing"
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a
[ -n "${DELIVERY_RECOVERY_IMAGE:-}" ] || fail "DELIVERY_RECOVERY_IMAGE is not set"

if ! status=$("$SERVICE_CONTROL" status); then
  fail "service status failed; backup aborted"
fi
case "$status" in
  running)
    "$SERVICE_CONTROL" stop || fail "service stop failed; backup aborted"
    restart=1
    ;;
  stopped|exited|created|missing)
    restart=0
    ;;
  *)
    fail "unexpected service status '$status'; backup aborted"
    ;;
esac
cleanup() {
  if [ "$restart" = "1" ]; then
    "$SERVICE_CONTROL" start || echo "backup-export: WARNING: service restart failed" >&2
  fi
}
trap cleanup EXIT

if ! current=$("$SERVICE_CONTROL" status); then
  fail "post-stop service status failed; backup aborted"
fi
case "$current" in
  running)
    fail "service is still running after stop; backup aborted"
    ;;
  stopped|exited|created|missing)
    ;;
  *)
    fail "unexpected post-stop status '$current'; backup aborted"
    ;;
esac

"$SERVICE_CONTROL" verify || fail "data mount verification failed; backup aborted"

mkdir -p "$BACKUP_ROOT" || fail "failed to create backup root"
chown 10001:10001 "$BACKUP_ROOT" || fail "failed to set backup ownership"
mkdir -p "$BACKUP_ROOT/$RUN_ID" || fail "failed to create run backup directory"
chown 10001:10001 "$BACKUP_ROOT/$RUN_ID" || fail "failed to set run backup ownership"

docker run --rm --read-only --tmpfs /tmp:size=64m,mode=1777 \
  --user 10001:10001 \
  -v "$DATA_DIR:/data" \
  -v "$BACKUP_ROOT:/backup" \
  "$DELIVERY_RECOVERY_IMAGE" \
  python scripts/phase4_backup_restore.py create \
    --data-dir /data --out "/backup/$RUN_ID" --include-identity \
  || fail "backup creation failed"

docker run --rm --read-only --tmpfs /tmp:size=64m,mode=1777 \
  --user 10001:10001 \
  -v "$BACKUP_ROOT:/backup" \
  "$DELIVERY_RECOVERY_IMAGE" \
  python scripts/phase4_backup_restore.py export \
    --backup "/backup/$RUN_ID" --archive "/backup/$RUN_ID.tgz" \
  || fail "backup export failed"

echo "backup-export: created $BACKUP_ROOT/$RUN_ID and $BACKUP_ROOT/$RUN_ID.tgz"
trap - EXIT
cleanup
