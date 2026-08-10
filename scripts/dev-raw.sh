#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "$0")/.." && pwd)"
python_bin="$project_dir/.venv/bin/python"

if [[ ! -x "$python_bin" ]]; then
  echo "RAW環境がありません。先に npm run setup:raw を実行してください。" >&2
  exit 1
fi

cleanup() {
  if [[ -n "${raw_server_pid:-}" ]]; then
    kill "$raw_server_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

cd "$project_dir"
"$python_bin" -m local_raw.server &
raw_server_pid=$!
npm run dev
