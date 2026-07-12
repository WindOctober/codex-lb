#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

load_env_file() {
  local file="$1" line key value
  [[ -f "$file" ]] || return 0
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" =~ ^[[:space:]]*$ ]] && continue
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    line="${line#export }"
    key="${line%%=*}"
    value="${line#*=}"
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    if [[ -z "${!key+x}" ]]; then
      value="${value%\"}"
      value="${value#\"}"
      value="${value%\'}"
      value="${value#\'}"
      export "$key=$value"
    fi
  done < "$file"
}

load_env_file "$ROOT_DIR/.env"
load_env_file "$ROOT_DIR/.env.local"

MIHOMO_BIN="${MIHOMO_BIN:-$HOME/.local/opt/clash-verge-rev/usr/bin/verge-mihomo}"
MIHOMO_DIR="${MIHOMO_DIR:-$HOME/.config/clash-verge-rev-from-windows}"
SOURCE_CONFIG="${MIHOMO_SOURCE_CONFIG:-$MIHOMO_DIR/clash-verge.yaml}"
RUNTIME_CONFIG="${MIHOMO_RUNTIME_CONFIG:-/tmp/codex-lb-mihomo.yaml}"
LOG_FILE="${MIHOMO_LOG_FILE:-$ROOT_DIR/var/log/verge-mihomo.log}"
PID_FILE="${MIHOMO_PID_FILE:-$ROOT_DIR/var/run/verge-mihomo.pid}"
PROXY_URL="${MIHOMO_PROXY_URL:-http://127.0.0.1:7897}"
CONTROLLER_URL="${MIHOMO_CONTROLLER_URL:-http://127.0.0.1:9097}"
OPENAI_PROBE_URL="${MIHOMO_OPENAI_PROBE_URL:-https://api.openai.com/v1/models}"

mkdir -p "$(dirname "$LOG_FILE")" "$(dirname "$PID_FILE")"

if [[ ! -x "$MIHOMO_BIN" ]]; then
  echo "Mihomo binary not found or not executable: $MIHOMO_BIN" >&2
  exit 1
fi

if [[ ! -f "$SOURCE_CONFIG" && ! -f "$RUNTIME_CONFIG" ]]; then
  echo "Mihomo config not found: $SOURCE_CONFIG or $RUNTIME_CONFIG" >&2
  exit 1
fi

runtime_pid() {
  pgrep -f "$MIHOMO_BIN .*${RUNTIME_CONFIG}" | head -n 1 || true
}

proxy_works() {
  local status
  status="$(curl -sS --connect-timeout 4 --max-time 10 --proxy "$PROXY_URL" -o /dev/null -w '%{http_code}' "$OPENAI_PROBE_URL" || true)"
  [[ "$status" == "200" || "$status" == "401" || "$status" == "403" ]]
}

controller_secret() {
  "$ROOT_DIR/.venv/bin/python" - "$RUNTIME_CONFIG" <<'PY'
from __future__ import annotations

import pathlib
import sys

import yaml

data = yaml.safe_load(pathlib.Path(sys.argv[1]).read_text()) or {}
print(data.get("secret") or "")
PY
}

select_openai_auto() {
  local secret header_args=()
  secret="$(controller_secret)"
  if [[ -n "$secret" ]]; then
    header_args=(-H "Authorization: Bearer ${secret}")
  fi
  curl -fsS \
    "${header_args[@]}" \
    -H "Content-Type: application/json" \
    -X PUT \
    --data '{"name":"OpenAI-Auto"}' \
    "${CONTROLLER_URL}/proxies/OpenAI" >/dev/null || true
}

write_runtime_config() {
  "$ROOT_DIR/.venv/bin/python" - "$SOURCE_CONFIG" "$RUNTIME_CONFIG" "$OPENAI_PROBE_URL" <<'PY'
from __future__ import annotations

import pathlib
import sys

import yaml

source = pathlib.Path(sys.argv[1])
target = pathlib.Path(sys.argv[2])
probe_url = sys.argv[3]
data = yaml.safe_load((source if source.exists() else target).read_text()) or {}
data["mixed-port"] = 7897
data["external-controller"] = "127.0.0.1:9097"
data["port"] = 0
data["socks-port"] = 0
data.pop("redir-port", None)
data.pop("tproxy-port", None)
data.pop("tun", None)

groups = data.setdefault("proxy-groups", [])
by_name = {group.get("name"): group for group in groups if isinstance(group, dict)}
proxy_names = [
    proxy.get("name")
    for proxy in data.get("proxies") or []
    if isinstance(proxy, dict) and isinstance(proxy.get("name"), str)
]

def append_unique(target_nodes: list[str], node: str) -> None:
    if node in proxy_names and node not in target_nodes:
        target_nodes.append(node)


def nodes_matching(keywords: tuple[str, ...]) -> list[str]:
    return [node for node in proxy_names if any(keyword in node for keyword in keywords)]


us_nodes: list[str] = []
for group_name in ("US", "美国", "United States"):
    group = by_name.get(group_name) or {}
    for node in group.get("proxies") or []:
        if isinstance(node, str):
            append_unique(us_nodes, node)

for node in nodes_matching(("美国", "United States", "US")):
    append_unique(us_nodes, node)

auto_nodes: list[str] = us_nodes

if not auto_nodes:
    for group_name in ("JP", "SG", "TW", "HK"):
        group = by_name.get(group_name) or {}
        for node in group.get("proxies") or []:
            if isinstance(node, str):
                append_unique(auto_nodes, node)

if not auto_nodes:
    keywords = (
        "日本",
        "Japan",
        "JP",
        "新加坡",
        "Singapore",
        "SG",
        "台湾",
        "Taiwan",
        "TW",
        "香港",
        "Hong Kong",
        "HK",
    )
    for node in proxy_names:
        if any(keyword in node for keyword in keywords):
            append_unique(auto_nodes, node)

if not auto_nodes:
    auto_nodes = proxy_names

if not auto_nodes:
    raise SystemExit("No proxy nodes available for OpenAI-Auto")

auto_group = {
    "name": "OpenAI-Auto",
    "type": "url-test",
    "proxies": auto_nodes,
    "url": probe_url,
    "interval": 60,
    "tolerance": 100,
    "lazy": False,
}
if "OpenAI-Auto" in by_name:
    by_name["OpenAI-Auto"].update(auto_group)
else:
    groups.insert(0, auto_group)

openai_group = by_name.get("OpenAI")
if isinstance(openai_group, dict):
    proxies = openai_group.setdefault("proxies", [])
    if "OpenAI-Auto" not in proxies:
        proxies.insert(0, "OpenAI-Auto")
else:
    groups.insert(
        0,
        {
            "name": "OpenAI",
            "type": "select",
            "proxies": ["OpenAI-Auto"],
        },
    )

rules = data.setdefault("rules", [])
openai_rules = [
    "DOMAIN-SUFFIX,openai.com,OpenAI",
    "DOMAIN-SUFFIX,chatgpt.com,OpenAI",
]
existing_rules = set(rule for rule in rules if isinstance(rule, str))
for rule in reversed(openai_rules):
    if rule not in existing_rules:
        rules.insert(0, rule)

target.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False))
PY
}

start_mihomo() {
  local existing
  existing="$(runtime_pid)"
  if [[ -n "$existing" ]]; then
    kill "$existing" 2>/dev/null || true
    sleep 1
    if kill -0 "$existing" 2>/dev/null; then
      kill -9 "$existing" 2>/dev/null || true
      sleep 0.5
    fi
  fi
  setsid "$MIHOMO_BIN" -d "$MIHOMO_DIR" -f "$RUNTIME_CONFIG" >"$LOG_FILE" 2>&1 < /dev/null &
  echo "$!" > "$PID_FILE"
}

write_runtime_config

if ! proxy_works; then
  start_mihomo
  sleep 2
fi

select_openai_auto

if ! proxy_works; then
  echo "Mihomo proxy is not healthy at $PROXY_URL for $OPENAI_PROBE_URL" >&2
  tail -n 40 "$LOG_FILE" >&2 || true
  exit 1
fi

echo "Mihomo proxy is healthy at $PROXY_URL"
