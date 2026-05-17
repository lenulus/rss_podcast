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


def discover(source_cfg: dict) -> list[tuple]:
    """Unified discovery dispatch.

    Returns [(url, published_iso, title, payload), …] where `payload` is None
    for RSS/sitemap sources (body fetched later via trafilatura) and a
    normalized dict for hf-daily-papers (body already in hand from the JSON
    API, no per-article fetch needed).
    """
    t = source_cfg.get("type", "").lower()
    if t == "rss":
        return [(u, d, ti, None) for u, d, ti in discover_rss(source_cfg)]
    if t == "sitemap":
        return [(u, d, ti, None) for u, d, ti in discover_sitemap(source_cfg)]
    if t == "hf-daily-papers":
        return discover_hf_papers(source_cfg)
    raise ValueError(f"Unknown source type: {t!r}")


# ── Hugging Face Daily Papers ────────────────────────────────────────────────

HF_API_DAILY = "https://huggingface.co/api/daily_papers"
HF_API_PAPER = "https://huggingface.co/api/papers/{arxiv_id}"
HF_HTML_WEEKLY = "https://huggingface.co/papers/week/{week}"


def _fetch_hf_daily_api() -> list[dict]:
    """GET /api/daily_papers — returns the most-recent 50 curated papers."""
    headers = {"User-Agent": DEFAULT_USER_AGENT, "Accept": "application/json"}
    r = requests.get(HF_API_DAILY, headers=headers, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()


def _fetch_hf_weekly() -> list[dict]:
    """Enumerate this week's papers via HTML, hydrate each via per-paper JSON API.

    The weekly HTML page lists arxiv IDs for the curated week (~105 papers);
    each is fetched via /api/papers/<id> for the structured payload. Same
    extraction quality as the daily API, broader date window per poll.
    """
    now = datetime.now()
    iso_year, iso_week, _ = now.isocalendar()
    week_str = f"{iso_year}-W{iso_week:02d}"
    headers = {"User-Agent": DEFAULT_USER_AGENT}
    r = requests.get(HF_HTML_WEEKLY.format(week=week_str), headers=headers, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    arxiv_ids = sorted(set(re.findall(r"/papers/(\d+\.\d+)", r.text)))
    out: list[dict] = []
    for arxiv_id in arxiv_ids:
        try:
            pr = requests.get(
                HF_API_PAPER.format(arxiv_id=arxiv_id),
                headers={"User-Agent": DEFAULT_USER_AGENT, "Accept": "application/json"},
                timeout=HTTP_TIMEOUT,
            )
            pr.raise_for_status()
            out.append(pr.json())
        except Exception as e:
            print(f"    ⚠ per-paper fetch failed for {arxiv_id}: {e}")
        time.sleep(0.3)
    return out


def _normalize_hf_paper(raw: dict) -> dict:
    """Flatten daily-feed or per-paper-API responses into one uniform shape.

    Daily feed items wrap the paper metadata inside a `paper:` key and add
    denormalized title/summary/publishedAt at the top level. The per-paper
    API returns the inner shape directly. Both produce the same flat dict
    here so downstream code doesn't branch.
    """
    paper = raw.get("paper") if isinstance(raw.get("paper"), dict) else raw
    sub_by = paper.get("submittedOnDailyBy") or {}
    org = paper.get("organization") or {}
    return {
        "arxiv_id": paper.get("id", ""),
        "title": (paper.get("title") or "").strip(),
        "authors": [a.get("name", "").strip() for a in paper.get("authors", []) if a.get("name")],
        "summary": (paper.get("summary") or "").strip(),
        "ai_summary": (paper.get("ai_summary") or "").strip(),
        "ai_keywords": [k.strip() for k in paper.get("ai_keywords", []) if k and k.strip()],
        "upvotes": int(paper.get("upvotes") or 0),
        "published_at": paper.get("publishedAt", "") or "",
        "submitted_on_daily_at": paper.get("submittedOnDailyAt", "") or "",
        "submitted_by": (sub_by.get("name") or sub_by.get("fullname") or "").strip(),
        "project_page": (paper.get("projectPage") or "").strip(),
        "organization": (org.get("fullname") or org.get("name") or "").strip(),
        "discussion_id": (paper.get("discussionId") or "").strip(),
    }


def discover_hf_papers(source_cfg: dict) -> list[tuple]:
    """Returns [(url, published_iso, title, payload_dict), …].

    Applies `min_upvotes` filter and `top_n` cap before returning. Filtered
    papers are NOT in the returned list, so they don't get appended to
    .processed and will be re-evaluated on the next poll against the (possibly
    updated) upvote count.

    Sort key: upvotes DESC, then submittedOnDailyAt DESC (Python's stable sort
    is leveraged — apply secondary key first, then primary).
    """
    mode = source_cfg.get("discovery_mode", "daily-api")
    if mode == "daily-api":
        raw_list = _fetch_hf_daily_api()
    elif mode == "weekly-html":
        raw_list = _fetch_hf_weekly()
    else:
        raise ValueError(f"Unknown discovery_mode: {mode!r} (use 'daily-api' or 'weekly-html')")

    normalized = [_normalize_hf_paper(r) for r in raw_list]

    min_upvotes = int(source_cfg.get("min_upvotes", 0) or 0)
    if min_upvotes > 0:
        before = len(normalized)
        normalized = [p for p in normalized if p["upvotes"] >= min_upvotes]
        print(f"    filtered by min_upvotes={min_upvotes}: {before} → {len(normalized)}")

    # Stable sort: secondary key first (submittedOnDailyAt DESC), then primary (upvotes DESC).
    normalized.sort(key=lambda p: p["submitted_on_daily_at"] or "", reverse=True)
    normalized.sort(key=lambda p: p["upvotes"], reverse=True)

    top_n = source_cfg.get("top_n")
    if top_n:
        normalized = normalized[: int(top_n)]
        print(f"    capped by top_n={top_n}: keeping {len(normalized)}")

    out: list[tuple] = []
    for p in normalized:
        if not p["arxiv_id"]:
            continue
        url = f"https://huggingface.co/papers/{p['arxiv_id']}"
        out.append((url, p["published_at"], p["title"], p))
    return out


# ── Article fetch + extract ───────────────────────────────────────────────────

def fetch_and_extract(url: str, sid: Optional[str] = None) -> Optional[tuple[str, dict]]:
    """Return (markdown_body, metadata_dict) or None on failure.

    When `sid` is provided, fetches via `requests` with the Substack
    session cookie attached (`connect.sid=<sid>`, the express-session
    cookie name that custom-domain Substack publications use). Unlocks
    paywalled content for publications the cookie's owner has access to.
    Without sid, falls back to trafilatura's default fetcher.

    Note: Substack uses `connect.sid` per publication custom domain
    (e.g. www.latent.space). The substack.com top-level cookie is named
    differently (substack.sid + substack.lli combo) and does NOT travel
    cross-origin to the custom domain. Always copy the cookie from the
    publication's own domain in dev tools, not from substack.com.
    """
    try:
        if sid:
            r = requests.get(
                url,
                headers={"User-Agent": DEFAULT_USER_AGENT},
                cookies={"connect.sid": sid},
                timeout=HTTP_TIMEOUT,
                allow_redirects=True,
            )
            r.raise_for_status()
            downloaded = r.text
        else:
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


def _slug_from_text(s: str, limit: int = 60) -> str:
    """lowercase, hyphenate non-alphanumerics, cap to `limit` chars. Used for paper titles."""
    safe = re.sub(r"[^a-z0-9-]+", "-", s.lower()).strip("-")
    return safe[:limit] or "untitled"


def render_hf_paper(source: str, payload: dict) -> tuple[Path, str, str]:
    """Render an HF Daily Papers entry to markdown.

    Differs from render_article in two ways: no trafilatura body (we already
    have the structured payload), and the frontmatter is `type: paper` with
    arxiv-specific fields (arxiv_id, project_page, authors list, organization,
    upvotes, ai_keywords as tags).
    """
    fetched_iso = datetime.now(timezone.utc).isoformat()
    arxiv_id = payload["arxiv_id"]
    date_prefix, published_iso = normalize_date(payload.get("published_at", ""))
    _, submitted_iso = normalize_date(payload.get("submitted_on_daily_at", ""))

    title_slug = _slug_from_text(payload["title"] or arxiv_id)
    filename = f"{date_prefix} - {arxiv_id} - {title_slug}.md"
    out_path = Path(f"./news/{source}/{filename}")

    fm_lines = [
        "---",
        f'title: "{escape_yaml(payload["title"])}"',
        "type: paper",
        f"source: {source}",
        f"arxiv_id: {arxiv_id}",
        f"arxiv_url: https://arxiv.org/abs/{arxiv_id}",
        f"hf_url: https://huggingface.co/papers/{arxiv_id}",
    ]
    if payload.get("project_page"):
        fm_lines.append(f"project_page: {payload['project_page']}")
    fm_lines.append(f"published: {published_iso}" if published_iso else "published:")
    if submitted_iso:
        fm_lines.append(f"submitted_to_hf: {submitted_iso}")
    fm_lines.append(f"fetched: {fetched_iso}")

    if payload["authors"]:
        fm_lines.append("authors:")
        for a in payload["authors"]:
            fm_lines.append(f'  - "{escape_yaml(a)}"')
    else:
        fm_lines.append("authors: []")
    if payload.get("organization"):
        fm_lines.append(f'organization: "{escape_yaml(payload["organization"])}"')
    if payload.get("submitted_by"):
        fm_lines.append(f'submitted_by: "{escape_yaml(payload["submitted_by"])}"')
    fm_lines.append(f"upvotes: {payload['upvotes']}")

    # Tags: literal "paper" first, then ai_keywords slugified
    fm_lines.append("tags:")
    fm_lines.append("  - paper")
    for kw in payload.get("ai_keywords", []):
        slug = _slug_from_text(kw, limit=50)
        if slug and slug != "untitled":
            fm_lines.append(f"  - {slug}")
    fm_lines.append("---")
    fm_lines.append("")

    body_parts = [f"# {payload['title']}", ""]
    if payload.get("ai_summary"):
        body_parts.extend(["## TL;DR (HF auto-summary)", "", payload["ai_summary"], ""])
    if payload.get("summary"):
        body_parts.extend(["## Abstract", "", payload["summary"], ""])

    content = "\n".join(fm_lines) + "\n".join(body_parts).rstrip() + "\n"
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
    pending = [t for t in discovered if t[0] not in seen]
    print(f"  pending (not in .processed): {len(pending)}")
    if not pending:
        return
    # For RSS/sitemap, sort newest first by lastmod/published date. For HF papers,
    # discover_hf_papers already sorted by (upvotes DESC, submittedOnDailyAt DESC),
    # so don't re-sort and lose that ordering.
    if source_cfg.get("type") != "hf-daily-papers":
        pending.sort(key=lambda x: x[1] or "", reverse=True)
    if limit:
        pending = pending[:limit]
        print(f"  capped to {limit} for this run")

    if check_only:
        for url, date, title, _payload in pending[:20]:
            print(f"    [{date or '?':<10}] {url}")
        if len(pending) > 20:
            print(f"    … and {len(pending) - 20} more")
        return

    written = 0
    for i, (url, discovery_date, title_hint, payload) in enumerate(pending, 1):
        print(f"\n  [{i}/{len(pending)}] {url}")
        if payload is not None:
            # HF paper path: payload carries the full normalized JSON already;
            # no per-article fetch + trafilatura step.
            out_path, content, published = render_hf_paper(source, payload)
            body_len = len(payload.get("summary", "") or "") + len(payload.get("ai_summary", "") or "")
        else:
            extracted = fetch_and_extract(url, sid=source_cfg.get("sid"))
            if not extracted:
                time.sleep(delay)
                continue
            body, meta = extracted
            if not meta.get("title") and title_hint:
                meta["title"] = title_hint
            out_path, content, published = render_article(source, url, body, meta, discovery_date)
            body_len = len(body)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(content, encoding="utf-8")
        record_processed(source, url)
        written += 1
        print(f"    ✓ wrote {out_path.name} (published={published or '?'}, {body_len} chars)")
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
