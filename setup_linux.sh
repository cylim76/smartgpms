#!/usr/bin/env sh
set -eu

# Always resolve paths from this script so installation works from any current
# directory and from project paths containing spaces.
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
cd "$SCRIPT_DIR"

say() {
  printf '%s\n' "$*"
}

if [ "$(uname -s 2>/dev/null || true)" != "Linux" ]; then
  say "setup_linux.sh is the Linux systemd installer. Use setup_win.bat on Windows."
  exit 1
fi

if ! command -v systemctl >/dev/null 2>&1; then
  say "systemctl was not found. smartGPMS Linux installation requires systemd."
  exit 1
fi

if [ ! -d /run/systemd/system ]; then
  say "systemd is not running on this Linux system. smartGPMS service installation cannot continue."
  exit 1
fi

if [ "$(id -u)" -eq 0 ]; then
  say "Do not run setup_linux.sh with sudo or from a root login."
  say "Run it as the Linux account that should own smartGPMS."
  say "The installer will request sudo only for system packages and systemd registration."
  exit 1
fi

SERVICE_USER=$(id -un)
SERVICE_GROUP=$(id -gn)
SERVICE_DATA_DIR=${SMARTGPMS_DATA_DIR:-$SCRIPT_DIR/data}
SERVICE_HOST=${SMARTGPMS_HOST:-0.0.0.0}
SERVICE_PORT=${SMARTGPMS_PORT:-8765}

run_as_root() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    say "Installation needs administrator privileges for Linux system packages."
    say "Install sudo or run this setup once as root."
    exit 1
  fi
}

install_debian_prerequisites() {
  say "Checking Debian/Ubuntu system dependencies..."
  run_as_root apt-get update

  # Prefer the venv package matching the selected Python interpreter. Ubuntu
  # development releases may ship a newer Python than the generic package.
  python_version=$(
    "$HOST_PYTHON" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")'
  )
  versioned_venv="python${python_version}-venv"
  if apt-cache show "$versioned_venv" 2>/dev/null | grep -q '^Package:'; then
    venv_package="$versioned_venv"
  else
    venv_package="python3-venv"
  fi

  run_as_root apt-get install -y \
    "$venv_package" \
    python3-pip \
    ca-certificates \
    fontconfig \
    fonts-noto-cjk
}

if [ -n "${PYTHON_BIN:-}" ]; then
  HOST_PYTHON="$PYTHON_BIN"
elif command -v python3 >/dev/null 2>&1; then
  HOST_PYTHON=$(command -v python3)
elif command -v apt-get >/dev/null 2>&1; then
  run_as_root apt-get update
  run_as_root apt-get install -y python3
  HOST_PYTHON=$(command -v python3)
else
  say "Python 3 was not found. Install Python 3.10 or newer and run setup_linux.sh again."
  exit 1
fi

if command -v apt-get >/dev/null 2>&1; then
  install_debian_prerequisites
elif ! "$HOST_PYTHON" -m ensurepip --version >/dev/null 2>&1; then
  say "The selected Python does not provide ensurepip/venv."
  say "Install the Python venv package with your Linux package manager, then rerun setup_linux.sh."
  exit 1
fi

# A failed venv creation can leave bin/python behind without pip. Only reuse a
# virtual environment after verifying that it is complete.
if [ -x ".venv/bin/python" ] && \
  ".venv/bin/python" -m pip --version >/dev/null 2>&1; then
  SETUP_PYTHON=".venv/bin/python"
else
  SETUP_PYTHON="$HOST_PYTHON"
fi

"$SETUP_PYTHON" tools/setup_environment.py "$@"

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  say "Linux service account does not exist: $SERVICE_USER"
  exit 1
fi
if ! printf '%s' "$SERVICE_PORT" | grep -Eq '^[0-9]+$' || \
  [ "$SERVICE_PORT" -lt 1 ] || [ "$SERVICE_PORT" -gt 65535 ]; then
  say "SMARTGPMS_PORT must be a number between 1 and 65535."
  exit 1
fi

run_as_root install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0700 "$SERVICE_DATA_DIR"
UNIT_TEMP=$(mktemp "${TMPDIR:-/tmp}/smartgpms.service.XXXXXX")
trap 'rm -f "$UNIT_TEMP"' EXIT HUP INT TERM
".venv/bin/python" tools/render_systemd_service.py \
  --user "$SERVICE_USER" \
  --group "$SERVICE_GROUP" \
  --working-directory "$SCRIPT_DIR" \
  --data-directory "$SERVICE_DATA_DIR" \
  --python "$SCRIPT_DIR/.venv/bin/python" \
  --host "$SERVICE_HOST" \
  --port "$SERVICE_PORT" \
  --output "$UNIT_TEMP"
run_as_root install -m 0644 "$UNIT_TEMP" /etc/systemd/system/smartgpms.service
run_as_root systemctl daemon-reload
run_as_root systemctl enable smartgpms.service

printf '\nsmartGPMS systemd service installed successfully.\n'
printf 'Start:   sudo systemctl start smartgpms\n'
printf 'Stop:    sudo systemctl stop smartgpms\n'
printf 'Restart: sudo systemctl restart smartgpms\n'
printf 'Status:  systemctl status smartgpms\n'
printf 'Logs:    journalctl -u smartgpms -f\n'
