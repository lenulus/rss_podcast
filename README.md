# Substack Podcast Toolkit

Download, index, and transcribe podcast episodes from one or more Substack RSS feeds.

Transcription runs locally on Apple Silicon via [Lightning Whisper MLX](https://github.com/mustafaaljadery/lightning-whisper-mlx) — no API keys or cloud services required.

## Prerequisites

- Python 3.9+
- macOS with Apple Silicon (M1/M2/M3/M4) for GPU-accelerated transcription
- `ffmpeg` (for audio duration detection): `brew install ffmpeg`

## Setup

```bash
./setup.sh
cp feeds.example.toml feeds.toml   # then edit with your private RSS URL(s)
```

`setup.sh` creates a `.venv` and installs `requirements.txt`. `feeds.toml` is gitignored — it holds your private RSS URLs.

## Configuring feeds

Each feed gets a tag, which is used as the subfolder name under `./downloads/<tag>/` and `./transcripts/<tag>/`:

```toml
[feeds.nates-notebook]
rss = "https://api.substack.com/feed/podcast/XXXXXX/private-key.rss"
# sid = "optional-substack.sid-cookie"

[feeds.new-show]
rss = "https://api.substack.com/feed/podcast/YYYYYY/private-key.rss"
max_episodes_on_disk = 20
max_downloads_per_run = 5
download_order = "oldest"
backup_path = "/Volumes/PodcastSD/archive"
```

### Per-feed settings

| Key | Description |
|-----|-------------|
| `rss` | Private RSS feed URL (required) |
| `sid` | `substack.sid` cookie for paywalled transcripts (optional) |
| `max_episodes_on_disk` | Cap mp3s kept in `./downloads/<tag>/`. Eviction runs after `--download` and `--transcribe`; oldest go first, but **only mp3s that already have a transcript are eligible** — source audio is never deleted unless its transcript exists. Transcripts themselves are never touched. |
| `backup_path` | When evicting, copy the mp3 to `<backup_path>/<tag>/<file>.mp3` first, then delete locally. If the path (or its parent — e.g. an unmounted SD card mount point) is missing, eviction is **skipped entirely** rather than risking unbacked deletion. |
| `max_downloads_per_run` | Default cap for `--download` and the transcript-fetch path in a single run. Overridden by an explicit `--limit`. |
| `download_order` | `"newest"` (default) or `"oldest"`. Use `"oldest"` for incremental backfill of an archive. |

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

Downloads skip files that already exist locally or already have a transcript in the matching `./transcripts/<feed-tag>/` folder.

### Transcribe

```bash
# Transcribe just one feed
./run.sh --transcribe --feed nates-notebook

# Transcribe everything across all feeds (walks ./downloads/*/)
./run.sh --transcribe

./run.sh --transcribe --feed nates-notebook --model large-v3
./run.sh --transcribe --feed nates-notebook --limit 5
```

Transcription skips mp3s that already have a matching `.md` — safe to interrupt and resume.

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
| `--limit N` | unlimited | Max entries to process |
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
# 1. Set up
./setup.sh
cp feeds.example.toml feeds.toml      # edit with your RSS URL

# 2. Browse what's available
./run.sh --feed nates-notebook --index

# 3. Download all episodes
./run.sh --feed nates-notebook --download

# 4. Transcribe everything across every feed (runs overnight for large archives)
./run.sh --transcribe

# 5. Search transcripts
grep -rl "context engineering" ./transcripts/
```

## Output format

Transcripts are saved as Markdown files named `YYYY-MM-DD - Episode Title.md` under `./transcripts/<feed-tag>/`. Downloaded mp3s use the same naming under `./downloads/<feed-tag>/`.
