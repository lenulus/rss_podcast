# Design: Hugging Face Daily Papers ingestion

Design notes for adding `huggingface-papers` as a source type in `news.py`. Captures the data-source survey, the chosen architecture, and the open questions worth resolving before code lands.

## Goal

Ingest curated AI/ML research papers from Hugging Face's Daily Papers feed into the existing `news/<source>/` markdown pipeline, with rich enough metadata for the wiki layer (per `CLAUDE.md`) to synthesize from. Treat each paper as a primary source — abstract + HF's auto-summary captured locally, with stable arxiv ID for cross-referencing.

## Available endpoints (probe results)

| Endpoint | Method | Content | Use |
|---|---|---|---|
| `https://huggingface.co/api/daily_papers` | GET (JSON) | 50 most-recent curated papers, full structured metadata | **Primary daily feed** |
| `https://huggingface.co/api/papers/<arxiv_id>` | GET (JSON) | Single-paper metadata, same shape as daily-feed item | **Per-paper lookups** |
| `https://huggingface.co/papers/week/<YYYY-Www>` | GET (HTML) | ~105 papers for that ISO week, requires scraping arxiv IDs | **Weekly roll-up** |
| `https://huggingface.co/papers/month/<YYYY-MM>` | GET (HTML) | Monthly roll-up, scraping required | Future use |
| `https://huggingface.co/api/papers?type=trending` | GET (JSON) | 50 papers trending **today** — misleadingly named, not weekly | Skip |
| `https://huggingface.co/api/daily_papers?period=weekly` | GET (JSON) | Param is silently ignored — returns same 50 as bare endpoint | Skip |

Notes:
- The JSON API is the cleanest path — no HTML scraping, no fragile selectors.
- Weekly mode needs HTML parsing only for **arxiv ID discovery**; body fetch goes through the same `/api/papers/<id>` JSON endpoint, so the per-paper render code is shared with daily mode.
- Walking back via week (`/papers/week/2026-W17`, `2026-W10`, etc.) returns HTTP 200 — no obvious historical cap, so backfill is possible.

## Per-paper JSON schema

Sample item from `/api/daily_papers` (shape is consistent across endpoints):

```json
{
  "paper": {
    "id": "2605.15193",
    "title": "Aligning Latent Geometry for Spherical Flow Matching in Image Generation",
    "authors": [
      {"name": "Tuna Han Salih Meral", "_id": "..."},
      {"name": "Kaan Oktay", "_id": "..."}
    ],
    "publishedAt": "2026-05-14T00:00:00.000Z",
    "submittedOnDailyAt": "2026-05-15T00:00:00.000Z",
    "summary": "Latent flow matching for image generation usually transports Gaussian noise...",
    "ai_summary": "Geodesic flow matching improves image generation by projecting latents onto fixed radius spheres...",
    "ai_keywords": ["latent flow matching", "variational autoencoder", "spherical shells", ...],
    "upvotes": 3,
    "discussionId": "...",
    "projectPage": "https://aligning-latent-geometry.github.io",
    "organization": {"fullname": "Virginia Tech", "name": "mayzovt"},
    "submittedOnDailyBy": {"name": "tmeral", "fullname": "Tuna Han Salih Meral"}
  }
}
```

Key fields and how they map to our markdown:
- **`paper.id`** → canonical dedup key (arxiv ID).
- **`paper.title`** → frontmatter `title:`.
- **`paper.authors[].name`** → frontmatter `authors:` list.
- **`paper.organization.fullname`** → frontmatter `organization:`.
- **`paper.summary`** → body section "Abstract".
- **`paper.ai_summary`** → body section "TL;DR (HF auto-summary)" — the highest-leverage field for wiki synthesis.
- **`paper.ai_keywords`** → frontmatter `tags:`.
- **`paper.publishedAt`** → frontmatter `published:`, used for filename date prefix.
- **`paper.submittedOnDailyAt`** → frontmatter `submitted_to_hf:` (different field — when HF curated it).
- **`paper.projectPage`** → frontmatter `project_page:`.

## Design decisions

### 1. Per-paper file, not daily/weekly digest

Same shape as `news/<source>/YYYY-MM-DD - <slug>.md` for regular news articles. Reasons:

- Each paper has a stable globally-unique ID (arxiv) — natural unit of granularity.
- Greppable individually; wiki cross-links work cleanly (paper-level, not date-level).
- Matches the transcript pattern (`transcripts/<feed>/YYYY-MM-DD - Title.md`).
- Avoids the daily-digest churn problem where a digest written Monday is incomplete by Tuesday.

Filename: `news/hf-papers/YYYY-MM-DD - <arxiv-id> - <slug>.md`

### 2. JSON API for bodies, HTML page only for weekly discovery

Two reasons to never scrape paper pages with trafilatura:
- The JSON has more structured data than the HTML would (separate `ai_summary` vs `summary`, structured authors, etc.).
- The HTML page changes layout; the JSON shape is more stable.

Weekly mode uses HTML only to enumerate arxiv IDs in the week:

```python
arxiv_ids = set(re.findall(r'/papers/(\d+\.\d+)', html_body))
```

Then fetches each via `/api/papers/<id>` for the structured payload.

### 3. Polling cadence is configurable, not hardcoded

The volume concern raised the question of daily vs weekly. Quick math:
- Daily API returns 50 papers (covers ~today + recent days; about 10–20 NEW per day)
- Weekly HTML returns ~105 papers for the ISO week — roughly the same 10–15/day, just rolled up.

So the **volume of new content is the same** either way. The real lever is **polling cadence + dedup**, not endpoint choice. Configurable in `news.toml`:

```toml
[sources.hf-papers]
type = "hf-daily-papers"          # always use the JSON API for ingestion
cadence = "weekly"                 # informational — operator-driven polling
# Or for higher fidelity:
# cadence = "daily"
```

Implementation note: `cadence` is informational only — the script doesn't enforce it. The user runs `./news.sh --source hf-papers` whenever they want. Daily users cron-trigger it daily; weekly users run it once a week. The dedup index (`.processed`) handles missed polls gracefully (any not-yet-seen paper gets ingested on the next run, regardless of cadence).

If the daily-API-only mode misses a paper that was curated 5 days ago (the API returns the most-recent 50, not all-time), the weekly HTML page is a fallback — its arxiv-ID list covers ~7 days. Two strategies in the code:

- `discovery_mode = "daily-api"` (default) — fetch `/api/daily_papers`, take the 50 entries, dedup.
- `discovery_mode = "weekly-html"` — fetch `/papers/week/<current-ISO-week>` HTML, extract arxiv IDs, fetch each via `/api/papers/<id>`. Catches a wider window per poll; costs 105 API calls/week instead of 1 daily call.

If the user wants both — set up the source twice with different slugs and `discovery_mode` values — but for v1 picking one is fine.

### 4. Wiki integration via frontmatter shape

Frontmatter matches the `type: paper` convention so the wiki ingest path (per `CLAUDE.md`) can pick these up alongside `type: source` (transcripts) and `type: article` (news). Each paper is treated as a primary research source.

## Configuration

`news.toml` addition:

```toml
[sources.hf-papers]
type = "hf-daily-papers"
discovery_mode = "daily-api"        # or "weekly-html"
# fetch_delay_seconds inherits from [defaults]; HF API is fast, 0.5s is fine
fetch_delay_seconds = 0.5
```

For weekly polling without the wider window, the user just runs the ingestor weekly via cron without changing this config — the dedup index does its job.

## Output

Per paper, `news/hf-papers/YYYY-MM-DD - <arxiv-id> - <slug>.md`:

```markdown
---
title: "Aligning Latent Geometry for Spherical Flow Matching in Image Generation"
type: paper
source: hf-papers
arxiv_id: 2605.15193
arxiv_url: https://arxiv.org/abs/2605.15193
hf_url: https://huggingface.co/papers/2605.15193
project_page: https://aligning-latent-geometry.github.io
published: 2026-05-14T00:00:00Z
submitted_to_hf: 2026-05-15T00:00:00Z
fetched: 2026-05-17T08:42:00Z
authors:
  - Tuna Han Salih Meral
  - Kaan Oktay
  - Hidir Yesiltepe
  - Adil Kaan Akan
  - Pinar Yanardag
organization: "Virginia Tech"
submitted_by: tmeral
upvotes: 3
tags:
  - paper
  - latent-flow-matching
  - variational-autoencoder
  - spherical-shells
  - geodesic-paths
---

# Aligning Latent Geometry for Spherical Flow Matching in Image Generation

## TL;DR (HF auto-summary)

Geodesic flow matching improves image generation by projecting latents onto
fixed radius spheres and using spherical linear interpolation instead of
linear paths, preserving semantic content through angular components.

## Abstract

Latent flow matching for image generation usually transports Gaussian noise
to variational autoencoder latents along linear paths. Both endpoints,
however, concentrate in thin spherical shells, and a ...
[full arxiv abstract — multi-paragraph]
```

## Implementation scope

~120 lines added to `news.py`:

- `discover_hf_daily_papers(cfg)` — GET `/api/daily_papers`, normalize into the existing `(url, date, title, payload)` shape (the payload carries the JSON for later render).
- `discover_hf_weekly_papers(cfg)` — fetch weekly HTML, extract arxiv IDs, GET each via `/api/papers/<id>`, return the same tuple shape.
- `render_hf_paper(payload)` — JSON → markdown with the frontmatter above. Different from `render_article` because we don't run trafilatura.
- Dispatcher tweak in `process_source` so `type = "hf-daily-papers"` skips `fetch_and_extract` and uses the pre-fetched payload.

CLI unchanged: `./news.sh --source hf-papers --check` (preview), `--source hf-papers` (ingest), `--limit N` (cap).

## Open questions

1. **Should `ai_keywords` go into `tags:`, or a separate `keywords:` field?**
   The wiki convention treats `tags:` as topical/freeform. `ai_keywords` are usually noun-phrases extracted from the paper — closer to topical tags than free annotations. Lean: use `tags:`, prefixed with the literal `paper` so the wiki layer can filter `type:paper` cleanly.

2. **What about deleted-but-still-in-the-feed entries?**
   The HF API occasionally returns papers that get withdrawn from arXiv. Behavior to decide: do we keep the local copy, or remove it on a re-poll? Lean: keep locally, log a warning. Same invariant as the transcript pipeline — never delete on the operator's behalf without explicit instruction.

3. **PDF link / full-text fetch?**
   The abstract + HF summary covers ~90% of synthesis value. Pulling the full PDF for storage adds disk cost and license complexity. **Defer** — abstract + ai_summary is good enough for v1.

4. **Author affiliation parsing.**
   `paper.organization.fullname` is sometimes a single university; sometimes a comma-separated list; sometimes missing. v1: capture as a single string. v2: parse into a list if it becomes useful for wiki cross-linking.

5. **Rate limiting.**
   HF doesn't publish explicit rate limits for the public API. v1 honors `fetch_delay_seconds = 0.5` (the default in `[defaults]` if not overridden). If we ever hit 429s, add exponential backoff in `news.py`'s generic fetch path.

## Related code

- `news.py:discover` — the type-dispatch entry point that needs the new `hf-daily-papers` case.
- `news.py:process_source` — the orchestration loop that needs to skip `fetch_and_extract` for this type.
- `news.py:render_article` — the rendering shape to mirror.
- `news.example.toml` — add a worked example `[sources.hf-papers]` block once implemented.
