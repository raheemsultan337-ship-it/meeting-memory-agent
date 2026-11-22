"""Step 3 tests: chunking, RRF fusion, vector search mechanics. No API key needed.

Run:  python -m pytest -v tests/test_index.py
"""
import sqlite3
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
from index import MAX_WORDS, OVERLAP_TURNS, build_chunks, chunk_all, normalize  # noqa: E402
from ingest import ingest_meeting  # noqa: E402
import config  # noqa: E402
from search import VectorIndex, chunk_keywords, chunk_passage, rrf  # noqa: E402


def fake_turns(word_counts):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE t (turn_idx, speaker, search_text)")
    for i, n in enumerate(word_counts):
        conn.execute("INSERT INTO t VALUES (?, ?, ?)", (i, "ABCD"[i % 4], " ".join(["w"] * n)))
    return conn.execute("SELECT * FROM t ORDER BY turn_idx").fetchall()


def test_chunks_cover_every_turn_and_overlap():
    turns = fake_turns([30] * 40)                    # 1,200 words
    chunks = build_chunks(turns)
    covered = set()
    for c in chunks:
        covered.update(range(c["start_idx"], c["end_idx"] + 1))
    assert covered == set(range(40))                 # nothing lost
    for a, b in zip(chunks, chunks[1:]):
        assert b["start_idx"] == a["end_idx"] - OVERLAP_TURNS + 1   # 2-turn overlap


def test_chunks_respect_max_words_except_single_long_turn():
    chunks = build_chunks(fake_turns([100, 100, 100, 900, 50]))
    for c in chunks:
        if c["start_idx"] != c["end_idx"]:
            assert c["n_words"] <= MAX_WORDS or c["end_idx"] == 3   # the 900-word turn
    assert any(c["start_idx"] <= 3 <= c["end_idx"] for c in chunks)


def test_chunk_text_keeps_speakers():
    chunks = build_chunks(fake_turns([5, 5]))
    assert chunks[0]["text"].startswith("A: w")


def test_rrf_rewards_agreement():
    fused = rrf({"bm25": [1, 2, 3], "vector": [3, 4, 1]})
    order = [cid for cid, _, _ in fused]
    assert order[0] in (1, 3)                        # found by both -> top
    assert set(order[:2]) == {1, 3}
    assert 4 in order and 2 in order                 # found by one -> still included
    ranks = dict((cid, r) for cid, _, r in fused)
    assert ranks[3] == {"bm25": 3, "vector": 1}


def test_normalize_gives_unit_vectors():
    m = normalize(np.array([[3.0, 4.0], [0.0, 0.0]], dtype=np.float32))
    assert np.allclose(np.linalg.norm(m[0]), 1.0)
    assert np.allclose(m[1], 0.0)                    # zero vector doesn't crash


def test_vector_index_ranks_by_cosine_and_filters_team(tmp_path):
    conn = db.connect(tmp_path / "v.db")
    db.init_db(conn)
    conn.execute("INSERT INTO meetings (meeting_id, team, letter) VALUES ('X1a','X1','a'), ('Y1a','Y1','a')")
    vecs = {1: ("X1", "X1a", [1, 0]), 2: ("X1", "X1a", [0.6, 0.8]), 3: ("Y1", "Y1a", [1, 0])}
    for cid, (team, mid, v) in vecs.items():
        conn.execute("INSERT INTO chunks VALUES (?, ?, ?, 'a', 0, 0, 1, 'x')", (cid, mid, team))
        conn.execute("INSERT INTO chunk_vectors VALUES (?, ?)", (cid, np.array(v, dtype=np.float32).tobytes()))
    idx = VectorIndex(conn)
    q = np.array([1, 0], dtype=np.float32)
    assert idx.search(q) [:2] in ([1, 3], [3, 1])     # both exact matches first
    assert idx.search(q, team="X1") == [1, 2]        # filter applied, then ranked


def test_real_transcript_chunking(tmp_path):
    conn = db.connect(tmp_path / "r.db")
    db.init_db(conn)
    ingest_meeting(conn, config.TRANSCRIPTS_DIR / "TS3009c.txt")
    chunk_all(conn)
    n = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    assert n > 5
    # keyword search over chunks works, and a passage shows ORIGINAL text with turn ids
    ids = chunk_keywords(conn, "titanium", team="TS3009")
    assert ids
    p = chunk_passage(conn, ids[0])
    assert any("titanium" in t["text"].lower() for t in p["turns"])
    # re-chunking is idempotent
    conn.execute("UPDATE meetings SET chunked = 0")
    chunk_all(conn)
    assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == n
