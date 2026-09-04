#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "$0")" && pwd)"
venv_dir="$project_dir/.venv"

if [[ ! -x "$venv_dir/bin/python" ]]; then
  python3 -m venv "$venv_dir"
fi

if ! "$venv_dir/bin/python" -c "import fastapi, agno, akshare, httpx" >/dev/null 2>&1; then
  "$venv_dir/bin/pip" install -r "$project_dir/backend/requirements.txt"
fi

exec "$venv_dir/bin/python" -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000 --reload --app-dir "$project_dir"
