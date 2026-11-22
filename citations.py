"""Stage [6] CITATION GATE - plain code, never an LLM.

The graders check every quote automatically:
  * at least 6 words
  * copied exactly from the cited meeting, from WITHIN ONE speaker turn
  * case and spacing are ignored; punctuation is NOT
LLMs paraphrase, fix grammar, and stitch two turns together. Whether a string occurs
inside another string is a deterministic check, so it belongs in code.

Policy for each citation:
  valid      -> keep
  near-miss  -> "snap" it to the exact transcript words (fuzzy alignment)
  invalid    -> rejected, with a reason the agent can act on
An unverified quote is never written to answers.json.
"""
import re
import sqlite3

from rapidfuzz import fuzz

MIN_WORDS = 6
SNAP_THRESHOLD = 88   # 0-100 similarity needed to snap a near-miss to real words


def norm(s: str) -> str:
    """What the grader ignores: case and spacing. Punctuation is kept."""
    return re.sub(r"\s+", " ", s).strip().lower()


def n_words(s: str) -> int:
    return len(norm(s).split())


def _turns(conn: sqlite3.Connection, meeting_id: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT turn_idx, speaker, text FROM turns WHERE meeting_id = ? "
                        "ORDER BY turn_idx", (meeting_id,)).fetchall()


def find_exact(conn: sqlite3.Connection, meeting_id: str, quote: str) -> int | None:
    """turn_idx of the turn that contains the quote, or None."""
    q = norm(quote)
    for t in _turns(conn, meeting_id):
        if q in norm(t["text"]):
            return t["turn_idx"]
    return None


def snap(conn: sqlite3.Connection, meeting_id: str, quote: str) -> tuple[str, int] | None:
    """Find the closest real span inside ONE turn; return (exact words, turn_idx).

    Example: the model writes "we should go for the rubber case" but the turn says
    "we should go for the um rubber case". The alignment finds the real span and we
    return the transcript's own words instead.
    """
    q = norm(quote)
    best: tuple[float, str, int] | None = None
    for t in _turns(conn, meeting_id):
        text = norm(t["text"])
        if len(text) < len(q) * 0.6:
            continue
        a = fuzz.partial_ratio_alignment(q, text)
        if a.score >= SNAP_THRESHOLD and (best is None or a.score > best[0]):
            # widen to whole words so we never cut a word in half
            start, end = a.dest_start, a.dest_end
            while start > 0 and text[start - 1] != " ":
                start -= 1
            while end < len(text) and text[end] != " ":
                end += 1
            best = (a.score, text[start:end].strip(), t["turn_idx"])
    if best and n_words(best[1]) >= MIN_WORDS:
        return best[1], best[2]
    return None


def check_citation(conn: sqlite3.Connection, meeting_id: str, quote: str,
                   team: str | None = None) -> dict:
    """Returns {"ok", "quote", "turn_idx", "status", "reason"}.

    status: "valid" | "snapped" | "rejected"
    """
    row = conn.execute("SELECT team FROM meetings WHERE meeting_id = ?", (meeting_id,)).fetchone()
    if row is None:
        return {"ok": False, "quote": quote, "turn_idx": None, "status": "rejected",
                "reason": f"meeting {meeting_id!r} does not exist"}
    if team and row["team"] != team:
        return {"ok": False, "quote": quote, "turn_idx": None, "status": "rejected",
                "reason": f"{meeting_id} belongs to team {row['team']}, not {team}"}
    if n_words(quote) < MIN_WORDS:
        return {"ok": False, "quote": quote, "turn_idx": None, "status": "rejected",
                "reason": f"quote has {n_words(quote)} words; needs at least {MIN_WORDS}"}

    idx = find_exact(conn, meeting_id, quote)
    if idx is not None:
        return {"ok": True, "quote": quote.strip(), "turn_idx": idx, "status": "valid", "reason": ""}

    snapped = snap(conn, meeting_id, quote)
    if snapped:
        return {"ok": True, "quote": snapped[0], "turn_idx": snapped[1], "status": "snapped",
                "reason": "near-miss replaced with the exact transcript words"}
    return {"ok": False, "quote": quote, "turn_idx": None, "status": "rejected",
            "reason": f"quote not found inside any single turn of {meeting_id}; copy the "
                      f"exact words of one turn (no paraphrase, no joining two turns)"}


def gate(conn: sqlite3.Connection, citations: list[dict], team: str | None = None,
         max_citations: int = 3) -> tuple[list[dict], list[str]]:
    """Check a list of {"meeting_id", "quote"}. Returns (accepted citations, problems).

    'problems' is fed back to the agent so it can fix things on its next step.
    """
    accepted, problems, seen = [], [], set()
    for c in citations:
        res = check_citation(conn, c.get("meeting_id", ""), c.get("quote", ""), team)
        if res["ok"]:
            key = (c["meeting_id"], norm(res["quote"]))
            if key not in seen:
                seen.add(key)
                accepted.append({"meeting_id": c["meeting_id"], "quote": res["quote"]})
        else:
            problems.append(f"{c.get('meeting_id')}: {res['reason']}: {c.get('quote', '')[:80]!r}")
    accepted = accepted[:max_citations]
    if accepted and len({c["meeting_id"] for c in accepted}) < 2:
        problems.append("all citations come from ONE meeting; every question needs at least two "
                        "meetings of the team: add a citation from another meeting")
    if not accepted:
        problems.append("no valid citations")
    return accepted, problems
