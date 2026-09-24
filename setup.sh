#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"

if [ -x ".venv/bin/python" ]; then
  PYTHON_BIN=".venv/bin/python"
else
  PYTHON_BIN="${PYTHON_BIN:-python3}"
fi
"$PYTHON_BIN" tools/setup_environment.py "$@"
printf '\nInstallation complete. Run ./run.sh for local use or ./run_server.sh for a headless server.\n'
