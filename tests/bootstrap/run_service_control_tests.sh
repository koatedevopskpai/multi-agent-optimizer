#!/usr/bin/env bash
# Execute service-control.sh with mocked docker/data-volume commands.
set -u

ROOT_DIR=$(cd "$(dirname "$0")/../.." && pwd)
SCRIPT="${SERVICE_CONTROL_OVERRIDE:-$ROOT_DIR/infra/bootstrap/service-control.sh}"
TMP_ROOT=$(mktemp -d)
trap 'rm -rf "$TMP_ROOT"' EXIT
PASS=0
FAIL=0

fail() { echo "FAIL $1: $2"; FAIL=$((FAIL + 1)); }
pass() { echo "PASS $1"; PASS=$((PASS + 1)); }

setup() {
  CASE="$1"
  CASE_ROOT="$TMP_ROOT/$CASE"
  rm -rf "$CASE_ROOT"
  mkdir -p "$CASE_ROOT/state" "$CASE_ROOT/data" "$CASE_ROOT/mockbin"
  echo "sentinel" > "$CASE_ROOT/data/keep.txt"
  echo 0 > "$CASE_ROOT/state/exists"
  echo 0 > "$CASE_ROOT/state/running"
  echo 0 > "$CASE_ROOT/state/stop_exit"
  echo 0 > "$CASE_ROOT/state/verify_exit"
  echo 0 > "$CASE_ROOT/state/inspect_exit"
  echo 0 > "$CASE_ROOT/state/compose_ps_exit"
  echo 0 > "$CASE_ROOT/state/stop_keep_running"
  : > "$CASE_ROOT/state/log"
  cat > "$CASE_ROOT/state/service.env" <<EOF
DELIVERY_RECOVERY_IMAGE=old@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
DELIVERY_RECOVERY_DATA_HOST=$CASE_ROOT/data
DELIVERY_RECOVERY_LOG_GROUP=/aws/delivery-recovery/app
AWS_REGION=eu-west-2
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
  if [ "$(cat "$CASE_ROOT/state/exists")" = "1" ]; then
    exit 0
  fi
  echo "Error: No such object: $NAME" >&2
  exit 1
fi
if [ "$cmd" = "compose" ]; then
  shift
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --env-file|-f) shift 2 ;;
      up) echo 1 > "$CASE_ROOT/state/exists"; echo 1 > "$CASE_ROOT/state/running"; exit 0 ;;
      stop) if [ "$(cat "$CASE_ROOT/state/stop_exit")" != "0" ]; then exit 1; fi; echo 0 > "$CASE_ROOT/state/running"; exit 0 ;;
      down) echo 0 > "$CASE_ROOT/state/exists"; echo 0 > "$CASE_ROOT/state/running"; exit 0 ;;
      ps)
        if [ "$(cat "$CASE_ROOT/state/compose_ps_exit")" != "0" ]; then exit "$(cat "$CASE_ROOT/state/compose_ps_exit")"; fi
        if [ "$(cat "$CASE_ROOT/state/running")" = "1" ]; then echo "delivery-recovery"; fi
        exit 0 ;;
      *) shift ;;
    esac
  done
  exit 0
fi
if [ "$cmd" = "inspect" ]; then
  if [ "$(cat "$CASE_ROOT/state/exists")" = "1" ]; then
    if [ "$(cat "$CASE_ROOT/state/running")" = "1" ]; then echo running; else echo exited; fi
    exit 0
  fi
  exit 1
fi
if [ "$cmd" = "start" ]; then
  [ "$(cat "$CASE_ROOT/state/exists")" = "1" ] || exit 1
  echo 1 > "$CASE_ROOT/state/running"
  exit 0
fi
if [ "$cmd" = "stop" ]; then
  if [ "$(cat "$CASE_ROOT/state/stop_exit")" != "0" ]; then exit 1; fi
  if [ "$(cat "$CASE_ROOT/state/stop_keep_running")" = "1" ]; then exit 0; fi
  echo 0 > "$CASE_ROOT/state/running"
  exit 0
fi
if [ "$cmd" = "run" ]; then
  if [ "$(cat "$CASE_ROOT/state/exists")" = "1" ]; then exit 1; fi
  echo 1 > "$CASE_ROOT/state/exists"
  echo 1 > "$CASE_ROOT/state/running"
  exit 0
fi
if [ "$cmd" = "rm" ]; then
  echo 0 > "$CASE_ROOT/state/exists"
  echo 0 > "$CASE_ROOT/state/running"
  exit 0
fi
exit 0
MOCK
  cat > "$CASE_ROOT/mockbin/data-volume.sh" <<'MOCK'
#!/usr/bin/env bash
echo "verify $*" >> "$CASE_ROOT/state/log"
exit "$(cat "$CASE_ROOT/state/verify_exit")"
MOCK
  chmod +x "$CASE_ROOT/mockbin"/*
  echo compose > "$CASE_ROOT/state/mode"
  echo "compose.yaml" > /dev/null
  cat > "$CASE_ROOT/state/compose.yaml" <<'YAML'
services:
  app:
    image: ${DELIVERY_RECOVERY_IMAGE}
    container_name: delivery-recovery
YAML
  export CASE_ROOT
  export DR_STATE_DIR="$CASE_ROOT/state"
  export DR_ENV_FILE="$CASE_ROOT/state/service.env"
  export DR_MODE_FILE="$CASE_ROOT/state/mode"
  export DR_COMPOSE_FILE="$CASE_ROOT/state/compose.yaml"
  export DR_DATA_MOUNTPOINT="$CASE_ROOT/data"
  export DR_VERIFY_SCRIPT="$CASE_ROOT/mockbin/data-volume.sh"
  export DR_DOCKER="$CASE_ROOT/mockbin/docker"
}

run_control() {
  bash "$SCRIPT" "$@" > "$CASE_ROOT/state/out" 2>&1
  RC=$?
}

log_has() { grep -q "$1" "$CASE_ROOT/state/log"; }

# 1. Compose start uses compose, not a hard-coded container name.
setup compose_start
run_control start
if [ "$RC" -ne 0 ] || ! log_has "compose .* up -d"; then
  fail compose_start "compose start did not use compose: $(cat "$CASE_ROOT/state/log")"
elif log_has "docker run"; then
  fail compose_start "compose mode unexpectedly used docker run"
else
  pass compose_start
fi

# 2. Compose stop leaves the named container in place (stopped).
setup compose_stop
echo 1 > "$CASE_ROOT/state/exists"
echo 1 > "$CASE_ROOT/state/running"
run_control stop
if [ "$RC" -ne 0 ] || ! log_has "compose .* stop"; then
  fail compose_stop "compose stop did not run: $(cat "$CASE_ROOT/state/log")"
elif [ "$(cat "$CASE_ROOT/state/exists")" != "1" ]; then
  fail compose_stop "compose stop removed the container unexpectedly"
else
  pass compose_stop
fi

# 3. Compose replace removes then starts.
setup compose_replace
echo 1 > "$CASE_ROOT/state/exists"
echo 1 > "$CASE_ROOT/state/running"
run_control replace
if [ "$RC" -ne 0 ] || ! log_has "compose .* down" || ! log_has "compose .* up -d"; then
  fail compose_replace "replace did not down+up: $(cat "$CASE_ROOT/state/log")"
else
  pass compose_replace
fi

# 4. Run mode creates the fixed-name container when absent.
setup run_start_absent
echo run > "$CASE_ROOT/state/mode"
run_control start
if [ "$RC" -ne 0 ] || ! log_has "docker run -d --name delivery-recovery"; then
  fail run_start_absent "run mode did not create the named container: $(cat "$CASE_ROOT/state/log")"
else
  pass run_start_absent
fi

# 5. Run mode starts an existing stopped container without recreating it.
setup run_start_existing
echo run > "$CASE_ROOT/state/mode"
echo 1 > "$CASE_ROOT/state/exists"
run_control start
if [ "$RC" -ne 0 ] || ! log_has "docker start delivery-recovery"; then
  fail run_start_existing "existing container was not started: $(cat "$CASE_ROOT/state/log")"
elif log_has "docker run"; then
  fail run_start_existing "existing container was recreated"
else
  pass run_start_existing
fi

# 6. Rollback stops/removes/recreates and updates the image, preserving data.
setup run_rollback
echo run > "$CASE_ROOT/state/mode"
echo 1 > "$CASE_ROOT/state/exists"
echo 1 > "$CASE_ROOT/state/running"
run_control rollback "new@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
if [ "$RC" -ne 0 ] || ! log_has "docker rm -f delivery-recovery" || ! log_has "docker run -d --name delivery-recovery"; then
  fail run_rollback "rollback did not remove/recreate: $(cat "$CASE_ROOT/state/log")"
elif ! grep -q "new@sha256:bbbb" "$CASE_ROOT/state/service.env"; then
  fail run_rollback "image was not updated in the environment file"
elif [ ! -f "$CASE_ROOT/data/keep.txt" ]; then
  fail run_rollback "data sentinel was removed"
else
  pass run_rollback
fi

# 7. A failed stop returns non-zero so a backup wrapper can abort.
setup failed_stop
echo run > "$CASE_ROOT/state/mode"
echo 1 > "$CASE_ROOT/state/exists"
echo 1 > "$CASE_ROOT/state/running"
echo 1 > "$CASE_ROOT/state/stop_exit"
run_control stop
if [ "$RC" -eq 0 ]; then
  fail failed_stop "stop failure was swallowed"
else
  pass failed_stop
fi

# 8. A missing mount prevents start.
setup verify_failure
echo run > "$CASE_ROOT/state/mode"
echo 1 > "$CASE_ROOT/state/verify_exit"
run_control start
if [ "$RC" -eq 0 ] || log_has "docker run"; then
  fail verify_failure "application started without a verified data mount"
else
  pass verify_failure
fi

# 9. Stopping an already stopped run-mode service is safe.
setup already_stopped
echo run > "$CASE_ROOT/state/mode"
run_control stop
if [ "$RC" -ne 0 ]; then
  fail already_stopped "stop of absent container was not idempotent"
else
  pass already_stopped
fi

setup inspect_error
echo 125 > "$CASE_ROOT/state/inspect_exit"
run_control status
if [ "$RC" -eq 0 ]; then
  fail inspect_error "status swallowed a docker inspect error"
else
  pass inspect_error
fi

setup compose_ps_error
echo 1 > "$CASE_ROOT/state/exists"
echo 1 > "$CASE_ROOT/state/running"
echo 1 > "$CASE_ROOT/state/compose_ps_exit"
run_control stop
if [ "$RC" -eq 0 ]; then
  fail compose_ps_error "compose status failure was swallowed"
else
  pass compose_ps_error
fi

setup continued_running
echo run > "$CASE_ROOT/state/mode"
echo 1 > "$CASE_ROOT/state/exists"
echo 1 > "$CASE_ROOT/state/running"
echo 1 > "$CASE_ROOT/state/stop_keep_running"
run_control stop
if [ "$RC" -eq 0 ]; then
  fail continued_running "stop reported success while container stayed running"
else
  pass continued_running
fi

echo "SUMMARY pass=$PASS fail=$FAIL"
[ "$FAIL" -eq 0 ]
