"""Search over the memory store.

  keyword_search  BM25 over single TURNS   - exact lookups ("titanium", "twelve Euros")
  chunk_keywords  BM25 over CHUNKS         - keyword ranking in the same unit as vectors
  vector_search   cosine over CHUNKS       - meaning ("remote getting lost" ~ "whistle to find it")
  hybrid_search   both chunk rankings fused with RRF  <- what the agent will use

Usage:
  python search.py "how did they stop the remote getting lost?" ES2005
  python search.py --compare "how did they stop the remote getting lost?" ES2005
"""
import re
import sqlite3
import sys

import numpy as np

import db
import llm
from ingest import normalize_acronyms

RRF_K = 60  # standard constant from the original RRF paper (Cormack et al., 2009)


# ================================================================== keywords
def to_fts_query(query: str) -> str | None:
    """Turn free text into a safe FTS5 query.

    FTS5 has its own query language where ? - " : * ( ) mean something, so raw
    questions like "what's the cost?" would crash. We keep only the words, quote
    each one, and join with OR: a passage matching more (and rarer) words gets a
    better BM25 score, instead of AND throwing away anything that misses one word.
    """
    words = re.findall(r"\w+", normalize_acronyms(query).lower())
    if not words:
        return None
    return " OR ".join(f'"{w}"' for w in dict.fromkeys(words))  # dedupe, keep order


def keyword_search(conn: sqlite3.Connection, query: str, team: str | None = None,
                   meeting: str | None = None, limit: int = 8,
                   skip_filler: bool = True) -> list[dict]:
    """Best-matching single turns, best first. Text returned is the ORIGINAL."""
    fts_query = to_fts_query(query)
    if fts_query is None:
        return []
    rows = conn.execute(
        """
        SELECT t.turn_id, t.meeting_id, t.team, t.letter, t.turn_idx, t.speaker,
               t.start_sec, t.text, bm25(turns_fts) AS score
        FROM turns_fts
        JOIN turns t ON t.turn_id = turns_fts.turn_id
        WHERE turns_fts MATCH ?
          AND (? IS NULL OR t.team = ?)
          AND (? IS NULL OR t.meeting_id = ?)
          AND (? = 0 OR t.is_filler = 0)
        ORDER BY score          -- SQLite's bm25() is negative: lower = better match
        LIMIT ?
        """,
        (fts_query, team, team, meeting, meeting, int(skip_filler), limit),
    ).fetchall()
    return [dict(r) for r in rows]


def chunk_keywords(conn: sqlite3.Connection, query: str, team: str | None = None,
                   meeting: str | None = None, limit: int = 30) -> list[int]:
    """BM25 over chunks -> chunk_ids, best first."""
    fts_query = to_fts_query(query)
    if fts_query is None:
        return []
    rows = conn.execute(
        """
        SELECT c.chunk_id FROM chunks_fts
        JOIN chunks c ON c.chunk_id = chunks_fts.chunk_id
        WHERE chunks_fts MATCH ?
          AND (? IS NULL OR c.team = ?) AND (? IS NULL OR c.meeting_id = ?)
        ORDER BY bm25(chunks_fts) LIMIT ?
        """,
        (fts_query, team, team, meeting, meeting, limit),
    ).fetchall()
    return [r["chunk_id"] for r in rows]


# =================================================================== vectors
class VectorIndex:
    """All chunk vectors in one numpy matrix, loaded once per process.

    ~2,700 x 768 floats = 8 MB: brute-force search is a single matrix multiply and
    takes milliseconds. At 10,000 meetings (~200k chunks, ~600 MB) you'd switch to an
    approximate-nearest-neighbour index (sqlite-vec, pgvector, FAISS); the interface
    (query in, ranked chunk_ids out) stays the same.
    """

    def __init__(self, conn: sqlite3.Connection):
        rows = conn.execute("SELECT v.chunk_id, c.team, c.meeting_id, v.vec FROM chunk_vectors v "
                            "JOIN chunks c USING (chunk_id) ORDER BY v.chunk_id").fetchall()
        self.ids = np.array([r["chunk_id"] for r in rows], dtype=np.int64)
        self.teams = np.array([r["team"] for r in rows])
        self.meetings = np.array([r["meeting_id"] for r in rows])
        self.matrix = (np.vstack([np.frombuffer(r["vec"], dtype=np.float32) for r in rows])
                       if rows else np.zeros((0, 1), dtype=np.float32))

    def search(self, query_vec: np.ndarray, team: str | None = None,
               meeting: str | None = None, limit: int = 30) -> list[int]:
        if len(self.ids) == 0:
            return []
        mask = np.ones(len(self.ids), dtype=bool)
        if team:
            mask &= self.teams == team
        if meeting:
            mask &= self.meetings == meeting
        idx = np.flatnonzero(mask)
        # Vectors are length-1, so the dot product IS the cosine similarity.
        sims = self.matrix[idx] @ query_vec
        best = idx[np.argsort(-sims)[:limit]]
        return self.ids[best].tolist()


_index: VectorIndex | None = None


def vector_index(conn: sqlite3.Connection) -> VectorIndex:
    global _index
    if _index is None:
        _index = VectorIndex(conn)
    return _index


def embed_query(query: str) -> np.ndarray:
    # RETRIEVAL_QUERY (not _DOCUMENT): places the question near passages that answer it.
    vec = np.asarray(llm.embed([normalize_acronyms(query)], task_type="RETRIEVAL_QUERY",
                               purpose="search:query")[0], dtype=np.float32)
    return vec / (np.linalg.norm(vec) or 1)


def vector_search(conn: sqlite3.Connection, query: str, team: str | None = None,
                  meeting: str | None = None, limit: int = 30) -> list[int]:
    return vector_index(conn).search(embed_query(query), team, meeting, limit)


# ==================================================================== hybrid
def rrf(rankings: dict[str, list[int]], k: int = RRF_K) -> list[tuple[int, float, dict]]:
    """Reciprocal Rank Fusion: score(chunk) = sum over rankings of 1 / (k + rank).

    Why ranks, not raw scores? BM25 scores (-12.3) and cosine similarities (0.71)
    are on different scales and can't be added. Ranks can. A chunk ranked high by
    BOTH methods wins; a chunk found by only one still gets in. k=60 softens the gap
    between rank 1 and rank 2 so one method can't dominate.
    """
    scores: dict[int, float] = {}
    ranks: dict[int, dict] = {}
    for name, ids in rankings.items():
        for rank, cid in enumerate(ids, start=1):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank)
            ranks.setdefault(cid, {})[name] = rank
    ordered = sorted(scores.items(), key=lambda kv: -kv[1])
    return [(cid, s, ranks[cid]) for cid, s in ordered]


def chunk_passage(conn: sqlite3.Connection, chunk_id: int) -> dict:
    """A chunk as the agent will see it: ORIGINAL turn text with citable turn ids."""
    c = conn.execute("SELECT * FROM chunks WHERE chunk_id = ?", (chunk_id,)).fetchone()
    turns = conn.execute("SELECT turn_idx, speaker, text FROM turns WHERE meeting_id = ? "
                         "AND turn_idx BETWEEN ? AND ? AND is_filler = 0 ORDER BY turn_idx",
                         (c["meeting_id"], c["start_idx"], c["end_idx"])).fetchall()
    return {"chunk_id": chunk_id, "meeting_id": c["meeting_id"], "team": c["team"],
            "letter": c["letter"], "start_idx": c["start_idx"], "end_idx": c["end_idx"],
            "turns": [dict(t) for t in turns]}


def hybrid_search(conn: sqlite3.Connection, query: str, team: str | None = None,
                  meeting: str | None = None, limit: int = 6) -> list[dict]:
    """Keyword + meaning, fused. Each result says which method(s) found it.

    Graceful degradation: if the embedding quota is used up (free tier: 1,000 texts
    a day) or vectors aren't built yet, fall back to keyword-only instead of failing.
    """
    rankings = {"bm25": chunk_keywords(conn, query, team, meeting)}
    try:
        rankings["vector"] = vector_search(conn, query, team, meeting)
    except (llm.QuotaExhausted, llm.MissingKey):
        pass   # no quota / no key for a NEW query: keyword-only, still works
    fused = rrf(rankings)
    results = []
    for cid, score, ranks in fused[:limit]:
        p = chunk_passage(conn, cid)
        p.update(score=round(score, 4), ranks=ranks)
        results.append(p)
    return results


def format_passage(p: dict, max_turns: int | None = None) -> str:
    """Render a passage with citable ids, e.g. '[ES2005b#214] C: Lost, yeah.'"""
    lines = [f"[{p['meeting_id']}#{t['turn_idx']}] {t['speaker']}: {t['text']}"
             for t in p["turns"][:max_turns]]
    return "\n".join(lines)


if __name__ == "__main__":
    args = sys.argv[1:]
    compare = "--compare" in args
    args = [a for a in args if a != "--compare"]
    q = args[0] if args else "how did they stop the remote getting lost?"
    team = args[1] if len(args) > 1 else None
    conn = db.connect()

    def show(title, ids):
        print(f"\n=== {title}")
        for cid in ids[:3]:
            p = chunk_passage(conn, cid)
            print(f"-- {p['meeting_id']} turns {p['start_idx']}-{p['end_idx']}")
            print(format_passage(p, max_turns=6))

    if compare:
        show("BM25 only", chunk_keywords(conn, q, team))
        try:
            show("Vectors only", vector_search(conn, q, team))
        except (llm.QuotaExhausted, llm.MissingKey) as e:
            print(f"\n=== Vectors only: skipped ({e})")
    print(f"\n=== HYBRID (RRF) for: {q!r}  team={team}")
    for p in hybrid_search(conn, q, team, limit=4):
        print(f"-- {p['meeting_id']} turns {p['start_idx']}-{p['end_idx']}  "
              f"rrf={p['score']}  ranks={p['ranks']}")
        print(format_passage(p, max_turns=6))
