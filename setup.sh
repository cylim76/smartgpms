#!/usr/bin/env sh
set -eu

# Always resolve paths from this script so installation works from any current
# directory and from project paths containing spaces.
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
cd "$SCRIPT_DIR"

say() {
  printf '%s\n' "$*"
}

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

if [ "$(uname -s 2>/dev/null || true)" = "Linux" ]; then
  if [ -n "${PYTHON_BIN:-}" ]; then
    HOST_PYTHON="$PYTHON_BIN"
  elif command -v python3 >/dev/null 2>&1; then
    HOST_PYTHON=$(command -v python3)
  elif command -v apt-get >/dev/null 2>&1; then
    run_as_root apt-get update
    run_as_root apt-get install -y python3
    HOST_PYTHON=$(command -v python3)
  else
    say "Python 3 was not found. Install Python 3.10 or newer and run setup.sh again."
    exit 1
  fi

  if command -v apt-get >/dev/null 2>&1; then
    install_debian_prerequisites
  elif ! "$HOST_PYTHON" -m ensurepip --version >/dev/null 2>&1; then
    say "The selected Python does not provide ensurepip/venv."
    say "Install the Python venv package with your Linux package manager, then rerun setup.sh."
    exit 1
  fi
else
  HOST_PYTHON="${PYTHON_BIN:-python3}"
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
printf '\nInstallation complete. Run ./run.sh for local use or ./run_server.sh for a headless server.\n'
