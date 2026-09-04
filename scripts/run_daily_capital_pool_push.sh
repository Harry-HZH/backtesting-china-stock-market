#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "$0")/.." && pwd)"
venv_dir="$project_dir/.venv"
log_dir="$project_dir/.codex-tmp/daily-push"
mkdir -p "$log_dir"

if [[ ! -x "$venv_dir/bin/python" ]]; then
  python3 -m venv "$venv_dir"
fi
if ! "$venv_dir/bin/python" -c "import fastapi, agno, akshare, httpx" >/dev/null 2>&1; then
  "$venv_dir/bin/pip" install -r "$project_dir/backend/requirements.txt"
fi

export TZ="Asia/Shanghai"
export PYTHONPATH="$project_dir${PYTHONPATH:+:$PYTHONPATH}"
exec "$venv_dir/bin/python" "$project_dir/scripts/daily_capital_pool_push.py"
