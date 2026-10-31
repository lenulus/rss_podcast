#!/usr/bin/env python3
"""Build/refresh the local hybrid search index over news/, transcripts/ and the briefs.

Two corpora, kept apart so the forward-trends blind phase can be enforced
(see search.py):
  raw     — news/<source>/*.md and transcripts/<show>/*.md
  briefs  — reports/the-week-in-ai-*.md (+ any at the repo root) and
            reports/story-log.yaml

Storage, all under search_index/:
  index.db         SQLite: docs, chunks, chunks_fts (FTS5/BM25), vec (fp16 BLOBs)
  vectors.npy      fp16 matrix of every chunk embedding, rebuilt after each run
  vector_ids.npy   chunk id per matrix row
  meta.json        model name, dims, chunking params

Incremental: a doc is re-chunked and re-embedded only when its size or mtime
changed; docs whose file is gone are dropped. Runs in .venv-search (it needs
sentence-transformers + torch, which the transcription venv deliberately
doesn't carry):

  .venv-search/bin/python build_index.py            # refresh everything
  .venv-search/bin/python build_index.py --no-embed # FTS only (fast)
  .venv-search/bin/python build_index.py --since 2026-07-01 --until 2026-09-30
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
INDEX_DIR = ROOT / "search_index"
DB_PATH = INDEX_DIR / "index.db"
MODEL = "Qwen/Qwen3-Embedding-0.6B"
CHUNK_WORDS = 450       # ~600 tokens
OVERLAP_WORDS = 50
MAX_SEQ = 1024

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.S)
DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")


def corpus_files() -> list[tuple[str, str, Path]]:
    """[(corpus, source, path)] for every indexable file."""
    out = []
    for top in ("news", "transcripts"):
        for p in sorted((ROOT / top).glob("*/*.md")):
            out.append(("raw", p.parent.name, p))
    briefs = sorted((ROOT / "reports").glob("the-week-in-ai-*.md")) + sorted(ROOT.glob("the-week-in-ai-*.md"))
    for p in briefs:
        out.append(("briefs", "the-week-in-ai", p))
    story_log = ROOT / "reports" / "story-log.yaml"
    if story_log.exists():
        out.append(("briefs", "story-log", story_log))
    return out


def parse_doc(path: Path) -> tuple[dict, list[str], int]:
    """(frontmatter fields we keep, body lines, line number of first body line)."""
    text = path.read_text(encoding="utf-8", errors="replace")
    fm: dict = {}
    first_line = 1
    m = FRONTMATTER_RE.match(text)
    if m:
        for line in m.group(1).splitlines():
            k, _, v = line.partition(":")
            k = k.strip()
            if k in ("title", "url", "link", "published", "date", "show"):
                fm[k] = v.strip().strip('"').strip("'")
        first_line = text[: m.end()].count("\n") + 1
        text = text[m.end():]
    return fm, text.splitlines(), first_line


def chunk_lines(lines: list[str], first_line: int) -> list[tuple[int, int, str]]:
    """[(start_line, end_line, text)] of ~CHUNK_WORDS words, overlapping.

    Lines are kept whole where possible so a chunk can be cited by line range
    (the forward-trends verifier cites 'file line N'). A single line longer
    than the budget (transcript turns run past 12k chars) is split by words
    and every piece carries that line's number.
    """
    units: list[tuple[int, list[str]]] = []
    for i, line in enumerate(lines):
        words = line.split()
        if not words:
            continue
        ln = first_line + i
        for j in range(0, len(words), CHUNK_WORDS):
            units.append((ln, words[j:j + CHUNK_WORDS]))
    chunks = []
    cur: list[tuple[int, list[str]]] = []
    n = 0
    for u in units:
        if cur and n + len(u[1]) > CHUNK_WORDS:
            chunks.append(cur)
            # carry trailing units up to OVERLAP_WORDS into the next chunk
            keep, kn = [], 0
            for v in reversed(cur):
                if kn + len(v[1]) > OVERLAP_WORDS:
                    break
                keep.insert(0, v)
                kn += len(v[1])
            cur, n = keep, kn
        cur.append(u)
        n += len(u[1])
    if cur:
        chunks.append(cur)
    return [(c[0][0], c[-1][0], " ".join(w for _, ws in c for w in ws)) for c in chunks]


def open_db() -> sqlite3.Connection:
    INDEX_DIR.mkdir(exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS docs(
            id INTEGER PRIMARY KEY, path TEXT UNIQUE, corpus TEXT, source TEXT,
            date TEXT, title TEXT, url TEXT, size INTEGER, mtime REAL);
        CREATE TABLE IF NOT EXISTS chunks(
            id INTEGER PRIMARY KEY, doc_id INTEGER, ord INTEGER,
            start_line INTEGER, end_line INTEGER, text TEXT);
        CREATE INDEX IF NOT EXISTS chunks_doc ON chunks(doc_id);
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
            text, content='chunks', content_rowid='id', tokenize='unicode61');
        CREATE TABLE IF NOT EXISTS vec(chunk_id INTEGER PRIMARY KEY, v BLOB);
    """)
    return db


def drop_doc(db: sqlite3.Connection, doc_id: int) -> None:
    rows = db.execute("SELECT id, text FROM chunks WHERE doc_id=?", (doc_id,)).fetchall()
    for cid, text in rows:
        db.execute("INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES('delete', ?, ?)", (cid, text))
        db.execute("DELETE FROM vec WHERE chunk_id=?", (cid,))
    db.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
    db.execute("DELETE FROM docs WHERE id=?", (doc_id,))


def sync_docs(db: sqlite3.Connection) -> int:
    """Add/replace changed docs and their chunks; drop vanished ones. Returns docs touched."""
    files = corpus_files()
    seen = set()
    known = {p: (i, s, m) for i, p, s, m in db.execute("SELECT id, path, size, mtime FROM docs")}
    touched = 0
    for corpus, source, path in files:
        rel = str(path.relative_to(ROOT))
        seen.add(rel)
        st = path.stat()
        k = known.get(rel)
        if k and k[1] == st.st_size and abs(k[2] - st.st_mtime) < 1e-6:
            continue
        if k:
            drop_doc(db, k[0])
        fm, lines, first = parse_doc(path)
        dm = DATE_PREFIX_RE.match(path.name) or DATE_PREFIX_RE.match(path.stem.replace("the-week-in-ai-", ""))
        date = dm.group(1) if dm else (fm.get("published") or fm.get("date") or "")[:10]
        cur = db.execute(
            "INSERT INTO docs(path, corpus, source, date, title, url, size, mtime) VALUES(?,?,?,?,?,?,?,?)",
            (rel, corpus, source, date, fm.get("title", path.stem), fm.get("url") or fm.get("link", ""),
             st.st_size, st.st_mtime))
        doc_id = cur.lastrowid
        for o, (a, b, text) in enumerate(chunk_lines(lines, first)):
            c = db.execute("INSERT INTO chunks(doc_id, ord, start_line, end_line, text) VALUES(?,?,?,?,?)",
                           (doc_id, o, a, b, text))
            db.execute("INSERT INTO chunks_fts(rowid, text) VALUES(?, ?)", (c.lastrowid, text))
        touched += 1
        if touched % 500 == 0:
            db.commit()
            print(f"  chunked {touched} docs…", flush=True)
    for rel, (doc_id, _, _) in known.items():
        if rel not in seen:
            drop_doc(db, doc_id)
            touched += 1
    db.commit()
    return touched


def embed_missing(db: sqlite3.Connection, since: str = "", until: str = "", batch: int = 256) -> int:
    """Embed chunks that have no vector yet, optionally only raw docs dated
    since..until (briefs are always included). A full build is ~120k chunks at
    ~15 chunks/s on an M2 Ultra (~2 h), so a range lets the window a run needs
    go first and the backlog follow."""
    import torch
    from sentence_transformers import SentenceTransformer
    todo = db.execute("""SELECT c.id, d.title, c.text FROM chunks c JOIN docs d ON d.id=c.doc_id
                         LEFT JOIN vec v ON v.chunk_id=c.id
                         WHERE v.chunk_id IS NULL
                           AND (d.corpus='briefs' OR (d.date >= ? AND d.date <= ?))
                         ORDER BY c.id""", (since or "0000", until or "9999")).fetchall()
    if not todo:
        return 0
    print(f"  embedding {len(todo)} chunks with {MODEL}…", flush=True)
    model = SentenceTransformer(MODEL, device="mps" if torch.backends.mps.is_available() else "cpu",
                                model_kwargs={"torch_dtype": torch.float16})
    model.max_seq_length = MAX_SEQ
    t0 = time.time()
    for i in range(0, len(todo), batch):
        part = todo[i:i + batch]
        # Title prefix gives a passage its document context ("Muse Spark 1.3" in a
        # chunk that only says "the model").
        texts = [f"{title}\n{text}" for _, title, text in part]
        emb = model.encode(texts, batch_size=32, normalize_embeddings=True).astype(np.float16)
        db.executemany("INSERT OR REPLACE INTO vec(chunk_id, v) VALUES(?, ?)",
                       [(cid, e.tobytes()) for (cid, _, _), e in zip(part, emb)])
        db.commit()
        done = i + len(part)
        rate = done / (time.time() - t0)
        print(f"    {done}/{len(todo)}  {rate:.0f}/s  eta {(len(todo) - done) / rate / 60:.1f} min", flush=True)
    return len(todo)


def export_vectors(db: sqlite3.Connection) -> None:
    rows = db.execute("SELECT chunk_id, v FROM vec ORDER BY chunk_id").fetchall()
    if not rows:
        return
    ids = np.array([r[0] for r in rows], dtype=np.int64)
    mat = np.frombuffer(b"".join(r[1] for r in rows), dtype=np.float16).reshape(len(rows), -1)
    np.save(INDEX_DIR / "vectors.npy", mat)
    np.save(INDEX_DIR / "vector_ids.npy", ids)
    (INDEX_DIR / "meta.json").write_text(json.dumps({
        "model": MODEL, "dims": int(mat.shape[1]), "chunk_words": CHUNK_WORDS,
        "overlap_words": OVERLAP_WORDS, "chunks": int(len(ids)),
        "built": time.strftime("%Y-%m-%dT%H:%M:%S")}, indent=2))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--no-embed", action="store_true", help="refresh docs/FTS only")
    ap.add_argument("--since", default="", help="embed only raw docs dated on/after YYYY-MM-DD")
    ap.add_argument("--until", default="", help="embed only raw docs dated on/before YYYY-MM-DD")
    args = ap.parse_args()
    db = open_db()
    t = time.time()
    n = sync_docs(db)
    docs, chunks = db.execute("SELECT (SELECT count(*) FROM docs), (SELECT count(*) FROM chunks)").fetchone()
    print(f"docs: {n} added/changed/removed; index holds {docs} docs, {chunks} chunks ({time.time() - t:.0f}s)")
    if not args.no_embed:
        e = embed_missing(db, args.since, args.until)
        print(f"embedded {e} new chunks")
        export_vectors(db)
    db.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('optimize')")
    db.commit()


if __name__ == "__main__":
    sys.exit(main())
