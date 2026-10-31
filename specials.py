#!/usr/bin/env python3
"""Build the per-topic input bundles for The Week in AI topic reports.

The monthly special-report job (prompts/specials/monthly-run-prompt-*-current.md)
calls this between its agent passes. Everything lives under
reports/specials/<RUN>/, where RUN is a finished forward-trends run:

  .venv-search/bin/python specials.py prepare RUN
      Check the run is ready, then write work/trends_summary.md (one line per
      trend) and work/section_candidates.json (each analysis.md section heading
      matched to a trend by its lineage counts) for the ownership agent.

  .venv-search/bin/python specials.py shared RUN
      Needs ownership.yaml with `sections` and `owns`. Writes
      work/shared_signals.jsonl: signals that reach more than one topic's
      inputs, so the ownership agent can assign them in `owned_events` and
      `mechanisms` before bundling.

  .venv-search/bin/python specials.py bundle RUN
      Needs the complete ownership.yaml. Writes work/bundles/<topic>/ with
      every input the writer prompt lists, and work/bundle_stats.json with the
      evidence floor and podcast share per topic.

ownership.yaml (written by the ownership agent, one per run):

  run: 2026-10
  sections: {<trend_id>: "<exact ### heading in analysis.md>"}
  owns: {<topic_id>: [<trend_id>, ...]}
  unassigned: [{trend: <trend_id>, reason: "..."}]
  owned_events: [{event: "...", owner: <topic_id>, match: "<regex>"}]
  mechanisms:   [{mechanism: "...", owner: <topic_id>, match: "<regex>"}]

A signal's owner is the first event or mechanism whose `match` regex hits its
claim or metrics; otherwise the one topic whose owned trends' canonical
lineages, supporting signals or bundled signals contain it, in that order;
otherwise it is shared by every topic that bundles it.
"""

import argparse
import collections
import json
import re
import shutil
import sys
from datetime import date, timedelta
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
FT = ROOT / "reports" / "forward-trends"
OUT = ROOT / "reports" / "specials"
TOPICS_GLOB = "topics-*-current.yaml"
WEEKLY_RE = re.compile(r"^the-week-in-ai-(\d{4}-\d{2}-\d{2})\.md$")
STRENGTH = {"strong": 3, "moderate": 2, "weak": 1}
COUNTS_RE = re.compile(r"(\d+) lineages \((\d+) first-party")


def die(msg: str) -> None:
    sys.exit(f"specials.py: {msg}")


def topics_file() -> Path:
    found = sorted((ROOT / "prompts" / "specials").glob(TOPICS_GLOB))
    if len(found) != 1:
        die(f"expected exactly one prompts/specials/{TOPICS_GLOB}, found {len(found)}")
    return found[0]


def load_run(run: str) -> dict:
    d = FT / run
    for f in ("trends.json", "analysis.md", "signals.jsonl", "verification.md"):
        if not (d / f).exists():
            die(f"{d / f} is missing; the forward-trends run is not complete")
    trends = json.loads((d / "trends.json").read_text())
    start, _, end = trends["_meta"]["window"].partition(" to ")
    sig = {}
    for line in (d / "signals.jsonl").open():
        s = json.loads(line)
        s["raw_ref"] = s["raw_ref"].removeprefix("c/")
        sig[s["signal_id"]] = s
    return {"dir": d, "trends": {t["trend_id"]: t for t in trends["trends"]},
            "order": [t["trend_id"] for t in trends["trends"]],
            "analysis": (d / "analysis.md").read_text().split("\n"),
            "signals": sig, "start": start.strip(), "end": end.strip()}


def analysis_sections(lines: list[str]) -> dict[str, str]:
    """### heading -> section text, for the trend sections (3 and 4) of analysis.md."""
    tops = [i for i, l in enumerate(lines) if l.startswith("## ")]
    out = {}
    for i, l in enumerate(lines):
        if not l.startswith("### "):
            continue
        parent = max((t for t in tops if t < i), default=-1)
        if parent < 0 or not re.match(r"## [34]\.", lines[parent]):
            continue
        end = next((j for j in range(i + 1, len(lines)) if lines[j].startswith(("### ", "## "))), len(lines))
        out[l[4:].strip()] = "\n".join(lines[i:end]).strip().rstrip("-").strip()
    return out


def watch_list(lines: list[str]) -> str:
    s = next((i for i, l in enumerate(lines) if l.startswith("## ") and "watch" in l.lower()), None)
    if s is None:
        return ""
    e = next((j for j in range(s + 1, len(lines)) if lines[j].startswith("## ")), len(lines))
    return "\n".join(lines[s:e]).strip()


def run_dir(run: str) -> Path:
    d = OUT / run
    (d / "work").mkdir(parents=True, exist_ok=True)
    return d


def prior_run(run: str) -> str:
    y, m = map(int, run.split("-"))
    return f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"


# ---------------------------------------------------------------- prepare

def cmd_prepare(run: str) -> None:
    latest = (FT / "LATEST").read_text().strip() if (FT / "LATEST").exists() else ""
    if latest != run:
        die(f"reports/forward-trends/LATEST is {latest!r}, not {run!r}; the run is not released yet")
    R = load_run(run)
    d = run_dir(run)
    secs = analysis_sections(R["analysis"])
    # Match each section to a trend by the "N lineages (M first-party" line,
    # tie-broken by tier and order; the ownership agent confirms or fixes it.
    cand = {}
    taken = set()
    for head, text in secs.items():
        m = COUNTS_RE.search(text)
        hits = [tid for tid, t in R["trends"].items() if m and tid not in taken
                and t.get("independent_lineage_count") == int(m.group(1))
                and t.get("first_party_or_measurement_lineages") == int(m.group(2))]
        cand[head] = hits
        if len(hits) == 1:
            taken.add(hits[0])
    (d / "work" / "section_candidates.json").write_text(json.dumps(cand, indent=1))
    lines = [f"# Trends in run {run} (window {R['start']} to {R['end']})\n",
             "trend_id | tier | status vs prior run | lineages (first-party or measured) | short title\n"]
    for tid in R["order"]:
        t = R["trends"][tid]
        lines.append(f"- `{tid}` | {t.get('tier')} | {t.get('status_vs_prior_run')} | "
                     f"{t.get('independent_lineage_count')} ({t.get('first_party_or_measurement_lineages')}) | "
                     f"{t.get('short_title')}"
                     + (f" | prior id: {t['prior_trend_id']}" if t.get("prior_trend_id") not in (None, tid) else ""))
    (d / "work" / "trends_summary.md").write_text("\n".join(lines) + "\n")
    unique = sum(1 for v in cand.values() if len(v) == 1)
    prev = OUT / prior_run(run) / "ownership.yaml"
    print(f"run {run} ready: {len(R['trends'])} trends, {len(secs)} analysis sections "
          f"({unique} matched to one trend by counts)")
    print(f"wrote {d / 'work' / 'trends_summary.md'} and section_candidates.json")
    print(f"prior ownership: {prev if prev.exists() else 'none (first run; seed from the topics file)'}")


# ---------------------------------------------------------------- ownership

def load_ownership(run: str, R: dict, need_registry: bool) -> dict:
    p = OUT / run / "ownership.yaml"
    if not p.exists():
        die(f"{p} is missing; run the ownership pass first")
    o = yaml.safe_load(p.read_text())
    T = yaml.safe_load(topics_file().read_text())
    topic_ids = [t["id"] for t in T["topics"]]
    errs = []
    owns = o.get("owns") or {}
    for tp in owns:
        if tp not in topic_ids:
            errs.append(f"owns: unknown topic {tp}")
    seen = collections.Counter(t for ts in owns.values() for t in ts)
    for tid, n in seen.items():
        if tid not in R["trends"]:
            errs.append(f"owns: unknown trend {tid}")
        if n > 1:
            errs.append(f"owns: {tid} is owned by {n} topics")
    unassigned = {u["trend"] for u in o.get("unassigned") or []}
    for tid, t in R["trends"].items():
        if tid not in seen and tid not in unassigned:
            errs.append(f"trend {tid} is neither owned nor listed in unassigned")
        if tid in unassigned and t.get("tier") == "material":
            errs.append(f"material trend {tid} must be owned")
    secs = analysis_sections(R["analysis"])
    for tid in seen:
        h = (o.get("sections") or {}).get(tid)
        if h not in secs:
            errs.append(f"sections: {tid} -> {h!r} is not a ### heading in analysis.md sections 3 or 4")
    if need_registry:
        for key, label in (("owned_events", "event"), ("mechanisms", "mechanism")):
            for e in o.get(key) or []:
                if e.get("owner") not in topic_ids:
                    errs.append(f"{key}: {e.get(label)!r} has unknown owner {e.get('owner')!r}")
                try:
                    re.compile(e.get("match") or "")
                except re.error as x:
                    errs.append(f"{key}: bad match regex for {e.get(label)!r}: {x}")
                if not e.get("match"):
                    errs.append(f"{key}: {e.get(label)!r} has no match regex")
    if errs:
        die("ownership.yaml has problems:\n  " + "\n  ".join(errs))
    o["_topics"] = T
    return o


def bundle_map(R: dict, o: dict) -> tuple[dict, dict, dict]:
    inb, canon, supp = (collections.defaultdict(set) for _ in range(3))
    for tp, tids in o["owns"].items():
        for tid in tids:
            t = R["trends"][tid]
            for s in t.get("supporting_signal_ids", []) + t.get("counter_signal_ids", []):
                inb[s].add(tp)
            for s in t.get("supporting_signal_ids", []):
                supp[s].add(tp)
            for L in t.get("canonical_lineages", []):
                if isinstance(L, dict):
                    for s in L.get("signal_ids", []):
                        canon[s].add(tp)
    return inb, canon, supp


def cmd_shared(run: str) -> None:
    R = load_run(run)
    o = load_ownership(run, R, need_registry=False)
    inb, canon, supp = bundle_map(R, o)
    rows = []
    for sid, tps in inb.items():
        if len(tps) < 2 or sid not in R["signals"]:
            continue
        s = R["signals"][sid]
        rows.append({"signal_id": sid, "topics": sorted(tps), "canonical_in": sorted(canon[sid]),
                     "supporting_in": sorted(supp[sid]), "date": s["date"], "provenance": s["provenance"],
                     "lineage_id": s.get("lineage_id"), "claim": s["claim"], "metrics": s.get("metrics", [])})
    rows.sort(key=lambda r: (r["lineage_id"] or "", r["date"]))
    p = OUT / run / "work" / "shared_signals.jsonl"
    p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    lin = collections.Counter(r["lineage_id"] for r in rows)
    print(f"{len(rows)} signals reach more than one topic, in {len(lin)} lineages; wrote {p}")


# ---------------------------------------------------------------- bundle

def signal_owner(s: dict, o: dict, inb, canon, supp) -> tuple[str | None, str]:
    txt = s["claim"] + " " + " ".join(map(str, s.get("metrics", [])))
    for key in ("owned_events", "mechanisms"):
        for e in o.get(key) or []:
            if re.search(e["match"], txt):
                return e["owner"], "registry" if key == "owned_events" else "mechanism"
    sid = s["signal_id"]
    for cand in (canon[sid], supp[sid], inb[sid]):
        if len(cand) == 1:
            return next(iter(cand)), "lineage"
        if len(cand) > 1:
            return "shared:" + ",".join(sorted(cand)), "shared"
    return None, "none"


def frontmatter(path: Path) -> dict:
    t = path.read_text(errors="replace")
    m = re.match(r"^---\n(.*?)\n---\n", t, re.S)
    fm = {}
    if m:
        for line in m.group(1).splitlines():
            k, _, v = line.partition(":")
            fm[k.strip()] = v.strip().strip('"').strip("'")
    return fm


def source_index(R: dict) -> dict[str, dict]:
    refs = sorted({s["raw_ref"] for s in R["signals"].values()})
    rows = {}
    for ref in refs:
        p = ROOT / ref
        if not p.exists():
            continue
        fm = frontmatter(p)
        rows[ref] = {"raw_ref": ref, "title": fm.get("title") or p.stem, "url": fm.get("url") or fm.get("link") or "",
                     "url_kind": "article" if ref.startswith("news/") else "episode",
                     "date": (fm.get("published") or fm.get("date") or p.name[:10])[:19],
                     "source": p.parent.name}
    # A transcript link shared by several episodes of one show is a show page, not an episode.
    by_show = collections.Counter((r["source"], r["url"]) for r in rows.values() if r["raw_ref"].startswith("transcripts/"))
    for r in rows.values():
        if r["raw_ref"].startswith("transcripts/") and (not r["url"] or by_show[(r["source"], r["url"])] > 1):
            r["url_kind"] = "show" if r["url"] else "none"
    return rows


def weeklies(start: str, end: str) -> list[Path]:
    last = (date.fromisoformat(end) + timedelta(days=7)).isoformat()
    out = []
    for p in sorted((ROOT / "reports").glob("the-week-in-ai-*.md")):
        m = WEEKLY_RE.match(p.name)
        if m and start <= m.group(1) <= last:
            out.append(p)
    return out


def weekly_digest(files: list[Path]) -> str:
    out = ["# Weekly issues in this window (readers have already read these)\n",
           "Each issue's dates, opening summary and section headings. Grep or search the full issues in weeklies/ for detail.\n"]
    for p in files:
        t = p.read_text()
        win = re.search(r"\*\*Window:\*\*\s*(.*)", t)
        m = re.search(r"## The 60-second version\n(.*?)\n---", t, re.S)
        heads = [h for h in re.findall(r"^## (.*)$", t, re.M) if "60-second" not in h]
        out.append(f"## {p.stem} ({win.group(1) if win else ''})\n\n{m.group(1).strip() if m else ''}\n\n"
                   f"Sections: {' | '.join(heads)}\n")
    return "\n".join(out)


def cmd_bundle(run: str) -> None:
    R = load_run(run)
    o = load_ownership(run, R, need_registry=True)
    T = o["_topics"]
    D = T["defaults"]
    names = {t["id"]: t["name"] for t in T["topics"]}
    secs = analysis_sections(R["analysis"])
    watch = watch_list(R["analysis"])
    inb, canon, supp = bundle_map(R, o)
    owner = {sid: signal_owner(R["signals"][sid], o, inb, canon, supp) for sid in inb if sid in R["signals"]}
    index = source_index(R)
    wk = weeklies(R["start"], R["end"])
    digest = weekly_digest(wk)
    prev = prior_run(run)
    corrections = ("# Known corrections\n\nThe forward-trends run's independent verification follows. Its high and "
                   "medium findings were fixed in analysis.md before release. Where any other input still disagrees "
                   "with a correction here, the correction wins.\n\n" + (R["dir"] / "verification.md").read_text())
    extra = OUT / run / "known_corrections.md"
    if extra.exists():
        corrections = extra.read_text() + "\n\n" + corrections
    base = OUT / run / "work" / "bundles"
    if base.exists():
        shutil.rmtree(base)
    stats = {}
    for tp in T["topics"]:
        tid_list = o["owns"].get(tp["id"], [])
        b = base / tp["id"]
        (b / "sources").mkdir(parents=True)
        (b / "weeklies").mkdir()

        def mine(sid, tp=tp):
            ow = owner[sid][0] or ""
            return ow == tp["id"] or (ow.startswith("shared:") and tp["id"] in ow.split(":", 1)[1].split(","))

        ex = [f"# Analysis excerpt for topic: {tp['name']}\n\nFrom reports/forward-trends/{run}/analysis.md "
              f"(window {R['start']} to {R['end']}, verified). Sections for this topic's owned trends, plus the "
              "run's watch list. Some sentences mention events or mechanisms the registry assigns to another "
              "topic; see do_not_restate.md.\n"]
        ex += [f"<!-- trend_id: {tid} -->\n" + secs[o["sections"][tid]] for tid in tid_list]
        ex.append(watch)
        (b / "analysis_excerpt.md").write_text("\n\n".join(ex) + "\n")
        (b / "dossiers.json").write_text(json.dumps([R["trends"][t] for t in tid_list], indent=1, ensure_ascii=False))

        ids = sorted({s for t in tid_list for s in R["trends"][t].get("supporting_signal_ids", [])
                      + R["trends"][t].get("counter_signal_ids", []) if s in R["signals"]})
        other = collections.Counter()
        sigs = []
        for sid in ids:
            ow, how = owner[sid]
            sigs.append({**R["signals"][sid], "owner_topic": ow, "owner_basis": how})
            if not mine(sid):
                other[ow] += 1
        (b / "signals.jsonl").write_text("".join(json.dumps(s, ensure_ascii=False) + "\n" for s in sigs))

        dn = ["# Do not restate\n\nThese events, mechanisms and figures belong to other topics. If one matters to "
              "your argument, use at most one sentence with no figures, naming the owning topic. A pointer replaces "
              "the analysis; it never follows a restatement.\n"]
        for key, label, title in (("owned_events", "event", "Registry events"), ("mechanisms", "mechanism", "Mechanisms")):
            dn.append(f"\n## {title} owned by other topics\n")
            dn += [f"- {e[label]}: owned by {names[e['owner']]}" for e in o.get(key) or [] if e["owner"] != tp["id"]]
            dn.append(f"\n## {title} owned by this topic\n")
            dn += [f"- {e[label]}" for e in o.get(key) or [] if e["owner"] == tp["id"]]
        dn.append("\n## Builder points owned by other topics (point to them; do not restate)\n")
        dn += [f"- {bp['point']}: owned by {names[bp['owner']]}" for bp in T["shared_builder_points"] if bp["owner"] != tp["id"]]
        own_bp = [bp["point"] for bp in T["shared_builder_points"] if bp["owner"] == tp["id"]]
        if own_bp:
            dn.append("\n## Builder points this topic owns\n")
            dn += [f"- {p}" for p in own_bp]
        dn.append(f"\n## Signals in your file owned elsewhere\n\n{sum(other.values())} of the signals in signals.jsonl "
                  f"have `owner_topic` set to another topic ({', '.join(f'{names.get(k, k)}: {v}' for k, v in other.most_common())}). "
                  "Do not take figures from them. Signals with `owner_topic` starting `shared:` may be used by any listed "
                  "topic, unless the figure belongs to a registry event or mechanism owned elsewhere.\n")
        (b / "do_not_restate.md").write_text("\n".join(dn))

        others = [f"- **{x['name']}** ({x['id']}): {x['decision_question']} Owns: "
                  + "; ".join(f"{t} ({R['trends'][t]['short_title']})" for t in o["owns"].get(x["id"], []))
                  for x in T["topics"] if x["id"] != tp["id"]]
        (b / "other_topics.md").write_text("# Other topics (pointers only)\n\n" + "\n".join(others) + "\n")

        td = {k: v for k, v in tp.items() if k != "seed_trends"}
        td["owns"] = tid_list
        td.update(run=run, window=f"{R['start']} to {R['end']}", audience=T["audience"],
                  minimum_evidence=D["minimum_evidence"], podcast_share_ceiling=D["podcast_share_ceiling"],
                  gate=D["gate"], target_words=D["target_words"],
                  ownership_rules=T["ownership_rules"], shared_builder_points=T["shared_builder_points"])
        (b / "topic.yaml").write_text(yaml.safe_dump(td, sort_keys=False, allow_unicode=True))
        (b / "known_corrections.md").write_text(corrections)
        (b / "source_index.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in index.values()))
        for p in wk:
            shutil.copy(p, b / "weeklies" / p.name)
        (b / "weekly_digest.md").write_text(digest)

        # Source pack: the strongest owned items, by summed signal strength.
        weight = collections.Counter()
        for s in sigs:
            if mine(s["signal_id"]) and s["raw_ref"] in index:
                weight[s["raw_ref"]] += STRENGTH.get(s.get("strength"), 1)
        ranked = [r for r, _ in weight.most_common()]
        pack = ([r for r in ranked if not r.startswith("transcripts/")][:D["source_pack"]["articles"]]
                + [r for r in ranked if r.startswith("transcripts/")][:D["source_pack"]["transcripts"]])
        lines = [f"# Source pack for {tp['name']}\n",
                 "Full text of this topic's strongest owned source items, in sources/ under the same path as "
                 "`raw_ref`. Grep them; do not read end to end.\n"]
        for ref in pack:
            dst = b / "sources" / ref
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(ROOT / ref, dst)
            si = index[ref]
            own = [s for s in sigs if s["raw_ref"] == ref and mine(s["signal_id"])]
            lines.append(f"## {si['title']}\n\n- file: sources/{ref}\n- outlet: {si['source']}\n- date: {si['date'][:10]}\n"
                         f"- url: {si['url'] or '(none)'} ({si['url_kind']})\n"
                         f"- provenance: {', '.join(sorted({s['provenance'] for s in own}))}\n"
                         f"- size: {dst.stat().st_size // 1000} KB\n- owned signals:\n"
                         + "\n".join(f"  - {s['signal_id']}: {s['claim'][:220]}" for s in own) + "\n")
        (b / "sources.md").write_text("\n".join(lines))

        prior = OUT / prev / f"topic-{tp['id']}-{prev}.md"
        if prior.exists():
            (b / "prior").mkdir()
            shutil.copy(prior, b / "prior" / prior.name)
            ledger = prior.with_suffix(".claims.jsonl")
            if ledger.exists():
                shutil.copy(ledger, b / "prior" / ledger.name)

        fp = sum(R["trends"][t].get("first_party_or_measurement_lineages", 0) for t in tid_list)
        sup = [R["signals"][s] for t in tid_list for s in R["trends"][t].get("supporting_signal_ids", []) if s in R["signals"]]
        pod = sum(1 for s in sup if s["provenance"] == "machine_transcript") / len(sup) if sup else 0.0
        stats[tp["id"]] = {"owns": tid_list, "signals": len(ids), "owned_or_shared": len(ids) - sum(other.values()),
                           "first_party_lineages": fp, "evidence_floor": D["minimum_evidence"]["first_party_or_measured_lineages"],
                           "below_floor": fp < D["minimum_evidence"]["first_party_or_measured_lineages"],
                           "podcast_share": round(pod, 3), "over_podcast_ceiling": pod > D["podcast_share_ceiling"],
                           "source_pack": len(pack), "prior": prior.exists()}
    (OUT / run / "work" / "bundle_stats.json").write_text(json.dumps(stats, indent=1))
    basis = collections.Counter(how for _, how in owner.values())
    print(f"bundles for {len(stats)} topics in {base}; {len(wk)} weekly issues; "
          f"{len(index)} indexed source items; signal owners by basis: {dict(basis)}")
    for k, v in stats.items():
        flags = [f for f, on in (("BELOW FLOOR", v["below_floor"]), ("podcast-heavy", v["over_podcast_ceiling"])) if on]
        print(f"  {k:20s} trends {len(v['owns'])}  signals {v['signals']:4d} (own/shared {v['owned_or_shared']:4d})  "
              f"FP lineages {v['first_party_lineages']:3d}  podcast {v['podcast_share']:.0%}  pack {v['source_pack']:2d}"
              + (f"  [{', '.join(flags)}]" if flags else ""))


def main() -> None:
    ap = argparse.ArgumentParser(description="Inputs for The Week in AI topic reports.")
    ap.add_argument("step", choices=["prepare", "shared", "bundle"])
    ap.add_argument("run", help="forward-trends run id, YYYY-MM")
    a = ap.parse_args()
    {"prepare": cmd_prepare, "shared": cmd_shared, "bundle": cmd_bundle}[a.step](a.run)


if __name__ == "__main__":
    main()
