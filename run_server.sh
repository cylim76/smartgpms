#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"

PYTHON_BIN=".venv/bin/python"
if [ ! -x "$PYTHON_BIN" ]; then
  echo "smartGPMS environment is missing. Run ./setup.sh first."
  exit 1
fi

HOST="${SMARTGPMS_HOST:-0.0.0.0}"
PORT="${SMARTGPMS_PORT:-8765}"
exec "$PYTHON_BIN" -m uvicorn app:app --host "$HOST" --port "$PORT" --workers 1
