"""Stage [2b] CHUNK + EMBED: turns -> chunks -> vectors, for meaning-based search.

Why chunks and not single turns?
  Most turns are a few words ("No titanium."). Embedded alone they carry almost
  no meaning. A window of conversation does: who proposed what, what was replied.

Why ~1,000 words (about 6 minutes of meeting)?
  The embedding free tier allows 1,000 TEXTS per day. 250-word chunks gave 3,334
  texts (3+ days of quota); 1,000-word chunks give ~780, which fits in one day
  AND leaves ~200 of the daily 1,000 for embedding search queries.
  Trade-off: a vector hit is less precise, so the agent narrows down with keyword
  search and read_turns. With paid quota you'd go back to ~250 words.

Both parts are resumable:
  chunking  -> per meeting, flag meetings.chunked = 1 last (same pattern as ingest)
  embedding -> only chunks without a vector are sent; vectors also live in cache.db

Usage:
  python index.py              chunk new meetings, then embed what's missing
  python index.py --rechunk    throw away all chunks + vectors and rebuild (after
                               changing chunk sizes or the embedding model)
"""
import sqlite3
import sys

import numpy as np

import db
import llm

TARGET_WORDS = 1000    # start a new chunk once we reach this
MAX_WORDS = 1400       # never grow beyond this (unless a single turn is longer)
OVERLAP_TURNS = 2      # carry the last 2 turns into the next chunk


def build_chunks(turns: list[sqlite3.Row]) -> list[dict]:
    """Group a meeting's non-filler turns into overlapping windows.

    Overlap matters: if a proposal ends one chunk and the reply starts the next,
    without overlap neither chunk contains the full exchange.
    """
    def n_words(t) -> int:
        return len(t["search_text"].split())

    chunks: list[dict] = []
    window: list[sqlite3.Row] = []   # turns in the chunk being built
    fresh = 0                        # how many of them are NOT in any earlier chunk

    def words() -> int:
        return sum(n_words(t) for t in window)

    def flush() -> None:
        nonlocal window, fresh
        if fresh == 0:               # only overlap turns: already saved, skip
            return
        chunks.append({
            "start_idx": window[0]["turn_idx"],
            "end_idx": window[-1]["turn_idx"],
            "n_words": words(),
            # Speaker labels stay in: "who said it" is part of the meaning.
            "text": "\n".join(f"{t['speaker']}: {t['search_text']}" for t in window),
        })
        window, fresh = window[-OVERLAP_TURNS:], 0   # carry overlap into next chunk

    for t in turns:
        if words() + n_words(t) > MAX_WORDS:
            flush()                  # window full: save it (if it has new turns)
            if words() + n_words(t) > MAX_WORDS:
                window = []          # overlap + this turn too big: drop the overlap
        window.append(t)
        fresh += 1
        if words() >= TARGET_WORDS:
            flush()
    flush()                          # the tail, if it has new turns
    return chunks


def chunk_all(conn: sqlite3.Connection) -> None:
    todo = conn.execute("SELECT meeting_id, team, letter FROM meetings "
                        "WHERE parsed = 1 AND chunked = 0 ORDER BY meeting_id").fetchall()
    for m in todo:
        turns = conn.execute("SELECT turn_idx, speaker, search_text FROM turns "
                             "WHERE meeting_id = ? AND is_filler = 0 ORDER BY turn_idx",
                             (m["meeting_id"],)).fetchall()
        with conn:  # one transaction per meeting, flag set last
            old = "SELECT chunk_id FROM chunks WHERE meeting_id = ?"
            conn.execute(f"DELETE FROM chunk_vectors WHERE chunk_id IN ({old})", (m["meeting_id"],))
            conn.execute(f"DELETE FROM chunks_fts WHERE chunk_id IN ({old})", (m["meeting_id"],))
            conn.execute("DELETE FROM chunks WHERE meeting_id = ?", (m["meeting_id"],))
            for c in build_chunks(turns):
                cur = conn.execute(
                    "INSERT INTO chunks (meeting_id, team, letter, start_idx, end_idx, n_words, text)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (m["meeting_id"], m["team"], m["letter"], c["start_idx"], c["end_idx"],
                     c["n_words"], c["text"]))
                conn.execute("INSERT INTO chunks_fts (text, chunk_id) VALUES (?, ?)",
                             (c["text"], cur.lastrowid))
            conn.execute("UPDATE meetings SET chunked = 1 WHERE meeting_id = ?", (m["meeting_id"],))
    n = conn.execute("SELECT COUNT(*), AVG(n_words), MAX(n_words) FROM chunks").fetchone()
    print(f"Chunked {len(todo)} meetings. {n[0]} chunks in store "
          f"(avg {n[1]:.0f} words, max {n[2]}).")


def normalize(vectors: np.ndarray) -> np.ndarray:
    """Scale every vector to length 1. Then cosine similarity is just a dot product,
    which makes search one fast matrix multiplication."""
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    return vectors / np.where(norms == 0, 1, norms)


def embed_all(conn: sqlite3.Connection, group: int = 500) -> None:
    """Embed every chunk that has no vector yet, saving after each group.

    If the daily quota runs out mid-way, llm.embed raises QuotaExhausted; every
    group saved before that is kept, and the next run continues from there.
    """
    total_missing = conn.execute("SELECT COUNT(*) FROM chunks c LEFT JOIN chunk_vectors v "
                                 "USING (chunk_id) WHERE v.chunk_id IS NULL").fetchone()[0]
    done = 0
    while True:
        rows = conn.execute("SELECT c.chunk_id, c.text FROM chunks c LEFT JOIN chunk_vectors v "
                            "USING (chunk_id) WHERE v.chunk_id IS NULL LIMIT ?", (group,)).fetchall()
        if not rows:
            break
        vecs = llm.embed([r["text"] for r in rows], task_type="RETRIEVAL_DOCUMENT",
                         purpose="index:chunks")
        mat = normalize(np.asarray(vecs, dtype=np.float32))
        with conn:
            conn.executemany("INSERT OR REPLACE INTO chunk_vectors (chunk_id, vec) VALUES (?, ?)",
                             [(r["chunk_id"], mat[i].tobytes()) for i, r in enumerate(rows)])
        done += len(rows)
        print(f"  embedded {done}/{total_missing}")
    print(f"Embeddings complete: {conn.execute('SELECT COUNT(*) FROM chunk_vectors').fetchone()[0]} vectors.")


def reset_index(conn: sqlite3.Connection) -> None:
    """Drop all chunks and vectors. Turns are untouched; old vectors stay in cache.db
    (keyed by model + text), so switching back later would cost nothing."""
    with conn:
        conn.execute("DELETE FROM chunk_vectors")
        conn.execute("DELETE FROM chunks_fts")
        conn.execute("DELETE FROM chunks")
        conn.execute("UPDATE meetings SET chunked = 0")
    print("Index reset: all chunks and vectors removed.")


def build_index(conn: sqlite3.Connection, rechunk: bool = False) -> None:
    db.init_db(conn)
    if rechunk:
        reset_index(conn)
    chunk_all(conn)
    embed_all(conn)


if __name__ == "__main__":
    try:
        build_index(db.connect(), rechunk="--rechunk" in sys.argv)
    except llm.QuotaExhausted as e:
        print(f"Stopped cleanly: {e}")
