#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"

PYTHON_BIN=".venv/bin/python"
if [ ! -x "$PYTHON_BIN" ]; then
  echo "smartGPMS environment is missing. Run ./setup.sh first."
  exit 1
fi

HOST="${SMARTGPMS_HOST:-127.0.0.1}"
PORT="${SMARTGPMS_PORT:-8765}"
if { [ -n "${DISPLAY:-}" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; } && [ "$HOST" = "127.0.0.1" ]; then
  "$PYTHON_BIN" tools/launch_ui.py >/dev/null 2>&1 &
fi
exec "$PYTHON_BIN" -m uvicorn app:app --host "$HOST" --port "$PORT"
