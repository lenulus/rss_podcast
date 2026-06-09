#!/usr/bin/env python3
from __future__ import annotations
"""
reprocess.py — prepare loop-corrupted transcripts for re-transcription.

Companion to reports/transcript-remediation-plan.md. The transcribe pipeline
(ss.py) has no first-class "redo this episode" path — once a stem is in
`transcripts/<feed>/.processed` it's skipped. This script does the surgical
invalidation the plan calls for, driven by the loop-audit worklist, so a plain
`./loop.sh --feed <feed>` afterwards picks the episodes back up.

For each selected transcript it:
  1. Restores the mp3 from the feed's backup with mtime preserved (shutil.copy2,
     i.e. `cp -p`) so the pyannote diarization cache stays valid and is reused.
     If no backup exists, it flags the episode for re-download instead.
  2. Removes the stem from `.processed`, deletes the corrupted `.md`, and clears
     any stale chunk checkpoint dir (`.chunks/<stem>/`) — a leftover lightning-era
     checkpoint would otherwise be reused and re-inject corrupted text.

DRY-RUN BY DEFAULT. Pass --apply to actually touch files. Pass --run to invoke
./loop.sh for each affected feed once preparation is done.

Examples:
  # See what re-doing all severe transcripts would touch (dry run):
  ./reprocess.py --severity severe

  # Actually prep latent-space severe episodes, then transcribe them:
  ./reprocess.py --severity severe --feed latent-space --apply --run

  # Prep the worst 20 of anything, dry run first:
  ./reprocess.py --min-pct 10 --limit 20

See feeds.toml / reports/loop-audit.csv for inputs.
"""

import argparse
import csv
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import ss  # import-safe: ss.py guards main() behind __name__ == "__main__"

REPO = Path(__file__).resolve().parent
SEVERITIES = ["clean", "mild", "moderate", "severe"]


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Prepare loop-corrupted transcripts for re-transcription.")
    p.add_argument("--audit", default="reports/loop-audit.csv",
                   help="Loop-audit CSV worklist (default: reports/loop-audit.csv)")
    p.add_argument("--config", default="feeds.toml", help="Feeds config (default: feeds.toml)")
    p.add_argument("--severity", default="severe",
                   help="Comma list of severities to include (default: severe). "
                        f"Choices: {','.join(SEVERITIES)}")
    p.add_argument("--feed", default=None,
                   help="Restrict to these feed tags (comma list). Default: all feeds.")
    p.add_argument("--min-pct", type=float, default=None,
                   help="Include any transcript with loop_pct >= this (overrides --severity when set).")
    p.add_argument("--limit", type=int, default=None, help="Cap the number of episodes prepared.")
    p.add_argument("--apply", action="store_true", help="Actually modify files (default: dry run).")
    p.add_argument("--run", action="store_true",
                   help="After preparing, invoke ./loop.sh --feed <tag> for each affected feed.")
    p.add_argument("--no-restore", action="store_true",
                   help="Skip mp3 restore (use when mp3s are already in downloads/<feed>/).")
    return p.parse_args(argv)


def select_rows(audit_path: Path, severities: set[str], feeds: set[str] | None,
                min_pct: float | None, limit: int | None) -> list[dict]:
    """Read the audit CSV (worst-first) and return the rows to reprocess."""
    if not audit_path.exists():
        sys.exit(f"✗ Audit file not found: {audit_path}. Generate it first (loop scan).")
    rows = []
    with open(audit_path, newline="") as fh:
        for r in csv.DictReader(fh):
            pct = float(r["loop_pct"])
            feed = r["feed"]
            if feeds and feed not in feeds:
                continue
            if min_pct is not None:
                if pct < min_pct:
                    continue
            elif r["severity"] not in severities:
                continue
            rows.append({"pct": pct, "severity": r["severity"], "feed": feed,
                         "path": r["path_under_transcripts"]})
    rows.sort(key=lambda x: x["pct"], reverse=True)  # worst first
    if limit is not None:
        rows = rows[:limit]
    return rows


def stem_from_path(path_under_transcripts: str, feed: str) -> str:
    """'<feed>/<stem>.md' -> '<stem>'."""
    name = Path(path_under_transcripts).name  # <stem>.md
    return name[:-3] if name.endswith(".md") else name


def main():
    args = parse_args()
    severities = {s.strip() for s in args.severity.split(",") if s.strip()}
    bad = severities - set(SEVERITIES)
    if bad and args.min_pct is None:
        sys.exit(f"✗ Unknown severity: {', '.join(bad)}. Choices: {','.join(SEVERITIES)}")
    feeds_filter = {f.strip() for f in args.feed.split(",")} if args.feed else None

    config = ss.load_config(REPO / args.config)
    rows = select_rows(REPO / args.audit, severities, feeds_filter, args.min_pct, args.limit)
    if not rows:
        print("Nothing matched the filter. Nothing to do.")
        return

    mode = "APPLY" if args.apply else "DRY RUN"
    crit = f"loop_pct >= {args.min_pct}" if args.min_pct is not None else f"severity in {sorted(severities)}"
    print(f"[{mode}] {len(rows)} transcript(s) selected — {crit}"
          + (f", feeds={sorted(feeds_filter)}" if feeds_filter else "") + "\n")

    by_feed: dict[str, list[str]] = defaultdict(list)   # feed -> stems to drop from .processed
    affected_feeds: set[str] = set()
    n_restored = n_present = n_missing = n_md = n_chunks = 0
    missing_backup: list[str] = []

    def act(msg: str):
        print(("  " if args.apply else "  [dry] ") + msg)

    for r in rows:
        feed, pct = r["feed"], r["pct"]
        stem = stem_from_path(r["path"], feed)
        feed_cfg = ss.feed_cfg_for(config, feed)
        dl_dir = REPO / "downloads" / feed
        tr_dir = REPO / "transcripts" / feed
        mp3_dest = dl_dir / f"{stem}.mp3"
        print(f"• [{pct:4.1f}%] {feed} / {stem}")

        # 1. Restore mp3 (mtime-preserving) unless already present or opted out.
        if not args.no_restore:
            if mp3_dest.exists():
                n_present += 1
                act(f"mp3 already in downloads/ — keep")
            else:
                media_dir = ss._media_dir_path(feed, feed_cfg)
                backup_mp3 = (media_dir / f"{stem}.mp3") if media_dir else None
                if backup_mp3 and backup_mp3.exists():
                    act(f"restore mp3 (cp -p) from {backup_mp3}")
                    if args.apply:
                        dl_dir.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(backup_mp3, mp3_dest)  # copy2 preserves mtime -> diarize cache stays valid
                    n_restored += 1
                else:
                    where = backup_mp3 if backup_mp3 else "(no backup configured)"
                    act(f"⚠ no backup mp3 at {where} — will need re-download")
                    missing_backup.append(f"{feed} / {stem}")
                    n_missing += 1

        # 2. Invalidate dedup state so the pipeline redoes it.
        md = tr_dir / f"{stem}.md"
        if md.exists():
            act(f"delete corrupted transcript {md.name}")
            if args.apply:
                md.unlink()
            n_md += 1
        chunks = tr_dir / ".chunks" / stem
        if chunks.is_dir():
            act(f"delete stale chunk checkpoint .chunks/{stem}/")
            if args.apply:
                shutil.rmtree(chunks)
            n_chunks += 1
        by_feed[feed].append(stem)
        affected_feeds.add(feed)

    # Drop stems from each feed's .processed in one rewrite per feed.
    for feed, stems in by_feed.items():
        proc = REPO / "transcripts" / feed / ".processed"
        if not proc.exists():
            continue
        drop = set(stems)
        lines = [ln.strip() for ln in proc.read_text(encoding="utf-8").splitlines() if ln.strip()]
        kept = [ln for ln in lines if ln not in drop]
        removed = len(lines) - len(kept)
        act(f"strip {removed} entry(ies) from {feed}/.processed")
        if args.apply:
            proc.write_text(("\n".join(kept) + "\n") if kept else "", encoding="utf-8")

    # Summary.
    print("\n── Summary ──")
    print(f"  selected:           {len(rows)}")
    print(f"  mp3 already present: {n_present}")
    print(f"  mp3 restored:        {n_restored}")
    print(f"  mp3 missing backup:  {n_missing}")
    print(f"  transcripts deleted: {n_md}")
    print(f"  chunk dirs cleared:  {n_chunks}")
    if missing_backup:
        print("\n  ⚠ No backup mp3 (re-download needed before transcribe):")
        for m in missing_backup:
            print(f"      {m}")

    loop_cmds = [f"./loop.sh --feed {f}" for f in sorted(affected_feeds)]
    if not args.apply:
        print("\nDry run only — nothing changed. Re-run with --apply to execute.")
        print("Then transcribe the affected feeds with:")
        for c in loop_cmds:
            print(f"  {c}")
        return

    if args.run:
        print("\nRunning transcription for affected feeds...")
        for feed in sorted(affected_feeds):
            print(f"\n$ ./loop.sh --feed {feed}")
            subprocess.run([str(REPO / "loop.sh"), "--feed", feed], cwd=REPO)
    else:
        print("\nPrepared. Now transcribe the affected feeds:")
        for c in loop_cmds:
            print(f"  {c}")


if __name__ == "__main__":
    main()
