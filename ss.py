#!/usr/bin/env python3
from __future__ import annotations
"""
ss.py — Download Substack podcast transcripts and audio as Markdown / mp3 files.

Usage:
    # Single-feed (escape hatch)
    python ss.py --rss <rss_url> [--out <dir>] [--limit <n>] [--sid <cookie>]

    # Multi-feed via feeds.toml
    python ss.py --feed <tag> [--download | --index | --transcribe]
    python ss.py --transcribe                  # transcribe every feed under ./downloads/*

See feeds.example.toml for the config format.
"""

import argparse
import gc
import re
import shutil
import sys
import time
from datetime import timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse

import feedparser
import requests


# ── Config ────────────────────────────────────────────────────────────────────

def load_config(config_path: Path) -> dict:
    """Load feeds.toml. Returns {} if file is missing."""
    if not config_path.exists():
        return {}
    try:
        import tomllib  # Python 3.11+
    except ImportError:
        import tomli as tomllib  # Python 3.9 / 3.10 fallback
    with open(config_path, "rb") as f:
        return tomllib.load(f)


def feed_cfg_for(config: dict, tag: str | None) -> dict:
    """Resolved config for a feed: [defaults] merged with per-feed overrides.

    Returns {} if tag is missing or not in config.
    """
    if not tag:
        return {}
    feeds = config.get("feeds", {})
    if tag not in feeds:
        return {}
    defaults = config.get("defaults", {}) or {}
    return {**defaults, **(feeds[tag] or {})}


def resolve_feed(args, config: dict) -> None:
    """If --feed <tag> was passed, populate rss/sid from config (CLI flags win)."""
    args._config = config
    args._feed_cfg = {}
    if not args.feed:
        return
    feeds = config.get("feeds", {})
    if args.feed not in feeds:
        available = ", ".join(sorted(feeds.keys())) or "(none)"
        sys.exit(f"Feed '{args.feed}' not found in {args.config}. Available: {available}")
    cfg = feed_cfg_for(config, args.feed)
    args._feed_cfg = cfg
    if not args.rss:
        args.rss = cfg.get("rss")
    if not args.sid:
        args.sid = cfg.get("sid")
    if not args.rss:
        sys.exit(f"Feed '{args.feed}' in {args.config} has no 'rss' field.")


def effective_limit(args) -> int | None:
    """Resolve the effective per-run cap.

    Precedence: --no-limit > --limit > feed's max_downloads_per_run > unbounded.
    """
    if getattr(args, "no_limit", False):
        return None
    if args.limit is not None:
        return args.limit
    return args._feed_cfg.get("max_downloads_per_run")


def sort_entries_by_order(entries, feed_cfg: dict):
    """Sort feed entries by download_order ('newest' default, or 'oldest')."""
    order = (feed_cfg.get("download_order") or "newest").lower()
    if order not in ("newest", "oldest"):
        sys.exit(f"Invalid download_order: {order!r} (must be 'newest' or 'oldest')")
    return sorted(
        entries,
        key=lambda e: parsedate_to_datetime(e.published),
        reverse=(order == "newest"),
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def sanitize_filename(title: str) -> str:
    """Strip characters that are unsafe in filenames, collapse whitespace."""
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', title)
    safe = re.sub(r'\s+', ' ', safe).strip()
    return safe[:120]


def pub_date_to_iso(entry) -> str:
    """Return YYYY-MM-DD from a feedparser entry."""
    try:
        dt = parsedate_to_datetime(entry.published)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        return "0000-00-00"


def transcript_to_text(segments: list) -> str:
    """Concatenate segment texts into a clean transcript string."""
    return "\n".join(seg["text"].strip() for seg in segments if seg.get("text", "").strip())


def base_url_from_entry(entry) -> str:
    """Extract 'https://<publication>.substack.com' from an entry link."""
    parsed = urlparse(entry.link)
    return f"{parsed.scheme}://{parsed.netloc}"


def get_transcript_url(base_url: str, slug: str, session_cookie: str | None) -> str | None:
    """Fetch the post JSON and return the transcript CDN URL, or None."""
    url = f"{base_url}/api/v1/posts/{slug}"
    cookies = {"substack.sid": session_cookie} if session_cookie else {}
    try:
        r = requests.get(url, cookies=cookies, timeout=15)
        r.raise_for_status()
        post = r.json()
        return (
            post.get("podcastUpload", {})
                .get("transcription", {})
                .get("cdn_url")
        )
    except Exception as e:
        print(f"  ✗ Could not fetch post JSON for {slug}: {e}")
        return None


def fetch_transcript_segments(cdn_url: str) -> list | None:
    """Download and parse the transcript JSON from the CDN."""
    try:
        r = requests.get(cdn_url, timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  ✗ Could not fetch transcript: {e}")
        return None


def write_markdown(path: Path, title: str, pub_date: str, source_url: str, text: str) -> None:
    """Write transcript as a Markdown file with a simple header."""
    content = f"""# {title}

**Date:** {pub_date}
**Source:** {source_url}

---

{text}
"""
    path.write_text(content, encoding="utf-8")


def format_duration(seconds: str | None) -> str:
    """Format seconds into MM:SS or HH:MM:SS."""
    if not seconds:
        return "--:--"
    try:
        s = int(seconds)
        m, s = divmod(s, 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
    except (ValueError, TypeError):
        return "--:--"


def content_type(entry) -> str:
    """Determine content type from feed entry links."""
    for link in entry.get("links", []):
        mime = link.get("type", "")
        if mime.startswith("audio"):
            return "audio"
        if mime.startswith("video"):
            return "video"
    return "text"


def get_audio_url(entry) -> str | None:
    """Extract the audio enclosure URL from a feed entry."""
    for link in entry.get("links", []):
        if link.get("type", "").startswith("audio"):
            return link.get("href")
    return None


def download_file(url: str, path: Path) -> bool:
    """Stream-download a file to disk. Returns True on success."""
    try:
        with requests.get(url, stream=True, timeout=30) as r:
            r.raise_for_status()
            size = 0
            with open(path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
                    size += len(chunk)
        mb = size / (1024 * 1024)
        print(f"    ✓ Saved: {path.name} ({mb:.1f} MB)")
        return True
    except Exception as e:
        print(f"    ✗ Download failed: {e}")
        if path.exists():
            path.unlink()
        return False


def get_audio_duration_secs(mp3_path: Path) -> float | None:
    """Get duration of an audio file in seconds using ffprobe."""
    import subprocess
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(mp3_path)],
            capture_output=True, text=True, timeout=10,
        )
        return float(result.stdout.strip())
    except Exception:
        return None


# ── Episode metadata (RSS → sidecar → markdown header) ───────────────────────
#
# At download time we capture useful RSS fields into a JSON sidecar next to
# the mp3. Transcribe reads the sidecar and renders a YAML frontmatter +
# visible header at the top of the .md. The sidecar is paired with the mp3
# and removed during eviction.

def extract_episode_metadata(entry, feed) -> dict:
    """Pull useful fields from a feedparser entry into a serializable dict."""
    import html as _html
    summary_raw = entry.get("summary", "") or entry.get("description", "")
    # Strip HTML tags, decode entities, normalize line endings.
    summary = re.sub(r"<[^>]+>", "", summary_raw)
    summary = _html.unescape(summary).replace("\r\n", "\n").replace("\r", "\n").strip()

    duration = entry.get("itunes_duration", "")
    if duration and ":" not in str(duration):
        try:
            duration = format_timestamp(int(duration))
        except (ValueError, TypeError):
            duration = str(duration)

    pub_date = ""
    if entry.get("published"):
        try:
            pub_date = parsedate_to_datetime(entry.published).astimezone(timezone.utc).strftime("%Y-%m-%d")
        except Exception:
            pass

    return {
        "title": entry.get("title", "").strip(),
        "link": entry.get("link", "").strip(),
        "summary": summary,
        "duration": str(duration) if duration else "",
        "pub_date": pub_date,
        "show": (feed.feed.get("title", "") or "").strip(),
        "host": (feed.feed.get("itunes_author", "") or feed.feed.get("author", "") or "").strip(),
        "guid": (entry.get("id", "") or entry.get("guid", "") or "").strip(),
    }


def metadata_sidecar_path(mp3_path: Path) -> Path:
    """Sidecar location next to the mp3."""
    return mp3_path.with_suffix(".meta.json")


def write_metadata_sidecar(mp3_path: Path, meta: dict) -> None:
    """Persist episode metadata next to the mp3 for later use by --transcribe."""
    import json
    metadata_sidecar_path(mp3_path).write_text(json.dumps(meta, indent=2), encoding="utf-8")


def load_metadata_sidecar(mp3_path: Path) -> dict | None:
    """Read sidecar if it exists; otherwise return None (legacy mp3 with no metadata)."""
    import json
    p = metadata_sidecar_path(mp3_path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _yaml_escape(s: str) -> str:
    """Quote a single-line string for YAML if it contains special characters."""
    s_str = str(s) if s is not None else ""
    if not s_str:
        return '""'
    if any(c in s_str for c in (':', '"', "'", '#', '\n', '[', ']', '{', '}', ',', '&', '*', '!', '|', '>', '%', '@', '`')) \
            or s_str[0] in ('-', '?', '"') \
            or s_str.strip() != s_str:
        return '"' + s_str.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s_str


def render_metadata_header(meta: dict | None) -> str:
    """YAML frontmatter + visible markdown header, ending with the body separator.

    Returns "" if `meta` is empty/None — caller can fall back to a minimal
    header. The output ends with `---\\n\\n` so the body can be concatenated
    directly.
    """
    if not meta:
        return ""

    title = meta.get("title", "")
    pub_date = meta.get("pub_date", "")
    show = meta.get("show", "")
    host = meta.get("host", "")
    link = meta.get("link", "")
    duration = meta.get("duration", "")
    guid = meta.get("guid", "")
    summary = meta.get("summary", "")

    fm: list[str] = ["---"]
    if title:    fm.append(f"title: {_yaml_escape(title)}")
    if pub_date: fm.append(f"date: {pub_date}")
    if show:     fm.append(f"show: {_yaml_escape(show)}")
    if host:     fm.append(f"host: {_yaml_escape(host)}")
    if link:     fm.append(f"link: {_yaml_escape(link)}")
    if duration: fm.append(f"duration: {_yaml_escape(duration)}")
    if guid:     fm.append(f"guid: {_yaml_escape(guid)}")
    fm.append("---")
    fm.append("")
    if title:
        fm.append(f"# {title}")
        fm.append("")
    if summary:
        for line in summary.split("\n"):
            stripped = line.strip()
            fm.append(f"> {stripped}" if stripped else ">")
        fm.append("")
    if link:
        fm.append(f"[Show notes]({link})")
        fm.append("")
    fm.append("---")
    fm.append("")
    return "\n".join(fm) + "\n"


# ── Whisper repetition-loop cleanup ───────────────────────────────────────────
#
# Whisper occasionally gets stuck producing the same sentence over and over —
# especially during silence, music, or sponsor breaks. These aren't hallucinated
# content (the words are real), they're decoder loops. We collapse runs of N+
# consecutive identical sentences to a single occurrence. Threshold of 3
# preserves legitimate doubled phrases ("Yes. Yes.") while killing the loops.

REPETITION_THRESHOLD = 3


def _normalize_for_compare(s: str) -> str:
    """Lowercase, strip, collapse internal whitespace — for run detection only."""
    return re.sub(r"\s+", " ", s.lower().strip())


def collapse_repetitions(text: str, threshold: int = REPETITION_THRESHOLD) -> str:
    """Collapse runs of `threshold`+ consecutive identical sentences to one.

    Splits on sentence-terminal punctuation followed by whitespace. Comparison
    is case- and whitespace-insensitive but punctuation-sensitive (a sentence
    ending in '?' is not equivalent to one ending in '.'). Preserves the
    original first occurrence verbatim.
    """
    if not text:
        return text
    sentences = re.split(r"(?<=[.!?])\s+", text)
    out: list[str] = []
    i = 0
    while i < len(sentences):
        j = i
        norm_i = _normalize_for_compare(sentences[i])
        while j + 1 < len(sentences) and _normalize_for_compare(sentences[j + 1]) == norm_i:
            j += 1
        run = j - i + 1
        if run >= threshold:
            out.append(sentences[i])
        else:
            out.extend(sentences[i:j + 1])
        i = j + 1
    return " ".join(out)


# ── Memory hygiene ────────────────────────────────────────────────────────────
#
# Long transcribe runs (hundreds of multi-hour episodes) accumulate Python heap
# growth from MLX/torch tensors that aren't aggressively returned to the OS.
# Two-pronged defense: GC + GPU-cache clear after each episode, plus a full
# model reload every N episodes to truly reset the working set.

RELOAD_EVERY = 5  # full pipeline+model reload cadence (episodes)


def _release_memory() -> None:
    """Force GC + clear MLX/torch GPU caches. No-op if libs aren't loaded."""
    gc.collect()
    try:
        import mlx.core as mx
        try:
            mx.clear_cache()
        except AttributeError:
            try:
                mx.metal.clear_cache()
            except AttributeError:
                pass
    except Exception:
        pass
    try:
        import torch
        if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            torch.mps.empty_cache()
    except Exception:
        pass


# ── Diarization ───────────────────────────────────────────────────────────────
#
# Speaker diarization runs alongside Whisper for feeds with `diarize = true`.
# Pipeline is lazy-loaded — pyannote.audio is only imported when at least one
# feed in the run actually needs it.

def load_diarization_pipeline():
    """Load pyannote/speaker-diarization-3.1, moving it to MPS if available."""
    try:
        from pyannote.audio import Pipeline
        import torch
    except ImportError as e:
        sys.exit(
            f"Diarization requested but pyannote.audio isn't installed: {e}\n"
            "Install with: .venv/bin/pip install pyannote.audio torchaudio"
        )
    try:
        pipeline = Pipeline.from_pretrained("pyannote/speaker-diarization-3.1")
    except Exception as e:
        sys.exit(
            f"Failed to load pyannote/speaker-diarization-3.1: {e}\n"
            "Make sure you've accepted the license at "
            "https://huggingface.co/pyannote/speaker-diarization-3.1 and "
            "run `huggingface-cli login`."
        )
    if pipeline is None:
        sys.exit("Pipeline loaded as None — check HuggingFace auth.")
    if torch.backends.mps.is_available():
        pipeline.to(torch.device("mps"))
    return pipeline


def diarize_audio(mp3_path: Path, pipeline) -> list[tuple[float, float, str]]:
    """Return [(start_seconds, end_seconds, speaker_label), …].

    Pre-converts the mp3 to a 16 kHz mono WAV via ffmpeg before feeding it to
    pyannote — mp3 frame quantization causes occasional sample-count mismatches
    in pyannote's internal chunker (~25 ms shortfalls). WAV avoids the issue.
    """
    import subprocess
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        wav_path = tmp.name
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(mp3_path),
             "-ar", "16000", "-ac", "1", wav_path],
            check=True,
        )
        diarization = pipeline(wav_path)
        # pyannote 4.x returns DiarizeOutput; the inner Annotation lives at
        # .exclusive_speaker_diarization (non-overlapping turns, which is what
        # our segment-midpoint alignment expects).
        annotation = getattr(diarization, "exclusive_speaker_diarization", None) \
            or getattr(diarization, "speaker_diarization", diarization)
        return [(turn.start, turn.end, speaker)
                for turn, _, speaker in annotation.itertracks(yield_label=True)]
    finally:
        Path(wav_path).unlink(missing_ok=True)


def humanize_speaker(label: str) -> str:
    """SPEAKER_00 → 'A', SPEAKER_01 → 'B', etc. Other labels pass through."""
    if label and label.startswith("SPEAKER_"):
        try:
            n = int(label[len("SPEAKER_"):])
            if 0 <= n < 26:
                return chr(ord("A") + n)
        except ValueError:
            pass
    return label or "?"


def format_timestamp(seconds: float) -> str:
    """Seconds → 'MM:SS' or 'H:MM:SS'."""
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _segment_fields(seg) -> tuple[float, float, str] | None:
    """Normalize a Whisper segment into (start_seconds, end_seconds, text).

    Handles both shapes seen in the wild:
    - dict-style (openai-whisper): {"start": 0.0, "end": 3.2, "text": "..."}
    - list-style (lightning_whisper_mlx): [start_centiseconds, end_centiseconds, text]
    """
    if isinstance(seg, dict):
        text = (seg.get("text") or "").strip()
        if not text:
            return None
        return float(seg.get("start", 0.0)), float(seg.get("end", 0.0)), text
    if isinstance(seg, (list, tuple)) and len(seg) >= 3:
        text = str(seg[2]).strip()
        if not text:
            return None
        # lightning_whisper_mlx uses centiseconds (start * 100, end * 100).
        return float(seg[0]) / 100.0, float(seg[1]) / 100.0, text
    return None


def align_segments_to_speakers(segments: list, turns: list) -> list[dict]:
    """Tag each Whisper segment with the speaker whose turn contains its midpoint.

    Falls back to the speaker with the largest temporal overlap when no turn
    covers the midpoint exactly.
    """
    aligned = []
    for seg in segments:
        fields = _segment_fields(seg)
        if fields is None:
            continue
        start, end, text = fields
        midpoint = (start + end) / 2

        speaker = None
        for t_start, t_end, t_speaker in turns:
            if t_start <= midpoint <= t_end:
                speaker = t_speaker
                break
        if speaker is None:
            best_overlap = 0.0
            for t_start, t_end, t_speaker in turns:
                overlap = max(0.0, min(end, t_end) - max(start, t_start))
                if overlap > best_overlap:
                    best_overlap = overlap
                    speaker = t_speaker
        aligned.append({"start": start, "end": end, "text": text,
                        "speaker": speaker or "Unknown"})
    return aligned


def _parse_chapter_timestamp(ts: str) -> float:
    """'14:08' → 848, '1:23:45' → 5025. Returns 0.0 on malformed input."""
    parts = ts.split(":")
    try:
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except ValueError:
        pass
    return 0.0


def parse_chapters_from_description(description: str) -> list[tuple[float, str]]:
    """Extract chapter markers from a podcast description.

    Handles three common formats, tried in order. First matching pattern wins
    (so we don't pick up stray timestamps from sponsor blurbs after the real
    outline):

        (00:00) – Introduction       Lex, Dwarkesh, many others
        [00:00] Introduction         bracketed
        00:00  Introduction          plain, line-start

    Duplicate (time, title) entries are dropped; results sorted by time.
    """
    # Title capture stops at the next timestamp marker, an end-of-line, or
    # end-of-input — this handles both line-broken outlines (Lex) and
    # run-together single-line outlines (Dwarkesh).
    patterns = [
        # (H:MM:SS) – Title
        re.compile(
            r"\((\d{1,2}:\d{2}(?::\d{2})?)\)\s*[–—\-:]?\s*(.+?)(?=\s*\(\d{1,2}:\d{2}|\s*\[\d{1,2}:\d{2}|$|\n|\r)",
            re.MULTILINE,
        ),
        # [H:MM:SS] Title
        re.compile(
            r"\[(\d{1,2}:\d{2}(?::\d{2})?)\]\s*(.+?)(?=\s*\(\d{1,2}:\d{2}|\s*\[\d{1,2}:\d{2}|$|\n|\r)",
            re.MULTILINE,
        ),
        # H:MM:SS Title  (line-start; requires at least one space before title)
        re.compile(r"^\s*(\d{1,2}:\d{2}(?::\d{2})?)\s+([^\n\r]+)", re.MULTILINE),
    ]
    for pat in patterns:
        raw = [(m.group(1), m.group(2).strip()) for m in pat.finditer(description)]
        # Drop "titles" that are just URLs (common in sponsor lines).
        raw = [(ts, t) for ts, t in raw
               if t and not t.startswith(("http://", "https://"))]
        if not raw:
            continue
        seen = set()
        out: list[tuple[float, str]] = []
        for ts, title in raw:
            secs = _parse_chapter_timestamp(ts)
            title = title.rstrip(".,;")
            key = (round(secs), title.lower())
            if key in seen:
                continue
            seen.add(key)
            out.append((secs, title))
        return sorted(out, key=lambda c: c[0])
    return []


def detect_host_speaker_label(aligned: list[dict], window_seconds: float = 60.0) -> str | None:
    """Return the pyannote label that talks the most in the opening window.

    For interview shows the host almost always introduces the episode in the
    first minute, so whichever speaker accumulates the most aligned talk time
    in [0, window_seconds] is a strong host signal. Returns None if there are
    no segments in that window.
    """
    talk_time: dict[str, float] = {}
    for seg in aligned:
        if seg.get("start", 0) >= window_seconds:
            break
        end_in_window = min(seg.get("end", 0), window_seconds)
        dur = max(0.0, end_in_window - seg.get("start", 0))
        if dur <= 0:
            continue
        talk_time[seg["speaker"]] = talk_time.get(seg["speaker"], 0.0) + dur
    if not talk_time:
        return None
    return max(talk_time.items(), key=lambda kv: kv[1])[0]


def _host_display_name(host_full: str | None) -> str | None:
    """Trim the full host name to a single-token display label ('Lex' from 'Lex Fridman')."""
    if not host_full:
        return None
    token = host_full.strip().split()[0] if host_full.strip() else ""
    return token or None


def _render_diarized_body(aligned: list[dict], host_label: str | None = None,
                          host_name: str | None = None,
                          chapters: list[tuple[float, str]] | None = None) -> str:
    """Render aligned segments as **Speaker X** (mm:ss): blocks (no header).

    If host_label + host_name are provided, segments whose pyannote label
    matches host_label render as `**<host_name>**` instead of `**Speaker A**`.
    If chapters is provided, `## Title (mm:ss)` headings are inserted
    between paragraphs at the appropriate timestamps so Obsidian's outline
    picks them up.
    """
    if not aligned:
        return "(no segments produced)\n"

    def label_for(speaker_id: str) -> str:
        if host_label and host_name and speaker_id == host_label:
            return f"**{host_name}**"
        return f"**Speaker {humanize_speaker(speaker_id)}**"

    pending = sorted(chapters or [], key=lambda c: c[0])

    lines: list[str] = []
    current_speaker = None
    block_start = 0.0
    buffer: list[str] = []

    def emit_chapters_up_to(time_seconds: float):
        nonlocal pending
        while pending and pending[0][0] <= time_seconds:
            ts_s, title = pending.pop(0)
            lines.append(f"## {title} ({format_timestamp(ts_s)})")
            lines.append("")

    def flush():
        if buffer and current_speaker is not None:
            emit_chapters_up_to(block_start)
            ts = format_timestamp(block_start)
            lines.append(f"{label_for(current_speaker)} ({ts}):")
            joined = " ".join(s.strip() for s in buffer if s.strip())
            lines.append(collapse_repetitions(joined))
            lines.append("")

    for seg in aligned:
        if seg["speaker"] != current_speaker:
            flush()
            current_speaker = seg["speaker"]
            block_start = seg["start"]
            buffer = []
        buffer.append(seg["text"])
    flush()

    # Any chapters past the last paragraph (rare) still get emitted at the end.
    while pending:
        ts_s, title = pending.pop(0)
        lines.append(f"## {title} ({format_timestamp(ts_s)})")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_diarized_markdown(stem: str, aligned: list[dict], meta: dict | None = None,
                              host_name: str | None = None) -> str:
    """Diarized markdown with optional metadata header, host-aware labels, and chapters.

    `host_name` (typically the host's first name) triggers the host-detection
    heuristic — whichever pyannote speaker talks most in the first minute
    gets rendered with that name instead of `Speaker A`. Chapter markers in
    the description (e.g. `(14:08) – Codecs`) become `## Codecs (14:08)`
    headings inline.
    """
    host_label = detect_host_speaker_label(aligned) if host_name else None
    chapters = parse_chapters_from_description(meta.get("summary", "")) if meta else []
    body = _render_diarized_body(aligned, host_label=host_label,
                                 host_name=host_name, chapters=chapters)
    header = render_metadata_header(meta) if meta else f"# {stem}\n\n---\n\n"
    return header + body


def render_flat_markdown(stem: str, text: str, meta: dict | None = None) -> str:
    """Flat (non-diarized) markdown with optional metadata header."""
    cleaned = collapse_repetitions(text)
    header = render_metadata_header(meta) if meta else f"# {stem}\n\n---\n\n"
    return header + cleaned + "\n"


def should_diarize(args, feed_cfg: dict) -> bool:
    """CLI override wins; otherwise read TOML."""
    if getattr(args, "diarize", None) is not None:
        return bool(args.diarize)
    return bool(feed_cfg.get("diarize", False))


def should_subprocess_per_episode(args, feed_cfg: dict) -> bool:
    """CLI override wins; otherwise read TOML. Defaults to False.

    When True, each episode of the feed runs in a fresh Python subprocess
    instead of the in-process loop. Trades ~30-60s per-episode model-load
    overhead for predictable per-episode cost on long backlogs where
    MPS allocator fragmentation otherwise causes progressive slowdown.
    """
    flag = getattr(args, "subprocess_per_episode", None)
    if flag is not None:
        return bool(flag)
    return bool(feed_cfg.get("subprocess_per_episode", False))


def subprocess_concurrency_for(args, feed_cfg: dict) -> int:
    """CLI override wins; otherwise read TOML. Defaults to 1.

    Only meaningful when subprocess_per_episode is True. Caps the number of
    concurrent transcribe subprocesses for a single feed — useful when
    individual subprocesses underutilize the GPU.
    """
    flag = getattr(args, "subprocess_concurrency", None)
    if flag is not None:
        return max(1, int(flag))
    return max(1, int(feed_cfg.get("subprocess_concurrency", 1)))


class _PrefixedWriter:
    """Wrap a stream so every written line is prefixed. Used by --label to
    distinguish stdout from concurrent transcribe subprocesses in the parent's
    merged output stream.

    Buffers partial writes until a newline lands so the prefix is applied
    once per logical line, not once per write call.
    """
    def __init__(self, stream, prefix: str):
        self.stream = stream
        self.prefix = prefix
        self._buffer = ""

    def write(self, s: str) -> int:
        if not s:
            return 0
        self._buffer += s
        flushed_any = False
        while "\n" in self._buffer:
            line, _, self._buffer = self._buffer.partition("\n")
            self.stream.write(self.prefix + line + "\n")
            flushed_any = True
        # Push each logical line out immediately so concurrent workers'
        # progress is visible in the parent's merged log stream.
        if flushed_any:
            self.stream.flush()
        return len(s)

    def flush(self) -> None:
        if self._buffer:
            self.stream.write(self.prefix + self._buffer)
            self._buffer = ""
        self.stream.flush()

    def __getattr__(self, name):
        return getattr(self.stream, name)


def diarize_cache_path(mp3_path: Path, transcript_dir: Path) -> Path:
    """Sidecar location for cached pyannote turns."""
    return transcript_dir / ".diarize" / f"{mp3_path.stem}.json"


def load_cached_diarization(mp3_path: Path, transcript_dir: Path) -> list[tuple[float, float, str]] | None:
    """Return cached turns if the sidecar exists and is newer than the mp3."""
    import json
    cache = diarize_cache_path(mp3_path, transcript_dir)
    if not cache.exists():
        return None
    if cache.stat().st_mtime < mp3_path.stat().st_mtime:
        return None  # mp3 changed since we cached
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
        return [(float(t[0]), float(t[1]), str(t[2])) for t in data]
    except Exception:
        return None


def save_diarization_cache(mp3_path: Path, transcript_dir: Path,
                           turns: list[tuple[float, float, str]]) -> None:
    """Persist diarization turns as a JSON sidecar so future retries skip the work."""
    import json
    cache = diarize_cache_path(mp3_path, transcript_dir)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps([[t[0], t[1], t[2]] for t in turns]), encoding="utf-8")


# ── Whisper chunked transcription ─────────────────────────────────────────────
#
# MLX/Metal trips a GPU command-buffer timeout on multi-hour audio (the mel
# spectrogram step generates a single tensor op longer than the system limit).
# Splitting the audio into ~30-minute chunks via ffmpeg avoids the issue.
# Each chunk transcribes independently; segment timestamps are offset by the
# chunk's start before being merged.

CHUNK_SECONDS_DEFAULT = 1800  # 30 minutes


def transcribe_chunked(mp3_path: Path, whisper, chunk_seconds: int = CHUNK_SECONDS_DEFAULT,
                       checkpoint_dir: Path | None = None) -> dict:
    """Transcribe a long mp3 by ffmpeg-slicing into chunks, then merging.

    For files at or below `chunk_seconds`, this is a single pass — no slicing.
    Otherwise it streams chunks through `whisper.transcribe` and stitches the
    results, adjusting segment timestamps by each chunk's start offset.

    If `checkpoint_dir` is provided, each completed chunk's result is persisted
    as `<checkpoint_dir>/<stem>/chunk_NNN.json`. A subsequent retry skips
    chunks that already have a checkpoint, so a Whisper crash mid-file doesn't
    cost the work already done. The chunk directory is cleaned up once all
    chunks merge successfully.
    """
    duration = get_audio_duration_secs(mp3_path)
    if duration is None or duration <= chunk_seconds:
        return whisper.transcribe(audio_path=str(mp3_path))

    import json
    import subprocess
    import tempfile

    n_chunks = int(duration // chunk_seconds) + (1 if duration % chunk_seconds > 0 else 0)
    all_text: list[str] = []
    all_segments: list = []

    cp_root = (checkpoint_dir / mp3_path.stem) if checkpoint_dir else None
    if cp_root is not None:
        cp_root.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="ss-chunks-") as tmpdir:
        tmp = Path(tmpdir)
        for i in range(n_chunks):
            start = i * chunk_seconds
            cp_file = (cp_root / f"chunk_{i:03d}.json") if cp_root else None

            cached = None
            if cp_file and cp_file.exists():
                try:
                    cached = json.loads(cp_file.read_text(encoding="utf-8"))
                except Exception:
                    cached = None

            if cached is not None:
                print(f"    ↻ Chunk {i+1}/{n_chunks} @{format_timestamp(start)} (cached)")
                text = (cached.get("text") or "").strip()
                segments = cached.get("segments") or []
            else:
                chunk_path = tmp / f"chunk_{i:03d}.mp3"
                # `-ss` before `-i` seeks before reading — fast for mp3 stream copy.
                subprocess.run([
                    "ffmpeg", "-y", "-loglevel", "error",
                    "-ss", str(start), "-i", str(mp3_path),
                    "-t", str(chunk_seconds), "-c", "copy",
                    str(chunk_path),
                ], check=True)

                print(f"    ↻ Chunk {i+1}/{n_chunks} @{format_timestamp(start)}")
                result = whisper.transcribe(audio_path=str(chunk_path))
                text = (result.get("text") or "").strip()
                segments = result.get("segments") or []

                if cp_file is not None:
                    try:
                        cp_file.write_text(
                            json.dumps({"text": text, "segments": segments}),
                            encoding="utf-8",
                        )
                    except OSError as e:
                        print(f"    ⚠ Could not write checkpoint {cp_file.name}: {e}")

            all_text.append(text)

            # lightning_whisper_mlx returns [start_cs, end_cs, text] per segment.
            offset_cs = int(start * 100)
            for seg in segments:
                if isinstance(seg, (list, tuple)) and len(seg) >= 3:
                    all_segments.append([seg[0] + offset_cs, seg[1] + offset_cs, seg[2]])
                elif isinstance(seg, dict):
                    s = dict(seg)
                    s["start"] = float(seg.get("start", 0)) + start
                    s["end"] = float(seg.get("end", 0)) + start
                    all_segments.append(s)

    # All chunks merged successfully — remove the checkpoint dir.
    if cp_root is not None and cp_root.is_dir():
        for f in cp_root.iterdir():
            try:
                f.unlink()
            except OSError:
                pass
        try:
            cp_root.rmdir()
        except OSError:
            pass

    return {"text": " ".join(all_text), "segments": all_segments}


# ── Path resolution ───────────────────────────────────────────────────────────

def default_dir(kind: str, tag: str | None) -> Path:
    """Default output directory for a given kind ('downloads' or 'transcripts')."""
    suffix = f"/{tag}" if tag else ""
    return Path(f"./{kind}{suffix}")


# ── Processed index ───────────────────────────────────────────────────────────
#
# `./transcripts/<tag>/.processed` is the canonical "have we transcribed this?"
# record. One episode stem per line. This decouples the dedup signal from the
# transcript files themselves — so .md files can be pruned locally without
# triggering re-downloads, while a failed transcription (which never appends
# to the index) stays visible and re-runnable.

def processed_index_path(tag: str) -> Path:
    return Path(f"./transcripts/{tag}/.processed")


def load_processed(tag: str) -> set[str]:
    """Return the set of episode stems already transcribed for this feed.

    If `.processed` doesn't exist yet, bootstrap it from any existing *.md
    files in the feed's transcript directory.
    """
    index = processed_index_path(tag)
    if index.exists():
        return {
            line.rstrip("\n")
            for line in index.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }

    transcript_dir = Path(f"./transcripts/{tag}")
    stems = sorted({p.stem for p in transcript_dir.glob("*.md")} if transcript_dir.is_dir() else set())
    if stems:
        transcript_dir.mkdir(parents=True, exist_ok=True)
        index.write_text("\n".join(stems) + "\n", encoding="utf-8")
        print(f"  [{tag}] Bootstrapped {index} from {len(stems)} existing transcript(s).")
    return set(stems)


def record_processed(tag: str, stem: str) -> None:
    """Append a stem to the feed's .processed index (called after a write)."""
    index = processed_index_path(tag)
    index.parent.mkdir(parents=True, exist_ok=True)
    with open(index, "a", encoding="utf-8") as f:
        f.write(f"{stem}\n")


# ── Backup paths ──────────────────────────────────────────────────────────────

def _ensure_backup_dir(target: Path, root: Path, tag: str, label: str) -> Path | None:
    """Make sure a backup directory is reachable. Returns the dir or None on failure.

    Reachability is checked against the user-configured `root` (backup_path or
    explicit media_dir/transcript_dir), not against `target`'s parent — so a
    brand-new feed can have its tag subdir created on first use without tripping
    the unmounted-volume guard. The guard still fires when the configured root
    (or its parent — e.g. /Volumes/SD when the card is missing) doesn't exist.
    """
    if not root.exists() and not root.parent.is_dir():
        print(f"  ⚠ [{tag}] {label} root {root} unavailable (parent missing) — skipping.")
        return None
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"  ⚠ [{tag}] cannot create {label} dir {target}: {e} — skipping.")
        return None
    return target


def resolve_media_dir(tag: str, feed_cfg: dict) -> Path | None:
    """Where evicted mp3s back up to. Returns None if no backup is configured."""
    explicit = feed_cfg.get("media_dir")
    if explicit:
        target = Path(explicit)
        return _ensure_backup_dir(target, target, tag, "media_dir")
    backup_path = feed_cfg.get("backup_path")
    if not backup_path:
        return None
    root = Path(backup_path)
    return _ensure_backup_dir(root / tag / "media", root, tag, "media")


def resolve_transcript_dir(tag: str, feed_cfg: dict) -> Path | None:
    """Where transcript backups land. Returns None if no backup is configured."""
    explicit = feed_cfg.get("transcript_dir")
    if explicit:
        target = Path(explicit)
        return _ensure_backup_dir(target, target, tag, "transcript_dir")
    backup_path = feed_cfg.get("backup_path")
    if not backup_path:
        return None
    root = Path(backup_path)
    return _ensure_backup_dir(root / tag / "text", root, tag, "text")


# ── Transcript backup ─────────────────────────────────────────────────────────

def backup_feed_transcripts(tag: str, feed_cfg: dict) -> None:
    """Sync ./transcripts/<tag>/*.md to the feed's text dir if configured.

    Idempotent: skips files already present at the target. Never deletes from
    the local source — transcripts stay locally for grep/wiki work.
    """
    src = Path(f"./transcripts/{tag}")
    if not src.is_dir():
        return
    md_files = sorted(src.glob("*.md"))
    if not md_files:
        return

    text_dir = resolve_transcript_dir(tag, feed_cfg)
    if text_dir is None:
        return

    copied = 0
    refreshed = 0
    for md in md_files:
        dest = text_dir / md.name
        if dest.exists():
            # Skip only if the backup is at least as fresh as the local file.
            # shutil.copy2 preserves mtime, so post-copy the two match exactly.
            if dest.stat().st_mtime >= md.stat().st_mtime:
                continue
            try:
                shutil.copy2(md, dest)
                refreshed += 1
            except OSError as e:
                print(f"  ✗ [{tag}] transcript refresh failed for {md.name}: {e}")
            continue
        try:
            shutil.copy2(md, dest)
            copied += 1
        except OSError as e:
            print(f"  ✗ [{tag}] transcript backup failed for {md.name}: {e}")

    if copied or refreshed:
        parts = []
        if copied:
            parts.append(f"{copied} new")
        if refreshed:
            parts.append(f"{refreshed} updated")
        print(f"  [{tag}] Backed up {' + '.join(parts)} transcript(s) → {text_dir}")


# ── Eviction ──────────────────────────────────────────────────────────────────

def prune_feed_mp3s(tag: str, feed_cfg: dict) -> None:
    """Cap mp3 count in ./downloads/<tag>/ to feed's max_episodes_on_disk.

    Eviction rules:
    - Keeps the newest N mp3s (by filename's YYYY-MM-DD prefix).
    - Only evicts mp3s that already have a matching transcript — never deletes
      source audio that hasn't been preserved.
    - If a media_dir is configured (explicit or derived from backup_path),
      copy each mp3 there before deleting locally. If the path is unreachable
      (e.g. SD card not mounted), eviction is skipped entirely.
    """
    cap = feed_cfg.get("max_episodes_on_disk")
    if not cap:
        return

    mp3_dir = Path(f"./downloads/{tag}")
    transcript_dir = Path(f"./transcripts/{tag}")
    if not mp3_dir.is_dir():
        return

    mp3s_old_to_new = sorted(mp3_dir.glob("*.mp3"))
    if len(mp3s_old_to_new) <= cap:
        return

    transcripts = {p.stem for p in transcript_dir.glob("*.md")} if transcript_dir.is_dir() else set()

    # Keep the newest N — evict from what's left.
    to_keep = set(mp3s_old_to_new[-cap:])
    candidates = [p for p in mp3s_old_to_new if p not in to_keep]
    evictable = [p for p in candidates if p.stem in transcripts]
    blocked = [p for p in candidates if p.stem not in transcripts]

    if blocked:
        print(f"  ⚠ [{tag}] {len(blocked)} old mp3(s) have no transcript — keeping for safety:")
        for p in blocked[:3]:
            print(f"      {p.name}")
        if len(blocked) > 3:
            print(f"      … and {len(blocked) - 3} more")

    if not evictable:
        return

    # Resolve media_dir only if backup config exists. If user opted out
    # (no media_dir, no backup_path), eviction still happens — locally only.
    backup_dir: Path | None = None
    if feed_cfg.get("media_dir") or feed_cfg.get("backup_path"):
        backup_dir = resolve_media_dir(tag, feed_cfg)
        if backup_dir is None:
            return  # path unavailable — _ensure_backup_dir already warned.

    print(f"  [{tag}] Pruning {len(evictable)} mp3(s) (cap {cap}, current {len(mp3s_old_to_new)}):")
    for mp3 in evictable:
        if backup_dir is not None:
            target = backup_dir / mp3.name
            if not target.exists():
                try:
                    shutil.copy2(mp3, target)
                except OSError as e:
                    print(f"    ✗ Backup failed for {mp3.name}: {e} — keeping local copy.")
                    continue
                print(f"    ↪ Backed up: {target}")
            else:
                print(f"    ↪ Already in backup: {target.name}")
        try:
            mp3.unlink()
            print(f"    🗑 Evicted: {mp3.name}")
        except OSError as e:
            print(f"    ✗ Could not delete {mp3.name}: {e}")
            continue
        # Drop the matching metadata sidecar — its contents are already in
        # the rendered transcript and have no other consumer.
        sidecar = metadata_sidecar_path(mp3)
        if sidecar.exists():
            try:
                sidecar.unlink()
            except OSError:
                pass


# ── Commands ──────────────────────────────────────────────────────────────────

def run_index(feed, limit: int | None):
    """Print a tabular index of all episodes."""
    entries = sorted(feed.entries, key=lambda e: parsedate_to_datetime(e.published), reverse=True)
    if limit:
        entries = entries[:limit]

    print(f"{'Date':<12} {'Duration':>9}  {'Type':<6}  Title")
    print(f"{'─'*12} {'─'*9}  {'─'*6}  {'─'*60}")

    for entry in entries:
        date = pub_date_to_iso(entry)
        dur = format_duration(entry.get("itunes_duration"))
        ctype = content_type(entry)
        title = entry.title[:80]
        print(f"{date:<12} {dur:>9}  {ctype:<6}  {title}")

    print(f"\n{len(entries)} episode(s) listed.")


def run_download(feed, args):
    """Download mp3 files from the feed."""
    out_dir = Path(args.out) if args.out else default_dir("downloads", args.feed)
    out_dir.mkdir(parents=True, exist_ok=True)

    existing_mp3s = {p.stem for p in out_dir.glob("*.mp3")}

    if args.feed:
        existing_transcripts = load_processed(args.feed)
    else:
        # No --feed → fall back to glob-based detection.
        transcript_dir = Path(args.transcript_dir) if args.transcript_dir else default_dir("transcripts", args.feed)
        existing_transcripts = {p.stem for p in transcript_dir.glob("*.md")} if transcript_dir.is_dir() else set()
    skip = existing_mp3s | existing_transcripts

    print(f"Output dir: {out_dir} ({len(existing_mp3s)} existing mp3(s), {len(existing_transcripts)} already transcribed)\n")

    limit = effective_limit(args)
    entries = sort_entries_by_order(feed.entries, args._feed_cfg)
    fetched = 0

    for entry in entries:
        if limit and fetched >= limit:
            print(f"\nLimit of {limit} reached. Done.")
            break

        title = entry.title
        pub_date = pub_date_to_iso(entry)
        safe_title = sanitize_filename(title)
        stem = f"{pub_date} - {safe_title}"

        audio_url = get_audio_url(entry)
        if not audio_url:
            continue

        if stem in skip:
            print(f"  ↷ Skip (exists): {stem}")
            continue

        print(f"  → {stem}")

        out_path = out_dir / f"{stem}.mp3"
        if download_file(audio_url, out_path):
            try:
                write_metadata_sidecar(out_path, extract_episode_metadata(entry, feed))
            except Exception as e:
                print(f"    ⚠ Could not write metadata sidecar: {e}")
            fetched += 1
            skip.add(stem)

        time.sleep(0.5)

    print(f"\nDone. {fetched} new mp3(s) downloaded.")

    if args.feed:
        backup_feed_transcripts(args.feed, args._feed_cfg)
        prune_feed_mp3s(args.feed, args._feed_cfg)


def run_fetch(feed, args):
    """Download Substack-hosted transcripts as Markdown files."""
    out_dir = Path(args.out) if args.out else default_dir("transcripts", args.feed)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.feed:
        existing = load_processed(args.feed)
    else:
        existing = {p.stem for p in out_dir.glob("*.md")}
    print(f"Output dir: {out_dir} ({len(existing)} existing transcript(s))\n")

    limit = effective_limit(args)
    entries = sort_entries_by_order(feed.entries, args._feed_cfg)
    fetched = 0

    for entry in entries:
        if limit and fetched >= limit:
            print(f"\nLimit of {limit} reached. Done.")
            break

        title = entry.title
        pub_date = pub_date_to_iso(entry)
        slug = entry.link.split("/p/")[1].rstrip("/")
        safe_title = sanitize_filename(title)
        stem = f"{pub_date} - {safe_title}"

        if stem in existing:
            print(f"  ↷ Skip (exists): {stem}.md")
            continue

        print(f"  → {stem}")

        base_url = base_url_from_entry(entry)
        cdn_url = get_transcript_url(base_url, slug, args.sid)
        if not cdn_url:
            print(f"    ✗ No transcript available — skipping.")
            continue

        segments = fetch_transcript_segments(cdn_url)
        if not segments:
            continue

        text = transcript_to_text(segments)
        if not text.strip():
            print(f"    ✗ Transcript was empty — skipping.")
            continue

        out_path = out_dir / f"{stem}.md"
        write_markdown(out_path, title, pub_date, f"{base_url}/p/{slug}", text)
        print(f"    ✓ Saved: {out_path.name} ({len(text.split())} words)")

        if args.feed:
            record_processed(args.feed, stem)

        fetched += 1
        existing.add(stem)

        time.sleep(0.5)

    print(f"\nDone. {fetched} new transcript(s) fetched.")

    if args.feed:
        backup_feed_transcripts(args.feed, args._feed_cfg)


def inject_chapters_into_body(body: str, chapters: list[tuple[float, str]]) -> str:
    """Inject ## Title (mm:ss) headings before paragraphs at chapter boundaries.

    Operates on a rendered transcript body. Locates speaker-paragraph headers
    (`**Whoever** (mm:ss):`) and inserts a chapter heading just before each
    paragraph whose start time is past one of the pending chapter timestamps.

    Removes any pre-existing `## ... (mm:ss)` headings in the body so the
    function is idempotent — re-running over an already-chaptered body
    produces the same output.
    """
    if not chapters:
        return body

    # Idempotency: strip any pre-existing chapter headings before re-injecting.
    body = re.sub(r"^## .+ \(\d{1,2}:\d{2}(?::\d{2})?\)\n+", "", body, flags=re.MULTILINE)

    pending = sorted(chapters, key=lambda c: c[0])
    speaker_line_re = re.compile(r"^\*\*[^*]+\*\* \((\d{1,2}:\d{2}(?::\d{2})?)\):", re.MULTILINE)
    matches = list(speaker_line_re.finditer(body))
    if not matches:
        return body

    parts: list[str] = []
    last_end = 0
    for m in matches:
        para_start_secs = _parse_chapter_timestamp(m.group(1))
        parts.append(body[last_end:m.start()])
        while pending and pending[0][0] <= para_start_secs:
            ts, title = pending.pop(0)
            parts.append(f"## {title} ({format_timestamp(ts)})\n\n")
        last_end = m.start()
    parts.append(body[last_end:])
    while pending:
        ts, title = pending.pop(0)
        parts.append(f"\n## {title} ({format_timestamp(ts)})\n")
    return "".join(parts)


def _media_dir_path(tag: str, feed_cfg: dict) -> Path | None:
    """Read-only path resolution for mp3 backups — does not mkdir."""
    if feed_cfg.get("media_dir"):
        return Path(feed_cfg["media_dir"])
    if feed_cfg.get("backup_path"):
        return Path(feed_cfg["backup_path"]) / tag / "media"
    return None


def _text_dir_path(tag: str, feed_cfg: dict) -> Path | None:
    """Read-only path resolution for transcript backups — does not mkdir."""
    if feed_cfg.get("transcript_dir"):
        return Path(feed_cfg["transcript_dir"])
    if feed_cfg.get("backup_path"):
        return Path(feed_cfg["backup_path"]) / tag / "text"
    return None


def _count_files(path: Path | None, glob: str) -> int | None:
    """Count files matching glob, excluding AppleDouble (._*) sidecars.

    Returns None if the path doesn't exist (used to render '—' in tables).
    """
    if path is None or not path.is_dir():
        return None
    return sum(1 for p in path.glob(glob) if not p.name.startswith("._"))


def run_status(args):
    """Print a per-feed health snapshot — RSS, local, SD card, gaps."""
    config = getattr(args, "_config", {}) or {}
    feeds = config.get("feeds", {})
    if args.feed:
        feeds = {args.feed: feeds[args.feed]} if args.feed in feeds else {}
    if not feeds:
        sys.exit("No feeds in config.")

    rows = []
    for tag in feeds:
        cfg = feed_cfg_for(config, tag)

        rss_count: str = "—"
        if not args.offline and cfg.get("rss"):
            try:
                feed = feedparser.parse(cfg["rss"])
                rss_count = str(len(feed.entries))
            except Exception:
                rss_count = "ERR"

        mp3_dir = Path(f"./downloads/{tag}")
        local_mp3 = sum(1 for _ in mp3_dir.glob("*.mp3")) if mp3_dir.is_dir() else 0

        processed = load_processed(tag) if Path(f"./transcripts/{tag}").is_dir() else set()
        done_count = len(processed)

        # Orphan mp3s: present locally but no .processed entry → needs transcribing.
        orphan = 0
        if mp3_dir.is_dir():
            orphan = sum(1 for p in mp3_dir.glob("*.mp3") if p.stem not in processed)

        # Headerless: .md without YAML frontmatter (i.e. metadata never landed).
        transcript_dir = Path(f"./transcripts/{tag}")
        headerless = 0
        if transcript_dir.is_dir():
            for md in transcript_dir.glob("*.md"):
                try:
                    with md.open(encoding="utf-8") as f:
                        if not f.readline().startswith("---"):
                            headerless += 1
                except OSError:
                    pass

        sd_media = _count_files(_media_dir_path(tag, cfg), "*.mp3")
        sd_text = _count_files(_text_dir_path(tag, cfg), "*.md")

        rows.append({
            "tag": tag,
            "rss": rss_count,
            "local": local_mp3,
            "done": done_count,
            "sd_media": sd_media if sd_media is not None else "—",
            "sd_text": sd_text if sd_text is not None else "—",
            "orphan": orphan,
            "no_header": headerless,
        })

    # Layout
    cols = [
        ("Feed",      "tag",       18, "<"),
        ("RSS",       "rss",        5, ">"),
        ("Local",     "local",      6, ">"),
        ("Done",      "done",       5, ">"),
        ("SD-media",  "sd_media",   9, ">"),
        ("SD-text",   "sd_text",    8, ">"),
        ("Orphan",    "orphan",     7, ">"),
        ("No-hdr",    "no_header",  7, ">"),
    ]
    print("  ".join(f"{h:{align}{w}}" for h, _, w, align in cols))
    print("  ".join("-" * w for _, _, w, _ in cols))
    for r in rows:
        print("  ".join(f"{str(r[key]):{align}{w}}" for _, key, w, align in cols))

    print()
    print("Legend:")
    print("  Done    = episode stems in .processed (canonical 'transcribed')")
    print("  Orphan  = local mp3 without a matching .processed entry (still needs transcribing)")
    print("  No-hdr  = local transcripts without YAML frontmatter (run --backfill-headers)")


def run_daily(args):
    """Download → transcribe for every feed (unless opted out via daily = false).

    Two phases:
      1. Downloads — each feed's mp3s pulled in turn; per-feed errors don't
         block other feeds.
      2. Transcribes — each feed processed individually so non-daily feeds
         don't accidentally pick up pending mp3s sitting in their dirs.

    Per-feed `daily = false` in feeds.toml opts a feed out (e.g. an archived
    show you don't actively follow but want kept in the config for backfill).
    Passing --feed <tag> alongside --daily limits the routine to that one
    feed, ignoring the daily attribute.
    """
    config = getattr(args, "_config", {}) or {}
    feeds = config.get("feeds", {})

    if args.feed:
        selected = [args.feed] if args.feed in feeds else []
    else:
        selected = [t for t in feeds if feed_cfg_for(config, t).get("daily", True)]
        skipped = [t for t in feeds if not feed_cfg_for(config, t).get("daily", True)]
        if skipped:
            print(f"Skipping (daily = false): {', '.join(skipped)}")

    if not selected:
        print("No feeds eligible for the daily routine.")
        return

    print(f"Daily routine: {', '.join(selected)}")

    # Phase 1 — downloads. Per-feed errors do not abort the loop.
    print("\n=== Phase 1: download ===")
    for tag in selected:
        cfg = feed_cfg_for(config, tag)
        rss = cfg.get("rss")
        if not rss:
            print(f"\n[{tag}] no rss configured — skipping download")
            continue
        print(f"\n--- [{tag}] ---")
        args.feed = tag
        args._feed_cfg = cfg
        args.rss = rss
        args.sid = args.sid or cfg.get("sid")
        try:
            feed = feedparser.parse(rss)
            if not feed.entries:
                print("  ↷ no entries in RSS")
                continue
            run_download(feed, args)
        except Exception as e:
            print(f"  ✗ download error: {e}")

    # Phase 2 — transcribes. One feed at a time so each gets its own
    # checkpoint dir + per-feed diarization config + host name.
    print("\n=== Phase 2: transcribe ===")
    for tag in selected:
        cfg = feed_cfg_for(config, tag)
        print(f"\n--- [{tag}] ---")
        args.feed = tag
        args._feed_cfg = cfg
        try:
            run_transcribe(args)
        except Exception as e:
            print(f"  ✗ transcribe error: {e}")

    print("\nDaily routine complete.")


def run_check(args):
    """List new episodes per feed without downloading anything.

    Cheap and cron-friendly: only the RSS XML is fetched (a few KB).
    Byte sizes are pulled from RSS enclosure `length` attributes when
    feeds populate them (most do).
    """
    config = getattr(args, "_config", {}) or {}
    feeds = config.get("feeds", {})
    if args.feed:
        feeds = {args.feed: feeds[args.feed]} if args.feed in feeds else {}
    if not feeds:
        sys.exit("No feeds in config.")

    grand_new = 0
    grand_bytes = 0

    for tag in feeds:
        cfg = feed_cfg_for(config, tag)
        rss = cfg.get("rss")
        if not rss:
            print(f"\n[{tag}] no rss configured")
            continue

        try:
            feed = feedparser.parse(rss)
        except Exception as e:
            print(f"\n[{tag}] RSS error: {e}")
            continue

        processed = load_processed(tag) if Path(f"./transcripts/{tag}").is_dir() else set()

        new_episodes: list[tuple[str, str, int]] = []
        for entry in feed.entries:
            try:
                pub_date = pub_date_to_iso(entry)
                stem = f"{pub_date} - {sanitize_filename(entry.title)}"
            except Exception:
                continue
            if stem in processed:
                continue

            size_bytes = 0
            for link in entry.get("links", []):
                if link.get("type", "").startswith("audio"):
                    try:
                        size_bytes = int(link.get("length") or 0)
                    except (ValueError, TypeError):
                        size_bytes = 0
                    break
            new_episodes.append((pub_date, entry.title, size_bytes))

        new_episodes.sort(key=lambda e: e[0], reverse=True)

        if not new_episodes:
            print(f"\n[{tag}] up to date")
            continue

        total_size = sum(s for _, _, s in new_episodes)
        size_str = f" (~{total_size / (1024*1024):.0f} MB total)" if total_size else ""
        print(f"\n[{tag}] {len(new_episodes)} new{size_str}")
        for pub_date, title, size_bytes in new_episodes[:10]:
            size_mark = f"  ({size_bytes / (1024*1024):.0f} MB)" if size_bytes else ""
            print(f"    {pub_date}  {title[:75]}{size_mark}")
        if len(new_episodes) > 10:
            print(f"    … and {len(new_episodes) - 10} more")

        grand_new += len(new_episodes)
        grand_bytes += total_size

    print()
    if grand_new == 0:
        print("All feeds up to date.")
    else:
        gb_str = f" (~{grand_bytes / (1024*1024):.0f} MB)" if grand_bytes else ""
        print(f"Total: {grand_new} new episode(s){gb_str}")


def _split_existing_body(content: str) -> str:
    """Strip the existing markdown header from a legacy transcript, return body.

    Old format(s) end with `\\n---\\n\\n` separating header from body. We split
    on the FIRST such separator and keep everything after it. Files that
    already have YAML frontmatter (start with `---\\n`) are detected upstream
    and not passed here.
    """
    sep = "\n---\n\n"
    idx = content.find(sep)
    if idx >= 0:
        return content[idx + len(sep):]
    # Files that don't have a separator at all → treat the entire content as body.
    return content


def run_backfill_headers(args):
    """For each feed in config, fetch RSS and splice metadata into existing transcripts.

    Skips files that already start with YAML frontmatter (`---\\n`). Reports
    counts per feed of: updated, already-headered, no-RSS-match, missing-feed.
    """
    config = getattr(args, "_config", {}) or {}
    feeds = config.get("feeds", {})
    if args.feed:
        if args.feed not in feeds:
            sys.exit(f"Feed '{args.feed}' not in {args.config}.")
        feeds = {args.feed: feeds[args.feed]}

    if not feeds:
        sys.exit("No feeds in config.")

    grand_updated = grand_skipped = grand_unmatched = 0
    for tag, _ in feeds.items():
        cfg = feed_cfg_for(config, tag)
        rss = cfg.get("rss")
        if not rss:
            print(f"\n[{tag}] no rss in config — skipping.")
            continue

        transcript_dir = Path(f"./transcripts/{tag}")
        if not transcript_dir.is_dir():
            print(f"\n[{tag}] no transcript directory — skipping.")
            continue

        print(f"\n[{tag}] Fetching RSS...")
        feed = feedparser.parse(rss)
        if not feed.entries:
            print(f"  ✗ No entries from RSS.")
            continue

        # Build stem → metadata map (mirroring how run_download names files).
        meta_by_stem: dict[str, dict] = {}
        for entry in feed.entries:
            try:
                pub_date = pub_date_to_iso(entry)
                stem = f"{pub_date} - {sanitize_filename(entry.title)}"
                meta_by_stem[stem] = extract_episode_metadata(entry, feed)
            except Exception:
                continue

        md_files = sorted(transcript_dir.glob("*.md"))
        updated = already = unmatched = chaptered = 0
        for md_path in md_files:
            existing = md_path.read_text(encoding="utf-8")
            has_frontmatter = existing.startswith("---\n")
            meta = meta_by_stem.get(md_path.stem)
            if not meta:
                if not has_frontmatter:
                    unmatched += 1
                else:
                    already += 1
                continue

            # Body extraction depends on whether there's already a header.
            if has_frontmatter:
                # Strip existing frontmatter so we can re-inject chapters into
                # the body without duplicating headers.
                _, _, after_close = existing.partition("---\n")
                _, _, body = after_close.partition("\n---\n")
                body = body.lstrip("\n")
            else:
                body = _split_existing_body(existing)

            chapters = parse_chapters_from_description(meta.get("summary", ""))
            had_chapters_before = bool(re.search(r"^## .+ \(\d{1,2}:\d{2}", body, re.MULTILINE))
            new_body = inject_chapters_into_body(body, chapters) if chapters else body

            new_content = render_metadata_header(meta) + new_body
            if new_content == existing:
                already += 1
                continue
            md_path.write_text(new_content, encoding="utf-8")
            updated += 1
            if chapters and not had_chapters_before:
                chaptered += 1

        print(f"  [{tag}] {len(md_files)} transcripts: "
              f"{updated} updated ({chaptered} newly chaptered), "
              f"{already} already current, "
              f"{unmatched} no RSS match (likely rotated out)")
        grand_updated += updated
        grand_skipped += already
        grand_unmatched += unmatched

        # Refresh SD card backups so the headers propagate.
        backup_feed_transcripts(tag, cfg)

    print(f"\nDone. {grand_updated} updated, {grand_skipped} already had headers, "
          f"{grand_unmatched} not in current RSS.")


def transcribe_pairs(args) -> list[tuple[Path, Path]]:
    """Determine (mp3_dir, transcript_dir) pairs to process based on args."""
    if args.feed:
        return [(default_dir("downloads", args.feed), default_dir("transcripts", args.feed))]

    if args.mp3_dir:
        out = Path(args.out) if args.out else Path("./transcripts")
        return [(Path(args.mp3_dir), out)]

    # No --feed, no explicit --mp3-dir → walk every subfolder of ./downloads
    downloads_root = Path("./downloads")
    if not downloads_root.is_dir():
        sys.exit(f"No mp3 directory found at {downloads_root}.")
    subdirs = sorted(d for d in downloads_root.iterdir() if d.is_dir())
    if subdirs:
        return [(d, Path("./transcripts") / d.name) for d in subdirs]

    # Legacy flat layout — no per-feed subfolders
    return [(downloads_root, Path("./transcripts"))]


def _run_feed_via_subprocess(args, mp3_dir: Path, out_dir: Path,
                             to_process: list[Path],
                             limit_remaining: int | None = None,
                             concurrency: int = 1) -> int:
    """Process one feed by spawning fresh subprocesses, one per episode.

    Each subprocess runs `ss.py --feed <tag> --transcribe --only <stem>
    --no-subprocess-per-episode`, which loads models, processes one
    specific episode, and exits. Process teardown reliably reclaims MPS
    allocator state that the in-process reload-every-N strategy can only
    partially reset.

    The parent dispatches `--only <stem>` per worker, so multiple workers
    never race over which pending episode to claim — assignment happens
    here, in serial order, before any work is spawned.

    `limit_remaining` caps how many episodes this helper will dispatch
    (used to honor the parent's --limit). None means no cap.

    `concurrency` is the maximum number of subprocesses to run in parallel.
    Each worker has its own pyannote + Whisper instance (~3 GB unified
    memory). Workers share the Metal command queue, so per-episode pace
    degrades modestly while total throughput rises.

    Returns the number of episodes successfully processed.
    """
    import subprocess
    from concurrent.futures import ThreadPoolExecutor, as_completed
    tag = mp3_dir.name
    diarize_only = bool(getattr(args, "diarize_only", False))

    # Different "done" signal per mode — see run_transcribe planning for the same split.
    def is_done(stem: str) -> bool:
        if diarize_only:
            return (out_dir / ".diarize" / f"{stem}.json").exists()
        return stem in load_processed(tag)

    # Snapshot the actually-pending set now and respect limit_remaining upfront.
    # Concurrency requires explicit per-worker --only assignment; doing it here
    # ahead of dispatch keeps the loop race-free.
    pending = [f for f in to_process if not is_done(f.stem)]
    if limit_remaining is not None:
        pending = pending[:limit_remaining]
    total = len(pending)
    if total == 0:
        return 0

    concurrency = max(1, min(concurrency, total))
    mode_note = f"concurrency={concurrency}" if concurrency > 1 else "serial"
    only_note = ", diarize-only" if diarize_only else ""
    print(f"\n[{mp3_dir}] {total} mp3(s) → {out_dir}  (subprocess-per-episode, {mode_note}{only_note})")

    def child_cmd(mp3: Path, worker_id: int) -> list[str]:
        # -u keeps the child's stdout unbuffered so its progress lines flow
        # through to the parent's log immediately (without -u Python
        # block-buffers stdout when it's a pipe, hiding worker progress).
        cmd = [
            sys.executable, "-u", str(Path(__file__).resolve()),
            "--config", args.config,
            "--feed", tag,
            "--transcribe",
            "--no-subprocess-per-episode",
            "--only", mp3.stem,
        ]
        if diarize_only:
            cmd.append("--diarize-only")
        if concurrency > 1:
            cmd.extend(["--label", f"[w{worker_id}]"])
        if args.diarize is not None:
            cmd.append("--diarize" if args.diarize else "--no-diarize")
        if args.model is not None:
            cmd.extend(["--model", args.model])
        return cmd

    processed = 0
    if concurrency == 1:
        # Serial path — preserves the original log shape, simpler to reason about.
        for idx, mp3 in enumerate(pending, 1):
            print(f"\n  → subprocess [{idx}/{total}]: {mp3.name}")
            result = subprocess.run(child_cmd(mp3, 1))
            if result.returncode != 0:
                print(f"  ✗ Subprocess exited {result.returncode}.")
            if not is_done(mp3.stem):
                print(f"  ✗ {mp3.name} still pending after subprocess. Aborting feed.")
                break
            processed += 1
        return processed

    # Parallel path — submit all pending up front; the executor caps in-flight
    # to `concurrency`. Worker labels cycle through [w1..wN] by submission order.
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        future_to_meta: dict = {}
        for idx, mp3 in enumerate(pending, 1):
            worker_id = ((idx - 1) % concurrency) + 1
            print(f"  → queue [{idx}/{total}]: {mp3.name}  (w{worker_id})")
            future = ex.submit(subprocess.run, child_cmd(mp3, worker_id))
            future_to_meta[future] = (mp3, idx)

        for future in as_completed(future_to_meta):
            mp3, idx = future_to_meta[future]
            try:
                result = future.result()
                if result.returncode != 0:
                    print(f"  ✗ [{idx}/{total}] {mp3.name} exited {result.returncode}.")
                    continue
            except Exception as e:
                print(f"  ✗ [{idx}/{total}] {mp3.name} crashed: {e}")
                continue
            if not is_done(mp3.stem):
                print(f"  ✗ [{idx}/{total}] {mp3.name} still pending after subprocess.")
                continue
            processed += 1
            print(f"  ✓ [{idx}/{total}] {mp3.name}")
    return processed


def run_transcribe(args):
    """Transcribe mp3 files using Lightning Whisper MLX (Apple Silicon GPU)."""
    pairs = transcribe_pairs(args)
    config = getattr(args, "_config", {}) or {}
    diarize_only = bool(getattr(args, "diarize_only", False))

    # Pre-scan to figure out how many files we'd process — lets us skip model load if zero.
    plan: list[tuple[Path, Path, list[Path]]] = []
    pair_diarize: dict[str, bool] = {}
    pair_batch: dict[str, int] = {}
    pair_model: dict[str, str] = {}
    pair_subprocess: dict[str, bool] = {}
    for mp3_dir, out_dir in pairs:
        if not mp3_dir.is_dir():
            print(f"  ↷ Skip {mp3_dir}: not a directory.")
            continue
        mp3s = sorted(mp3_dir.glob("*.mp3"))
        tag = mp3_dir.name
        feed_cfg = feed_cfg_for(config, tag)
        # Different "done" signal per mode: --diarize-only keys on the diarize
        # cache file's existence; --transcribe keys on .processed entries.
        if diarize_only:
            existing = {p.stem for p in (out_dir / ".diarize").glob("*.json")} \
                if (out_dir / ".diarize").is_dir() else set()
        elif feed_cfg:
            existing = load_processed(tag)
        else:
            existing = {p.stem for p in out_dir.glob("*.md")} if out_dir.is_dir() else set()
        to_process = [f for f in mp3s if f.stem not in existing]
        # --only <stem> narrows this run to a single episode by stem name.
        # Used by --subprocess-per-episode to dispatch specific episodes
        # from a parent's parallel queue without races.
        if getattr(args, "only", None):
            to_process = [f for f in to_process if f.stem == args.only]
        plan.append((mp3_dir, out_dir, to_process))
        pair_diarize[tag] = should_diarize(args, feed_cfg)
        pair_batch[tag] = int(feed_cfg.get("whisper_batch_size", 12))
        # CLI --model wins; otherwise per-feed `model`; otherwise default.
        pair_model[tag] = args.model if args.model is not None else feed_cfg.get("model", "medium")
        pair_subprocess[tag] = should_subprocess_per_episode(args, feed_cfg)
        diar_marker = "  ✦ diarize" if pair_diarize[tag] else ""
        batch_marker = f"  batch={pair_batch[tag]}" if pair_batch[tag] != 12 else ""
        model_marker = f"  model={pair_model[tag]}" if pair_model[tag] != "medium" else ""
        subproc_marker = "  ⊞ subprocess" if pair_subprocess[tag] else ""
        only_marker = "  🜨 diarize-only" if diarize_only else ""
        done_label = "diarized" if diarize_only else "transcribed"
        print(f"  {mp3_dir}: {len(mp3s)} mp3(s), {len(existing)} {done_label}, {len(to_process)} pending → {out_dir}{diar_marker}{batch_marker}{model_marker}{subproc_marker}{only_marker}")

    def post_process(mp3_dir: Path):
        """Run transcript backup + mp3 pruning for a finished feed dir."""
        tag = mp3_dir.name
        cfg = feed_cfg_for(config, tag)
        if not cfg:
            return
        backup_feed_transcripts(tag, cfg)
        prune_feed_mp3s(tag, cfg)

    total_pending = sum(len(p[2]) for p in plan)
    if total_pending == 0:
        print("\nAll mp3s already have transcripts.")
        for mp3_dir, _, _ in plan:
            post_process(mp3_dir)
        return

    print(f"\nDevice: Apple Silicon GPU (MLX)")
    whisper = None
    last_batch: int | None = None
    last_model: str | None = None

    # Only load diarize pipeline upfront for feeds that run in-process — subprocess-mode
    # feeds load their own pipeline inside each child process.
    diarization_pipeline = None
    in_process_diarize_needed = any(
        pair_diarize.get(d.name, False) and not pair_subprocess.get(d.name, False)
        for d, _, _ in plan
    )
    if in_process_diarize_needed:
        print("Loading diarization pipeline (pyannote)...")
        diarization_pipeline = load_diarization_pipeline()

    total_done = 0

    for mp3_dir, out_dir, to_process in plan:
        if not to_process:
            post_process(mp3_dir)
            continue
        out_dir.mkdir(parents=True, exist_ok=True)

        if pair_subprocess.get(mp3_dir.name, False):
            limit_remaining = (args.limit - total_done) if args.limit else None
            tag = mp3_dir.name
            concurrency = subprocess_concurrency_for(args, feed_cfg_for(config, tag))
            done = _run_feed_via_subprocess(args, mp3_dir, out_dir, to_process,
                                            limit_remaining=limit_remaining,
                                            concurrency=concurrency)
            total_done += done
            post_process(mp3_dir)
            if args.limit and total_done >= args.limit:
                done_verb = "diarized" if diarize_only else "transcribed"
                print(f"\nLimit of {args.limit} reached.")
                print(f"\nDone. {total_done} file(s) {done_verb}.")
                return
            continue

        # Lazy-load Whisper for in-process feeds only — skipped in --diarize-only
        # mode since we never call into Whisper.
        desired_batch = pair_batch.get(mp3_dir.name, 12)
        desired_model = pair_model.get(mp3_dir.name, "medium")
        if not diarize_only:
            from lightning_whisper_mlx import LightningWhisperMLX
            if whisper is None or last_batch != desired_batch or last_model != desired_model:
                print(f"Loading model '{desired_model}' (batch_size={desired_batch})...")
                whisper = LightningWhisperMLX(model=desired_model, batch_size=desired_batch, quant=None)
                last_batch = desired_batch
                last_model = desired_model

        print(f"\n[{mp3_dir}] {len(to_process)} mp3(s) → {out_dir}")

        for i, mp3_path in enumerate(to_process, 1):
            if args.limit and total_done >= args.limit:
                done_verb = "diarized" if diarize_only else "transcribed"
                print(f"\nLimit of {args.limit} reached.")
                post_process(mp3_dir)
                print(f"\nDone. {total_done} file(s) {done_verb}.")
                return

            # Re-hydrate models if a periodic reload nuked them. Cheap when not needed.
            if not diarize_only and whisper is None:
                from lightning_whisper_mlx import LightningWhisperMLX
                print(f"  ↻ Reloading Whisper '{desired_model}' (batch_size={desired_batch})...")
                whisper = LightningWhisperMLX(model=desired_model, batch_size=desired_batch, quant=None)
                last_batch = desired_batch
                last_model = desired_model
            want_diarize = pair_diarize.get(mp3_dir.name, False)
            if want_diarize and diarization_pipeline is None:
                print(f"  ↻ Reloading diarization pipeline (pyannote)...")
                diarization_pipeline = load_diarization_pipeline()

            audio_dur = get_audio_duration_secs(mp3_path)
            dur_str = format_duration(str(int(audio_dur))) if audio_dur else "??:??"
            do_diarize = want_diarize and diarization_pipeline is not None
            mode_marker = " (diarize-only)" if diarize_only else (" (with diarization)" if do_diarize else "")
            print(f"  [{i}/{len(to_process)}] Transcribing{mode_marker} ({dur_str}): {mp3_path.name}")

            speaker_turns = None
            if do_diarize:
                cached = load_cached_diarization(mp3_path, out_dir)
                if cached is not None:
                    speaker_turns = cached
                    n_speakers = len(set(t[2] for t in speaker_turns))
                    print(f"    ✓ Diarization (cached) — {n_speakers} speaker(s), {len(speaker_turns)} turn(s)")
                else:
                    t_d = time.time()
                    try:
                        speaker_turns = diarize_audio(mp3_path, diarization_pipeline)
                    except Exception as e:
                        print(f"    ✗ Diarization failed: {e} — falling back to flat transcript.")
                        speaker_turns = None
                    else:
                        n_speakers = len(set(t[2] for t in speaker_turns))
                        print(f"    ✓ Diarized in {time.time() - t_d:.1f}s — {n_speakers} speaker(s), {len(speaker_turns)} turn(s)")
                        save_diarization_cache(mp3_path, out_dir, speaker_turns)

            if diarize_only:
                # Diarize-only mode: cache is written above; skip Whisper, rendering,
                # .md write, and .processed append entirely. The cache file is the
                # done-signal — a later --transcribe will pick it up automatically.
                total_done += 1
                speaker_turns = None
                _release_memory()
                continue

            t0 = time.time()
            result = transcribe_chunked(mp3_path, whisper, checkpoint_dir=out_dir / ".chunks")
            elapsed = time.time() - t0
            text = result["text"].strip()

            if not text:
                print(f"    ✗ Empty transcription — skipping.")
                continue

            words = len(text.split())
            speed = audio_dur / elapsed if audio_dur else 0
            print(f"    ✓ {words} words in {elapsed:.1f}s ({speed:.1f}x realtime)")

            out_path = out_dir / f"{mp3_path.stem}.md"
            segments = result.get("segments") or []
            meta = load_metadata_sidecar(mp3_path)
            # Resolve host display name: per-feed TOML `host` wins, else use the
            # RSS-derived host stored in the metadata sidecar. Trim to first token
            # so the on-page label reads naturally ("Lex" not "Lex Fridman").
            cfg_for_pair = feed_cfg_for(config, mp3_dir.name)
            host_full = cfg_for_pair.get("host") or (meta.get("host") if meta else "")
            host_display = _host_display_name(host_full)
            if speaker_turns and segments:
                aligned = align_segments_to_speakers(segments, speaker_turns)
                content = render_diarized_markdown(mp3_path.stem, aligned, meta, host_name=host_display)
            else:
                if do_diarize and not segments:
                    print(f"    ⚠ Whisper returned no segments — saving flat transcript.")
                content = render_flat_markdown(mp3_path.stem, text, meta)
            out_path.write_text(content, encoding="utf-8")
            print(f"    ✓ Saved: {out_path.name}")

            tag = mp3_dir.name
            if feed_cfg_for(config, tag):
                record_processed(tag, mp3_path.stem)

            total_done += 1

            # Per-episode memory hygiene: drop big locals, force GC, clear GPU caches.
            # MLX/torch tensors on Apple Silicon unified memory accumulate heap
            # if left to Python's lazy collector — manifests as the process RSS
            # climbing into the tens of GB over a long run and eventually OOM'ing
            # the system.
            result = text = segments = speaker_turns = content = aligned = None
            _release_memory()

            # Every RELOAD_EVERY episodes, fully tear down models so the next
            # iteration rebuilds from scratch. Reload cost (~10-20s) is dwarfed
            # by the heap-reset benefit on multi-hour episodes.
            if total_done % RELOAD_EVERY == 0:
                print(f"    ⟳ Periodic model reset (every {RELOAD_EVERY} episodes)")
                whisper = None
                last_batch = None
                last_model = None
                if diarization_pipeline is not None:
                    diarization_pipeline = None
                _release_memory()

        post_process(mp3_dir)

    done_verb = "diarized" if diarize_only else "transcribed"
    print(f"\nDone. {total_done} file(s) {done_verb}.")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Fetch Substack podcast transcripts and audio.")
    parser.add_argument("--config", default="./feeds.toml", help="Config file path (default: ./feeds.toml)")
    parser.add_argument("--feed",   default=None, help="Feed tag from feeds.toml (e.g. 'nates-notebook')")
    parser.add_argument("--rss",    default=None, help="RSS feed URL (overrides --feed's rss)")
    parser.add_argument("--sid",    default=None, help="substack.sid session cookie (overrides --feed's sid)")
    parser.add_argument("--out",    default=None, help="Output directory (default: ./<kind>/<feed-tag>/)")
    parser.add_argument("--limit",  type=int, default=None, help="Max number of entries")
    parser.add_argument("--no-limit", action="store_true",
                        help="Bypass max_downloads_per_run from feeds.toml for this run "
                             "(useful for bulk pre-downloads on unmetered networks).")
    parser.add_argument("--index",  action="store_true", help="List episodes instead of downloading transcripts")
    parser.add_argument("--download", action="store_true", help="Download mp3 files instead of transcripts")
    parser.add_argument("--transcribe", action="store_true", help="Transcribe mp3s using Whisper")
    parser.add_argument("--mp3-dir", default=None, help="Directory containing mp3s to transcribe")
    parser.add_argument("--transcript-dir", default=None, help="Transcript dir checked by --download to skip already-transcribed episodes")
    parser.add_argument("--model",  default=None,
                        help="Whisper model: tiny, base, small, medium (default), large-v3. "
                             "Overrides per-feed `model` from feeds.toml when set.")
    parser.add_argument("--diarize", action=argparse.BooleanOptionalAction, default=None,
                        help="Force speaker diarization on/off (overrides feeds.toml)")
    parser.add_argument("--subprocess-per-episode", action=argparse.BooleanOptionalAction, default=None,
                        help="Force subprocess-per-episode mode on/off (overrides feeds.toml). "
                             "Runs each episode in a fresh Python process to isolate MPS "
                             "allocator state; trades ~30-60s model-load overhead per episode "
                             "for predictable, bounded per-episode cost on long backlogs.")
    parser.add_argument("--diarize-only", action="store_true",
                        help="Run only the pyannote diarization stage; skip Whisper. "
                             "Writes .diarize/<stem>.json cache files so a later --transcribe "
                             "is Whisper-only. Pairs naturally with --subprocess-concurrency N "
                             "since pyannote's working set (~5 GB) is far smaller than Whisper's "
                             "(up to ~50 GB for large-v3), making N concurrent diarize workers "
                             "safe where N concurrent transcribes would OOM.")
    parser.add_argument("--subprocess-concurrency", type=int, default=None,
                        help="Max concurrent transcribe subprocesses for one feed "
                             "(overrides feeds.toml). Only meaningful with "
                             "--subprocess-per-episode. Each worker uses ~3 GB unified memory.")
    parser.add_argument("--only", default=None,
                        help="Restrict --transcribe to a single mp3 by stem name. "
                             "Used internally by --subprocess-per-episode to dispatch "
                             "specific episodes to parallel workers without races.")
    parser.add_argument("--label", default=None,
                        help="Prefix every stdout line with the given text. Used internally "
                             "to distinguish concurrent transcribe subprocesses in the parent's "
                             "merged log stream.")
    parser.add_argument("--backfill-headers", action="store_true",
                        help="For existing transcripts, fetch each feed's RSS and splice "
                             "metadata (title, link, summary, etc.) into the .md header. "
                             "Skips files that already have YAML frontmatter.")
    parser.add_argument("--status", action="store_true",
                        help="Print a per-feed health snapshot (RSS, local, SD card, gaps).")
    parser.add_argument("--offline", action="store_true",
                        help="With --status, skip the RSS fetch and use only local data.")
    parser.add_argument("--check", action="store_true",
                        help="List new episodes per feed without downloading. "
                             "Lightweight enough for cron / metered connections.")
    parser.add_argument("--daily", action="store_true",
                        help="Run --download then --transcribe for every feed that hasn't "
                             "opted out with `daily = false` in feeds.toml. Per-feed errors "
                             "don't block the rest of the routine. With --feed <tag>, "
                             "scopes the routine to that one feed.")
    args = parser.parse_args()

    # Wrap stdout for concurrent subprocesses so each line carries its worker
    # label (set by the parent via --label). Skipped when --label is empty.
    if args.label:
        sys.stdout = _PrefixedWriter(sys.stdout, args.label + " ")

    config = load_config(Path(args.config))
    resolve_feed(args, config)

    if args.status:
        run_status(args)
        return

    if args.check:
        run_check(args)
        return

    if args.daily:
        run_daily(args)
        return

    if args.backfill_headers:
        run_backfill_headers(args)
        return

    if args.transcribe or args.diarize_only:
        run_transcribe(args)
        return

    if not args.rss:
        parser.error("--rss or --feed is required unless using --transcribe")

    print("Fetching RSS feed...")
    feed = feedparser.parse(args.rss)
    if not feed.entries:
        print("No entries found in RSS feed. Check your URL.")
        sys.exit(1)

    print(f"Found {len(feed.entries)} episode(s) in feed.\n")

    if args.index:
        run_index(feed, args.limit)
    elif args.download:
        run_download(feed, args)
    else:
        run_fetch(feed, args)


if __name__ == "__main__":
    main()
