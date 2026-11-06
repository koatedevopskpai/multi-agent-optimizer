#!/usr/bin/env bash
# Exercise the real service-control controller from backup-export.
set -u

ROOT_DIR=$(cd "$(dirname "$0")/../.." && pwd)
BACKUP="${BACKUP_OVERRIDE:-$ROOT_DIR/infra/bootstrap/backup-export.sh}"
CONTROL="${SERVICE_CONTROL_OVERRIDE:-$ROOT_DIR/infra/bootstrap/service-control.sh}"
TMP_ROOT=$(mktemp -d)
trap 'rm -rf "$TMP_ROOT"' EXIT
PASS=0; FAIL=0
fail(){ echo "FAIL $1: $2"; FAIL=$((FAIL+1)); }
pass(){ echo "PASS $1"; PASS=$((PASS+1)); }

setup() {
  CASE="$1"; CASE_ROOT="$TMP_ROOT/$CASE"
  mkdir -p "$CASE_ROOT/state" "$CASE_ROOT/backups" "$CASE_ROOT/data" "$CASE_ROOT/mockbin"
  echo 0 > "$CASE_ROOT/state/exists"
  echo 0 > "$CASE_ROOT/state/running"
  echo 0 > "$CASE_ROOT/state/stop_exit"
  echo 0 > "$CASE_ROOT/state/verify_exit"
  echo 0 > "$CASE_ROOT/state/inspect_exit"
  echo 0 > "$CASE_ROOT/state/stop_keep_running"
  echo 0 > "$CASE_ROOT/state/run_count"
  : > "$CASE_ROOT/state/log"
  echo run > "$CASE_ROOT/state/mode"
  cat > "$CASE_ROOT/state/service.env" <<EOF
DELIVERY_RECOVERY_IMAGE=image@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc
EOF
  cat > "$CASE_ROOT/mockbin/docker" <<'MOCK'
#!/usr/bin/env bash
echo "docker $*" >> "$CASE_ROOT/state/log"
cmd=${1:-}
if [ "$cmd" = "container" ] && [ "${2:-}" = "inspect" ]; then
  if [ "$(cat "$CASE_ROOT/state/inspect_exit")" != "0" ]; then
    echo "Error response from daemon: simulated inspect failure" >&2
    exit "$(cat "$CASE_ROOT/state/inspect_exit")"
  fi
  if [ "$(cat "$CASE_ROOT/state/exists")" = "1" ]; then exit 0; fi
  echo "Error: No such object: delivery-recovery" >&2
  exit 1
fi
if [ "$cmd" = "inspect" ]; then
  if [ "$(cat "$CASE_ROOT/state/running")" = "1" ]; then echo running; else echo exited; fi
  exit 0
fi
if [ "$cmd" = "stop" ]; then
  if [ "$(cat "$CASE_ROOT/state/stop_exit")" != "0" ]; then exit 1; fi
  if [ "$(cat "$CASE_ROOT/state/stop_keep_running")" = "1" ]; then exit 0; fi
  echo 0 > "$CASE_ROOT/state/running"
  exit 0
fi
if [ "$cmd" = "start" ]; then
  echo 1 > "$CASE_ROOT/state/running"
  exit 0
fi
if [ "$cmd" = "run" ]; then
  n=$(cat "$CASE_ROOT/state/run_count"); echo $((n+1)) > "$CASE_ROOT/state/run_count"
  exit 0
fi
if [ "$cmd" = "rm" ]; then echo 0 > "$CASE_ROOT/state/exists"; exit 0; fi
exit 0
MOCK
  cat > "$CASE_ROOT/mockbin/data-volume.sh" <<'MOCK'
#!/usr/bin/env bash
echo "verify $*" >> "$CASE_ROOT/state/log"
exit "$(cat "$CASE_ROOT/state/verify_exit")"
MOCK
  cat > "$CASE_ROOT/mockbin/chown" <<'MOCK'
#!/usr/bin/env bash
exit 0
MOCK
  chmod +x "$CASE_ROOT/mockbin"/*
  export CASE_ROOT
  export DR_STATE_DIR="$CASE_ROOT/state"
  export DR_ENV_FILE="$CASE_ROOT/state/service.env"
  export DR_MODE_FILE="$CASE_ROOT/state/mode"
  export DR_DATA_MOUNTPOINT="$CASE_ROOT/data"
  export DR_DATA_DIR="$CASE_ROOT/data/current"
  mkdir -p "$DR_DATA_DIR"
  export DR_BACKUP_ROOT="$CASE_ROOT/backups"
  export DR_SERVICE_CONTROL="$CONTROL"
  export DR_VERIFY_SCRIPT="$CASE_ROOT/mockbin/data-volume.sh"
  export DR_DOCKER="$CASE_ROOT/mockbin/docker"
  export PATH="$CASE_ROOT/mockbin:$PATH"
}

run_backup() {
  bash "$BACKUP" test-run > "$CASE_ROOT/state/out" 2>&1
  RC=$?
}

setup stop_failure
echo 1 > "$CASE_ROOT/state/exists"; echo 1 > "$CASE_ROOT/state/running"; echo 1 > "$CASE_ROOT/state/stop_exit"
run_backup
if [ "$RC" -eq 0 ] || [ "$(cat "$CASE_ROOT/state/run_count")" != "0" ]; then
  fail stop_failure "copy started after a failed stop"
else
  pass stop_failure
fi

setup mount_failure
echo 1 > "$CASE_ROOT/state/exists"; echo 1 > "$CASE_ROOT/state/running"; echo 1 > "$CASE_ROOT/state/verify_exit"
run_backup
if [ "$RC" -eq 0 ] || [ "$(cat "$CASE_ROOT/state/run_count")" != "0" ]; then
  fail mount_failure "copy started without mount verification"
else
  pass mount_failure
fi

setup discovery_failure
echo 1 > "$CASE_ROOT/state/exists"; echo 125 > "$CASE_ROOT/state/inspect_exit"
run_backup
if [ "$RC" -eq 0 ] || [ "$(cat "$CASE_ROOT/state/run_count")" != "0" ]; then
  fail discovery_failure "inspect error was treated as stopped"
else
  pass discovery_failure
fi

setup continued_running
echo 1 > "$CASE_ROOT/state/exists"; echo 1 > "$CASE_ROOT/state/running"; echo 1 > "$CASE_ROOT/state/stop_keep_running"
run_backup
if [ "$RC" -eq 0 ] || [ "$(cat "$CASE_ROOT/state/run_count")" != "0" ]; then
  fail continued_running "copy started while the container remained running"
else
  pass continued_running
fi

setup success
echo 1 > "$CASE_ROOT/state/exists"; echo 1 > "$CASE_ROOT/state/running"
run_backup
if [ "$RC" -ne 0 ]; then
  fail success "backup/export failed: $(cat "$CASE_ROOT/state/out")"
elif [ "$(cat "$CASE_ROOT/state/run_count")" != "2" ]; then
  fail success "expected create+export container runs"
elif ! grep -q "docker start delivery-recovery" "$CASE_ROOT/state/log"; then
  fail success "service was not restarted"
else
  pass success
fi

echo "SUMMARY pass=$PASS fail=$FAIL"
[ "$FAIL" -eq 0 ]
