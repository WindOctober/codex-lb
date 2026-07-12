#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SILENCE_SECONDS="${SILENCE_SECONDS:-310}"
BACKEND_PORT="${BACKEND_PORT:-3456}"
MOCK_PORT="${MOCK_PORT:-3460}"
TEST_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/codex-lb-keepalive-e2e.XXXXXX")"
MOCK_PID=""
BACKEND_PID_FILE="${TEST_ROOT}/codex-lb.pid"
BACKEND_LOG_FILE="${TEST_ROOT}/codex-lb.log"

cleanup() {
  CODEX_LB_PORT="${BACKEND_PORT}" \
  CODEX_LB_HOST="127.0.0.1" \
  CODEX_LB_PID_FILE="${BACKEND_PID_FILE}" \
  CODEX_LB_LOG_FILE="${BACKEND_LOG_FILE}" \
    "${ROOT_DIR}/restart-codex-lb.sh" --stop-only >/dev/null 2>&1 || true
  if [[ -n "${MOCK_PID}" ]]; then
    kill "${MOCK_PID}" >/dev/null 2>&1 || true
    wait "${MOCK_PID}" >/dev/null 2>&1 || true
  fi
  if [[ "${KEEP_TEST_ROOT:-false}" == "true" ]]; then
    echo "Retained test artifacts: ${TEST_ROOT}"
  else
    rm -rf "${TEST_ROOT}"
  fi
}
trap cleanup EXIT

mkdir -p "${TEST_ROOT}/codex-home" "${TEST_ROOT}/work"

setsid "${ROOT_DIR}/.venv/bin/python" \
  "${ROOT_DIR}/scripts/testing/mock_silent_responses_upstream.py" \
  --port "${MOCK_PORT}" \
  --silence-seconds "${SILENCE_SECONDS}" \
  >"${TEST_ROOT}/mock-upstream.log" 2>&1 < /dev/null &
MOCK_PID="$!"

for _ in $(seq 1 50); do
  if curl -fsS "http://127.0.0.1:${MOCK_PORT}/health" >/dev/null; then
    break
  fi
  sleep 0.1
done
curl -fsS "http://127.0.0.1:${MOCK_PORT}/health" >/dev/null

export CODEX_LB_PORT="${BACKEND_PORT}"
export CODEX_LB_HOST="127.0.0.1"
export CODEX_LB_PID_FILE="${BACKEND_PID_FILE}"
export CODEX_LB_LOG_FILE="${BACKEND_LOG_FILE}"
export CODEX_LB_DATABASE_URL="sqlite+aiosqlite:////${TEST_ROOT#/}/codex-lb.sqlite"
export CODEX_LB_DATABASE_SQLITE_PRE_MIGRATE_BACKUP_ENABLED="false"
export CODEX_LB_DATABASE_SQLITE_STARTUP_CHECK_MODE="off"
export CODEX_LB_DASHBOARD_AUTH_MODE="disabled"
export CODEX_LB_API_KEY_AUTH_ENABLED="false"
export CODEX_LB_UPSTREAM_EGRESS_MODE="direct"
export CODEX_LB_HTTP_RESPONSES_SESSION_BRIDGE_ENABLED="true"
export CODEX_LB_PROXY_REQUEST_BUDGET_SECONDS="480"
export CODEX_LB_STREAM_IDLE_TIMEOUT_SECONDS="480"
export CODEX_LB_HEALTH_TIMEOUT_SECONDS="60"

"${ROOT_DIR}/restart-codex-lb.sh"
curl -fsS "http://127.0.0.1:${BACKEND_PORT}/health/live" >/dev/null
curl -fsS "http://127.0.0.1:${BACKEND_PORT}/api/dashboard/bridge-runtime" >/dev/null

curl -fsS \
  -X POST \
  -H "Content-Type: application/json" \
  --data "{\"name\":\"silent-e2e\",\"base_url\":\"http://127.0.0.1:${MOCK_PORT}\",\"api_key\":\"mock-api-key\",\"priority\":0}" \
  "http://127.0.0.1:${BACKEND_PORT}/api/accounts/providers" \
  >"${TEST_ROOT}/provider.json"

cat >"${TEST_ROOT}/codex-home/config.toml" <<EOF
model = "gpt-5.6-sol"
model_provider = "keepalive-e2e"
model_reasoning_effort = "ultra"

[model_providers.keepalive-e2e]
name = "codex-lb keepalive e2e"
base_url = "http://127.0.0.1:${BACKEND_PORT}/v1"
wire_api = "responses"
env_key = "CODEX_LB_TEST_API_KEY"
requires_openai_auth = false
supports_websockets = false
stream_idle_timeout_ms = 300000
stream_max_retries = 5
EOF

export CODEX_HOME="${TEST_ROOT}/codex-home"
export CODEX_LB_TEST_API_KEY="test-client-key"
STARTED_AT="$(date +%s)"
codex exec \
  --ephemeral \
  --json \
  --ignore-rules \
  --skip-git-repo-check \
  --sandbox read-only \
  -C "${TEST_ROOT}/work" \
  -m gpt-5.6-sol \
  "Reply exactly KEEPALIVE_OK. Do not call tools." \
  >"${TEST_ROOT}/codex.jsonl" 2>"${TEST_ROOT}/codex.stderr"
ELAPSED_SECONDS="$(( $(date +%s) - STARTED_AT ))"

curl -fsS "http://127.0.0.1:${MOCK_PORT}/stats" >"${TEST_ROOT}/stats.json"
RESPONSES_REQUESTS="$(
  "${ROOT_DIR}/.venv/bin/python" -c \
    'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["responses_requests"])' \
    "${TEST_ROOT}/stats.json"
)"

if [[ "${RESPONSES_REQUESTS}" != "1" ]]; then
  echo "Expected one upstream Responses request; observed ${RESPONSES_REQUESTS}." >&2
  cat "${TEST_ROOT}/stats.json" >&2
  exit 1
fi
if ! grep -q "KEEPALIVE_OK" "${TEST_ROOT}/codex.jsonl"; then
  echo "Codex CLI did not receive the expected final assistant message." >&2
  cat "${TEST_ROOT}/codex.stderr" >&2
  exit 1
fi
if (( ELAPSED_SECONDS < ${SILENCE_SECONDS%.*} )); then
  echo "Codex completed before the configured upstream silence elapsed." >&2
  exit 1
fi

echo "Codex keepalive E2E passed: silence=${SILENCE_SECONDS}s elapsed=${ELAPSED_SECONDS}s upstream_requests=1"
