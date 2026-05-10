# Substack Podcast Toolkit

Download, index, and transcribe podcast episodes from one or more Substack RSS feeds.

Transcription runs locally on Apple Silicon via [Lightning Whisper MLX](https://github.com/mustafaaljadery/lightning-whisper-mlx) — no API keys or cloud services required.

## Prerequisites

- Python **3.11+** (3.9 works for everything except diarization, which requires `pyannote.audio` and bumps the floor to 3.11)
- macOS with Apple Silicon (M1/M2/M3/M4) for GPU-accelerated transcription
- `ffmpeg` (`brew install ffmpeg`) — used for audio duration probing, WAV preconversion before diarization, and 30-min chunking on long audio files

## Setup

```bash
./setup.sh
cp feeds.example.toml feeds.toml   # then edit with your private RSS URL(s)
```

`setup.sh` creates a `.venv` and installs `requirements.txt`. `feeds.toml` is gitignored — it holds your private RSS URLs.

## Configuring feeds

Each feed gets a tag, which is used as the subfolder name under `./downloads/<tag>/` and `./transcripts/<tag>/`. A `[defaults]` block applies to every feed; per-feed values override:

```toml
[defaults]
backup_path = "/Volumes/PodcastSD/archive"   # one SD card, all feeds

[feeds.nates-notebook]
rss = "https://api.substack.com/feed/podcast/XXXXXX/private-key.rss"
max_episodes_on_disk = 10
# Inherits backup_path from [defaults] → backups go to
# /Volumes/PodcastSD/archive/nates-notebook/

[feeds.new-show]
rss = "https://api.substack.com/feed/podcast/YYYYYY/private-key.rss"
max_episodes_on_disk = 20
max_downloads_per_run = 5
download_order = "oldest"
backup_path = "/Volumes/OtherSD/archive"     # overrides default for this feed
# media_dir = "/Volumes/Audio/new-show"      # overrides just the mp3 path
# transcript_dir = "/Volumes/Cloud/new-show" # overrides just the transcript path
```

Backups land at `<backup_path>/<tag>/media/` (mp3s) and `<backup_path>/<tag>/text/` (transcripts). Transcript backup runs on every `--download`, `--transcribe`, and `--fetch` — it's idempotent and never deletes from local. Mp3 eviction only happens when `max_episodes_on_disk` is set.

### Per-feed settings

These can be set under `[defaults]` (applies to every feed) or under `[feeds.<tag>]` (overrides the default).

| Key | Description |
|-----|-------------|
| `rss` | Private RSS feed URL (required, per-feed only) |
| `sid` | `substack.sid` cookie for paywalled transcripts |
| `max_episodes_on_disk` | Cap mp3s kept in `./downloads/<tag>/`. Eviction runs after `--download` and `--transcribe`; oldest go first, but **only mp3s that already have a transcript are eligible** — source audio is never deleted unless its transcript exists. Transcripts themselves are never touched. |
| `backup_path` | Root for backups. Mp3s back up to `<backup_path>/<tag>/media/<file>.mp3`; transcripts back up to `<backup_path>/<tag>/text/<file>.md`. If the path (or its parent — e.g. an unmounted SD card mount point) is missing, the affected backup is **skipped entirely** rather than risking unbacked deletion. |
| `media_dir` | Override where this feed's mp3 backups land. Absolute path. If set, replaces `<backup_path>/<tag>/media/`. |
| `transcript_dir` | Override where this feed's transcript backups land. Absolute path. If set, replaces `<backup_path>/<tag>/text/`. |
| `max_downloads_per_run` | Default cap for `--download` and the transcript-fetch path in a single run. Overridden by an explicit `--limit N` or bypassed entirely with `--no-limit`. |
| `download_order` | `"newest"` (default) or `"oldest"`. Use `"oldest"` for incremental backfill of an archive. |
| `diarize` | `true` runs speaker diarization (pyannote.audio) during `--transcribe`. The `.md` output gains `**Speaker A** (mm:ss):` headers per turn. Adds runtime and a one-time HuggingFace setup; opt-in per feed. |
| `daily` | Defaults to `true`. Set `daily = false` to exclude this feed from the `--daily` macro (the feed remains usable via explicit `--feed <tag> --download` / `--transcribe`). |
| `whisper_batch_size` | Lightning Whisper MLX batch size (default `12`). Lower it (e.g. `6`) for very long episodes (3+ hours) that trip Metal GPU command-buffer timeouts. Tradeoff: ~linear slowdown. |
| `host` | Override the RSS-derived host name. Used for diarization speaker-naming (most-talked speaker in first 60s renders as `**<first-name>**` instead of `Speaker A`). Defaults to whatever the feed's `itunes_author` says. |

## Usage

All commands run through `./run.sh` (which activates the venv).

### List episodes

```bash
./run.sh --feed nates-notebook --index
./run.sh --feed nates-notebook --index --limit 20
```

### Download mp3s

Audio lands in `./downloads/<feed-tag>/`:

```bash
./run.sh --feed nates-notebook --download
./run.sh --feed nates-notebook --download --limit 10
```

Downloads skip episodes whose stem appears in `./transcripts/<feed-tag>/.processed` (the canonical "already handled" record — see the `.processed` section below) or already exist as an mp3 locally.

### Transcribe

```bash
# Transcribe just one feed
./run.sh --transcribe --feed nates-notebook

# Transcribe everything across all feeds (walks ./downloads/*/)
./run.sh --transcribe

./run.sh --transcribe --feed nates-notebook --model large-v3
./run.sh --transcribe --feed nates-notebook --limit 5
```

Transcription skips mp3s whose stem is already in `.processed` — safe to interrupt and resume. To force a re-transcribe, remove the stem from `.processed` (and optionally delete the `.md`).

### Fetch Substack-hosted transcripts

For episodes Substack already provides a transcript for (not all do):

```bash
./run.sh --feed nates-notebook
./run.sh --feed nates-notebook --limit 5
```

### One-off without a config entry

`--rss` still works as an escape hatch and falls back to the flat `./downloads/` and `./transcripts/` layout:

```bash
./run.sh --rss "<rss-url>" --download
```

## Options

| Flag | Default | Description |
|------|---------|-------------|
| `--feed TAG` | | Feed tag from `feeds.toml` |
| `--config PATH` | `./feeds.toml` | Config file path |
| `--rss URL` | | RSS feed URL (overrides `--feed`'s rss) |
| `--sid COOKIE` | | `substack.sid` cookie (overrides `--feed`'s sid) |
| `--index` | | List episodes in a table |
| `--download` | | Download mp3 files instead of transcripts |
| `--transcribe` | | Transcribe mp3s locally with Whisper |
| `--out DIR` | `./<kind>/<feed-tag>/` | Output directory override |
| `--limit N` | unlimited | Max entries to process. Overrides `max_downloads_per_run` from `feeds.toml`. |
| `--no-limit` | | Bypass `max_downloads_per_run` from `feeds.toml`. Useful for bulk pre-downloads on an unmetered network when you'll be processing the audio later at home. Takes precedence over `--limit` if both are given. |
| `--backfill-headers` | | Splice RSS-derived metadata (title, link, summary, etc.) into existing transcripts that don't yet have YAML frontmatter. Idempotent. Use with `--feed <tag>` to scope to one feed. |
| `--status` | | Print a per-feed health snapshot (RSS count, local mp3s, transcribed, SD card mp3s/transcripts, orphan mp3s without transcripts, headerless transcripts). Combine with `--offline` to skip the RSS fetch. |
| `--check` | | List new episodes per feed (in RSS but not in `.processed`), including byte sizes when the feed publishes them. Doesn't download anything — RSS fetch only. Cheap enough for cron / metered connections. Use with `--feed <tag>` to scope. |
| `--daily` | | Run `--download` then `--transcribe` for every feed not opted out with `daily = false`. Per-feed errors don't block the rest of the routine. Combine with `--feed <tag>` to scope to one feed. |
| `--model SIZE` | `medium` | `tiny`, `base`, `small`, `medium`, `large-v3` |
| `--mp3-dir DIR` | derived from `--feed` | mp3 source for `--transcribe` |
| `--transcript-dir DIR` | derived from `--feed` | Checked by `--download` to skip already-transcribed episodes |

## Whisper models

| Model | Parameters | Relative speed | Quality |
|-------|-----------|----------------|---------|
| `tiny` | 39M | ~40x realtime | Low |
| `base` | 74M | ~30x realtime | Fair |
| `small` | 244M | ~25x realtime | Good |
| `medium` | 769M | ~18x realtime | Very good |
| `large-v3` | 1.5B | ~10x realtime | Best |

Speed estimates are approximate on an M1 MacBook Pro.

## Typical workflow

```bash
# 1. Set up (once)
./setup.sh
cp feeds.example.toml feeds.toml      # edit with your RSS URLs

# 2. Browse what's available
./run.sh --feed nates-notebook --index

# 3. Daily routine — download + transcribe every active feed
./run.sh --daily

# 4. Pre-flight check on a metered connection (no audio fetched)
./run.sh --check

# 5. See how everything's wired
./run.sh --status

# 6. Search transcripts
grep -rl "context engineering" ./transcripts/
```

## Output format

Transcripts are Markdown files named `YYYY-MM-DD - Episode Title.md` under `./transcripts/<feed-tag>/`. Downloaded mp3s use the same naming under `./downloads/<feed-tag>/`. Each transcript opens with YAML frontmatter (title, date, show, host, link, duration, guid) plus a visible header containing the episode's full RSS description and a Show notes link — see the [Episode metadata](#episode-metadata-in-transcripts) section. Diarized transcripts emit `**Speaker A** (mm:ss):` blocks per turn.

Sidecars:
- `./downloads/<feed-tag>/<stem>.meta.json` — episode metadata captured at download (small, paired with the mp3, removed during eviction)
- `./transcripts/<feed-tag>/.diarize/<stem>.json` — cached pyannote turns (lets a retry skip the ~40-min diarize pass on long files)
- `./transcripts/<feed-tag>/.chunks/<stem>/chunk_NNN.json` — per-chunk Whisper output, written as each chunk completes. If transcribe crashes mid-file, a retry resumes from the last successful chunk. Cleaned up automatically once all chunks merge.

## Speaker diarization (optional)

For multi-speaker feeds (interviews, panels), set `diarize = true` on that feed in `feeds.toml` and `--transcribe` will emit speaker-labeled markdown:

```
**Speaker A** (00:00:00):
…opening…

**Speaker B** (00:01:42):
…response…
```

One-time setup:

1. `.venv/bin/pip install pyannote.audio torchaudio` (~3 GB).
2. Make a HuggingFace account, accept the license at <https://huggingface.co/pyannote/speaker-diarization-3.1>.
3. Run `huggingface-cli login` and paste your access token.

Diarization runs alongside Whisper — net wall-clock is roughly 2× the transcribe-only time. Speaker labels are generic (`A`, `B`, …) per-episode; they do not carry across episodes.

For one-off testing, `--diarize` / `--no-diarize` on the CLI overrides whatever's in `feeds.toml`.

### Chapter timestamps

When the RSS description contains an outline like:

```
OUTLINE:
(00:00) – Introduction
(03:00) – Sponsors, Comments, and Reflections
(14:08) – Codecs
```

…the transcript gets `## Title (mm:ss)` headings injected inline, so Obsidian's outline view becomes a navigable chapter index. Detection handles parenthesized, bracketed, and plain line-start formats; works whether chapters are on separate lines (Lex) or run together on one line (Dwarkesh).

Chapter injection is also applied during `--backfill-headers`, so existing transcripts get chapters without re-transcribing. Idempotent — re-running strips and re-injects, so the output is stable.

### Host naming

When the feed's host is known (extracted from the RSS `itunes_author` or set explicitly via `host = "Lex Fridman"` in `feeds.toml`), the speaker who talks the most in the first 60 seconds of the episode gets rendered with the host's first name instead of `Speaker A`:

```
**Lex** (00:00):
…intro…

**Speaker B** (01:42):
…guest's response…
```

The detection is heuristic — interview shows almost always open with the host introducing the episode. Other speakers keep the generic `Speaker B/C/…` labels. The mapping only applies to new transcriptions; to relabel an existing transcript, remove its stem from `.processed` and re-run `--transcribe` (the diarization cache keeps this cheap).

## Episode metadata in transcripts

Each transcript opens with YAML frontmatter (Obsidian-friendly) plus a visible markdown header carrying the episode's title, show, host, link, duration, and full description from the RSS feed:

```
---
title: "FFmpeg: The Incredible Technology Behind Video on the Internet"
date: 2026-05-06
show: Lex Fridman Podcast
host: Lex Fridman
link: https://lexfridman.com/ffmpeg/
duration: 4:23:41
guid: https://lexfridman.com/?p=6450
---

# FFmpeg: The Incredible Technology Behind Video on the Internet

> Jean-Baptiste Kempf is lead developer of VLC and president of VideoLAN…

[Show notes](https://lexfridman.com/ffmpeg/)

---

**Speaker A** (00:00):
…
```

**How the data flows**: `--download` writes a `<stem>.meta.json` sidecar next to the mp3 (~500 bytes). `--transcribe` reads the sidecar and renders the header. The sidecar is removed when the mp3 is evicted (the metadata is permanent inside the transcript by then).

### Backfilling old transcripts

```bash
./run.sh --backfill-headers              # all feeds
./run.sh --backfill-headers --feed lex-fridman
```

Walks each feed's RSS and splices the metadata header into existing `.md` files that don't already have YAML frontmatter. Idempotent — re-running is a no-op for files that already have headers. Files whose stem doesn't appear in the current RSS feed (rotated out, renamed) are skipped with a count.

After backfill, the SD card transcripts get refreshed automatically via the mtime-aware backup logic.

## How dedup works (`.processed`)

Each feed gets an append-only `./transcripts/<feed-tag>/.processed` file — one episode stem per line. This is the canonical "have we transcribed this?" record:

- **`--download`, `--fetch`, `--transcribe`** consult `.processed` to skip episodes already handled — *not* the presence of `.md` files.
- A successful transcript write appends to `.processed`. A failed transcription does not, so failures stay visible and re-runnable on the next pass.
- On first run after this change, `.processed` is bootstrapped from existing `*.md` filenames automatically — no manual migration.

What this enables: you can prune old transcript `.md` files locally without triggering re-downloads or re-transcription. The transcripts on the SD-card backup remain available for grep/restore; the index keeps the dedup intact.

To force a re-transcribe of a specific episode, remove its line from `.processed`. To rebuild the index from scratch, delete `.processed` and run any command — it'll re-bootstrap from whatever `*.md` files are present.
