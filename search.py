#!/usr/bin/env python3
"""Hybrid search over the local index built by build_index.py.

Keyword (SQLite FTS5 / BM25) and meaning (Qwen3-Embedding cosine) results are
merged with reciprocal-rank fusion. Keyword search is what finds exact names,
model versions and figures ("Muse Spark 1.3", "20 GW"); vector search is what
finds a paraphrase ("benchmark leakage" for "eval contamination").

  .venv-search/bin/python search.py "agent escaped its sandbox" --since 2026-07-01
  .venv-search/bin/python search.py '"20 GW"' --mode keyword --source 'transcripts/*'
  .venv-search/bin/python search.py "Sonnet 5.5 free tier" --sort date --json

Results are one passage per document (best-scoring chunk), cited as
path:start_line-end_line so they can be opened and checked. They are leads to
read, never evidence on their own.

Blind runs: when SEARCH_BLIND_RUN=<run_dir> is set (the forward-trends
analysis sets it), the briefs corpus (weekly issues + story log) is refused
until <run_dir>/freeze.txt exists, so search can't become a way around the
blind extraction and trend passes.
"""

import argparse
import json
import os
import re
import sqlite3
import sys
from functools import lru_cache
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
INDEX_DIR = ROOT / "search_index"
DB_PATH = INDEX_DIR / "index.db"
RRF_K = 60
POOL = 200          # candidates taken from each ranker before fusion
# Keyword ranks count half in the fusion. Natural-language queries OR their
# terms, so BM25's pool fills with loose matches that, at equal weight, pushed
# real vector hits out of the top 10 (replay of the 2026-09 verification:
# 13/16 at 1.0, 14/16 at 0.5; at 0.3 the exact-figure case, "108x" vs "108×",
# dropped out). Tuned on 16 items, so a default, not an optimum.
KW_WEIGHT = 0.5
# Fusion rewards documents both rankers find, so a BM25 #1 that the vectors
# miss (an exact name or figure buried in a digest) could drop out of hybrid
# results entirely. The top KW_PIN keyword documents always stay in, taking
# the last slots if they didn't place on their own.
KW_PIN = 3


class BlindPhaseError(RuntimeError):
    pass


def check_blind(corpora: list[str]) -> None:
    run = os.environ.get("SEARCH_BLIND_RUN")
    if run and "briefs" in corpora and not (Path(run) / "freeze.txt").exists():
        raise BlindPhaseError(
            f"briefs corpus is closed until {run}/freeze.txt exists (SEARCH_BLIND_RUN is set)")


@lru_cache(maxsize=1)
def _db() -> sqlite3.Connection:
    if not DB_PATH.exists():
        sys.exit(f"no index at {DB_PATH}; run build_index.py first")
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)


@lru_cache(maxsize=1)
def _chunk_meta() -> dict:
    """Per-chunk doc fields as arrays, aligned to vectors.npy rows."""
    ids = np.load(INDEX_DIR / "vector_ids.npy")
    rows = dict(((cid, (corpus, source, date, path)) for cid, corpus, source, date, path in _db().execute(
        "SELECT c.id, d.corpus, d.source, d.date, d.path FROM chunks c JOIN docs d ON d.id=c.doc_id")))
    meta = [rows.get(int(i), ("", "", "", "")) for i in ids]
    return {
        "ids": ids,
        "corpus": np.array([m[0] for m in meta]),
        "date": np.array([m[2] for m in meta]),
        "path": np.array([m[3] for m in meta]),
    }


@lru_cache(maxsize=1)
def _vectors() -> np.ndarray:
    return np.load(INDEX_DIR / "vectors.npy", mmap_mode="r")


@lru_cache(maxsize=1)
def _model():
    import torch
    from sentence_transformers import SentenceTransformer
    model_name = json.loads((INDEX_DIR / "meta.json").read_text())["model"]
    return SentenceTransformer(model_name, device="mps" if torch.backends.mps.is_available() else "cpu",
                               model_kwargs={"torch_dtype": torch.float16})


def fts_query(q: str) -> str:
    """User text → FTS5 MATCH expression.

    Double-quoted spans stay phrases ("20 GW"); everything else becomes OR'd
    terms, so a natural-language query ranks by BM25 instead of requiring
    every word.
    """
    phrases = re.findall(r'"([^"]+)"', q)
    rest = re.sub(r'"[^"]+"', " ", q)
    terms = [t for t in re.findall(r"[\w.$%-]+", rest, re.UNICODE) if len(t) > 1]
    parts = ['"' + p.replace('"', "") + '"' for p in phrases]
    parts += ['"' + t.strip(".-") + '"' for t in terms if t.strip(".-")]
    if phrases and not terms:
        return " AND ".join(parts)
    return " OR ".join(parts)


def _filters_sql(corpora, since, until, sources) -> tuple[str, list]:
    sql, args = [f"d.corpus IN ({','.join('?' * len(corpora))})"], list(corpora)
    if since:
        sql.append("d.date >= ?"); args.append(since)
    if until:
        sql.append("d.date <= ?"); args.append(until)
    if sources:
        sql.append("(" + " OR ".join("d.path GLOB ?" for _ in sources) + ")")
        args += [s if "/" in s else f"*/{s}/*" for s in sources]
    return " AND ".join(sql), args


def keyword_rank(q, corpora, since, until, sources) -> list[int]:
    where, args = _filters_sql(corpora, since, until, sources)
    expr = fts_query(q)
    if not expr:
        return []
    rows = _db().execute(
        f"""SELECT c.id FROM chunks_fts f JOIN chunks c ON c.id=f.rowid JOIN docs d ON d.id=c.doc_id
            WHERE chunks_fts MATCH ? AND {where} ORDER BY bm25(chunks_fts) LIMIT ?""",
        [expr, *args, POOL]).fetchall()
    return [r[0] for r in rows]


def vector_rank(q, corpora, since, until, sources) -> list[int]:
    import fnmatch
    m = _chunk_meta()
    mask = np.isin(m["corpus"], corpora)
    if since:
        mask &= m["date"] >= since
    if until:
        mask &= m["date"] <= until
    if sources:
        pats = [s if "/" in s else f"*/{s}/*" for s in sources]
        uniq = np.unique(m["path"][mask])
        ok = {p for p in uniq if any(fnmatch.fnmatch(p, pat) for pat in pats)}
        mask &= np.isin(m["path"], list(ok))
    rows = np.nonzero(mask)[0]
    if not len(rows):
        return []
    qv = _model().encode([q], prompt_name="query", normalize_embeddings=True)[0].astype(np.float32)
    scores = np.asarray(_vectors()[rows], dtype=np.float32) @ qv
    top = rows[np.argsort(-scores)[:POOL]]
    return [int(m["ids"][i]) for i in top]


def search(q: str, *, corpora=("raw",), since=None, until=None, sources=(), mode="hybrid",
           k=10, per_doc=1, sort="score") -> list[dict]:
    corpora = list(corpora)
    check_blind(corpora)
    ranked = {}
    if mode in ("hybrid", "keyword"):
        ranked["keyword"] = keyword_rank(q, corpora, since, until, sources)
    if mode in ("hybrid", "vector"):
        ranked["vector"] = vector_rank(q, corpora, since, until, sources)
    score: dict[int, float] = {}
    via: dict[int, set] = {}
    for name, ids in ranked.items():
        w = KW_WEIGHT if name == "keyword" and mode == "hybrid" else 1.0
        for r, cid in enumerate(ids):
            score[cid] = score.get(cid, 0.0) + w / (RRF_K + r + 1)
            via.setdefault(cid, set()).add(name)
    if not score:
        return []
    order = sorted(score, key=score.get, reverse=True)
    rows = {r[0]: r for r in _db().execute(
        f"""SELECT c.id, d.path, d.date, d.source, d.title, d.url, c.start_line, c.end_line, c.text
            FROM chunks c JOIN docs d ON d.id=c.doc_id WHERE c.id IN ({','.join('?' * len(order))})""",
        order)}
    def entry(cid):
        r = rows[cid]
        return {"path": r[1], "date": r[2], "source": r[3], "title": r[4], "url": r[5],
                "lines": f"{r[6]}-{r[7]}", "score": round(score[cid], 5),
                "via": "+".join(sorted(via[cid])), "text": r[8]}

    out, per = [], {}
    for cid in order:
        r = rows[cid]
        if per.get(r[1], 0) >= per_doc:
            continue
        per[r[1]] = per.get(r[1], 0) + 1
        out.append(entry(cid))
        if len(out) >= k:
            break
    if mode == "hybrid":
        pins, pin_paths = [], set()
        for cid in ranked["keyword"]:
            if rows[cid][1] not in pin_paths:
                pin_paths.add(rows[cid][1])
                pins.append(cid)
            if len(pins) >= KW_PIN:
                break
        placed = {o["path"] for o in out}
        missing = [cid for cid in pins if rows[cid][1] not in placed]
        if missing:
            out = out[:max(0, k - len(missing))] + [entry(cid) for cid in missing]
    if sort == "date":
        out.sort(key=lambda x: x["date"])
    return out


def snippet(text: str, q: str, width: int = 280) -> str:
    terms = [t.lower() for t in re.findall(r"[\w$%]+", q) if len(t) > 2]
    low = text.lower()
    hits = [low.find(t) for t in terms if low.find(t) >= 0]
    start = max(0, min(hits) - width // 3) if hits else 0
    s = text[start:start + width].replace("\n", " ")
    return ("…" if start else "") + s + ("…" if start + width < len(text) else "")


def main() -> None:
    ap = argparse.ArgumentParser(description="Hybrid search over news/, transcripts/ and briefs.")
    ap.add_argument("query")
    ap.add_argument("--corpus", choices=["raw", "briefs", "all"], default="raw")
    ap.add_argument("--since", help="YYYY-MM-DD (doc date, inclusive)")
    ap.add_argument("--until", help="YYYY-MM-DD (doc date, inclusive)")
    ap.add_argument("--source", action="append", default=[],
                    help="source folder name (openai) or path glob (transcripts/*); repeatable")
    ap.add_argument("--mode", choices=["hybrid", "keyword", "vector"], default="hybrid")
    ap.add_argument("-k", type=int, default=10)
    ap.add_argument("--per-doc", type=int, default=1, help="max passages per document")
    ap.add_argument("--sort", choices=["score", "date"], default="score")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    corpora = ["raw", "briefs"] if a.corpus == "all" else [a.corpus]
    try:
        res = search(a.query, corpora=corpora, since=a.since, until=a.until, sources=a.source,
                     mode=a.mode, k=a.k, per_doc=a.per_doc, sort=a.sort)
    except BlindPhaseError as e:
        sys.exit(f"refused: {e}")
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=1))
        return
    for i, r in enumerate(res, 1):
        print(f"{i:>2}. {r['date']}  {r['path']}:{r['lines']}  [{r['via']}]")
        print(f"    {snippet(r['text'], a.query)}")


if __name__ == "__main__":
    main()
