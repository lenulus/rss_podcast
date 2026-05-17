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

# Point Python's SSL at certifi's CA bundle — the framework Python on macOS
# doesn't ship its own trust store, which breaks feedparser/urllib RSS fetches
# (curl works because it uses the system store). Defensive: silently skip if
# certifi isn't present.
SSL_CERT_FILE="$("$VENV_DIR/bin/python" -c 'import certifi; print(certifi.where())' 2>/dev/null || true)"
if [ -n "$SSL_CERT_FILE" ]; then
  export SSL_CERT_FILE
  export REQUESTS_CA_BUNDLE="$SSL_CERT_FILE"
fi

exec "$VENV_DIR/bin/python" -u "$SCRIPT_DIR/ss.py" "$@"
