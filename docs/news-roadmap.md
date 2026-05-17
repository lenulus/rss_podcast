# news.py Roadmap

Status, test plan, and pending work for the news ingestion pipeline. Keep this current as items land.

## Status

### Shipped (`ee85226`)

- `news.py` (~300 lines): TOML config, RSS + sitemap discovery, trafilatura extraction, per-source markdown render, `.processed` dedup, mtime-aware SD-card backup, crash-isolated per-source loop.
- `news.example.toml`: 9 sources pre-configured (4 RSS + 5 sitemap).
- `news.sh`: wrapper with `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE` exports (same shape as `run.sh`).
- CLI: `--source <slug>`, `--all`, `--check`, `--limit N`, `--status`.
- Output layout: `news/<source>/YYYY-MM-DD - <slug>.md` with YAML frontmatter matching the `type: article` wiki convention.
- Backup namespacing: `<backup_path>/news/<source>/text/*.md` (under `news/` so it never collides with podcast feed slugs).
- gitignored: `news/`, `news.toml`.

### Smoke-tested

- **Sitemap path → Anthropic, 1 article.** End-to-end works: discovery (211 URLs), filter (path_filter `/news/`), fetch (132 KB HTML), trafilatura extract (~10 KB clean markdown, correct title + date), render, dedup append. Output reviewed by hand.

### Designed but unbuilt

- **HF Daily Papers ingestion** — full design in `docs/news-hf-papers-design.md`. ~150 lines to add to `news.py`: `discover_hf_daily_papers`, `discover_hf_weekly_papers`, `render_hf_paper`, filter logic (`min_upvotes` + `top_n` with don't-touch-`.processed` semantics for filtered entries), type dispatch.

## Untested code paths

These are written but unverified in production. Each should land a sample artifact and a hand-review before declaring the path solid.

| Path | Risk | Test |
|---|---|---|
| **RSS source ingest** | feedparser → URL → trafilatura → render chain on each of 4 RSS sources. Each may have quirks (paywall headers, JS-rendered content, redirects). | `./news.sh --source openai --limit 1` then inspect output |
| | | `./news.sh --source deepmind --limit 1` |
| | | `./news.sh --source google-research --limit 1` |
| | | `./news.sh --source google-keyword --limit 1` |
| **Sitemap-index follow** | AI21 uses a sitemap-index → sub-sitemap pattern. Currently `news.example.toml` points at the sub-sitemap directly. The index-following code in `discover_sitemap` is untested. | Point a test config at `https://www.ai21.com/sitemap.xml` (root index) and verify discovery returns the same URLs as the sub-sitemap-direct config |
| **Per-sitemap-source extraction quality** | Anthropic verified; Mistral, xAI, Cohere, AI21 may render differently. Trafilatura is robust but not perfect. | `--limit 1` per source; eyeball the resulting `.md` for boilerplate leakage, missing body, broken markdown |
| **`--check` mode** | Lists pending URLs without fetching. Untested. | `./news.sh --source anthropic --check` should print URLs only, no extraction |
| **`--status` mode** | Tabular per-source summary. Untested. | `./news.sh --status` after at least one source has content |
| **`--all` mode** | Iterates every source; per-source try/except isolates crashes. Untested. | `./news.sh --all --limit 1` — one article per source, verify a deliberately-broken source doesn't poison the others |
| **Backup path with SD mounted** | `backup_source` writes to `<backup_path>/news/<source>/text/`. Only the "skip with warning" branch has run so far (SD unmounted). | Mount SD, run `./news.sh --source anthropic --limit 1`, verify mirror file exists |
| **Dedup across runs** | Second run on the same source should be a no-op for already-ingested URLs. | Run any source twice; second run should report "0 pending" |

**Suggested first pass:** `./news.sh --all --limit 1` once. That exercises 7 of the 8 rows above in one shot. Then go back and hand-review the 9 produced `.md` files for extraction quality issues.

## Pending build work

### 1. HF Daily Papers (highest leverage)

Design complete (`docs/news-hf-papers-design.md`). Implementation order:

1. Add `discover_hf_daily_papers(cfg)` — GET `/api/daily_papers`, return `(url, date, title, payload)` tuples with the JSON payload riding along.
2. Add `discover_hf_weekly_papers(cfg)` — fetch weekly HTML, regex arxiv IDs, GET each via `/api/papers/<id>` for structured payload.
3. Add `render_hf_paper(payload)` — JSON → markdown with `type: paper` frontmatter, abstract + `ai_summary` + `ai_keywords` → tags.
4. Add filter logic: `min_upvotes` (drops), `top_n` (caps after sort). Filtered papers do NOT append to `.processed`.
5. Wire into `process_source` so `type = "hf-daily-papers"` skips `fetch_and_extract` and uses pre-fetched payload.
6. Update `news.example.toml` with worked `[sources.hf-papers]` block.
7. Smoke-test: `./news.sh --source hf-papers --check` (preview filter), then `--limit 3` (actual ingest), inspect output.

### 2. Meta AI (lowest priority, hardest case)

No RSS, no sitemap. Two paths to evaluate when we get there:

- **Direct CSS scraping** — point at `https://ai.meta.com/blog/`, extract post links via selector, then fetch each via trafilatura. Adds a `type = "html"` source kind with a selector field. Fragile — Meta's site has rebuilt twice in living memory.
- **Third-party converter** — RSSHub publishes a feed at `https://rsshub.app/meta/ai/blog` that does the scraping for us. Less fragile but adds a dependency on a hosted service.

Defer until the other 9 sources are running reliably and Meta AI's content is something the wiki layer actually wants.

### 3. Operational improvements (nice to have, evaluate after first month of real use)

- **`--check-sources`** — probe every configured source, report `(source, discoverable_url_count, last_lastmod_or_publishedAt)`. Useful for spotting feed rot.
- **Per-source freshness alerts** — if a source's most-recent article date is > 30 days old, log warning. Avoids silent staleness.
- **Date floor for backfill** — `[sources.<slug>].backfill_after = "2026-01-01"` to skip pre-2026 entries on first ingest. Currently `--limit N` is the only knob; a date floor is friendlier for "I only want this year forward."
- **Raw-HTML save on extraction failure** — if trafilatura returns empty, save `news/<source>/.failures/<slug>.html` for manual triage. Currently we just log and skip.
- **Cron-friendly lock file** — `news/.lock` to prevent concurrent runs from racing on `.processed` writes. Almost certainly safe today since `.processed` is atomic-append, but explicit is better than implicit for cron.
- **Tags from URL path** — `https://research.google/blog/category/healthcare/...` could auto-add `healthcare` to `tags:`. Currently tags are hardcoded `[news]`.

## Open design questions

1. **Polling cadence.** Once HF papers + the 9 article sources are reliable, expected operator usage is `./news.sh --all` on a daily or weekly cron. Should we document a recommended crontab in `CLI_README.md`?

2. **Concurrency.** Current implementation is serial (1.5s delay per source). For 10 sources × ~10 articles each, that's still under 5 min for a typical poll — no concurrency needed. Worth revisiting only if a source has hundreds of new articles per poll.

3. **Body length floor.** Some sites (xAI's news posts especially) are short — sometimes 1–2 paragraphs. Worth a `min_body_chars` setting per source to skip non-substantive entries? Or let the wiki layer filter downstream?

4. **Re-fetch on stale content.** Articles get edited (typo fixes, broken-link patches). Currently we ingest once and never re-check. Possibly: a `--refresh-after-days N` mode that re-fetches articles older than N days from local. Defer until a use case actually demands it.

5. **Trafilatura version pin.** `requirements.txt` has `trafilatura` unpinned. The library is actively maintained; a major-version bump could change output. Worth pinning to current minor (`trafilatura~=2.0`) once we've verified extraction quality across all 9 sources?

## Acceptance criteria for "news is done"

We're "v1 complete" when:

- `./news.sh --all --limit 5` runs cleanly with no crashes across all configured sources.
- Each source has at least 3 hand-reviewed sample articles with no boilerplate leakage or missing-body cases.
- HF Daily Papers ingestion is shipped and tested (separate from the 9 generic sources).
- Backup path tested with SD card mounted.
- `--status` and `--check` modes exercised at least once.

Until then: each successful smoke test is incremental, not "done."

## Related files

- `news.py` — main script
- `news.example.toml` — config template
- `news.sh` — venv wrapper
- `docs/news-hf-papers-design.md` — full design for HF Papers
- `CLI_README.md` § "News ingestion" — operator-facing commands
