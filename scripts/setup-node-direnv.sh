#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NODE_VERSION="$(tr -d '[:space:]' < "$ROOT_DIR/.node-version")"
NODE_PREFIX="$ROOT_DIR/.direnv/node-v$NODE_VERSION"
NODE_BIN="$NODE_PREFIX/node_modules/node/bin"

mkdir -p "$NODE_PREFIX"
npm install --prefix "$NODE_PREFIX" --no-save "node@$NODE_VERSION"

if [[ ! -x "$NODE_BIN/node" ]]; then
  echo "node executable was not installed at $NODE_BIN/node" >&2
  exit 1
fi

echo "installed $("$NODE_BIN/node" -v) at $NODE_BIN/node"
echo "run: direnv allow $ROOT_DIR"
