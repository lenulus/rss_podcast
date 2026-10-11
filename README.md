# Podcast & News Ingestion Toolkit

Two companion pipelines that turn feeds into local, greppable Markdown:

- **Podcasts** (`ss.py`, via `./run.sh`) — download, index, and transcribe episodes from one or more podcast RSS feeds (Substack or otherwise). Most of this README covers this pipeline.
- **News** (`news.py`, via `./news.sh`) — pull full article text from AI-company blogs, newsletters, and paper feeds. See [News ingestion](#news-ingestion-newspy).

Transcription runs locally on Apple Silicon via [mlx-whisper](https://github.com/ml-explore/mlx-examples/tree/main/whisper) — no API keys or cloud services required.

## Prerequisites

- Python **3.11+** (3.9 works for everything except diarization, which requires `pyannote.audio` and bumps the floor to 3.11)
- macOS with Apple Silicon (M1/M2/M3/M4) for GPU-accelerated transcription
- `ffmpeg` (`brew install ffmpeg`) — used for audio duration probing, WAV preconversion before diarization, and 30-min chunking on long audio files
- `curl` (ships with macOS) — only used by feeds or news sources that set `fetcher = "curl"`

## Setup

```bash
./setup.sh
cp feeds.example.toml feeds.toml   # then edit with your private RSS URL(s)
cp news.example.toml news.toml     # optional — only if you want news ingestion
```

`setup.sh` creates a `.venv` and installs `requirements.txt`. `feeds.toml` and `news.toml` are gitignored — they hold private RSS URLs and session cookies.

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

Backups land at `<backup_path>/<tag>/media/` (mp3s), `<backup_path>/<tag>/text/` (transcripts), and `<backup_path>/<tag>/text/.diarize/` (pyannote turn cache). Transcript and diarize-cache backup runs on every `--download`, `--transcribe`, and `--fetch` — idempotent and never deletes from local. Mp3 eviction only happens when `max_episodes_on_disk` is set. The diarize-cache backup is cheap (a few KB per episode) and lets a fresh clone with the SD card mounted recover the cache instead of re-paying hours of pyannote compute.

### Per-feed settings

These can be set under `[defaults]` (applies to every feed) or under `[feeds.<tag>]` (overrides the default).

| Key | Description |
|-----|-------------|
| `rss` | Private RSS feed URL (required, per-feed only) |
| `sid` | `substack.sid` cookie for paywalled transcripts |
| `max_episodes_on_disk` | Cap mp3s kept in `./downloads/<tag>/`. Eviction runs after `--download` and `--transcribe`; oldest go first, but **only mp3s that already have a transcript are eligible** — source audio is never deleted unless its transcript exists. Transcripts themselves are never touched. |
| `backup_path` | Root for backups. Mp3s back up to `<backup_path>/<tag>/media/<file>.mp3`; transcripts back up to `<backup_path>/<tag>/text/<file>.md`; pyannote turn caches back up to `<backup_path>/<tag>/text/.diarize/<stem>.json`. If the path (or its parent — e.g. an unmounted SD card mount point) is missing, the affected backup is **skipped entirely** rather than risking unbacked deletion. |
| `media_dir` | Override where this feed's mp3 backups land. Absolute path. If set, replaces `<backup_path>/<tag>/media/`. |
| `transcript_dir` | Override where this feed's transcript backups land. Absolute path. If set, replaces `<backup_path>/<tag>/text/`. |
| `max_downloads_per_run` | Default cap for `--download` and the transcript-fetch path in a single run. Overridden by an explicit `--limit N` or bypassed entirely with `--no-limit`. |
| `download_order` | `"newest"` (default) or `"oldest"`. Use `"oldest"` for incremental backfill of an archive. |
| `since` | Skip episodes older than this when downloading or fetching transcripts. Accepts an ISO date (`"2026-01-01"`) or a relative age (`"1y"`, `"6m"`, `"2w"`, `"14d"`, resolved against now on each run). Forward-only — already-ingested local files are untouched. Unset = no cutoff. |
| `fetcher` | Set to `"curl"` to download mp3s with the system `curl` binary instead of python-requests. For CDNs that 403 the requests User-Agent (notably Acast). Leave unset otherwise. |
| `diarize` | `true` runs speaker diarization (pyannote.audio) during `--transcribe`. The `.md` output gains `**Speaker A** (mm:ss):` headers per turn. Adds runtime and a one-time HuggingFace setup; opt-in per feed. |
| `daily` | Defaults to `true`. Set `daily = false` to exclude this feed from the `--daily` macro (the feed remains usable via explicit `--feed <tag> --download` / `--transcribe`). |
| `whisper_batch_size` | Legacy setting from the previous Lightning Whisper MLX engine. Still parsed, but mlx-whisper decodes sequentially, so it currently has no effect on transcription. |
| `subprocess_per_episode` | `true` runs each episode in a fresh Python process instead of an in-process loop. Adds ~30–60s of model-load overhead per episode, but stops the progressive slowdown seen on long backlogs (50+ episodes). Default `false`. CLI: `--subprocess-per-episode` / `--no-subprocess-per-episode`. |
| `subprocess_concurrency` | How many transcribe subprocesses to run in parallel for this feed (default `1`). Only meaningful with `subprocess_per_episode = true`; each worker uses ~3 GB of unified memory. CLI: `--subprocess-concurrency N`. |
| `model` | Whisper model size for this feed (`tiny`, `base`, `small`, `medium` (default), `large-v3`). Useful for boosting quality on tougher audio (technical jargon, accents, music-heavy ads) without slowing other feeds. CLI `--model` overrides when set. |
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

Transcription skips mp3s whose stem is already in `.processed` — safe to interrupt and resume.

If 8% or more of a transcript's words are caught in a repeated phrase (the signature of a Whisper decoder loop), a warning is printed at transcription time. It warns rather than blocks; re-run the episode with `--reprocess` if the transcript really is corrupted.

### Re-transcribe an episode

```bash
# One episode, by stem (the filename without .mp3)
./run.sh --feed latent-space --transcribe --reprocess "2024-05-16 - Episode Title"

# Every episode of the feed that still has a local mp3
./run.sh --feed latent-space --transcribe --reprocess
```

`--reprocess` forces episodes already in `.processed` back into scope, clears any stale chunk checkpoint, and overwrites the `.md` only once the new transcription succeeds. The mp3 must be present in `./downloads/<feed-tag>/` — restore it from the backup first if it has been evicted. The diarization cache is reused, so only the Whisper pass is repeated.

`reprocess.py` is a batch helper for the same job: given an audit CSV of affected transcripts, it restores mp3s from backup and invalidates the old output so the episodes get picked up again. It is dry-run by default (`--apply` to change files, `--run` to transcribe); see the docstring at the top of the file.

### Long backlogs

```bash
./loop.sh --feed lex-fridman
./loop.sh --feed lex-fridman --batch 40 --subprocess-concurrency 2
./loop.sh --feed lex-fridman --diarize-only --subprocess-concurrency 6
```

On multi-hour runs, throughput drifts down as GPU driver state accumulates under the Python parent process. `loop.sh` wraps `./run.sh --transcribe` in batches (`--batch N`, default 60), restarting the parent between batches, and keeps the Mac awake with `caffeinate` for the duration. Extra arguments pass through to `run.sh`. It exits with an error if a full batch makes no progress.

To split diarization from transcription, run `--diarize-only` first (it fills the `.diarize/` cache and skips Whisper), then a plain `--transcribe`. `CLI_README.md` has the recommended settings.

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
| `--reprocess [STEM]` | | With `--transcribe`: re-transcribe episodes even if already in `.processed`, overwriting the `.md` in place. With a stem, redo just that episode; bare, redo every episode of the feed that has a local mp3. |
| `--diarize` / `--no-diarize` | per-feed `diarize` | Force speaker diarization on or off for this run. |
| `--diarize-only` | | Run only the pyannote diarization stage and write the `.diarize/` cache; skip Whisper. A later `--transcribe` then reuses the cache. |
| `--subprocess-per-episode` / `--no-subprocess-per-episode` | per-feed `subprocess_per_episode` | Run each episode in a fresh Python process. |
| `--subprocess-concurrency N` | per-feed `subprocess_concurrency` | Max parallel transcribe subprocesses for one feed. |
| `--only STEM` | | Restrict `--transcribe` to a single mp3 by stem. |
| `--backfill-headers` | | Splice RSS-derived metadata (title, link, summary, etc.) into existing transcripts that don't yet have YAML frontmatter. Idempotent. Use with `--feed <tag>` to scope to one feed. |
| `--status` | | Print a per-feed health snapshot (RSS count, local mp3s, transcribed, SD card mp3s/transcripts, orphan mp3s without transcripts, headerless transcripts). Combine with `--offline` to skip the RSS fetch. |
| `--offline` | | With `--status`, use only local data. |
| `--check` | | List new episodes per feed (in RSS but not in `.processed`), including byte sizes when the feed publishes them. Doesn't download anything — RSS fetch only. Cheap enough for cron / metered connections. Use with `--feed <tag>` to scope. |
| `--daily` | | Run `--download` then `--transcribe` for every feed not opted out with `daily = false`. Per-feed errors don't block the rest of the routine. Combine with `--feed <tag>` to scope to one feed. |
| `--model SIZE` | `medium` (or per-feed `model`) | `tiny`, `base`, `small`, `medium`, `large-v3`. CLI flag overrides per-feed `model` in `feeds.toml` when set. |
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

# 6. Pull new articles from every configured news source (optional)
./news.sh --all

# 7. Search transcripts and articles
grep -rl "context engineering" ./transcripts/ ./news/
```

## Output format

Transcripts are Markdown files named `YYYY-MM-DD - Episode Title.md` under `./transcripts/<feed-tag>/`. Downloaded mp3s use the same naming under `./downloads/<feed-tag>/`. Each transcript opens with YAML frontmatter (title, date, show, host, link, duration, guid) plus a visible header containing the episode's full RSS description and a Show notes link — see the [Episode metadata](#episode-metadata-in-transcripts) section. Diarized transcripts emit `**Speaker A** (mm:ss):` blocks per turn.

Sidecars:
- `./downloads/<feed-tag>/<stem>.meta.json` — episode metadata captured at download (small, paired with the mp3, removed during eviction)
- `./transcripts/<feed-tag>/.diarize/<stem>.json` — cached pyannote turns (lets a retry skip the ~40-min diarize pass on long files). The cache is validated against the mp3's size and SHA-256, so it survives a re-download or a restore from backup. Mirrored to `<backup_path>/<tag>/text/.diarize/` on every pass — a fresh clone with the SD card mounted can copy these back to skip re-diarize entirely.
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

The detection is heuristic — interview shows almost always open with the host introducing the episode. Other speakers keep the generic `Speaker B/C/…` labels. The mapping only applies to new transcriptions; to relabel an existing transcript, re-run it with `--transcribe --reprocess "<stem>"` (the diarization cache keeps this cheap).

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

After backfill, the SD card transcripts get refreshed automatically: the backup pass re-copies any file whose size or content differs from the backed-up copy.

## How dedup works (`.processed`)

Each feed gets an append-only `./transcripts/<feed-tag>/.processed` file — one episode stem per line. This is the canonical "have we transcribed this?" record:

- **`--download`, `--fetch`, `--transcribe`** consult `.processed` to skip episodes already handled — *not* the presence of `.md` files.
- A successful transcript write appends to `.processed`. A failed transcription does not, so failures stay visible and re-runnable on the next pass.
- On first run after this change, `.processed` is bootstrapped from existing `*.md` filenames automatically — no manual migration.

What this enables: you can prune old transcript `.md` files locally without triggering re-downloads or re-transcription. The transcripts on the SD-card backup remain available for grep/restore; the index keeps the dedup intact.

To force a re-transcribe of a specific episode, use `--transcribe --reprocess "<stem>"` rather than editing `.processed` by hand. To rebuild the index from scratch, delete `.processed` and run any command — it'll re-bootstrap from whatever `*.md` files are present.

## News ingestion (`news.py`)

A companion pipeline that fetches full article text from blogs, newsletters, and paper feeds, one Markdown file per article. It follows the same conventions as the podcast side: a gitignored TOML config (`news.toml`), a per-source `.processed` dedup index, and an optional backup mirror. Run it through `./news.sh`, which activates the venv the same way `./run.sh` does.

### Setup

```bash
cp news.example.toml news.toml          # then edit — enable the sources you want
./news.sh --source openai --limit 1     # smoke-test one source
```

`news.example.toml` is the reference for the config: it has a worked, commented block for most source types and options.

### Usage

```bash
# Ingest every configured source
./news.sh --all

# Ingest one source, optionally capped
./news.sh --source anthropic
./news.sh --source anthropic --limit 10

# Preview new URLs without fetching anything
./news.sh --source anthropic --check

# Per-source article counts and last-fetched time
./news.sh --status

# Give URLs that were previously given up on another try
./news.sh --all --retry-failed

# Manually ingest a single article into news/<slug>/
./news.sh --source anthropic --fetch-one "https://example.com/post" --date 2026-09-01
```

| Flag | Default | Description |
|------|---------|-------------|
| `--config PATH` | `./news.toml` | Config file path |
| `--source SLUG` | | Process one source from `news.toml` |
| `--all` | | Process every configured source. A crash in one source doesn't stop the rest. |
| `--check` | | List new URLs without fetching them |
| `--limit N` | unlimited | Max articles per source this run |
| `--status` | | Per-source local article count and last-fetched timestamp |
| `--retry-failed` | | Clear each targeted source's `.failed` list and attempt counts before discovery |
| `--fetch-one URL` | | Ingest a single URL into `news/<--source>/` through the normal flow (frontmatter, dedup, backup). The slug doesn't have to exist in `news.toml`. Combine with `--check` for a dry run. |
| `--date YYYY-MM-DD` | today | With `--fetch-one`: override the publish date |
| `--title TEXT` | | With `--fetch-one`: override the title when extraction misses it |

### Source types

Each `[sources.<slug>]` block has a `type` that decides how articles are discovered:

| `type` | Discovers articles from | Main keys |
|--------|-------------------------|-----------|
| `rss` | An RSS/Atom feed. Only the links are used — each article page is fetched for the full body. | `url` |
| `rss-inline` | An RSS feed that carries the whole article in the feed itself. No per-article request is made, which suits paywalled feeds whose pages only return a teaser. | `url` |
| `sitemap` | A `sitemap.xml` (or sitemap index), filtered down to the article URLs. | `sitemap_url`, `path_filter`, `path_regex`, `path_exclude`, `sub_sitemap_filter` |
| `html-listing` | The cards on a server-rendered index page, matched by a regex. The card's date is treated as the publish date. | `listing_url`, `item_regex`, `path_exclude` |
| `substack-section-archive` | Substack's paginated archive API for one section — the full history, where RSS stops at the 20 most recent. | `base_url`, `section_id`, `sid` |
| `static-markdown-spa` | Client-rendered sites that load raw `.md` files at runtime; slugs are read out of the JS bundle. | `base_url`, `bundle_regex`, `slug_regex`, `article_url_template` |
| `hf-daily-papers` | Hugging Face Daily Papers, via its JSON API. Writes one file per paper with the abstract and metadata. | `discovery_mode`, `min_upvotes`, `top_n`, `fetch_fulltext` |

### Common source settings

These can be set per source; `backup_path`, `since`, `fetch_delay_seconds`, and `max_failures` can also go under `[defaults]`.

| Key | Description |
|-----|-------------|
| `since` | Skip articles older than an ISO date or relative age (`"1y"`, `"6m"`, `"2w"`, `"14d"`). Forward-only, same semantics as the per-feed setting. |
| `fetch_delay_seconds` | Pause between article fetches (default `1.5`). |
| `max_failures` | Consecutive failures before a URL is moved to `.failed` and skipped (default `3`). |
| `backup_path` | Root for the backup mirror. Articles are copied to `<backup_path>/news/<slug>/text/`. The podcast `backup_path` can be reused — the `news/` level keeps the two apart. |
| `fetcher` | `"curl"` routes discovery and article fetches through the system `curl` binary, for sites whose bot protection blocks python-requests. |
| `sid` | `connect.sid` cookie for paid Substack publications on a custom domain. This is not the `substack.sid` cookie the podcast side uses — see `docs/news-substack-auth.md`. |
| `date_regex` | Regex matched against the raw page HTML to supply the publish date, for sites where automatic date extraction picks the wrong one. |
| `date_from_slug` | Regex that derives the date from a date-coded URL slug (with `slug_year_fallback` when the slug has no year). |
| `ignore_dates` | List of extracted dates known to be bogus for this source; on a hit the article falls back to the sitemap date, then the fetch date. |
| `strip_after` | Marker line; everything from it onward is cut from the body. For sites that append fixed boilerplate. |

### Output

```
news/<slug>/YYYY-MM-DD - <url-slug>.md     # one article per file
news/<slug>/.processed                     # dedup index — one URL per line
news/<slug>/.failed                        # URLs given up on after max_failures attempts
news/<slug>/.attempts.json                 # consecutive-failure counts
<backup_path>/news/<slug>/text/*.md        # backup mirror, when configured
```

Each article opens with YAML frontmatter, followed by the body as Markdown:

```
---
title: "Example post title"
type: article
source: anthropic
url: https://www.anthropic.com/news/example-post
published: 2026-09-01T00:00:00+00:00
fetched: 2026-09-02T13:04:11.512345+00:00
authors: []
tags: [news]
---

# Example post title

…
```

Articles ingested with `--fetch-one` are tagged `[news, backfilled]`. Hugging Face papers use `type: paper` and carry extra fields (arXiv ID and links, authors, upvotes, keywords).

Body extraction uses [trafilatura](https://trafilatura.readthedocs.io/), with a plainer HTML-to-text fallback when it returns nothing. Dedup compares normalized URLs, so a change of scheme, a trailing slash, or a site moving its posts under a new path prefix doesn't cause a re-fetch.

## Further reading

- `CLI_README.md` — operator runbook: recommended configuration, command map, precedence rules, failure recovery, performance reference
- `feeds.example.toml` / `news.example.toml` — annotated config templates
- `docs/news-roadmap.md` — news pipeline status and known limitations (e.g. sites that block automated fetches)
- `docs/news-substack-auth.md` — how to get the `connect.sid` cookie for paid Substack sources
- `docs/news-hf-papers-design.md` — design notes for Hugging Face Daily Papers ingestion
- `docs/lessons-learned.md` — notes from tuning transcribe-pipeline throughput
