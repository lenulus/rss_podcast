# Reports pipeline

How the corpus becomes The Week in AI: the scheduled jobs, where each one runs, what each reads and writes, and how they depend on each other. Current as of 2026-10-31.

The report prompts live in `prompts/` and the outputs in `reports/`, both gitignored. This doc describes structure only. For prompt versions and changelogs, see `prompts/forward-trends/README.md` and `prompts/specials/README.md`.

## Data flow

```
news.sh --all ──────────► news/<source>/*.md ─────┐
run.sh --daily ─────────► transcripts/<show>/*.md ┤
                                                  ▼
                              build_index.py ──► search_index/   (keyword + vector)
                                                  │
        ┌─────────────────────────────────────────┼──────────────────────────────┐
        ▼                                         ▼                              ▼
  48-hour brief (Tu/Th)                  weekly brief (Sat)            forward-trends (monthly)
  reports/the-48-hours-in-ai-*.md        reports/the-week-in-ai-*.md   reports/forward-trends/<RUN>/
                                         reports/story-log.yaml          ├─ LATEST
                                                  ▲                      ├─ latest-wia-tuning.yaml ──► weekly brief
                                                  │                      ▼
                                                  └──────────── topic reports (monthly, after LATEST)
                                                                reports/specials/<RUN>/
                                                                         │
                                                  quarterly reader report (Jan/Apr/Jul/Oct)
                                                  reports/the-week-in-ai-quarterly-<Q>.md
```

## Jobs

Times are Pacific; the scheduler adds a few minutes of jitter.

| Job | Runs on | Schedule | Does | Writes |
|---|---|---|---|---|
| `news-sync` | Mac scheduled task | daily 03:00 | `./news.sh --all` | `news/` |
| `daily-transcriptions` | Mac scheduled task | daily 06:00 | `./run.sh --daily`, then `build_index.py`, detached | `transcripts/`, `search_index/` |
| Biweekly 48-hour brief | Cowork routine | Tue, Thu 04:00 | 48-hour brief, Gmail draft, Drive upload | `reports/the-48-hours-in-ai-*.md` |
| `weekly-ai-brief` | Mac scheduled task | Sat 07:00 | weekly brief, Gmail draft, Drive upload, story log update | `reports/the-week-in-ai-*.md`, `reports/story-log.yaml` |
| `forward-trends-monthly` | Mac scheduled task | 1st, 05:50 | forward-trends analysis over the trailing three months (first pass) | `reports/forward-trends/<RUN>/`, `LATEST`, `latest-wia-tuning.yaml` |
| `specials-monthly` | Mac scheduled task | days 2 to 10, 08:40 | up to nine gated topic reports; does nothing until `LATEST` names last month | `reports/specials/<RUN>/` |
| Quarterly reader report | Cowork routine | 1st of Jan, Apr, Jul, Oct, 13:50 | reader-facing quarterly report from the run for the quarter's last month (second pass) | `reports/the-week-in-ai-quarterly-<Q>.md` |

Mac scheduled tasks live in `~/.claude/scheduled-tasks/<id>/SKILL.md`. The report tasks are thin wrappers: each reads the `-current` prompt file, so a new prompt version takes effect by renaming files, with no task edit. Cowork routines are managed from claude.ai and carry their prompts inline.

The Cowork routines that used to run the weekly brief and the forward-trends analysis are disabled, not deleted, as a fallback.

## Why some jobs run on the Mac

Cowork routines run their shell in a Linux VM with the repo mounted. That VM can't run `.venv-search`, which is macOS-native and uses the Apple GPU. Sharing the SQLite index between the VM and the Mac would also risk corrupting it. Any job that uses `search.py` or `build_index.py` therefore runs as a Mac scheduled task. The 48-hour brief and the quarterly report don't use search, so they stay in Cowork.

Mac scheduled tasks:

- run only while the Claude desktop app is open; a missed fire runs when the app next opens;
- need a permission mode set (Auto) in the app, or an unattended run stops at its first approval prompt and hangs;
- are single-instance: a run still open blocks that task's next fire, so short checks end their turn right away;
- stop firing, and leave no log, if the Claude login lapses. Check file dates under `downloads/`, `news/` and `reports/` to spot gaps.

## Dependencies and gating

- **Index freshness.** `daily-transcriptions` refreshes the index after transcription. The weekly brief and both monthly jobs also refresh it at the start; with nothing new, that takes seconds. A lock file allows one refresh at a time.
- **Forward-trends release.** A forward-trends run updates `reports/forward-trends/LATEST` only after its independent verification passes. `specials-monthly` waits for that, and the quarterly report reads the run whose window matches the just-finished quarter.
- **Blind phase.** Forward-trends extracts signals and builds trends without seeing the weekly issues, then writes `freeze.txt` before calibrating against them. Every search in that run sets `SEARCH_BLIND_RUN`, so `search.py` refuses the `briefs` corpus until the freeze.
- **Tuning loop.** Each forward-trends run writes `latest-wia-tuning.yaml`, a bounded editorial prior that the weekly brief reads (Step 3.6 of its prompt) to correct for what it over- or under-covered.
- **Continuity.** The weekly brief reads and updates `reports/story-log.yaml`, and searches past issues for each candidate headline. The 48-hour brief reads the latest weekly and 48-hour issues.
- **Topic reports** use each month's `reports/specials/<RUN>/ownership.yaml` (which topic owns which trends, events and explanations), built by an agent pass and `specials.py`. Last month's published report for each topic is the next month's prior.

## Where search is used

| Job | Use |
|---|---|
| Weekly brief | finding first-party originals of aggregator claims in the corpus; checking past issues for prior coverage; tracing quotes and figures |
| Forward-trends | dossier fact checks and counterevidence (raw corpus only); coverage calibration and verification after the freeze |
| Topic reports | novelty against the weekly issues, by the writer and the verifier |
| 48-hour brief, quarterly report | none |

## When something looks wrong

- **A report didn't appear:** check the task's runs in the app (Scheduled), or for Cowork, the routine's runs on claude.ai. Then check whether the app was open and the task's permission mode is set.
- **Monthly outputs:** `reports/forward-trends/<RUN>/` ends with run notes in the task's final message; `reports/specials/<RUN>/README.md` is the topic-report run summary, including skipped topics and why.
- **Search errors in a run:** run `.venv-search/bin/python build_index.py` by hand. A run whose refresh failed says so in its notes and falls back to grep.
- **The wiki layer** (`CLAUDE.md`, `wiki/`) is a separate, manual workflow and isn't part of this pipeline.
