#!/bin/bash
# Single service-control abstraction for Compose and docker-run modes.
# A stopped container keeps its name, so replace always removes before start.
set -u

NAME="${DELIVERY_RECOVERY_CONTAINER_NAME:-delivery-recovery}"
STATE_DIR="${DR_STATE_DIR:-/etc/delivery-recovery}"
ENV_FILE="${DR_ENV_FILE:-$STATE_DIR/service.env}"
MODE_FILE="${DR_MODE_FILE:-$STATE_DIR/mode}"
COMPOSE_FILE="${DR_COMPOSE_FILE:-/opt/delivery-recovery/compose.yaml}"
DATA_MOUNTPOINT="${DR_DATA_MOUNTPOINT:-/srv/delivery-recovery/data}"
DATA_SUBDIR="${DR_DATA_SUBDIR:-current}"
DATA_DIR="${DR_DATA_DIR:-$DATA_MOUNTPOINT/$DATA_SUBDIR}"
VERIFY_SCRIPT="${DR_VERIFY_SCRIPT:-/usr/local/lib/delivery-recovery/data-volume.sh}"
DOCKER="${DR_DOCKER:-docker}"

fail() {
  echo "service-control: $*" >&2
  exit 1
}

mode() {
  if [ -f "$MODE_FILE" ]; then
    cat "$MODE_FILE"
  else
    echo "run"
  fi
}

load_env() {
  [ -f "$ENV_FILE" ] || fail "environment file $ENV_FILE is missing"
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
}

verify_mount() {
  [ -x "$VERIFY_SCRIPT" ] || fail "data-volume verifier is not executable"
  "$VERIFY_SCRIPT" verify --mountpoint "$DATA_MOUNTPOINT" \
    || fail "data mount verification failed; refusing to start the application"
}

compose() {
  "$DOCKER" compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"
}

container_exists() {
  local output
  output=$("$DOCKER" container inspect "$NAME" 2>&1)
  local rc=$?
  if [ "$rc" -eq 0 ]; then
    return 0
  fi
  if echo "$output" | grep -qiE 'no such (object|container)'; then
    return 1
  fi
  fail "docker inspect failed for $NAME: $output"
}

container_status() {
  if ! container_exists; then
    echo missing
    return 0
  fi
  "$DOCKER" inspect "$NAME" --format '{{.State.Status}}' \
    || fail "docker status inspection failed for $NAME"
}

start_run_mode() {
  load_env
  [ -n "${DELIVERY_RECOVERY_IMAGE:-}" ] || fail "DELIVERY_RECOVERY_IMAGE is not set"
  if container_exists; then
    "$DOCKER" start "$NAME" || fail "failed to start existing container $NAME"
    return 0
  fi
  local log_args=()
  if [ -n "${DELIVERY_RECOVERY_LOG_GROUP:-}" ]; then
    log_args=(
      --log-driver awslogs
      --log-opt "awslogs-region=${AWS_REGION:-eu-west-2}"
      --log-opt "awslogs-group=${DELIVERY_RECOVERY_LOG_GROUP}"
      --log-opt "awslogs-stream=app"
    )
  fi
  "$DOCKER" run -d --name "$NAME" --restart no \
    --read-only --tmpfs /tmp:size=64m,mode=1777 \
    --security-opt no-new-privileges:true --cap-drop ALL \
    -p 127.0.0.1:8000:8000 \
    -v "${DELIVERY_RECOVERY_DATA_HOST:-$DATA_DIR}:/data" \
    "${log_args[@]}" \
    "$DELIVERY_RECOVERY_IMAGE" \
    || fail "failed to create container $NAME"
}

start_compose_mode() {
  load_env
  compose up -d || fail "compose up failed"
}

stop_compose_mode() {
  local running
  if ! running=$(compose ps --status running --quiet 2>&1); then
    fail "compose status failed: $running"
  fi
  if [ -n "$running" ]; then
    compose stop || fail "compose stop failed"
    state=$("$DOCKER" inspect "$NAME" --format '{{.State.Status}}' 2>/dev/null || true)
    [ "$state" != "running" ] || fail "compose stop returned but $NAME is still running"
  fi
}

stop_run_mode() {
  if ! container_exists; then
    return 0
  fi
  "$DOCKER" stop "$NAME" || fail "docker stop failed for $NAME"
  state=$("$DOCKER" inspect "$NAME" --format '{{.State.Status}}' 2>/dev/null || true)
  [ "$state" != "running" ] || fail "docker stop returned but $NAME is still running"
}

remove_compose_mode() {
  compose down || fail "compose down failed"
}

remove_run_mode() {
  if container_exists; then
    "$DOCKER" rm -f "$NAME" || fail "docker rm failed for $NAME"
  fi
}

case "${1:-}" in
  verify)
    verify_mount
    ;;
  start)
    verify_mount
    if [ "$(mode)" = "compose" ]; then
      start_compose_mode
    else
      start_run_mode
    fi
    ;;
  stop)
    if [ "$(mode)" = "compose" ]; then
      stop_compose_mode
    else
      stop_run_mode
    fi
    ;;
  remove)
    if [ "$(mode)" = "compose" ]; then
      remove_compose_mode
    else
      remove_run_mode
    fi
    ;;
  replace)
    if [ "$(mode)" = "compose" ]; then
      remove_compose_mode
    else
      remove_run_mode
    fi
    verify_mount
    if [ "$(mode)" = "compose" ]; then
      start_compose_mode
    else
      start_run_mode
    fi
    ;;
  rollback)
    [ "$#" -eq 2 ] || fail "usage: service-control.sh rollback IMAGE_URI"
    load_env
    [ -f "$ENV_FILE" ] || fail "environment file is missing"
    sed -i "s|^DELIVERY_RECOVERY_IMAGE=.*|DELIVERY_RECOVERY_IMAGE=$2|" "$ENV_FILE" \
      || fail "failed to update image in $ENV_FILE"
    if [ "$(mode)" = "compose" ]; then
      remove_compose_mode
      verify_mount
      start_compose_mode
    else
      remove_run_mode
      verify_mount
      start_run_mode
    fi
    ;;
  status)
    container_status
    ;;
  *)
    fail "usage: service-control.sh {verify|start|stop|remove|replace|rollback IMAGE|status}"
    ;;
esac
