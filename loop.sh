#!/usr/bin/env bash
# loop.sh — batch-restart wrapper for ss.py transcribe / diarize jobs.
#
# Each call to ./run.sh stays bounded by --limit N. After every batch the
# Python parent process exits cleanly and gets respawned, releasing whatever
# Metal driver / kernel state had accumulated below the Python heap. Keeps
# per-worker throughput from drifting down on multi-hour runs.
#
# Composes with subprocess-per-episode, --subprocess-concurrency N,
# --diarize-only, --model X, --no-diarize, etc. — all extra args pass
# through to run.sh verbatim.
set -uo pipefail

# Keep the Mac awake for the whole run. macOS throttles background GPU/compute
# (and can nap) when logged out with the display off — observed dropping
# episodes from ~20x to ~1x realtime mid-batch, then snapping back on login.
# That stalls long unattended/overnight runs. Re-exec under caffeinate so the
# protection is automatic and self-reverts when the run ends (vs. permanently
# disabling system sleep). -i no idle sleep, -m no disk sleep, -s no system
# sleep on AC. The guard var prevents an infinite re-exec loop; we skip the
# screen-awake (-d) assertion so the display can still turn off.
if [ -z "${LOOP_CAFFEINATED:-}" ] && command -v caffeinate >/dev/null 2>&1; then
    export LOOP_CAFFEINATED=1
    exec caffeinate -ims "$0" "$@"
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

FEED=""
BATCH_SIZE=60
PASSTHROUGH=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --feed)
            FEED="$2"
            shift 2
            ;;
        --batch)
            BATCH_SIZE="$2"
            shift 2
            ;;
        --help|-h)
            cat <<'EOF'
loop.sh — restart-resistant transcribe / diarize loop.

Calls ./run.sh --feed <tag> --transcribe --limit N in a loop, respawning
the Python parent every N episodes to dodge below-Python state drift.

Usage:
  loop.sh --feed <tag> [--batch N] [extra args ...]

Required:
  --feed <tag>        Feed slug from feeds.toml

Optional:
  --batch N           Episodes per child invocation (default 60)
  [extra args]        Passed verbatim to ./run.sh:
                        --diarize-only
                        --subprocess-concurrency N
                        --model X
                        --no-diarize
                        ... etc.

Examples:
  loop.sh --feed lex-fridman --diarize-only --subprocess-concurrency 6
  loop.sh --feed dwarkesh --diarize-only --subprocess-concurrency 10
  loop.sh --feed dwarkesh --batch 40

Exit codes:
  0  done (no pending remain)
  1  stuck (pending didn't decrease across a batch — likely real failure)
EOF
            exit 0
            ;;
        *)
            PASSTHROUGH+=("$1")
            shift
            ;;
    esac
done

if [ -z "$FEED" ]; then
    echo "✗ --feed <tag> is required. Try --help." >&2
    exit 1
fi

# Mode is implied by whether --diarize-only is in the passthrough args.
DIARIZE_ONLY=false
for arg in "${PASSTHROUGH[@]:-}"; do
    if [ "$arg" = "--diarize-only" ]; then
        DIARIZE_ONLY=true
        break
    fi
done

count_pending() {
    local feed="$1"
    local mp3_dir="$SCRIPT_DIR/downloads/$feed"
    local mp3_count=0
    if [ -d "$mp3_dir" ]; then
        mp3_count=$(find "$mp3_dir" -maxdepth 1 -name '*.mp3' 2>/dev/null | wc -l | tr -d ' ')
    fi
    local done_count=0
    if [ "$DIARIZE_ONLY" = true ]; then
        local diarize_dir="$SCRIPT_DIR/transcripts/$feed/.diarize"
        if [ -d "$diarize_dir" ]; then
            done_count=$(find "$diarize_dir" -maxdepth 1 -name '*.json' 2>/dev/null | wc -l | tr -d ' ')
        fi
    else
        local processed="$SCRIPT_DIR/transcripts/$feed/.processed"
        if [ -f "$processed" ]; then
            done_count=$(wc -l < "$processed" | tr -d ' ')
        fi
    fi
    echo $((mp3_count - done_count))
}

MODE="transcribe"
[ "$DIARIZE_ONLY" = true ] && MODE="diarize-only"

extra_display="${PASSTHROUGH[*]:-(none)}"
echo "loop.sh: feed=$FEED  mode=$MODE  batch=$BATCH_SIZE  extra=${extra_display}"

prev_pending=-1
batch_n=0
while true; do
    pending=$(count_pending "$FEED")
    if [ "$pending" -le 0 ]; then
        echo
        echo "✓ Done — no pending."
        break
    fi
    if [ "$pending" -eq "$prev_pending" ]; then
        echo
        echo "✗ Stuck — pending unchanged ($pending) across last batch. Aborting." >&2
        exit 1
    fi
    prev_pending=$pending
    batch_n=$((batch_n + 1))
    echo
    echo "═══ batch $batch_n  ($pending pending) ═══"
    # `|| true` so a single child non-zero exit (e.g. SIGTERM, transient
    # failure) doesn't abort the whole loop — the stuck-detection above
    # catches genuine progress failures.
    "$SCRIPT_DIR/run.sh" --feed "$FEED" --transcribe --limit "$BATCH_SIZE" "${PASSTHROUGH[@]+"${PASSTHROUGH[@]}"}" || true
done
