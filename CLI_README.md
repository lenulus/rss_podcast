# CLI runbook for `ss.py`

Operating reference for `./run.sh` — designed for Claude (or other automated agents) to use this tool reliably without burning bandwidth, transcribe time, or destroying state. For end-user onboarding see `README.md`.

## State map (where everything lives)

| Path | Owner | What |
|---|---|---|
| `feeds.toml` | user | per-feed config; **gitignored**, contains private RSS URLs |
| `feeds.example.toml` | tracked | template + inline docs |
| `downloads/<tag>/*.mp3` | tool | working set; pruned by `max_episodes_on_disk` when set |
| `downloads/<tag>/<stem>.meta.json` | tool | RSS metadata sidecar (paired with mp3, removed on eviction) |
| `transcripts/<tag>/*.md` | tool | rendered output with YAML frontmatter + chapter `##` headings + speaker turns |
| `transcripts/<tag>/.processed` | tool | **canonical dedup signal** — one stem per line, append-only |
| `transcripts/<tag>/.diarize/<stem>.json` | tool | cached pyannote turns (skip re-diarize on retry) |
| `transcripts/<tag>/.chunks/<stem>/chunk_NNN.json` | tool | per-chunk Whisper output (resumable on crash; auto-removed on success) |
| `<backup_path>/<tag>/media/*.mp3` | tool | SD card mp3 archive (eviction target) |
| `<backup_path>/<tag>/text/*.md` | tool | SD card transcript archive (mtime-synced) |
| `<backup_path>/<tag>/text/.diarize/*.json` | tool | SD card diarize-cache backup (mtime-synced; preserves hours of pyannote compute across fresh-clone / disaster-recovery) |

`<backup_path>` comes from `[defaults]` or per-feed `backup_path` in `feeds.toml`. The script never deletes anything on the SD card. Local mp3s are only deleted if (a) a transcript exists in `.processed`, and (b) the backup path is reachable.

## Command map

| Want to … | Command | Bandwidth | Time |
|---|---|---|---|
| See what's new across feeds without downloading | `./run.sh --check` | RSS only (~KB) | seconds |
| Show local / SD / RSS state for every feed | `./run.sh --status [--offline]` | optional RSS | seconds |
| Run the daily download + transcribe pipeline | `./run.sh --daily` | depends on backlog | depends |
| Download newest N for one feed | `./run.sh --feed <tag> --download --limit N` | N × ~100 MB | minutes |
| Bulk pre-download (e.g. on unmetered) | `./run.sh --feed <tag> --download --no-limit` | full feed size | hours |
| Transcribe everything pending | `./run.sh --transcribe` | none | hours |
| Transcribe one feed | `./run.sh --feed <tag> --transcribe` | none | hours |
| Pre-diarize a feed at high concurrency, then transcribe later | `./run.sh --feed <tag> --diarize-only --subprocess-concurrency 6` then `./run.sh --feed <tag> --transcribe` | none | hours |
| Add metadata + chapter headings to old transcripts | `./run.sh --backfill-headers` | RSS only | seconds |
| List episodes in a feed (tabular) | `./run.sh --feed <tag> --index [--limit N]` | RSS only | seconds |
| Force diarization on/off for one run | `... --diarize` or `... --no-diarize` | — | — |
| Pick a different Whisper model for one run | `... --model large-v3` | — | — |

## Precedence rules (CLI overrides TOML overrides defaults)

- **Limit**: `--no-limit` > `--limit N` > feed `max_downloads_per_run` > unbounded
- **Diarize**: `--diarize` / `--no-diarize` > feed `diarize` > `false`
- **Model**: `--model X` > feed `model` > `"medium"`
- **Subprocess-per-episode**: `--subprocess-per-episode` / `--no-subprocess-per-episode` > feed `subprocess_per_episode` > `false`
- **Subprocess concurrency**: `--subprocess-concurrency N` > feed `subprocess_concurrency` > `1`
- **Host (speaker naming)**: feed `host` > RSS `itunes_author` > generic `Speaker A/B/C`
- **Backup path**: per-feed `media_dir` / `transcript_dir` > per-feed `backup_path` > `[defaults].backup_path`

## feeds.toml schema

```toml
[defaults]                            # values inherited by every feed
backup_path = "/Volumes/SD/archive"   # required for backup; per-feed override allowed

[feeds.<tag>]                         # tag becomes the subfolder name
rss = "https://..."                   # required
sid = "..."                           # optional, Substack paywall cookie
diarize = true                        # optional, default false; needs pyannote.audio + HF token
host = "Lex Fridman"                  # optional, overrides RSS-derived host
daily = true                          # optional, default true; set false to skip in --daily
download_order = "newest"             # optional, "newest" (default) or "oldest"
max_downloads_per_run = 5             # optional, no default (unbounded)
max_episodes_on_disk = 10             # optional, no eviction if unset
backup_path = "..."                   # optional, overrides [defaults]
media_dir = "..."                     # optional, fully overrides media backup path
transcript_dir = "..."                # optional, fully overrides transcript backup path
model = "large-v3"                    # optional, default "medium"
whisper_batch_size = 12               # optional, default 12; lower (e.g. 6) for 3 h+ episodes
subprocess_per_episode = false        # optional, default false; isolate each episode in a
                                      # fresh Python process to dodge MPS allocator drift
                                      # on long backlogs (50+ eps). +30-60s/ep overhead.
subprocess_concurrency = 1            # optional, default 1; max parallel subprocesses for
                                      # this feed. Only used when subprocess_per_episode=true.
                                      # Each worker uses ~3 GB unified memory.
```

## Always check before risky operations

```bash
df -h .                              # local disk free
./run.sh --check                     # incoming counts + byte sizes (RSS-only, cheap)
./run.sh --status                    # local + SD state per feed
ls /Volumes/<sd-card>/               # backup volume sanity if eviction will fire
```

A bulk pre-download or full backfill should always be preceded by `--check` to see the byte count. Pulling >5 GB on a metered connection is the kind of thing to confirm before kicking off.

## Failure recovery

| Symptom | Cheap recovery |
|---|---|
| Whisper Metal command-buffer timeout | Just re-run `--transcribe`. Chunk checkpoints (`.chunks/<stem>/`) + diarize cache (`.diarize/<stem>.json`) resume cleanly. |
| `--download` aborted mid-episode | Re-run `--download`. The partial mp3 was deleted; the script finds the gap automatically. |
| Diarization fails to load model | Check the HF token has *"Access public gated repositories"*, and both `pyannote/speaker-diarization-3.1` **and** `pyannote/speaker-diarization-community-1` licenses are accepted. |
| Whole-file mp3 corruption suspected | Delete the local mp3 and its `<stem>.meta.json`, remove the stem from `.processed`, re-run `--download` + `--transcribe`. |
| SD card not mounted during eviction or backup | Eviction is skipped automatically with a warning. Re-mount and re-run any normal command (eviction + backup retry on the next pass). |
| Stale SD card transcript after re-render | Run any pass that touches the feed; the mtime check refreshes. |
| Fresh clone needs the diarize cache to avoid hours of re-diarize | Copy `<backup_path>/<tag>/text/.diarize/` → `transcripts/<tag>/.diarize/`, then run `--transcribe`. Backups are written on every pass when the SD card is mounted. |
| Force re-transcribe of one episode | Remove its stem from `transcripts/<tag>/.processed` (and delete the `.md` so the speaker labels regenerate). Then `--transcribe`. Diarize cache reused — only Whisper re-runs. |
| Force re-render headers/chapters without re-transcribing | Just run `--backfill-headers`. It rewrites existing `.md` files in place from current RSS metadata. Idempotent. **Do not delete the `.md` first** — backfill needs the body. |
| Relabel speakers on an existing diarized transcript | Same as "force re-transcribe": clear `.processed` entry + delete `.md`. Cached diarization (`.diarize/<stem>.json`) makes Whisper the only slow step. |

## Performance reference (M-series Apple Silicon, `medium` model)

| Operation | Throughput | Per hour of audio |
|---|---|---|
| Download | network-bound | ~100 MB ≈ one typical episode |
| Transcribe (no diarize) | ~13–15× realtime | 4–5 min |
| Transcribe (with diarize) | ~5× realtime | ~12 min |
| Diarize alone | ~10× realtime | ~6 min |
| Backfill headers | ~1s per file (RSS-bound) | — |

Whisper model size scales linearly: `large-v3` ≈ 2× the time of `medium`, `small` ≈ 0.5× — quality / speed trade-off.

## Two-pass diarize-then-transcribe for high concurrency

Diarize and Whisper have wildly different memory profiles:

- **Pyannote** (diarize): ~5 GB working set per process. Safely parallelizable to 4–8 workers on a 64 GB Mac.
- **Whisper-large-v3 at batch=48** (transcribe): can burst to ~50 GB per process. Effective max concurrency = 1 (two simultaneous bursts can collide and trigger heavy swap or OOM).

When both stages run in one subprocess (`--transcribe`), per-feed concurrency is bounded by Whisper's burst. To use the idle GPU/CPU during diarize, split the workload:

```bash
# Pass 1: fast parallel pyannote (cache fills the .diarize/ sidecars)
./run.sh --feed <tag> --diarize-only --subprocess-concurrency 6

# Pass 2: serial Whisper (every episode reads from the diarize cache)
./run.sh --feed <tag> --transcribe
```

`--diarize-only` writes only the `.diarize/<stem>.json` cache file and does NOT append to `.processed` — the cache file is the done-signal. The follow-up `--transcribe` is Whisper-only and runs at full per-episode speed without contention.

## Gotchas

- **Don't run `--backfill-headers` while `--transcribe` is in flight on the same feed** — they both write to `.md` files and could race. Tools don't enforce a lock.
- **The `--feed` argument is required for `--rss`-less commands** — there's an escape hatch (`--rss <url>`) but it bypasses per-feed config (host, diarize, eviction, etc.) and falls back to the flat `./downloads/` layout. Avoid unless one-off.
- **`--limit` and `--no-limit` are mutually exclusive in intent** — if both are passed, `--no-limit` wins (it's the more explicit override).
- **`max_episodes_on_disk` triggers eviction at end of `--download` and `--transcribe`** — so setting it after a fresh bulk download will immediately evict, which is fine *if* the SD card is mounted. If not, eviction is skipped with a warning (safe).
- **Chapter detection is heuristic** — it parses RSS descriptions for `(MM:SS) – Title` patterns. If a feed's description doesn't use that format, no chapters get injected. Re-running `--backfill-headers` is safe (idempotent) once a feed adopts the pattern.
- **Sidecar files (`.processed`, `.chunks/`, `.meta.json`) are not committed to git** — `transcripts/` and `downloads/` are gitignored in their entirety. Don't expect them to survive a fresh clone; the `.processed` index bootstraps from existing `.md` files on first run. **Exception: `.diarize/<stem>.json` is mirrored to the SD card backup** (`<text_dir>/.diarize/`) on every `--transcribe` / `--diarize-only` pass, so a re-clone with the SD card mounted recovers the diarize cache and avoids re-paying that compute.
- **Background tasks during downloads should NOT touch `./downloads/<tag>/`** — the dedup check happens at start, but new files arriving mid-run could fool the skip logic.

## When committing changes

Commits in this repo follow:

- Concise subject (≤72 chars) starting with a present-tense verb
- Body explains the *why* and any non-obvious behavior changes
- Single commit per logical feature
- Co-author tag at the bottom

Use a heredoc to preserve formatting:

```bash
git add <files>
git commit -m "$(cat <<'EOF'
Subject line in present tense

Body paragraph explaining the why and tradeoffs.

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

Don't push without explicit user instruction. Don't commit `feeds.toml`, `CLAUDE.md`, `wiki/`, or anything under `transcripts/` or `downloads/`.

## Pointers

- `ss.py` — single-file implementation; all CLI flags wired in `main()` near the bottom
- `README.md` — user-facing setup + feature docs
- `CLAUDE.md` — wiki-pattern conventions for synthesizing transcripts into a knowledge layer (separate concern)
