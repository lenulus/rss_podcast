# Substack Podcast Toolkit

Download, index, and transcribe podcast episodes from a Substack RSS feed.

Transcription runs locally on Apple Silicon via [Lightning Whisper MLX](https://github.com/mustafaaljadery/lightning-whisper-mlx) — no API keys or cloud services required.

## Prerequisites

- Python 3.9+
- macOS with Apple Silicon (M1/M2/M3/M4) for GPU-accelerated transcription
- `ffmpeg` (for audio duration detection): `brew install ffmpeg`

## Setup

```bash
./setup.sh
```

This creates a `.venv` virtual environment and installs all dependencies from `requirements.txt`.

## Usage

All commands are run through `./run.sh`, which activates the venv automatically.

### List episodes

Print a table of all episodes with date, duration, content type, and title:

```bash
./run.sh --rss "<your-rss-url>" --index
./run.sh --rss "<your-rss-url>" --index --limit 20
```

### Download mp3s

Download podcast audio files to `./downloads/`:

```bash
./run.sh --rss "<your-rss-url>" --download
./run.sh --rss "<your-rss-url>" --download --limit 10
./run.sh --rss "<your-rss-url>" --download --out ./my-episodes
```

Downloads skip files that already exist in the output directory or already have a transcript in `./transcripts/`.

### Transcribe

Transcribe downloaded mp3s to Markdown files in `./transcripts/`:

```bash
./run.sh --transcribe
./run.sh --transcribe --model large-v3
./run.sh --transcribe --limit 5
./run.sh --transcribe --mp3-dir ./my-episodes --out ./my-transcripts
```

Transcription automatically skips mp3s that already have a matching `.md` file, so it's safe to interrupt and resume.

### Fetch Substack-hosted transcripts

For episodes that have transcripts hosted on Substack (not all do):

```bash
./run.sh --rss "<your-rss-url>"
./run.sh --rss "<your-rss-url>" --limit 5
```

## Options

| Flag | Default | Description |
|------|---------|-------------|
| `--rss URL` | *(required unless `--transcribe`)* | Private RSS feed URL |
| `--index` | | List episodes in a table instead of downloading |
| `--download` | | Download mp3 files instead of fetching transcripts |
| `--transcribe` | | Transcribe mp3s locally using Whisper |
| `--out DIR` | `./transcripts` or `./downloads` | Output directory |
| `--limit N` | unlimited | Max entries to process |
| `--model SIZE` | `medium` | Whisper model: `tiny`, `base`, `small`, `medium`, `large-v3` |
| `--mp3-dir DIR` | `./downloads` | Source directory for `--transcribe` |
| `--transcript-dir DIR` | `./transcripts` | Checked by `--download` to skip already-transcribed episodes |
| `--sid COOKIE` | | `substack.sid` session cookie (optional, for paywalled transcripts) |

## Whisper models

| Model | Parameters | Relative speed | Quality |
|-------|-----------|----------------|---------|
| `tiny` | 39M | ~40x realtime | Low |
| `base` | 74M | ~30x realtime | Fair |
| `small` | 244M | ~25x realtime | Good |
| `medium` | 769M | ~18x realtime | Very good |
| `large-v3` | 1.5B | ~10x realtime | Best |

Speed estimates are approximate on an M1 MacBook Pro. `medium` is the default and offers a good balance of quality and speed.

## Typical workflow

```bash
# 1. Set up
./setup.sh

# 2. Browse what's available
./run.sh --rss "<url>" --index

# 3. Download all episodes
./run.sh --rss "<url>" --download

# 4. Transcribe everything (runs overnight for large archives)
./run.sh --transcribe

# 5. Search transcripts
grep -rl "context engineering" ./transcripts/
```

## Output format

Transcripts are saved as Markdown files named `YYYY-MM-DD - Episode Title.md` with the full text content. Downloaded mp3s follow the same naming convention.
