#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

# Load .env if present (no error if absent)
[ -f .env ] && set -a && . ./.env && set +a

# Prefer a local .venv if the user created one there; otherwise use whatever
# python3 is on PATH (assume caller activated their own venv).
if [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"
else
  PY="python3"
fi

exec "$PY" backend/main.py
