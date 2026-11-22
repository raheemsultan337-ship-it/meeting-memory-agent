"""Citation gate tests: the rules the graders check automatically."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import db  # noqa: E402
from citations import check_citation, gate  # noqa: E402
from ingest import ingest_meeting  # noqa: E402


@pytest.fixture(scope="module")
def conn(tmp_path_factory):
    c = db.connect(tmp_path_factory.mktemp("c") / "c.db")
    db.init_db(c)
    for m in ("TS3009a", "TS3009c", "TS3009d"):
        ingest_meeting(c, config.TRANSCRIPTS_DIR / f"{m}.txt")
    return c


def real_turn(conn, meeting, min_words=12):
    return conn.execute("SELECT text FROM turns WHERE meeting_id=? AND "
                        "length(text)-length(replace(text,' ',''))>=? ORDER BY turn_idx LIMIT 1",
                        (meeting, min_words)).fetchone()["text"]


def test_exact_quote_valid_ignoring_case_and_spacing(conn):
    words = real_turn(conn, "TS3009c").split()[:8]
    quote = "  ".join(words).upper()
    r = check_citation(conn, "TS3009c", quote, team="TS3009")
    assert r["status"] == "valid"


def test_too_short_rejected(conn):
    assert check_citation(conn, "TS3009c", "No titanium.")["status"] == "rejected"


def test_wrong_team_or_meeting_rejected(conn):
    q = " ".join(real_turn(conn, "TS3009c").split()[:8])
    assert check_citation(conn, "TS3009c", q, team="ES2002")["status"] == "rejected"
    assert check_citation(conn, "XX9999a", q)["status"] == "rejected"


def test_near_miss_snaps_to_exact_words(conn):
    words = real_turn(conn, "TS3009c", 15).split()[:12]
    words.pop(5)                                 # model "drops" an um/uh-like word
    r = check_citation(conn, "TS3009c", " ".join(words))
    assert r["status"] in ("snapped", "valid")
    assert check_citation(conn, "TS3009c", r["quote"])["status"] == "valid"


def test_paraphrase_rejected(conn):
    r = check_citation(conn, "TS3009c", "the team decided the remote would be made of rubber and wood")
    assert r["status"] == "rejected"


def test_quote_spanning_two_turns_rejected(conn):
    t = conn.execute("SELECT text FROM turns WHERE meeting_id='TS3009d' ORDER BY turn_idx "
                     "LIMIT 2 OFFSET 100").fetchall()
    joined = " ".join(t[0]["text"].split()[-4:] + t[1]["text"].split()[:4])
    if len(joined.split()) >= 6:
        assert check_citation(conn, "TS3009d", joined)["status"] == "rejected"


def test_gate_requires_two_meetings(conn):
    qc = " ".join(real_turn(conn, "TS3009c").split()[:8])
    qd = " ".join(real_turn(conn, "TS3009d").split()[:8])
    ok, problems = gate(conn, [{"meeting_id": "TS3009c", "quote": qc}], team="TS3009")
    assert ok and any("ONE meeting" in p for p in problems)
    ok, problems = gate(conn, [{"meeting_id": "TS3009c", "quote": qc},
                               {"meeting_id": "TS3009d", "quote": qd}], team="TS3009")
    assert len(ok) == 2 and problems == []
