#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [[ "${1:-}" == "--search" ]]; then
    # Separate venv for build_index.py / search.py / specials.py, so the
    # transcription venv doesn't carry a second torch stack.
    VENV_DIR="$SCRIPT_DIR/.venv-search"
    echo "Creating search virtual environment in $VENV_DIR..."
    python3 -m venv "$VENV_DIR"
    "$VENV_DIR/bin/pip" install --upgrade pip
    "$VENV_DIR/bin/pip" install -r "$SCRIPT_DIR/requirements-search.txt"
    echo "Done. Build the index with: .venv-search/bin/python build_index.py"
    exit 0
fi

VENV_DIR="$SCRIPT_DIR/.venv"

echo "Creating virtual environment in $VENV_DIR..."
python3 -m venv "$VENV_DIR"

echo "Installing dependencies..."
"$VENV_DIR/bin/pip" install --upgrade pip
"$VENV_DIR/bin/pip" install -r "$SCRIPT_DIR/requirements.txt"

echo "Done. Run ./run.sh to execute ss.py. For local search: ./setup.sh --search"
