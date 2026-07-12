#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ACTION="${1:-start}"
REQUESTED_ACTION="$ACTION"

config_value() {
  local name="$1"
  local default="${2:-}"
  local file line value="$default"

  for file in "${ROOT_DIR}/.env" "${ROOT_DIR}/.env.local"; do
    [[ -f "$file" ]] || continue
    while IFS= read -r line; do
      line="${line#export }"
      value="${line#*=}"
      value="${value%$'\r'}"
      value="${value%\"}"
      value="${value#\"}"
      value="${value%\'}"
      value="${value#\'}"
    done < <(grep -E "^[[:space:]]*(export[[:space:]]+)?${name}=" "$file" | sed 's/^[[:space:]]*//')
  done

  if [[ -n "${!name:-}" ]]; then
    value="${!name}"
  fi
  printf '%s\n' "$value"
}

config_is_set() {
  local name="$1"
  local file

  if [[ -n "${!name+x}" ]]; then
    return 0
  fi

  for file in "${ROOT_DIR}/.env" "${ROOT_DIR}/.env.local"; do
    [[ -f "$file" ]] || continue
    if grep -Eq "^[[:space:]]*(export[[:space:]]+)?${name}=" "$file"; then
      return 0
    fi
  done

  return 1
}

strip_scheme() {
  local value="$1"
  value="${value#http://}"
  value="${value#https://}"
  printf '%s\n' "$value"
}

host_from_bind() {
  local value
  value="$(strip_scheme "$1")"
  printf '%s\n' "${value%:*}"
}

port_from_bind() {
  local value
  value="$(strip_scheme "$1")"
  printf '%s\n' "${value##*:}"
}

python_bin() {
  if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
    printf '%s\n' "$ROOT_DIR/.venv/bin/python"
  elif command -v uv >/dev/null 2>&1; then
    printf '%s\n' "uv run python"
  elif command -v python3 >/dev/null 2>&1; then
    printf '%s\n' "python3"
  else
    echo "No Python executable found." >&2
    exit 1
  fi
}

GATEWAY_BIND="$(config_value CODEX_LB_GATEWAY_BIND "127.0.0.1:2455")"
GATEWAY_HOST="$(host_from_bind "$GATEWAY_BIND")"
GATEWAY_PORT="$(port_from_bind "$GATEWAY_BIND")"
BACKEND_HOST="$(config_value CODEX_LB_BACKEND_HOST "127.0.0.1")"
BACKEND_PORT="$(config_value CODEX_LB_BACKEND_PORT "2456")"
BACKEND_UPSTREAM="$(config_value CODEX_LB_BACKEND_UPSTREAM "${BACKEND_HOST}:${BACKEND_PORT}")"
BACKEND_UPSTREAM_EXPLICIT="false"
config_is_set CODEX_LB_BACKEND_UPSTREAM && BACKEND_UPSTREAM_EXPLICIT="true"
CADDY_BIN="$(config_value CODEX_LB_CADDY_BIN "${ROOT_DIR}/var/bin/caddy")"
CADDYFILE="$(config_value CODEX_LB_CADDYFILE "${ROOT_DIR}/config/caddy/Caddyfile")"
CADDY_PID_FILE="$(config_value CODEX_LB_CADDY_PID_FILE "${ROOT_DIR}/var/run/codex-lb-caddy.pid")"
CADDY_LOG_FILE="$(config_value CODEX_LB_CADDY_LOG_FILE "${ROOT_DIR}/var/log/caddy/codex-lb-gateway.log")"
CADDY_ACCESS_LOG="$(config_value CODEX_LB_CADDY_ACCESS_LOG "${ROOT_DIR}/var/log/caddy/access.log")"
BACKEND_PID_FILE="$(config_value CODEX_LB_BACKEND_PID_FILE "${ROOT_DIR}/var/run/codex-lb-backend.pid")"
BACKEND_LOG_FILE="$(config_value CODEX_LB_BACKEND_LOG_FILE "${ROOT_DIR}/var/log/codex-lb-backend.log")"
CADDY_PID_FILE_EXPLICIT="false"
CADDY_LOG_FILE_EXPLICIT="false"
CADDY_ACCESS_LOG_EXPLICIT="false"
BACKEND_PID_FILE_EXPLICIT="false"
BACKEND_LOG_FILE_EXPLICIT="false"
config_is_set CODEX_LB_CADDY_PID_FILE && CADDY_PID_FILE_EXPLICIT="true"
config_is_set CODEX_LB_CADDY_LOG_FILE && CADDY_LOG_FILE_EXPLICIT="true"
config_is_set CODEX_LB_CADDY_ACCESS_LOG && CADDY_ACCESS_LOG_EXPLICIT="true"
config_is_set CODEX_LB_BACKEND_PID_FILE && BACKEND_PID_FILE_EXPLICIT="true"
config_is_set CODEX_LB_BACKEND_LOG_FILE && BACKEND_LOG_FILE_EXPLICIT="true"
STOP_TIMEOUT_SECONDS="$(config_value CODEX_LB_CADDY_SWITCH_STOP_TIMEOUT_SECONDS "20")"
STOP_POLL_INTERVAL_SECONDS="$(config_value CODEX_LB_CADDY_SWITCH_STOP_POLL_INTERVAL_SECONDS "0.05")"
HEALTH_TIMEOUT_SECONDS="$(config_value CODEX_LB_CADDY_SWITCH_HEALTH_TIMEOUT_SECONDS "60")"
PREFLIGHT_ENABLED="$(config_value CODEX_LB_CADDY_SWITCH_PREFLIGHT "true")"
SWITCH_MODE="$(config_value CODEX_LB_CADDY_SWITCH_MODE "additive")"
ALLOW_GATEWAY_CHANGE="$(config_value CODEX_LB_CADDY_SWITCH_ALLOW_GATEWAY_CHANGE "false")"
ALLOW_PRIMARY_STOP="$(config_value CODEX_LB_CADDY_SWITCH_ALLOW_PRIMARY_STOP "false")"
PRIMARY_GATEWAY_PORT="$(config_value CODEX_LB_PRIMARY_GATEWAY_PORT "2455")"
PRIMARY_BACKEND_PORT="$(config_value CODEX_LB_PRIMARY_BACKEND_PORT "2456")"
PORT_SCAN_LIMIT="$(config_value CODEX_LB_CADDY_SWITCH_PORT_SCAN_LIMIT "200")"
ADDITIVE_GATEWAY_PORT_START="$(config_value CODEX_LB_CADDY_ADDITIVE_GATEWAY_PORT_START "$((GATEWAY_PORT + 1000))")"
ADDITIVE_BACKEND_PORT_START="$(config_value CODEX_LB_CADDY_ADDITIVE_BACKEND_PORT_START "$((ADDITIVE_GATEWAY_PORT_START + 1))")"

if [[ "$ACTION" == "replace" ]]; then
  ACTION="start"
  SWITCH_MODE="replace"
fi

case "$SWITCH_MODE" in
  additive|replace)
    ;;
  *)
    echo "CODEX_LB_CADDY_SWITCH_MODE must be additive or replace; got: ${SWITCH_MODE}" >&2
    exit 2
    ;;
esac

require_gateway_change_confirmation() {
  case "$ACTION" in
    start|stop)
      ;;
    *)
      return
      ;;
  esac

  if [[ "$ALLOW_GATEWAY_CHANGE" == "true" ]]; then
    return
  fi

  cat >&2 <<EOF
Refusing to ${REQUESTED_ACTION} the Caddy gateway without explicit confirmation.

For ordinary codex-lb validation, use a backend-only instance instead:
  CODEX_LB_PORT=3456 CODEX_LB_HOST=127.0.0.1 ./restart-codex-lb.sh
  CODEX_LB_PORT=3456 CODEX_LB_HOST=127.0.0.1 ./restart-codex-lb.sh --stop-only

To intentionally change Caddy, rerun with:
  CODEX_LB_CADDY_SWITCH_ALLOW_GATEWAY_CHANGE=true
EOF
  exit 2
}

require_primary_stop_confirmation() {
  [[ "$ACTION" == "stop" ]] || return
  if [[ "$GATEWAY_PORT" != "$PRIMARY_GATEWAY_PORT" && "$BACKEND_PORT" != "$PRIMARY_BACKEND_PORT" ]]; then
    return
  fi
  if [[ "$ALLOW_PRIMARY_STOP" == "true" ]]; then
    return
  fi

  cat >&2 <<EOF
Refusing to stop the primary codex-lb gateway/backend ports (${PRIMARY_GATEWAY_PORT}/${PRIMARY_BACKEND_PORT}).

If this is intentional gateway maintenance, rerun with both:
  CODEX_LB_CADDY_SWITCH_ALLOW_GATEWAY_CHANGE=true
  CODEX_LB_CADDY_SWITCH_ALLOW_PRIMARY_STOP=true
EOF
  exit 2
}

require_gateway_change_confirmation
require_primary_stop_confirmation

ensure_runtime_dirs() {
  mkdir -p \
    "$(dirname "$CADDY_PID_FILE")" \
    "$(dirname "$CADDY_LOG_FILE")" \
    "$(dirname "$CADDY_ACCESS_LOG")" \
    "$(dirname "$BACKEND_PID_FILE")" \
    "$(dirname "$BACKEND_LOG_FILE")" \
    "${ROOT_DIR}/var/caddy/config" \
    "${ROOT_DIR}/var/caddy/data"
}

is_codex_lb_pid() {
  local pid="$1"
  [[ -d "/proc/$pid" ]] || return 1

  local cwd=""
  cwd="$(readlink -f "/proc/$pid/cwd" 2>/dev/null || true)"
  [[ "$cwd" == "$ROOT_DIR" ]] || return 1

  local cmd=""
  cmd="$(tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null || true)"
  [[ -n "$cmd" ]] || return 1

  [[ "$cmd" == *"python -m app.cli"* ]] && return 0
  [[ "$cmd" == *"python3 -m app.cli"* ]] && return 0
  [[ "$cmd" == *"uv run python -m app.cli"* ]] && return 0
  [[ "$cmd" == *"uvicorn"* && "$cmd" == *"app.main:app"* ]] && return 0
  return 1
}

is_caddy_pid() {
  local pid="$1"
  [[ -d "/proc/$pid" ]] || return 1

  local cwd=""
  cwd="$(readlink -f "/proc/$pid/cwd" 2>/dev/null || true)"
  [[ "$cwd" == "$ROOT_DIR" ]] || return 1

  local cmd=""
  cmd="$(tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null || true)"
  [[ "$cmd" == *"caddy run"* && "$cmd" == *"$CADDYFILE"* ]]
}

pids_listening_on_port() {
  local port="$1"
  if command -v lsof >/dev/null 2>&1; then
    lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true
    return
  fi

  ss -ltnp "( sport = :${port} )" 2>/dev/null \
    | sed -nE 's/.*pid=([0-9]+).*/\1/p' \
    | awk 'NF && !seen[$0]++'
}

port_has_listener() {
  local port="$1"
  local pid
  while read -r pid; do
    [[ -n "$pid" ]] || continue
    return 0
  done < <(pids_listening_on_port "$port")
  return 1
}

pid_listens_on_port() {
  local target_pid="$1"
  local port="$2"
  local pid
  while read -r pid; do
    [[ -n "$pid" ]] || continue
    [[ "$pid" == "$target_pid" ]] && return 0
  done < <(pids_listening_on_port "$port")
  return 1
}

find_free_port() {
  local start="$1"
  local skip="${2:-}"
  local offset port

  for (( offset = 0; offset < PORT_SCAN_LIMIT; offset++ )); do
    port=$((start + offset))
    [[ "$port" == "$skip" ]] && continue
    if ! port_has_listener "$port"; then
      printf '%s\n' "$port"
      return
    fi
  done

  echo "No free port found from ${start} within ${PORT_SCAN_LIMIT} attempts." >&2
  exit 1
}

retarget_default_runtime_files_for_ports() {
  if [[ "$CADDY_PID_FILE_EXPLICIT" != "true" ]]; then
    CADDY_PID_FILE="${ROOT_DIR}/var/run/codex-lb-caddy-${GATEWAY_PORT}.pid"
  fi
  if [[ "$CADDY_LOG_FILE_EXPLICIT" != "true" ]]; then
    CADDY_LOG_FILE="${ROOT_DIR}/var/log/caddy/codex-lb-gateway-${GATEWAY_PORT}.log"
  fi
  if [[ "$CADDY_ACCESS_LOG_EXPLICIT" != "true" ]]; then
    CADDY_ACCESS_LOG="${ROOT_DIR}/var/log/caddy/access-${GATEWAY_PORT}.log"
  fi
  if [[ "$BACKEND_PID_FILE_EXPLICIT" != "true" ]]; then
    BACKEND_PID_FILE="${ROOT_DIR}/var/run/codex-lb-backend-${BACKEND_PORT}.pid"
  fi
  if [[ "$BACKEND_LOG_FILE_EXPLICIT" != "true" ]]; then
    BACKEND_LOG_FILE="${ROOT_DIR}/var/log/codex-lb-backend-${BACKEND_PORT}.log"
  fi
}

sync_bind_values() {
  GATEWAY_BIND="${GATEWAY_HOST}:${GATEWAY_PORT}"
  if [[ "$BACKEND_UPSTREAM_EXPLICIT" != "true" ]]; then
    BACKEND_UPSTREAM="${BACKEND_HOST}:${BACKEND_PORT}"
  fi
}

select_additive_ports() {
  local original_gateway_port="$GATEWAY_PORT"
  local original_backend_port="$BACKEND_PORT"

  if port_has_listener "$GATEWAY_PORT"; then
    GATEWAY_PORT="$(find_free_port "$ADDITIVE_GATEWAY_PORT_START")"
    BACKEND_PORT="$(find_free_port "$ADDITIVE_BACKEND_PORT_START" "$GATEWAY_PORT")"
    echo "Gateway port ${original_gateway_port} is already in use; additive mode selected ${GATEWAY_PORT} -> ${BACKEND_PORT}."
  elif port_has_listener "$BACKEND_PORT"; then
    BACKEND_PORT="$(find_free_port "$ADDITIVE_BACKEND_PORT_START" "$GATEWAY_PORT")"
    echo "Backend port ${original_backend_port} is already in use; additive mode selected backend ${BACKEND_PORT}."
  fi

  sync_bind_values
  retarget_default_runtime_files_for_ports
}

assert_port_clear() {
  local port="$1"
  local purpose="$2"
  local pid cmd
  while read -r pid; do
    [[ -n "$pid" ]] || continue
    cmd="$(tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null || true)"
    echo "Port ${port} is occupied for ${purpose}: pid=${pid} cmd=${cmd}" >&2
    exit 1
  done < <(pids_listening_on_port "$port")
}

stop_pids() {
  local reason="$1"
  shift
  local pids=("$@")
  if (( ${#pids[@]} == 0 )); then
    return
  fi

  echo "Stopping ${reason}: ${pids[*]}"
  kill -TERM "${pids[@]}" 2>/dev/null || true

  local deadline=$((SECONDS + STOP_TIMEOUT_SECONDS))
  while (( SECONDS < deadline )); do
    local still_running=()
    for pid in "${pids[@]}"; do
      [[ -d "/proc/$pid" ]] && still_running+=("$pid")
    done
    (( ${#still_running[@]} == 0 )) && return
    sleep "$STOP_POLL_INTERVAL_SECONDS"
  done

  local stubborn=()
  for pid in "${pids[@]}"; do
    [[ -d "/proc/$pid" ]] && stubborn+=("$pid")
  done
  if (( ${#stubborn[@]} > 0 )); then
    echo "Force killing ${reason}: ${stubborn[*]}"
    kill -KILL "${stubborn[@]}" 2>/dev/null || true
  fi
}

stop_pids_until_port_free() {
  local reason="$1"
  local port="$2"
  shift 2
  local pids=("$@")
  if (( ${#pids[@]} == 0 )); then
    return
  fi

  echo "Stopping ${reason}: ${pids[*]}"
  kill -TERM "${pids[@]}" 2>/dev/null || true

  local deadline=$((SECONDS + STOP_TIMEOUT_SECONDS))
  while (( SECONDS < deadline )); do
    if ! port_has_listener "$port"; then
      return
    fi
    sleep "$STOP_POLL_INTERVAL_SECONDS"
  done

  echo "Force killing ${reason}: ${pids[*]}"
  kill -KILL "${pids[@]}" 2>/dev/null || true
}

collect_managed_codex_lb_pids() {
  local port="$1"
  local pid
  while read -r pid; do
    [[ -n "$pid" ]] || continue
    is_codex_lb_pid "$pid" && printf '%s\n' "$pid"
  done < <(pids_listening_on_port "$port")
}

collect_managed_caddy_pids() {
  local pid
  if [[ -f "$CADDY_PID_FILE" ]]; then
    pid="$(tr -d '[:space:]' <"$CADDY_PID_FILE")"
    [[ "$pid" =~ ^[0-9]+$ ]] && is_caddy_pid "$pid" && pid_listens_on_port "$pid" "$GATEWAY_PORT" && printf '%s\n' "$pid"
  fi

  while read -r pid; do
    [[ -n "$pid" ]] || continue
    is_caddy_pid "$pid" && printf '%s\n' "$pid"
  done < <(pids_listening_on_port "$GATEWAY_PORT")
}

collect_managed_caddy_pids_from_pid_files() {
  local file pid
  for file in "${ROOT_DIR}"/var/run/codex-lb-caddy*.pid; do
    [[ -f "$file" ]] || continue
    pid="$(tr -d '[:space:]' <"$file")"
    [[ "$pid" =~ ^[0-9]+$ ]] && is_caddy_pid "$pid" && printf '%s\n' "$pid"
  done
}

collect_managed_backend_pids_from_pid_files() {
  local file pid
  for file in "${ROOT_DIR}"/var/run/codex-lb-backend*.pid; do
    [[ -f "$file" ]] || continue
    pid="$(tr -d '[:space:]' <"$file")"
    [[ "$pid" =~ ^[0-9]+$ ]] && is_codex_lb_pid "$pid" && printf '%s\n' "$pid"
  done
}

assert_port_clear_or_managed() {
  local port="$1"
  local purpose="$2"
  local pid cmd
  while read -r pid; do
    [[ -n "$pid" ]] || continue
    if is_codex_lb_pid "$pid" || is_caddy_pid "$pid"; then
      continue
    fi
    cmd="$(tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null || true)"
    echo "Port ${port} is occupied by an unmanaged process for ${purpose}: pid=${pid} cmd=${cmd}" >&2
    exit 1
  done < <(pids_listening_on_port "$port")
}

start_detached() {
  local log_file="$1"
  shift

  if command -v setsid >/dev/null 2>&1; then
    (
      cd "$ROOT_DIR"
      exec setsid "$@"
    ) >>"$log_file" 2>&1 < /dev/null &
  else
    (
      cd "$ROOT_DIR"
      exec nohup "$@"
    ) >>"$log_file" 2>&1 < /dev/null &
  fi
  printf '%s\n' "$!"
}

run_preflight() {
  if [[ "$PREFLIGHT_ENABLED" != "true" ]]; then
    return
  fi
  echo "Running PostgreSQL and migration preflight."
  CODEX_LB_RESTART_PREFLIGHT_ONLY=true "${ROOT_DIR}/restart-codex-lb.sh"
}

start_caddy() {
  if [[ ! -x "$CADDY_BIN" ]]; then
    echo "Caddy binary is not executable: ${CADDY_BIN}" >&2
    exit 1
  fi
  if [[ ! -f "$CADDYFILE" ]]; then
    echo "Caddyfile not found: ${CADDYFILE}" >&2
    exit 1
  fi

  echo "Starting Caddy gateway on ${GATEWAY_BIND} -> ${BACKEND_UPSTREAM}"
  local command=(
    env
    "XDG_CONFIG_HOME=${ROOT_DIR}/var/caddy/config"
    "XDG_DATA_HOME=${ROOT_DIR}/var/caddy/data"
    "CODEX_LB_GATEWAY_BIND=${GATEWAY_BIND}"
    "CODEX_LB_GATEWAY_PORT=${GATEWAY_PORT}"
    "CODEX_LB_BACKEND_UPSTREAM=${BACKEND_UPSTREAM}"
    "CODEX_LB_CADDY_ACCESS_LOG=${CADDY_ACCESS_LOG}"
    "$CADDY_BIN"
    run
    --config
    "$CADDYFILE"
    --adapter
    caddyfile
  )
  local pid
  pid="$(start_detached "$CADDY_LOG_FILE" "${command[@]}")"
  echo "$pid" >"$CADDY_PID_FILE"

  sleep 1
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "Caddy failed to start. Last log lines:" >&2
    tail -n 80 "$CADDY_LOG_FILE" >&2 || true
    exit 1
  fi
}

start_backend() {
  local python_cmd
  python_cmd="$(python_bin)"

  echo "Starting codex-lb backend on ${BACKEND_HOST}:${BACKEND_PORT}"
  # shellcheck disable=SC2206
  local python_parts=($python_cmd)
  local command=(
    env
    "CODEX_LB_HOST=${BACKEND_HOST}"
    "CODEX_LB_PORT=${BACKEND_PORT}"
    "PORT=${BACKEND_PORT}"
    "${python_parts[@]}"
    -m
    app.cli
    --host
    "$BACKEND_HOST"
    --port
    "$BACKEND_PORT"
  )
  local pid
  pid="$(start_detached "$BACKEND_LOG_FILE" "${command[@]}")"
  echo "$pid" >"$BACKEND_PID_FILE"

  sleep 2
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "codex-lb backend failed to start. Last log lines:" >&2
    tail -n 100 "$BACKEND_LOG_FILE" >&2 || true
    exit 1
  fi
}

wait_for_health() {
  local name="$1"
  local url="$2"
  local deadline=$((SECONDS + HEALTH_TIMEOUT_SECONDS))
  while (( SECONDS < deadline )); do
    if curl -fsS --max-time 2 "$url" >/dev/null 2>&1; then
      echo "${name} health check passed: ${url}"
      return
    fi
    sleep 1
  done
  echo "${name} health check did not pass within ${HEALTH_TIMEOUT_SECONDS}s: ${url}" >&2
  return 1
}

stop_managed() {
  mapfile -t caddy_pids < <(collect_managed_caddy_pids | awk 'NF && !seen[$0]++')
  mapfile -t backend_pids < <(collect_managed_codex_lb_pids "$BACKEND_PORT" | awk 'NF && !seen[$0]++')
  stop_pids "managed Caddy gateway" "${caddy_pids[@]}"
  stop_pids "managed codex-lb backend" "${backend_pids[@]}"
  rm -f \
    "$CADDY_PID_FILE" \
    "$BACKEND_PID_FILE" \
    "${ROOT_DIR}/var/run/codex-lb-caddy-${GATEWAY_PORT}.pid" \
    "${ROOT_DIR}/var/run/codex-lb-backend-${BACKEND_PORT}.pid"
}

start_all() {
  run_preflight

  if [[ "$SWITCH_MODE" == "additive" ]]; then
    select_additive_ports
    ensure_runtime_dirs
    assert_port_clear "$GATEWAY_PORT" "Caddy gateway"
    assert_port_clear "$BACKEND_PORT" "codex-lb backend"
    start_backend
    wait_for_health "backend" "http://${BACKEND_HOST}:${BACKEND_PORT}/health/ready"
    start_caddy
    wait_for_health "gateway" "http://${GATEWAY_HOST}:${GATEWAY_PORT}/health/ready"
  else
    sync_bind_values
    ensure_runtime_dirs

    mapfile -t backend_codex_pids < <(collect_managed_codex_lb_pids "$BACKEND_PORT" | awk 'NF && !seen[$0]++')
    stop_pids_until_port_free "managed codex-lb backend on ${BACKEND_PORT}" "$BACKEND_PORT" "${backend_codex_pids[@]}"
    assert_port_clear "$BACKEND_PORT" "codex-lb backend"

    start_backend
    wait_for_health "backend" "http://${BACKEND_HOST}:${BACKEND_PORT}/health/ready"

    mapfile -t gateway_codex_pids < <(collect_managed_codex_lb_pids "$GATEWAY_PORT" | awk 'NF && !seen[$0]++')
    mapfile -t caddy_pids < <(collect_managed_caddy_pids | awk 'NF && !seen[$0]++')
    stop_pids_until_port_free "codex-lb process on gateway port ${GATEWAY_PORT}" "$GATEWAY_PORT" "${gateway_codex_pids[@]}"
    stop_pids "managed Caddy gateway" "${caddy_pids[@]}"
    assert_port_clear "$GATEWAY_PORT" "Caddy gateway"

    start_caddy
    wait_for_health "gateway" "http://${GATEWAY_HOST}:${GATEWAY_PORT}/health/ready"
  fi

  echo "Caddy gateway is ready: http://${GATEWAY_HOST}:${GATEWAY_PORT}"
  echo "codex-lb backend is ready: http://${BACKEND_HOST}:${BACKEND_PORT}"
  echo "Switch mode: ${SWITCH_MODE}"
  echo "Caddy PID file: ${CADDY_PID_FILE}"
  echo "Backend PID file: ${BACKEND_PID_FILE}"
}

status_all() {
  echo "Gateway: ${GATEWAY_BIND} -> ${BACKEND_UPSTREAM}"
  ss -ltnp "( sport = :${GATEWAY_PORT} or sport = :${BACKEND_PORT} )" || true
  if [[ -f "$CADDY_PID_FILE" ]]; then
    echo "Caddy PID: $(tr -d '[:space:]' <"$CADDY_PID_FILE")"
  fi
  if [[ -f "$BACKEND_PID_FILE" ]]; then
    echo "Backend PID: $(tr -d '[:space:]' <"$BACKEND_PID_FILE")"
  fi
}

case "$ACTION" in
  start)
    start_all
    ;;
  stop)
    ensure_runtime_dirs
    stop_managed
    ;;
  status)
    status_all
    ;;
  *)
    echo "Usage: $0 [start|stop|status]" >&2
    exit 2
    ;;
esac
