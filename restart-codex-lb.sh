#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

RESTART_MODE="direct"
HOST_PROXY_URL="http://127.0.0.1:7897"
ACTION="restart"

usage() {
  cat <<'EOF'
Usage: restart-codex-lb.sh [--stop-only] [--mode direct|host-proxy] [--host-proxy-url URL]

Modes:
  direct      Default. Start codex-lb with the normal network environment.
  host-proxy  Start codex-lb through a proxy reachable from inner-server.

Actions:
  default     Restart the backend on CODEX_LB_HOST:CODEX_LB_PORT.
  --stop-only Stop only the backend on CODEX_LB_HOST:CODEX_LB_PORT.

The host-proxy mode uses a proxy reachable from this host. The default is the
local Mihomo mixed proxy:
  http://127.0.0.1:7897
EOF
}

parse_args() {
  while (($#)); do
    case "$1" in
      --stop-only)
        ACTION="stop-only"
        shift
        ;;
      --mode)
        [[ $# -ge 2 ]] || { echo "--mode requires a value." >&2; exit 2; }
        RESTART_MODE="$2"
        shift 2
        ;;
      --host-proxy)
        RESTART_MODE="host-proxy"
        shift
        ;;
      --host-proxy-url)
        [[ $# -ge 2 ]] || { echo "--host-proxy-url requires a value." >&2; exit 2; }
        HOST_PROXY_URL="$2"
        shift 2
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        echo "Unknown argument: $1" >&2
        usage >&2
        exit 2
        ;;
    esac
  done

  case "$RESTART_MODE" in
    direct|host-proxy)
      ;;
    *)
      echo "Unsupported restart mode: ${RESTART_MODE}" >&2
      exit 2
      ;;
  esac
}

parse_args "$@"

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

HOST="$(config_value CODEX_LB_HOST "127.0.0.1")"
PORT="$(config_value CODEX_LB_PORT "${PORT:-2456}")"
PRIMARY_BACKEND_PORT="$(config_value CODEX_LB_PRIMARY_BACKEND_PORT "2456")"
LOG_FILE="$(config_value CODEX_LB_LOG_FILE "${ROOT_DIR}/var/log/codex-lb-backend-${PORT}.log")"
PID_FILE="$(config_value CODEX_LB_PID_FILE "${ROOT_DIR}/var/run/codex-lb-backend-${PORT}.pid")"
STOP_TIMEOUT_SECONDS="$(config_value CODEX_LB_STOP_TIMEOUT_SECONDS "15")"
HEALTH_TIMEOUT_SECONDS="$(config_value CODEX_LB_HEALTH_TIMEOUT_SECONDS "30")"
POSTGRES_BIN_DIR="$(config_value CODEX_LB_POSTGRES_BIN_DIR "")"
POSTGRES_DATA_DIR="$(config_value CODEX_LB_POSTGRES_DATA_DIR "${ROOT_DIR}/var/postgres-data")"
POSTGRES_LOG_FILE="$(config_value CODEX_LB_POSTGRES_LOG_FILE "${ROOT_DIR}/var/postgres.log")"
POSTGRES_LISTEN_ADDRESSES="$(config_value CODEX_LB_POSTGRES_LISTEN_ADDRESSES "127.0.0.1")"
POSTGRES_MAX_CONNECTIONS="$(config_value CODEX_LB_POSTGRES_MAX_CONNECTIONS "300")"
POSTGRES_SHARED_BUFFERS="$(config_value CODEX_LB_POSTGRES_SHARED_BUFFERS "512MB")"
POSTGRES_START_TIMEOUT_SECONDS="$(config_value CODEX_LB_POSTGRES_START_TIMEOUT_SECONDS "30")"
UPSTREAM_EGRESS_MODE="$(config_value CODEX_LB_UPSTREAM_EGRESS_MODE "auto")"
UPSTREAM_PROXY_URL="$(config_value CODEX_LB_UPSTREAM_PROXY_URL "http://127.0.0.1:7897")"

mkdir -p "$(dirname "$LOG_FILE")" "$(dirname "$PID_FILE")"

egress_env_args() {
  printf '%s\n' \
    "CODEX_LB_UPSTREAM_EGRESS_MODE=${UPSTREAM_EGRESS_MODE}" \
    "CODEX_LB_UPSTREAM_PROXY_URL=${UPSTREAM_PROXY_URL}"
}

proxy_env_args() {
  [[ "$RESTART_MODE" == "host-proxy" ]] || return 0
  printf '%s\n' \
    "http_proxy=${HOST_PROXY_URL}" \
    "https_proxy=${HOST_PROXY_URL}" \
    "HTTP_PROXY=${HOST_PROXY_URL}" \
    "HTTPS_PROXY=${HOST_PROXY_URL}" \
    "all_proxy=${HOST_PROXY_URL}" \
    "ALL_PROXY=${HOST_PROXY_URL}" \
    "no_proxy=127.0.0.1,localhost,::1" \
    "NO_PROXY=127.0.0.1,localhost,::1" \
    "CODEX_LB_UPSTREAM_STREAM_TRANSPORT=http" \
    "CODEX_LB_UPSTREAM_WEBSOCKET_TRUST_ENV=true"
}

preflight_host_proxy() {
  [[ "$RESTART_MODE" == "host-proxy" ]] || return 0
  if ! command -v curl >/dev/null 2>&1; then
    echo "curl is required to verify host-proxy mode." >&2
    exit 1
  fi

  echo "Host-proxy mode enabled: ${HOST_PROXY_URL}"
  local status
  status="$(curl -sS --connect-timeout 5 --max-time 12 --proxy "$HOST_PROXY_URL" -o /dev/null -w '%{http_code}' https://api.openai.com/v1/models || true)"
  case "$status" in
    401|403)
      ;;
    *)
      echo "Host proxy is not usable from inner-server: ${HOST_PROXY_URL} (HTTP ${status:-000})" >&2
      echo "Start or repair the local Mihomo proxy first:" >&2
      echo "  ./scripts/ensure-mihomo-proxy.sh" >&2
      exit 1
      ;;
  esac
  echo "Host proxy preflight passed."
}

configured_database_url() {
  config_value CODEX_LB_DATABASE_URL ""
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

database_url_for_pg_tools() {
  local database_url="$1"
  case "$database_url" in
    postgresql+asyncpg://*)
      database_url="postgresql://${database_url#postgresql+asyncpg://}"
      ;;
    postgresql+psycopg://*)
      database_url="postgresql://${database_url#postgresql+psycopg://}"
      ;;
  esac
  printf '%s\n' "$database_url"
}

database_url_component() {
  local database_url="$1"
  local component="$2"
  local python_cmd
  python_cmd="$(python_bin)"
  # shellcheck disable=SC2086
  $python_cmd - "$database_url" "$component" <<'PY'
from __future__ import annotations

import sys
from urllib.parse import unquote, urlsplit

url = sys.argv[1]
component = sys.argv[2]
if url.startswith("postgresql+"):
    url = "postgresql://" + url.split("://", 1)[1]
parsed = urlsplit(url)

if component == "host":
    print(parsed.hostname or "127.0.0.1")
elif component == "port":
    print(parsed.port or 5432)
elif component == "user":
    print(unquote(parsed.username or ""))
elif component == "database":
    print(unquote(parsed.path.lstrip("/") or parsed.username or "postgres"))
else:
    raise SystemExit(f"unknown component: {component}")
PY
}

postgres_tool() {
  local name="$1"
  local path="${POSTGRES_BIN_DIR}/${name}"
  if [[ -n "$POSTGRES_BIN_DIR" && -x "$path" ]]; then
    printf '%s\n' "$path"
  elif command -v "$name" >/dev/null 2>&1; then
    command -v "$name"
  else
    echo "PostgreSQL tool not found: ${name}. Set CODEX_LB_POSTGRES_BIN_DIR." >&2
    exit 1
  fi
}

is_local_postgres_host() {
  local host="$1"
  [[ "$host" == "127.0.0.1" || "$host" == "localhost" || "$host" == "::1" ]]
}

ensure_local_postgres() {
  local database_url="$1"
  local ready_url host port user db initdb pg_ctl createdb pg_isready_bin psql start_options deadline
  ready_url="$(database_url_for_pg_tools "$database_url")"
  host="$(database_url_component "$database_url" host)"
  port="$(database_url_component "$database_url" port)"
  user="$(database_url_component "$database_url" user)"
  db="$(database_url_component "$database_url" database)"

  pg_isready_bin="$(postgres_tool pg_isready)"
  psql="$(postgres_tool psql)"
  createdb="$(postgres_tool createdb)"
  if "$pg_isready_bin" -h "$host" -p "$port" -U "$user" >/dev/null 2>&1; then
    echo "PostgreSQL is already running at ${host}:${port}."
    if ! "$psql" "$ready_url" -Atqc "select 1" >/dev/null 2>&1; then
      echo "Ensuring PostgreSQL database exists: ${db}"
      "$createdb" -h "$host" -p "$port" -U "$user" "$db" 2>/dev/null || true
    fi
    if ! "$psql" "$ready_url" -Atqc "select 1" >/dev/null 2>&1; then
      echo "PostgreSQL is running, but ${db} is not reachable." >&2
      exit 1
    fi
    echo "PostgreSQL readiness check passed."
    return
  fi

  if ! is_local_postgres_host "$host"; then
    echo "PostgreSQL is configured at ${host}:${port}, but it is not ready." >&2
    exit 1
  fi

  initdb="$(postgres_tool initdb)"
  pg_ctl="$(postgres_tool pg_ctl)"
  mkdir -p "$POSTGRES_DATA_DIR" "$(dirname "$POSTGRES_LOG_FILE")"

  if [[ ! -s "${POSTGRES_DATA_DIR}/PG_VERSION" ]]; then
    if [[ -z "$user" ]]; then
      echo "PostgreSQL URL must include a user when initializing local PostgreSQL." >&2
      exit 1
    fi
    echo "Initializing local PostgreSQL data directory: ${POSTGRES_DATA_DIR}"
    "$initdb" -D "$POSTGRES_DATA_DIR" --username="$user" --auth=trust --encoding=UTF8 --locale=C.UTF-8
  fi

  echo "Starting local PostgreSQL on ${host}:${port}"
  start_options="-p ${port} -c listen_addresses=${POSTGRES_LISTEN_ADDRESSES} -c max_connections=${POSTGRES_MAX_CONNECTIONS} -c shared_buffers=${POSTGRES_SHARED_BUFFERS}"
  "$pg_ctl" -D "$POSTGRES_DATA_DIR" -l "$POSTGRES_LOG_FILE" -o "$start_options" start >/dev/null

  deadline=$((SECONDS + POSTGRES_START_TIMEOUT_SECONDS))
  while (( SECONDS < deadline )); do
    if "$pg_isready_bin" -h "$host" -p "$port" -U "$user" >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done

  if ! "$pg_isready_bin" -h "$host" -p "$port" -U "$user" >/dev/null 2>&1; then
    echo "PostgreSQL did not become ready. Last log lines:" >&2
    tail -n 80 "$POSTGRES_LOG_FILE" >&2 || true
    exit 1
  fi

  if ! "$psql" "$ready_url" -Atqc "select 1" >/dev/null 2>&1; then
    echo "Ensuring PostgreSQL database exists: ${db}"
    "$createdb" -h "$host" -p "$port" -U "$user" "$db" 2>/dev/null || true
  fi

  if ! "$psql" "$ready_url" -Atqc "select 1" >/dev/null 2>&1; then
    echo "PostgreSQL is running, but ${db} is not reachable." >&2
    exit 1
  fi
  echo "PostgreSQL readiness check passed."
}

run_database_migrations() {
  local database_url="$1"
  local python_cmd
  python_cmd="$(python_bin)"
  echo "Checking database migrations."
  # shellcheck disable=SC2086
  CODEX_LB_DATABASE_URL="$database_url" $python_cmd -m app.db.migrate upgrade head
  # shellcheck disable=SC2086
  CODEX_LB_DATABASE_URL="$database_url" $python_cmd -m app.db.migrate check
}

preflight_database() {
  local database_url
  database_url="$(configured_database_url)"
  case "$database_url" in
    postgresql://*|postgresql+asyncpg://*|postgresql+psycopg://*)
      ;;
    *)
      return
      ;;
  esac

  ensure_local_postgres "$database_url"
  run_database_migrations "$database_url"
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
  [[ "$cmd" == *"fastapi run app/main.py"* ]] && return 0
  [[ "$cmd" == *"uvicorn"* && "$cmd" == *"app.main:app"* ]] && return 0

  return 1
}

collect_pids() {
  local pids=()

  if [[ -f "$PID_FILE" ]]; then
    local pid_from_file=""
    pid_from_file="$(tr -d '[:space:]' <"$PID_FILE")"
    if [[ "$pid_from_file" =~ ^[0-9]+$ ]] && is_codex_lb_pid "$pid_from_file" && pid_listens_on_port "$pid_from_file" "$PORT"; then
      pids+=("$pid_from_file")
    fi
  fi

  while read -r pid; do
    [[ -n "$pid" ]] || continue
    if is_codex_lb_pid "$pid"; then
      pids+=("$pid")
    fi
  done < <(pids_listening_on_port "$PORT")

  printf '%s\n' "${pids[@]}" | awk 'NF && !seen[$0]++'
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

stop_existing() {
  mapfile -t pids < <(collect_pids)
  if (( ${#pids[@]} == 0 )); then
    echo "No existing codex-lb process found for ${ROOT_DIR}."
    rm -f "$PID_FILE"
    return
  fi

  echo "Stopping existing codex-lb process(es): ${pids[*]}"
  kill -TERM "${pids[@]}" 2>/dev/null || true

  local deadline=$((SECONDS + STOP_TIMEOUT_SECONDS))
  while (( SECONDS < deadline )); do
    local still_running=()
    for pid in "${pids[@]}"; do
      if is_codex_lb_pid "$pid"; then
        still_running+=("$pid")
      fi
    done
    if (( ${#still_running[@]} == 0 )); then
      rm -f "$PID_FILE"
      return
    fi
    sleep 1
  done

  local stubborn=()
  for pid in "${pids[@]}"; do
    if is_codex_lb_pid "$pid"; then
      stubborn+=("$pid")
    fi
  done
  if (( ${#stubborn[@]} > 0 )); then
    echo "Force killing codex-lb process(es): ${stubborn[*]}"
    kill -KILL "${stubborn[@]}" 2>/dev/null || true
  fi
  rm -f "$PID_FILE"
}

start_new() {
  cd "$ROOT_DIR"
  local start_cmd=()
  local env_args=()
  if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
    start_cmd=("$ROOT_DIR/.venv/bin/python" -m app.cli --host "$HOST" --port "$PORT")
  elif command -v uv >/dev/null 2>&1; then
    start_cmd=(uv run python -m app.cli --host "$HOST" --port "$PORT")
  else
    echo "Neither uv nor ${ROOT_DIR}/.venv/bin/python is available." >&2
    exit 1
  fi

  echo "Starting codex-lb on ${HOST}:${PORT}"
  echo "Restart mode: ${RESTART_MODE}"
  if [[ "$RESTART_MODE" == "host-proxy" ]]; then
    echo "Proxy URL: ${HOST_PROXY_URL}"
  fi
  echo "Upstream egress: ${UPSTREAM_EGRESS_MODE} (${UPSTREAM_PROXY_URL})"
  echo "Logging to ${LOG_FILE}"
  mapfile -t env_args < <(
    egress_env_args
    proxy_env_args
  )

  if command -v setsid >/dev/null 2>&1; then
    setsid env "${env_args[@]}" "${start_cmd[@]}" >>"$LOG_FILE" 2>&1 < /dev/null &
  else
    nohup env "${env_args[@]}" "${start_cmd[@]}" >>"$LOG_FILE" 2>&1 < /dev/null &
  fi
  local pid="$!"
  echo "$pid" >"$PID_FILE"

  sleep 2
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "codex-lb failed to start. Last log lines:"
    tail -n 80 "$LOG_FILE" || true
    exit 1
  fi

  echo "codex-lb restarted with pid ${pid}."
  echo "PID file: ${PID_FILE}"
  echo "URL: http://${HOST}:${PORT}"

  if command -v curl >/dev/null 2>&1; then
    local deadline=$((SECONDS + HEALTH_TIMEOUT_SECONDS))
    while (( SECONDS < deadline )); do
      if curl -fsS "http://${HOST}:${PORT}/health/ready" >/dev/null 2>&1; then
        echo "Health check passed."
        return
      fi
      if ! kill -0 "$pid" 2>/dev/null; then
        echo "codex-lb exited during startup. Last log lines:"
        tail -n 80 "$LOG_FILE" || true
        exit 1
      fi
      sleep 1
    done
    echo "Started, but health check did not pass within ${HEALTH_TIMEOUT_SECONDS} seconds."
  fi
}

stop_conflicting_user_systemd_service() {
  [[ "$PORT" == "$PRIMARY_BACKEND_PORT" ]] || return 0

  local unit="codex-lb.service"
  command -v systemctl >/dev/null 2>&1 || return 0
  systemctl --user cat "$unit" >/dev/null 2>&1 || return 0

  echo "Stopping conflicting user systemd unit if it is running or scheduled: ${unit}"
  systemctl --user stop "$unit" >/dev/null 2>&1 || true

  if systemctl --user is-enabled --quiet "$unit"; then
    echo "Disabling conflicting user systemd unit: ${unit}"
    systemctl --user disable "$unit" >/dev/null
  fi
}

if [[ "$ACTION" == "stop-only" ]]; then
  stop_existing
  exit 0
fi

preflight_database
preflight_host_proxy
if [[ "$(config_value CODEX_LB_RESTART_PREFLIGHT_ONLY "false")" == "true" ]]; then
  echo "Preflight only; codex-lb was not restarted."
  exit 0
fi

stop_conflicting_user_systemd_service
stop_existing
start_new
