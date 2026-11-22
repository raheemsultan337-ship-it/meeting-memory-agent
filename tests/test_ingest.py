"""Step 1 tests: parsing, normalisation, ingest counts, keyword search.

Run:  python -m pytest -v
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import db  # noqa: E402
from ingest import ingest_all, is_filler, normalize_acronyms, parse_file  # noqa: E402
from search import keyword_search, to_fts_query  # noqa: E402

T = config.TRANSCRIPTS_DIR


@pytest.fixture(scope="module")
def conn(tmp_path_factory):
    """A fresh store built from all transcripts, in a temp folder (never touches memory.db)."""
    c = db.connect(tmp_path_factory.mktemp("db") / "test.db")
    ingest_all(c)
    return c


def test_normalize_acronyms():
    assert normalize_acronyms("an L_C_D_ and a T_V_") == "an lcd and a tv"
    assert normalize_acronyms("the L_C_ display") == "the lc display"
    assert normalize_acronyms("no acronyms here") == "no acronyms here"


def test_is_filler():
    assert is_filler("Mm-hmm.")
    assert is_filler("Okay, yeah.")
    assert not is_filler("Okay, so the budget is twelve Euros")
    assert not is_filler("Titanium.")


def test_parse_file_first_turn():
    meta, turns = parse_file(T / "TS3009a.txt")
    assert meta == {"meeting_id": "TS3009a", "team": "TS3009", "letter": "a"}
    first = turns[0]
    assert first["turn_idx"] == 0
    assert first["speaker"] == "D"
    assert first["start_sec"] == 8
    assert first["text"] == "I dunno. Throwing away my toothpick."


def test_letter_comes_from_header_not_file_order():
    # IS1002 has no meeting a, so its first file must still be letter b.
    meta, _ = parse_file(T / "IS1002b.txt")
    assert meta["letter"] == "b"


def test_full_ingest_counts(conn):
    assert conn.execute("SELECT COUNT(*) FROM meetings WHERE parsed = 1").fetchone()[0] == 138
    n_turns = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
    assert n_turns == 48148
    assert conn.execute("SELECT COUNT(*) FROM turns_fts").fetchone()[0] == n_turns


def test_reingest_is_idempotent(conn):
    ingest_all(conn)  # second run must skip everything
    assert conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 48148
    assert conn.execute("SELECT COUNT(*) FROM turns_fts").fetchone()[0] == 48148


def test_search_finds_acronym(conn):
    hits = keyword_search(conn, "LCD", team="TS3006")
    assert hits, "acronym normalisation failed: 'LCD' should match L_C_D_"
    assert "L_C_D_" in " ".join(h["text"] for h in hits)  # original text is returned


def test_search_team_filter(conn):
    hits = keyword_search(conn, "titanium", team="TS3009")
    assert hits
    assert all(h["meeting_id"].startswith("TS3009") for h in hits)


def test_search_survives_punctuation(conn):
    assert to_fts_query("what's the cost?") is not None
    keyword_search(conn, 'what\'s the "cost"? -price (euros)*')  # must not raise
    assert keyword_search(conn, "?!") == []
