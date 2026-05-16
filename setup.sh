#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"

echo "Creating virtual environment in $VENV_DIR..."
python3 -m venv "$VENV_DIR"

echo "Installing dependencies..."
"$VENV_DIR/bin/pip" install --upgrade pip
# lightning-whisper-mlx hard-pins tiktoken==0.3.3 (no Python 3.13 wheels).
# Install it without deps; requirements.txt provides them with looser bounds.
"$VENV_DIR/bin/pip" install --no-deps lightning-whisper-mlx
"$VENV_DIR/bin/pip" install -r "$SCRIPT_DIR/requirements.txt"

echo "Done. Run ./run.sh to execute ss.py."
