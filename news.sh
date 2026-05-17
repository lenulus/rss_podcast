#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"

# Ensure Homebrew binaries on PATH (consistent with run.sh).
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

if [ ! -d "$VENV_DIR" ]; then
  echo "Virtual environment not found. Run ./setup.sh first." >&2
  exit 1
fi

# Point Python's SSL at certifi's CA bundle — macOS framework Python
# doesn't ship its own trust store, which breaks RSS/HTTPS fetches.
SSL_CERT_FILE="$("$VENV_DIR/bin/python" -c 'import certifi; print(certifi.where())' 2>/dev/null || true)"
if [ -n "$SSL_CERT_FILE" ]; then
  export SSL_CERT_FILE
  export REQUESTS_CA_BUNDLE="$SSL_CERT_FILE"
fi

exec "$VENV_DIR/bin/python" -u "$SCRIPT_DIR/news.py" "$@"
