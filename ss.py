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
    cfg = feeds[args.feed]
    args._feed_cfg = cfg
    if not args.rss:
        args.rss = cfg.get("rss")
    if not args.sid:
        args.sid = cfg.get("sid")
    if not args.rss:
        sys.exit(f"Feed '{args.feed}' in {args.config} has no 'rss' field.")


def feed_cfg_for(config: dict, tag: str | None) -> dict:
    """Look up a feed's config by tag — returns {} if tag missing or not in config."""
    if not tag:
        return {}
    return config.get("feeds", {}).get(tag, {}) or {}


def effective_limit(args) -> int | None:
    """CLI --limit wins; otherwise fall back to feed's max_downloads_per_run."""
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


# ── Path resolution ───────────────────────────────────────────────────────────

def default_dir(kind: str, tag: str | None) -> Path:
    """Default output directory for a given kind ('downloads' or 'transcripts')."""
    suffix = f"/{tag}" if tag else ""
    return Path(f"./{kind}{suffix}")


# ── Eviction ──────────────────────────────────────────────────────────────────

def prune_feed_mp3s(tag: str, feed_cfg: dict) -> None:
    """Cap mp3 count in ./downloads/<tag>/ to feed's max_episodes_on_disk.

    Eviction rules:
    - Keeps the newest N mp3s (by filename's YYYY-MM-DD prefix).
    - Only evicts mp3s that already have a matching transcript — never deletes
      source audio that hasn't been preserved.
    - If backup_path is set, copy to <backup_path>/<tag>/<file>.mp3 first.
      Skips eviction entirely if the backup path's parent directory is missing
      (e.g. the SD card isn't mounted) — safer than silent deletion.
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

    backup_path = feed_cfg.get("backup_path")
    backup_dir: Path | None = None
    if backup_path:
        bp = Path(backup_path)
        # If neither the path nor its parent exists, the volume is likely unmounted.
        if not bp.exists() and not bp.parent.is_dir():
            print(f"  ⚠ [{tag}] backup_path {bp} unavailable (parent missing) — skipping eviction.")
            return
        backup_dir = bp / tag
        try:
            backup_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            print(f"  ⚠ [{tag}] cannot create backup dir {backup_dir}: {e} — skipping eviction.")
            return

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
            fetched += 1
            skip.add(stem)

        time.sleep(0.5)

    print(f"\nDone. {fetched} new mp3(s) downloaded.")

    if args.feed:
        prune_feed_mp3s(args.feed, args._feed_cfg)


def run_fetch(feed, args):
    """Download Substack-hosted transcripts as Markdown files."""
    out_dir = Path(args.out) if args.out else default_dir("transcripts", args.feed)
    out_dir.mkdir(parents=True, exist_ok=True)

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

        fetched += 1
        existing.add(stem)

        time.sleep(0.5)

    print(f"\nDone. {fetched} new transcript(s) fetched.")


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


def run_transcribe(args):
    """Transcribe mp3 files using Lightning Whisper MLX (Apple Silicon GPU)."""
    pairs = transcribe_pairs(args)

    # Pre-scan to figure out how many files we'd process — lets us skip model load if zero.
    plan: list[tuple[Path, Path, list[Path]]] = []
    for mp3_dir, out_dir in pairs:
        if not mp3_dir.is_dir():
            print(f"  ↷ Skip {mp3_dir}: not a directory.")
            continue
        mp3s = sorted(mp3_dir.glob("*.mp3"))
        existing = {p.stem for p in out_dir.glob("*.md")} if out_dir.is_dir() else set()
        to_process = [f for f in mp3s if f.stem not in existing]
        plan.append((mp3_dir, out_dir, to_process))
        print(f"  {mp3_dir}: {len(mp3s)} mp3(s), {len(existing)} transcribed, {len(to_process)} pending → {out_dir}")

    total_pending = sum(len(p[2]) for p in plan)
    if total_pending == 0:
        print("\nAll mp3s already have transcripts. Nothing to do.")
        return

    print(f"\nDevice: Apple Silicon GPU (MLX)")
    print(f"Loading model '{args.model}'...")
    from lightning_whisper_mlx import LightningWhisperMLX
    whisper = LightningWhisperMLX(model=args.model, batch_size=12, quant=None)

    total_done = 0
    config = getattr(args, "_config", {}) or {}

    def prune_dir(mp3_dir: Path):
        """Prune a finished feed dir if its tag matches a config entry."""
        tag = mp3_dir.name
        cfg = feed_cfg_for(config, tag)
        if cfg:
            prune_feed_mp3s(tag, cfg)

    for mp3_dir, out_dir, to_process in plan:
        if not to_process:
            prune_dir(mp3_dir)
            continue
        out_dir.mkdir(parents=True, exist_ok=True)
        print(f"\n[{mp3_dir}] {len(to_process)} mp3(s) → {out_dir}")

        for i, mp3_path in enumerate(to_process, 1):
            if args.limit and total_done >= args.limit:
                print(f"\nLimit of {args.limit} reached.")
                prune_dir(mp3_dir)
                print(f"\nDone. {total_done} file(s) transcribed.")
                return

            audio_dur = get_audio_duration_secs(mp3_path)
            dur_str = format_duration(str(int(audio_dur))) if audio_dur else "??:??"
            print(f"  [{i}/{len(to_process)}] Transcribing ({dur_str}): {mp3_path.name}")

            t0 = time.time()
            result = whisper.transcribe(audio_path=str(mp3_path))
            elapsed = time.time() - t0
            text = result["text"].strip()

            if not text:
                print(f"    ✗ Empty transcription — skipping.")
                continue

            words = len(text.split())
            speed = audio_dur / elapsed if audio_dur else 0
            print(f"    ✓ {words} words in {elapsed:.1f}s ({speed:.1f}x realtime)")

            out_path = out_dir / f"{mp3_path.stem}.md"
            out_path.write_text(f"# {mp3_path.stem}\n\n---\n\n{text}\n", encoding="utf-8")
            print(f"    ✓ Saved: {out_path.name}")
            total_done += 1

        prune_dir(mp3_dir)

    print(f"\nDone. {total_done} file(s) transcribed.")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Fetch Substack podcast transcripts and audio.")
    parser.add_argument("--config", default="./feeds.toml", help="Config file path (default: ./feeds.toml)")
    parser.add_argument("--feed",   default=None, help="Feed tag from feeds.toml (e.g. 'nates-notebook')")
    parser.add_argument("--rss",    default=None, help="RSS feed URL (overrides --feed's rss)")
    parser.add_argument("--sid",    default=None, help="substack.sid session cookie (overrides --feed's sid)")
    parser.add_argument("--out",    default=None, help="Output directory (default: ./<kind>/<feed-tag>/)")
    parser.add_argument("--limit",  type=int, default=None, help="Max number of entries")
    parser.add_argument("--index",  action="store_true", help="List episodes instead of downloading transcripts")
    parser.add_argument("--download", action="store_true", help="Download mp3 files instead of transcripts")
    parser.add_argument("--transcribe", action="store_true", help="Transcribe mp3s using Whisper")
    parser.add_argument("--mp3-dir", default=None, help="Directory containing mp3s to transcribe")
    parser.add_argument("--transcript-dir", default=None, help="Transcript dir checked by --download to skip already-transcribed episodes")
    parser.add_argument("--model",  default="medium", help="Whisper model: tiny, base, small, medium, large-v3")
    args = parser.parse_args()

    config = load_config(Path(args.config))
    resolve_feed(args, config)

    if args.transcribe:
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
