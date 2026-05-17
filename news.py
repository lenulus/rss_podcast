#!/usr/bin/env python3
"""News ingestor — fetches full article bodies from RSS feeds and sitemaps.

Companion to ss.py (which handles podcast audio). Same conventions:
- TOML config (news.toml, gitignored)
- Per-source output dir (news/<source>/)
- .processed dedup index (one URL per line, append-only)
- SD-card backup mirroring (when backup_path is configured)

Discovery is pluggable per source: RSS feeds via feedparser, sitemap.xml
via simple regex parsing. Body extraction is uniform: trafilatura pulls
the main article content as markdown and the canonical publish date
from <meta> tags, falling back to the sitemap's <lastmod> when needed.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import feedparser
import requests
import trafilatura


DEFAULT_FETCH_DELAY = 1.5
DEFAULT_USER_AGENT = "Mozilla/5.0 (Macintosh; ss-news/1.0) Gecko/Firefox"
HTTP_TIMEOUT = 20

# ── Config ────────────────────────────────────────────────────────────────────

def load_config(config_path: Path) -> dict:
    """Load news.toml. Returns {} if missing."""
    if not config_path.exists():
        return {}
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib
    with open(config_path, "rb") as f:
        return tomllib.load(f)


def resolve_defaults(config: dict, source_cfg: dict) -> dict:
    """Merge [defaults] under per-source overrides."""
    defaults = config.get("defaults", {}) or {}
    return {**defaults, **(source_cfg or {})}


# ── State helpers (.processed) ────────────────────────────────────────────────

def processed_path(source: str) -> Path:
    return Path(f"./news/{source}/.processed")


def load_processed(source: str) -> set[str]:
    p = processed_path(source)
    if not p.exists():
        return set()
    return {line.strip() for line in p.read_text(encoding="utf-8").splitlines() if line.strip()}


def record_processed(source: str, url: str) -> None:
    p = processed_path(source)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(url + "\n")


# ── Discovery ─────────────────────────────────────────────────────────────────

def discover_rss(source_cfg: dict) -> list[tuple[str, str, str]]:
    """Returns [(url, published_iso, title), …] from an RSS/Atom feed."""
    url = source_cfg["url"]
    feed = feedparser.parse(url)
    if feed.bozo and not feed.entries:
        raise RuntimeError(f"RSS parse failed: {feed.bozo_exception}")
    out: list[tuple[str, str, str]] = []
    for e in feed.entries:
        link = e.get("link", "").strip()
        if not link:
            continue
        published_iso = ""
        for key in ("published_parsed", "updated_parsed"):
            t = e.get(key)
            if t:
                published_iso = datetime(*t[:6], tzinfo=timezone.utc).isoformat()
                break
        out.append((link, published_iso, e.get("title", "").strip()))
    return out


def discover_sitemap(source_cfg: dict) -> list[tuple[str, str, str]]:
    """Returns [(url, lastmod_iso, ""), …] from a sitemap.xml.

    Handles plain `<urlset>` sitemaps and `<sitemapindex>` files that
    point at sub-sitemaps (only those that match `sub_sitemap_filter`
    if set, otherwise all).
    """
    url = source_cfg["sitemap_url"]
    path_filter = source_cfg.get("path_filter", "")
    sub_filter = source_cfg.get("sub_sitemap_filter", "")
    headers = {"User-Agent": DEFAULT_USER_AGENT}
    r = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    body = r.text

    if "<sitemapindex" in body[:500].lower():
        sub_locs = re.findall(r"<sitemap[^>]*>\s*<loc[^>]*>([^<]+)</loc>", body, re.IGNORECASE)
        if sub_filter:
            sub_locs = [u for u in sub_locs if sub_filter in u]
        out: list[tuple[str, str, str]] = []
        for sub in sub_locs:
            try:
                sr = requests.get(sub, headers=headers, timeout=HTTP_TIMEOUT)
                sr.raise_for_status()
                out.extend(_parse_urlset(sr.text, path_filter))
            except Exception as e:
                print(f"    ⚠ sub-sitemap fetch failed for {sub}: {e}")
            time.sleep(0.3)
        return out
    return _parse_urlset(body, path_filter)


def _parse_urlset(body: str, path_filter: str) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    for block in re.findall(r"<url[^>]*>(.*?)</url>", body, re.DOTALL | re.IGNORECASE):
        loc = re.search(r"<loc[^>]*>([^<]+)</loc>", block, re.IGNORECASE)
        if not loc:
            continue
        u = loc.group(1).strip()
        if path_filter and path_filter not in u:
            continue
        # Skip the index pages themselves (e.g. /news, /blog with no trailing path)
        if u.rstrip("/").endswith(path_filter.rstrip("/")):
            continue
        lastmod = re.search(r"<lastmod[^>]*>([^<]+)</lastmod>", block, re.IGNORECASE)
        out.append((u, lastmod.group(1).strip() if lastmod else "", ""))
    return out


def discover(source_cfg: dict) -> list[tuple[str, str, str]]:
    t = source_cfg.get("type", "").lower()
    if t == "rss":
        return discover_rss(source_cfg)
    if t == "sitemap":
        return discover_sitemap(source_cfg)
    raise ValueError(f"Unknown source type: {t!r}")


# ── Article fetch + extract ───────────────────────────────────────────────────

def fetch_and_extract(url: str) -> Optional[tuple[str, dict]]:
    """Return (markdown_body, metadata_dict) or None on failure."""
    try:
        downloaded = trafilatura.fetch_url(url)
    except Exception as e:
        print(f"    ✗ fetch failed: {e}")
        return None
    if not downloaded:
        print(f"    ✗ fetch returned empty")
        return None
    try:
        body = trafilatura.extract(
            downloaded,
            output_format="markdown",
            include_links=True,
            include_formatting=True,
            favor_recall=True,
        )
    except Exception as e:
        print(f"    ✗ extract failed: {e}")
        return None
    if not body or not body.strip():
        print(f"    ✗ extract returned empty body")
        return None
    meta = trafilatura.extract_metadata(downloaded)
    meta_dict = {
        "title": (meta.title if meta and meta.title else "").strip(),
        "author": (meta.author if meta and meta.author else "").strip(),
        "date": (meta.date if meta and meta.date else "").strip(),
        "sitename": (meta.sitename if meta and meta.sitename else "").strip(),
    }
    return body, meta_dict


# ── Slug / date helpers ───────────────────────────────────────────────────────

_DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")


def normalize_date(*candidates: str) -> tuple[str, str]:
    """Return (date_prefix, iso_string) from the first parseable candidate.

    Date prefix is YYYY-MM-DD for filenames; iso_string is the full ISO-8601
    timestamp if available, else just the date.
    """
    for raw in candidates:
        if not raw:
            continue
        s = raw.strip().replace("Z", "+00:00")
        # Try full ISO
        try:
            dt = datetime.fromisoformat(s)
            return dt.strftime("%Y-%m-%d"), dt.isoformat()
        except ValueError:
            pass
        # Try just the leading YYYY-MM-DD
        m = _DATE_PREFIX_RE.match(s)
        if m:
            return m.group(1), m.group(1)
    return "0000-00-00", ""


def slug_from_url(url: str) -> str:
    """Last path segment, lowercased, safe-chars-only, length-capped."""
    tail = url.rstrip("/").rsplit("/", 1)[-1] or "index"
    tail = tail.split("?", 1)[0].split("#", 1)[0]
    safe = re.sub(r"[^a-z0-9-]+", "-", tail.lower()).strip("-")
    if not safe:
        safe = hashlib.sha1(url.encode()).hexdigest()[:10]
    return safe[:80]


def escape_yaml(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


# ── Render ────────────────────────────────────────────────────────────────────

def render_article(source: str, url: str, body: str, meta: dict,
                   discovery_date: str) -> tuple[Path, str, str]:
    """Return (out_path, content, published_iso). Caller writes the file."""
    fetched_iso = datetime.now(timezone.utc).isoformat()
    date_prefix, published_iso = normalize_date(meta.get("date", ""), discovery_date)
    slug = slug_from_url(url)
    filename = f"{date_prefix} - {slug}.md"
    out_path = Path(f"./news/{source}/{filename}")

    title = meta.get("title", "") or slug.replace("-", " ").title()
    author = meta.get("author", "")

    fm_lines = [
        "---",
        f'title: "{escape_yaml(title)}"',
        "type: article",
        f"source: {source}",
        f"url: {url}",
        f"published: {published_iso}" if published_iso else "published:",
        f"fetched: {fetched_iso}",
    ]
    if author:
        fm_lines.append(f'authors: ["{escape_yaml(author)}"]')
    else:
        fm_lines.append("authors: []")
    fm_lines.extend([
        "tags: [news]",
        "---",
        "",
    ])
    # If trafilatura's body already opens with the h1, don't duplicate
    body_text = body.strip()
    if not body_text.lstrip().startswith("# "):
        body_text = f"# {title}\n\n{body_text}"
    content = "\n".join(fm_lines) + body_text + "\n"
    return out_path, content, published_iso


# ── Backup ────────────────────────────────────────────────────────────────────

def backup_source(source: str, defaults: dict, source_cfg: dict) -> None:
    """Sync ./news/<source>/*.md to <backup_path>/news/<source>/text/."""
    cfg = resolve_defaults({"defaults": defaults}, source_cfg)
    backup_path = source_cfg.get("backup_path") or defaults.get("backup_path")
    if not backup_path:
        return
    src_dir = Path(f"./news/{source}")
    if not src_dir.is_dir():
        return
    md_files = sorted(src_dir.glob("*.md"))
    if not md_files:
        return
    root = Path(backup_path)
    if not root.exists() and not root.parent.is_dir():
        print(f"  ⚠ [{source}] backup root {root} unavailable — skipping.")
        return
    dest_dir = root / "news" / source / "text"
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"  ⚠ [{source}] cannot create backup dir {dest_dir}: {e}")
        return
    copied = refreshed = 0
    for md in md_files:
        dest = dest_dir / md.name
        if dest.exists():
            if dest.stat().st_mtime >= md.stat().st_mtime:
                continue
            try:
                shutil.copy2(md, dest)
                refreshed += 1
            except OSError as e:
                print(f"  ✗ [{source}] backup refresh failed for {md.name}: {e}")
            continue
        try:
            shutil.copy2(md, dest)
            copied += 1
        except OSError as e:
            print(f"  ✗ [{source}] backup failed for {md.name}: {e}")
    if copied or refreshed:
        parts = []
        if copied: parts.append(f"{copied} new")
        if refreshed: parts.append(f"{refreshed} updated")
        print(f"  [{source}] Backed up {' + '.join(parts)} article(s) → {dest_dir}")


# ── Source processing ────────────────────────────────────────────────────────

def process_source(source: str, source_cfg: dict, defaults: dict,
                   limit: Optional[int], check_only: bool) -> None:
    delay = float(source_cfg.get("fetch_delay_seconds",
                                 defaults.get("fetch_delay_seconds", DEFAULT_FETCH_DELAY)))
    print(f"\n[{source}] type={source_cfg.get('type','?')}")
    try:
        discovered = discover(source_cfg)
    except Exception as e:
        print(f"  ✗ discovery failed: {e}")
        return
    print(f"  discovered: {len(discovered)} URL(s)")
    seen = load_processed(source)
    pending = [(url, date, title) for url, date, title in discovered if url not in seen]
    print(f"  pending (not in .processed): {len(pending)}")
    if not pending:
        return
    # Sort newest first so the freshest articles land first; useful when --limit caps.
    pending.sort(key=lambda x: x[1] or "", reverse=True)
    if limit:
        pending = pending[:limit]
        print(f"  capped to {limit} for this run")

    if check_only:
        for url, date, title in pending[:20]:
            print(f"    [{date or '?':<10}] {url}")
        if len(pending) > 20:
            print(f"    … and {len(pending) - 20} more")
        return

    written = 0
    for i, (url, discovery_date, title_hint) in enumerate(pending, 1):
        print(f"\n  [{i}/{len(pending)}] {url}")
        extracted = fetch_and_extract(url)
        if not extracted:
            time.sleep(delay)
            continue
        body, meta = extracted
        if not meta.get("title") and title_hint:
            meta["title"] = title_hint
        out_path, content, published = render_article(source, url, body, meta, discovery_date)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(content, encoding="utf-8")
        record_processed(source, url)
        written += 1
        print(f"    ✓ wrote {out_path.name} (published={published or '?'}, {len(body)} chars)")
        time.sleep(delay)
    print(f"\n  [{source}] wrote {written} new article(s).")
    backup_source(source, defaults, source_cfg)


# ── Status ────────────────────────────────────────────────────────────────────

def run_status(sources: dict) -> None:
    print(f"{'Source':<20} {'Type':<8} {'Local':>7}  {'Last fetched':<25}")
    print(f"{'─'*20} {'─'*8} {'─'*7}  {'─'*25}")
    for slug in sorted(sources.keys()):
        cfg = sources[slug]
        d = Path(f"./news/{slug}")
        local_count = len(list(d.glob("*.md"))) if d.is_dir() else 0
        last = "—"
        if d.is_dir():
            mds = sorted(d.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
            if mds:
                last = datetime.fromtimestamp(mds[0].stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        print(f"{slug:<20} {cfg.get('type','?'):<8} {local_count:>7}  {last:<25}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Ingest news articles from RSS feeds and sitemaps.")
    parser.add_argument("--config", default="./news.toml")
    parser.add_argument("--source", default=None, help="Specific source slug from news.toml")
    parser.add_argument("--all", action="store_true", help="Process every source")
    parser.add_argument("--check", action="store_true", help="List new URLs without fetching")
    parser.add_argument("--limit", type=int, default=None, help="Max articles per source this run")
    parser.add_argument("--status", action="store_true", help="Per-source counts + last-fetched timestamp")
    args = parser.parse_args()

    config = load_config(Path(args.config))
    sources = config.get("sources", {}) or {}
    if not sources:
        sys.exit(f"No [sources.<slug>] entries found in {args.config}.")

    if args.status:
        run_status(sources)
        return

    if args.source:
        if args.source not in sources:
            available = ", ".join(sorted(sources.keys())) or "(none)"
            sys.exit(f"Source '{args.source}' not found. Available: {available}")
        targets = {args.source: sources[args.source]}
    elif args.all:
        targets = sources
    else:
        sys.exit("Specify --source <slug>, --all, or --status.")

    defaults = config.get("defaults", {}) or {}
    for slug, cfg in targets.items():
        try:
            process_source(slug, cfg, defaults, args.limit, args.check)
        except Exception as e:
            print(f"\n  ✗ [{slug}] crashed: {e}")


if __name__ == "__main__":
    main()
