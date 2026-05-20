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
from datetime import datetime, timedelta, timezone
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


def parse_since(spec) -> Optional[datetime]:
    """Parse a TOML `since` value into an aware UTC datetime, or None.

    Accepts:
      '2026-01-01'              — absolute date (UTC midnight)
      '2026-01-01T12:00:00Z'    — absolute datetime
      '1y' / '6m' / '2w' / '14d' — relative age (subtracted from now)

    Raises ValueError on unparseable strings. None / empty returns None.
    """
    if spec is None or (isinstance(spec, str) and not spec.strip()):
        return None
    s = str(spec).strip()
    m = re.fullmatch(r"(\d+)([ymwd])", s)
    if m:
        n = int(m.group(1))
        days_per = {"y": 365, "m": 30, "w": 7, "d": 1}[m.group(2)]
        return datetime.now(timezone.utc) - timedelta(days=n * days_per)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(
            f"Invalid 'since' value: {spec!r} "
            "(expected 'YYYY-MM-DD' or 'Ny/Nm/Nw/Nd')"
        )
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_iso_datetime(s: str) -> Optional[datetime]:
    """Best-effort ISO-8601 parse returning aware UTC datetime, or None."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        try:
            from email.utils import parsedate_to_datetime
            dt = parsedate_to_datetime(s)
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ── State helpers (.processed / .failed / .attempts.json) ───────────────────

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


def failed_path(source: str) -> Path:
    return Path(f"./news/{source}/.failed")


def load_failed(source: str) -> set[str]:
    """Permanently-failed URLs — skipped on discovery until --retry-failed clears them."""
    p = failed_path(source)
    if not p.exists():
        return set()
    return {line.strip() for line in p.read_text(encoding="utf-8").splitlines() if line.strip()}


def record_failed(source: str, url: str) -> None:
    p = failed_path(source)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(url + "\n")


def attempts_path(source: str) -> Path:
    return Path(f"./news/{source}/.attempts.json")


def load_attempts(source: str) -> dict:
    """Per-URL transient failure counter. Reset on success; promoted to .failed
    when count >= max_failures."""
    p = attempts_path(source)
    if not p.exists():
        return {}
    try:
        import json
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_attempts(source: str, attempts: dict) -> None:
    import json
    p = attempts_path(source)
    p.parent.mkdir(parents=True, exist_ok=True)
    # Drop URLs that are no longer failing (counter cleared)
    pruned = {k: v for k, v in attempts.items() if v > 0}
    p.write_text(json.dumps(pruned, indent=2), encoding="utf-8")


def reset_failed(source: str) -> int:
    """Clear .failed + .attempts.json for a source. Returns # entries cleared."""
    n = 0
    fp = failed_path(source)
    if fp.exists():
        n = sum(1 for _ in fp.read_text(encoding="utf-8").splitlines() if _.strip())
        fp.unlink()
    ap = attempts_path(source)
    if ap.exists():
        ap.unlink()
    return n


# ── Discovery ─────────────────────────────────────────────────────────────────

def discover_rss(source_cfg: dict) -> list[tuple[str, str, str]]:
    """Returns [(url, published_iso, title), …] from an RSS/Atom feed."""
    url = source_cfg["url"]
    feed = feedparser.parse(url)
    if feed.bozo and not feed.entries:
        raise RuntimeError(f"RSS parse failed: {feed.bozo_exception}")
    cutoff = parse_since(source_cfg.get("since"))
    out: list[tuple[str, str, str]] = []
    skipped = 0
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
        if cutoff and published_iso:
            pub_dt = _parse_iso_datetime(published_iso)
            if pub_dt and pub_dt < cutoff:
                skipped += 1
                continue
        out.append((link, published_iso, e.get("title", "").strip()))
    if skipped:
        print(f"    ↷ {skipped} entries skipped (older than since={source_cfg.get('since')!r})")
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
    cutoff = parse_since(source_cfg.get("since"))
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
                out.extend(_parse_urlset(sr.text, path_filter, cutoff))
            except Exception as e:
                print(f"    ⚠ sub-sitemap fetch failed for {sub}: {e}")
            time.sleep(0.3)
        if cutoff:
            _report_since_skip(source_cfg, sum(1 for u, _, _ in out if not u))  # no-op; reported inside
        return out
    return _parse_urlset(body, path_filter, cutoff, since_label=source_cfg.get("since"))


def _parse_urlset(body: str, path_filter: str, cutoff=None, since_label=None) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    skipped = 0
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
        lastmod_m = re.search(r"<lastmod[^>]*>([^<]+)</lastmod>", block, re.IGNORECASE)
        lastmod = lastmod_m.group(1).strip() if lastmod_m else ""
        if cutoff and lastmod:
            lm_dt = _parse_iso_datetime(lastmod)
            if lm_dt and lm_dt < cutoff:
                skipped += 1
                continue
        out.append((u, lastmod, ""))
    if skipped and since_label is not None:
        print(f"    ↷ {skipped} URLs skipped (older than since={since_label!r})")
    return out


def _report_since_skip(*args, **kwargs):
    """Placeholder for future per-source skip-reporting helper. Currently noop."""
    pass


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
    if t == "substack-section-archive":
        return discover_substack_archive(source_cfg)
    if t == "static-markdown-spa":
        return [(u, d, ti, None) for u, d, ti in discover_static_markdown_spa(source_cfg)]
    raise ValueError(f"Unknown source type: {t!r}")


# ── Substack section archive ─────────────────────────────────────────────────

def discover_substack_archive(source_cfg: dict) -> list[tuple]:
    """Paginate /api/v1/archive for a Substack section. Returns full backlog.

    Unlike RSS (capped at 20 most-recent), the archive endpoint paginates
    through the entire history. Useful for one-time backfills and as the
    primary discovery method going forward — dedup via .processed makes
    re-polling cheap (only newly-published posts get fetched).

    Required config fields:
      base_url    — publication root, e.g. "https://www.latent.space"
      section_id  — numeric section id from /s/<slug> page <link rel=alternate>

    Optional:
      sid         — connect.sid cookie for paid-tier posts
      min_date    — ISO date floor (e.g. "2026-01-01"); skip older entries
    """
    base_url = source_cfg["base_url"].rstrip("/")
    section_id = source_cfg["section_id"]
    sid = source_cfg.get("sid")
    # Honor `since` (preferred) or legacy `min_date` field for backward compat.
    cutoff = parse_since(source_cfg.get("since") or source_cfg.get("min_date"))

    headers = {"User-Agent": DEFAULT_USER_AGENT, "Accept": "application/json"}
    cookies = {"connect.sid": sid} if sid else {}
    page_size = 20
    out: list[tuple] = []
    offset = 0
    while True:
        url = (f"{base_url}/api/v1/archive"
               f"?sort=new&search=&offset={offset}&limit={page_size}"
               f"&section_id={section_id}")
        r = requests.get(url, headers=headers, cookies=cookies, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        if not data:
            break
        for post in data:
            slug = (post.get("slug") or "").strip()
            canonical = (post.get("canonical_url") or "").strip() or f"{base_url}/p/{slug}"
            post_date = (post.get("post_date") or "")[:25]
            if cutoff and post_date:
                post_dt = _parse_iso_datetime(post_date)
                if post_dt and post_dt < cutoff:
                    # Posts come newest-first; once below cutoff, the rest is
                    # older still — stop paginating.
                    return out
            title = (post.get("title") or "").strip()
            out.append((canonical, post_date, title, None))
        if len(data) < page_size:
            break
        offset += page_size
        time.sleep(0.3)
    return out


# ── Static-markdown SPAs (Angular/React sites serving raw .md files) ─────────

def discover_static_markdown_spa(source_cfg: dict) -> list[tuple[str, str, str]]:
    """Discover articles from a JS-rendered SPA that fetches static .md files.

    Some publishers (e.g. antigravity.google) ship pure client-side apps that
    populate blog posts by fetch()ing raw markdown from a fixed path. The full
    slug list is typically embedded in the main JS bundle as router metadata.
    We exploit this by reading the bundle as text and extracting slugs with a
    user-supplied regex — no headless browser required.

    Required config:
      base_url             — e.g. "https://antigravity.google" (no trailing slash)
      bundle_regex         — regex with one capture group matching the bundle src
                             inside the bundle_path HTML. The captured value is
                             treated as a path relative to base_url.
      slug_regex           — regex with one capture group matching slugs inside
                             the bundle text (one match per article).
      article_url_template — path template; {slug} is substituted per match.
                             Example: "/assets/blog-posts/{slug}.md"

    Optional:
      bundle_path          — page to fetch first (default "/"). Must contain
                             a <script src=…> reference the bundle_regex matches.

    Returns [(article_url, "", ""), …]. Date and title are filled later from
    the markdown's YAML frontmatter (fetch_markdown_passthrough).
    """
    base_url = source_cfg["base_url"].rstrip("/")
    bundle_path = source_cfg.get("bundle_path", "/")
    bundle_regex = source_cfg["bundle_regex"]
    slug_regex = source_cfg["slug_regex"]
    article_url_template = source_cfg["article_url_template"]

    headers = {"User-Agent": DEFAULT_USER_AGENT}

    home_url = base_url + (bundle_path if bundle_path.startswith("/") else "/" + bundle_path)
    r = requests.get(home_url, headers=headers, timeout=HTTP_TIMEOUT, allow_redirects=True)
    r.raise_for_status()

    m = re.search(bundle_regex, r.text)
    if not m:
        raise ValueError(f"bundle_regex did not match in {home_url}")
    bundle_ref = m.group(1)
    if bundle_ref.startswith("http"):
        bundle_url = bundle_ref
    else:
        bundle_url = base_url + (bundle_ref if bundle_ref.startswith("/") else "/" + bundle_ref)

    rb = requests.get(bundle_url, headers=headers, timeout=HTTP_TIMEOUT, allow_redirects=True)
    rb.raise_for_status()

    slugs = sorted(set(re.findall(slug_regex, rb.text)))
    if not slugs:
        raise ValueError(f"slug_regex matched 0 slugs in {bundle_url}")

    out: list[tuple[str, str, str]] = []
    for slug in slugs:
        path = article_url_template.format(slug=slug)
        if not path.startswith("/"):
            path = "/" + path
        out.append((base_url + path, "", ""))
    return out


def _parse_simple_frontmatter(text: str) -> tuple[dict, str]:
    """Tiny YAML-frontmatter parser for the subset our sources actually use.

    Handles `key: value` lines (with optional quotes) and one level of list
    nesting (`categories:` followed by `  - Foo` lines). Returns (meta, body).
    If `text` doesn't start with a `---` fence, meta is {} and body is text.
    """
    if not text.startswith("---"):
        return {}, text
    lines = text.split("\n")
    if lines[0].strip() != "---":
        return {}, text
    fm_end = None
    for i, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            fm_end = i
            break
    if fm_end is None:
        return {}, text
    fm_lines = lines[1:fm_end]
    body = "\n".join(lines[fm_end + 1:]).lstrip("\n")

    meta: dict = {}
    current_list_key: Optional[str] = None
    for line in fm_lines:
        stripped = line.strip()
        if not stripped:
            current_list_key = None
            continue
        if current_list_key and stripped.startswith("- "):
            meta.setdefault(current_list_key, []).append(stripped[2:].strip())
            continue
        if ":" in line:
            current_list_key = None
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip()
            if (val.startswith('"') and val.endswith('"')) or \
               (val.startswith("'") and val.endswith("'")):
                val = val[1:-1]
            if not val:
                current_list_key = key
                meta[key] = []
                continue
            meta[key] = val
    return meta, body


def fetch_markdown_passthrough(url: str) -> Optional[tuple[str, dict]]:
    """Fetch a raw markdown URL and split frontmatter without HTML extraction.

    Used by source types where the publisher already serves articles as static
    markdown files (e.g. static-markdown-spa). Returns the same (body, meta)
    shape as fetch_and_extract so downstream render_article works unchanged.
    """
    headers = {"User-Agent": DEFAULT_USER_AGENT}
    try:
        r = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT, allow_redirects=True)
    except Exception as e:
        print(f"    ✗ fetch failed: {e}")
        return None
    if not r.ok:
        print(f"    ✗ HTTP {r.status_code} from {r.url}")
        return None
    if not r.text:
        print(f"    ✗ fetch returned empty")
        return None
    fm, body = _parse_simple_frontmatter(r.text)
    if not body.strip():
        print(f"    ✗ markdown body empty after frontmatter")
        return None
    def _s(v):
        return v.strip() if isinstance(v, str) else ""
    meta_dict = {
        "title": _s(fm.get("title")),
        "author": _s(fm.get("author")),
        "date": _s(fm.get("date")),
        "sitename": "",
    }
    return body, meta_dict


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

    cutoff = parse_since(source_cfg.get("since"))
    if cutoff is not None:
        before = len(normalized)
        filtered = []
        for p in normalized:
            ref = p.get("submitted_on_daily_at") or p.get("published_at") or ""
            ref_dt = _parse_iso_datetime(ref)
            if ref_dt and ref_dt < cutoff:
                continue
            filtered.append(p)
        normalized = filtered
        if before != len(normalized):
            print(f"    filtered by since={source_cfg.get('since')!r}: {before} → {len(normalized)}")

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

    Always uses `requests.get` (not trafilatura's bundled fetcher) because:
      - requests transparently follows 30x redirects (trafilatura's urllib
        fetcher silently returns empty on some redirect chains — e.g. DeepMind's
        deepmind.google → blog.google redirects)
      - Surfaces explicit HTTP status codes for failure diagnosis instead of
        an opaque "empty body"
      - Trivially supports `connect.sid` cookie auth for Substack paywall bypass

    Note on Substack auth: `connect.sid` is the express-session cookie set by
    each publication's custom domain (e.g. www.latent.space). The substack.com
    top-level cookies are different (substack.sid + substack.lli combo) and do
    NOT travel cross-origin. Always copy the cookie from the publication's own
    domain in dev tools, not from substack.com.
    """
    headers = {"User-Agent": DEFAULT_USER_AGENT}
    cookies = {"connect.sid": sid} if sid else None
    try:
        r = requests.get(
            url, headers=headers, cookies=cookies,
            timeout=HTTP_TIMEOUT, allow_redirects=True,
        )
    except Exception as e:
        print(f"    ✗ fetch failed: {e}")
        return None
    if not r.ok:
        print(f"    ✗ HTTP {r.status_code} from {r.url}")
        return None
    downloaded = r.text
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
    # Strip a single trailing file extension (.md / .html / .htm / .json …).
    # Otherwise sanitization turns "foo.md" into "foo-md" which is ugly.
    tail = re.sub(r"\.[a-z0-9]{1,5}$", "", tail, flags=re.IGNORECASE)
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
                   limit: Optional[int], check_only: bool,
                   retry_failed: bool = False) -> None:
    delay = float(source_cfg.get("fetch_delay_seconds",
                                 defaults.get("fetch_delay_seconds", DEFAULT_FETCH_DELAY)))
    max_failures = int(source_cfg.get("max_failures",
                                      defaults.get("max_failures", 3)))
    print(f"\n[{source}] type={source_cfg.get('type','?')}")
    if retry_failed:
        n = reset_failed(source)
        if n:
            print(f"  cleared {n} entries from .failed (retry-failed mode)")
    try:
        discovered = discover(source_cfg)
    except Exception as e:
        print(f"  ✗ discovery failed: {e}")
        return
    print(f"  discovered: {len(discovered)} URL(s)")
    seen = load_processed(source)
    failed = load_failed(source)
    pending = [t for t in discovered if t[0] not in seen and t[0] not in failed]
    if failed:
        print(f"  pending (not in .processed): {len(pending)}  (skipping {len(failed)} in .failed)")
    else:
        print(f"  pending (not in .processed): {len(pending)}")
    if not pending:
        # Still run the backup pass — picks up locally-newer files that
        # weren't yet mirrored (e.g. first run after backup_path was set,
        # or after the SD card was unmounted on a prior pass).
        backup_source(source, defaults, source_cfg)
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

    attempts = load_attempts(source)
    written = 0
    promoted_to_failed = 0
    for i, (url, discovery_date, title_hint, payload) in enumerate(pending, 1):
        print(f"\n  [{i}/{len(pending)}] {url}")
        if payload is not None:
            # HF paper path: payload carries the full normalized JSON already;
            # no per-article fetch + trafilatura step.
            out_path, content, published = render_hf_paper(source, payload)
            body_len = len(payload.get("summary", "") or "") + len(payload.get("ai_summary", "") or "")
        else:
            if source_cfg.get("type") == "static-markdown-spa":
                extracted = fetch_markdown_passthrough(url)
            else:
                extracted = fetch_and_extract(url, sid=source_cfg.get("sid"))
            if not extracted:
                # Bump the per-URL failure counter; promote to .failed at threshold.
                attempts[url] = attempts.get(url, 0) + 1
                if attempts[url] >= max_failures:
                    record_failed(source, url)
                    attempts.pop(url, None)
                    promoted_to_failed += 1
                    print(f"    ✗ giving up after {max_failures} attempts — moved to .failed")
                else:
                    remaining = max_failures - attempts[url]
                    print(f"    ↻ attempt {attempts[url]}/{max_failures} — will retry on future runs ({remaining} left)")
                time.sleep(delay)
                continue
            # Success → clear any prior attempt counter for this URL.
            attempts.pop(url, None)
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
    save_attempts(source, attempts)
    suffix = f", {promoted_to_failed} moved to .failed" if promoted_to_failed else ""
    print(f"\n  [{source}] wrote {written} new article(s){suffix}.")
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
    parser.add_argument("--retry-failed", action="store_true",
                        help="Clear each targeted source's .failed list (and .attempts.json) before discovery, "
                             "so URLs previously given up on get one more shot.")
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
            process_source(slug, cfg, defaults, args.limit, args.check,
                           retry_failed=args.retry_failed)
        except Exception as e:
            print(f"\n  ✗ [{slug}] crashed: {e}")


if __name__ == "__main__":
    main()
