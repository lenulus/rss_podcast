#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"

# Ensure Homebrew binaries (e.g. ffmpeg) are on PATH
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

if [ ! -d "$VENV_DIR" ]; then
  echo "Virtual environment not found. Run ./setup.sh first." >&2
  exit 1
fi

exec "$VENV_DIR/bin/python" -u "$SCRIPT_DIR/ss.py" "$@"
