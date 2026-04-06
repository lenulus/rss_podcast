#!/usr/bin/env python3
from __future__ import annotations
"""
fetch_transcripts.py — Download Substack podcast transcripts as Markdown files.

Usage:
    python fetch_transcripts.py --rss <rss_url> [--out <dir>] [--limit <n>] [--sid <cookie>]

Examples:
    python fetch_transcripts.py --rss "https://api.substack.com/feed/podcast/..." --limit 5
    python fetch_transcripts.py --rss "https://api.substack.com/feed/podcast/..." --out ./transcripts
"""

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import feedparser
import requests


# ── Helpers ───────────────────────────────────────────────────────────────────

def sanitize_filename(title: str) -> str:
    """Strip characters that are unsafe in filenames, collapse whitespace."""
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', title)
    safe = re.sub(r'\s+', ' ', safe).strip()
    return safe[:120]  # cap length


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


def get_transcript_url(slug: str, session_cookie: str | None) -> str | None:
    """Fetch the post JSON and return the transcript CDN URL, or None."""
    url = f"https://natesnewsletter.substack.com/api/v1/posts/{slug}"
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


def write_markdown(path: Path, title: str, pub_date: str, slug: str, text: str) -> None:
    """Write transcript as a Markdown file with a simple frontmatter header."""
    content = f"""# {title}

**Date:** {pub_date}  
**Source:** https://natesnewsletter.substack.com/p/{slug}

---

{text}
"""
    path.write_text(content, encoding="utf-8")


# ── Main ──────────────────────────────────────────────────────────────────────

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


def run_transcribe(args):
    """Transcribe mp3 files using Lightning Whisper MLX (Apple Silicon GPU)."""
    from lightning_whisper_mlx import LightningWhisperMLX

    mp3_dir = Path(args.mp3_dir)
    if not mp3_dir.is_dir():
        print(f"Directory not found: {mp3_dir}")
        sys.exit(1)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    existing = {p.stem for p in out_dir.glob("*.md")}
    mp3s = sorted(mp3_dir.glob("*.mp3"))

    if not mp3s:
        print(f"No mp3 files found in {mp3_dir}")
        sys.exit(1)

    to_process = [f for f in mp3s if f.stem not in existing]

    if args.limit:
        to_process = to_process[:args.limit]

    if not to_process:
        print("All mp3s already have transcripts. Nothing to do.")
        return

    print(f"Found {len(mp3s)} mp3(s), {len(existing)} already transcribed, {len(to_process)} to process.")
    print(f"Device: Apple Silicon GPU (MLX)\n")
    print(f"Loading model '{args.model}'...")
    whisper = LightningWhisperMLX(model=args.model, batch_size=12, quant=None)

    for i, mp3_path in enumerate(to_process, 1):
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

    print(f"\nDone. {len(to_process)} file(s) transcribed.")


def run_download(feed, args):
    """Download mp3 files from the feed."""
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    existing_mp3s = {p.stem for p in out_dir.glob("*.mp3")}

    # Also check for existing transcripts so we don't re-download mp3s that
    # have already been transcribed (e.g. after moving mp3s to external storage)
    transcript_dir = Path(args.transcript_dir)
    existing_transcripts = {p.stem for p in transcript_dir.glob("*.md")} if transcript_dir.is_dir() else set()
    skip = existing_mp3s | existing_transcripts

    print(f"Output dir: {out_dir} ({len(existing_mp3s)} existing mp3(s), {len(existing_transcripts)} already transcribed)\n")

    entries = sorted(feed.entries, key=lambda e: parsedate_to_datetime(e.published), reverse=True)

    fetched = 0

    for entry in entries:
        if args.limit and fetched >= args.limit:
            print(f"\nLimit of {args.limit} reached. Done.")
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


def run_fetch(feed, args):
    """Download transcripts as Markdown files."""
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    existing = {p.stem for p in out_dir.glob("*.md")}
    print(f"Output dir: {out_dir} ({len(existing)} existing transcript(s))\n")

    entries = sorted(feed.entries, key=lambda e: parsedate_to_datetime(e.published), reverse=True)

    fetched = 0

    for entry in entries:
        if args.limit and fetched >= args.limit:
            print(f"\nLimit of {args.limit} reached. Done.")
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

        cdn_url = get_transcript_url(slug, args.sid)
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
        write_markdown(out_path, title, pub_date, slug, text)
        print(f"    ✓ Saved: {out_path.name} ({len(text.split())} words)")

        fetched += 1
        existing.add(stem)

        time.sleep(0.5)

    print(f"\nDone. {fetched} new transcript(s) fetched.")


def main():
    parser = argparse.ArgumentParser(description="Fetch Substack podcast transcripts as Markdown.")
    parser.add_argument("--rss",   default=None, help="Private RSS feed URL")
    parser.add_argument("--out",   default=None, help="Output directory (default: ./transcripts or ./downloads)")
    parser.add_argument("--limit", type=int, default=None, help="Max number of entries")
    parser.add_argument("--sid",   default=None, help="substack.sid session cookie (optional)")
    parser.add_argument("--index", action="store_true", help="List episodes instead of downloading transcripts")
    parser.add_argument("--download", action="store_true", help="Download mp3 files instead of transcripts")
    parser.add_argument("--transcribe", action="store_true", help="Transcribe mp3s from --mp3-dir using Whisper")
    parser.add_argument("--mp3-dir", default="./downloads", help="Directory containing mp3s to transcribe (default: ./downloads)")
    parser.add_argument("--transcript-dir", default="./transcripts", help="Transcript directory to check before re-downloading (default: ./transcripts)")
    parser.add_argument("--model", default="medium", help="Whisper model size: tiny, base, small, medium, large-v3 (default: medium)")
    args = parser.parse_args()

    if args.transcribe:
        if not args.out:
            args.out = "./transcripts"
        run_transcribe(args)
        return

    if not args.rss:
        parser.error("--rss is required unless using --transcribe")

    print("Fetching RSS feed...")
    feed = feedparser.parse(args.rss)
    if not feed.entries:
        print("No entries found in RSS feed. Check your URL.")
        sys.exit(1)

    print(f"Found {len(feed.entries)} episode(s) in feed.\n")

    if not args.out:
        args.out = "./downloads" if args.download else "./transcripts"

    if args.index:
        run_index(feed, args.limit)
    elif args.download:
        run_download(feed, args)
    else:
        run_fetch(feed, args)


if __name__ == "__main__":
    main()
