"""Stage [3] EXTRACT: one LLM call per meeting -> structured facts (semantic memory).

Why an LLM here: deciding what counts as a decision, a proposal, a commitment or an
objection in messy speech needs language understanding. Code can't do that.
Why NOT the LLM for the rest: every fact's quote is verified by the citation gate
(code), ids/teams/order come from the database (code).

"People refer back to things without naming them": each meeting is extracted with
the team's facts from EARLIER meetings as context, so "let's go with that one" in
meeting c can be resolved to "the rubber case" from meeting b.

Resumable: meetings.extracted = 1 is set last, per meeting.
Usage:  python extract.py [TEAM ...]     (no args = all teams)
"""
import sqlite3
import sys
from typing import Literal

from pydantic import BaseModel, Field

import db
import llm
from citations import check_citation, n_words

FactType = Literal["decision", "proposal", "change", "action", "followup", "number", "disagreement"]


class Fact(BaseModel):
    type: FactType
    topic: str = Field(description="2-4 word lowercase topic, reused consistently, e.g. 'case material', 'energy source', 'lcd screen', 'selling price', 'budget total'")
    statement: str = Field(description="one self-contained sentence: who, what, and why if given")
    speaker: str = Field(description="speaker letter A-D who said it")
    turn_idx: int = Field(description="the [number] of the turn the quote comes from")
    quote: str = Field(description="at least 8 consecutive words copied EXACTLY from that one turn")
    owner: str | None = Field(default=None, description="for action: letter of who will do it")
    value: float | None = Field(default=None, description="for number: the numeric value, e.g. 12.5")
    unit: str | None = Field(default=None, description="for number: e.g. 'euro', 'percent'")


class Role(BaseModel):
    speaker: str
    role: Literal["project manager", "industrial designer", "user interface designer", "marketing"]
    turn_idx: int


class MeetingFacts(BaseModel):
    roles: list[Role]
    facts: list[Fact]


SYSTEM = """You extract memory from a transcript of a product-design meeting (a team of four designing a TV remote control).
Return JSON only. Be precise and faithful: never invent anything not said.

Extract 10-30 facts of these types:
- decision: something the team agreed or settled on
- proposal: someone suggests an option (record WHO)
- change: a decision from an earlier meeting is replaced, reversed or dropped (say what replaced what)
- action: someone commits to do/find out something (owner = their letter)
- followup: someone reports back on an earlier action or question (what was the answer)
- number: costs, prices, totals, budgets, percentages, ages (fill value + unit)
- disagreement: an objection or pushback, with who and why

Rules:
- speaker and owner are letters A-D exactly as labelled.
- turn_idx is the [number] shown before the turn.
- quote: copy at least 8 consecutive words EXACTLY from that single turn, keeping the original spelling, punctuation and filler (um, uh). Never paraphrase, never join two turns.
- topic: short and consistent across meetings (reuse earlier topics from context when it is the same thing).
- Prefer facts about features, components, materials, energy/battery, costs, price, target group, design, budget cuts.
- roles: who is project manager / industrial designer / user interface designer / marketing, if the transcript makes it clear (often from introductions or presentations)."""


def format_turns(turns: list[sqlite3.Row]) -> str:
    return "\n".join(f"[{t['turn_idx']}] {t['speaker']}: {t['text']}" for t in turns)


def earlier_context(conn: sqlite3.Connection, team: str, letter: str) -> str:
    rows = conn.execute("SELECT meeting_id, type, topic, statement FROM facts WHERE team = ? "
                        "AND letter < ? AND type IN ('decision','change','action','proposal','number') "
                        "ORDER BY letter, fact_id", (team, letter)).fetchall()
    if not rows:
        return "(this is the team's first meeting in the corpus)"
    return "\n".join(f"- {r['meeting_id']} [{r['type']}] {r['topic']}: {r['statement']}" for r in rows[:120])


def store(conn: sqlite3.Connection, meeting: sqlite3.Row, out: MeetingFacts) -> tuple[int, int]:
    """Gate every quote; repair from the turn text if needed. Returns (kept, repaired)."""
    mid, team = meeting["meeting_id"], meeting["team"]
    kept = repaired = 0
    for f in out.facts:
        res = check_citation(conn, mid, f.quote, team)
        quote, turn_idx = res["quote"], res["turn_idx"]
        if not res["ok"]:
            # Fallback: the model pointed at a turn; use that turn's own words.
            t = conn.execute("SELECT text FROM turns WHERE meeting_id=? AND turn_idx=?",
                             (mid, f.turn_idx)).fetchone()
            if t is None or n_words(t["text"]) < 6:
                continue                      # unverifiable fact: dropped, never stored
            quote, turn_idx = " ".join(t["text"].split()[:30]), f.turn_idx
            repaired += 1
        speaker = conn.execute("SELECT speaker FROM turns WHERE meeting_id=? AND turn_idx=?",
                               (mid, turn_idx)).fetchone()["speaker"]  # trust the DB, not the model
        conn.execute(
            "INSERT INTO facts (meeting_id, team, letter, type, topic, statement, speaker, owner,"
            " turn_idx, quote, value, unit) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (mid, team, meeting["letter"], f.type, f.topic.lower().strip(), f.statement, speaker,
             f.owner, turn_idx, quote, f.value, f.unit))
        kept += 1
    for r in out.roles:
        conn.execute("INSERT OR IGNORE INTO speaker_roles (team, speaker, role, meeting_id, turn_idx)"
                     " VALUES (?,?,?,?,?)", (team, r.speaker.strip()[:1].upper(), r.role, mid, r.turn_idx))
    return kept, repaired


def extract_meeting(conn: sqlite3.Connection, meeting: sqlite3.Row) -> str:
    turns = conn.execute("SELECT turn_idx, speaker, text FROM turns WHERE meeting_id = ? AND "
                         "is_filler = 0 ORDER BY turn_idx", (meeting["meeting_id"],)).fetchall()
    prompt = (f"Meeting {meeting['meeting_id']} (team {meeting['team']}, meeting {meeting['letter']} of a-d).\n\n"
              f"Facts already known from this team's EARLIER meetings:\n"
              f"{earlier_context(conn, meeting['team'], meeting['letter'])}\n\n"
              f"TRANSCRIPT (filler turns removed):\n{format_turns(turns)}")
    out = llm.generate_json("fast", prompt, schema=MeetingFacts, system=SYSTEM,
                            max_output_tokens=8192, purpose=f"extract:{meeting['meeting_id']}")
    with conn:
        conn.execute("DELETE FROM facts WHERE meeting_id = ?", (meeting["meeting_id"],))
        kept, repaired = store(conn, meeting, out)
        conn.execute("UPDATE meetings SET extracted = 1 WHERE meeting_id = ?", (meeting["meeting_id"],))
    return f"{len(out.facts)} facts, {kept} kept ({repaired} quotes repaired), {len(out.roles)} roles"


def extract_all(conn: sqlite3.Connection, teams: list[str] | None = None) -> None:
    db.init_db(conn)
    q = "SELECT * FROM meetings WHERE parsed = 1 AND extracted = 0"
    args: list = []
    if teams:
        q += f" AND team IN ({','.join('?' * len(teams))})"
        args = teams
    todo = conn.execute(q + " ORDER BY team, letter", args).fetchall()  # a before b before c ...
    for i, m in enumerate(todo, 1):
        try:
            print(f"  [{i}/{len(todo)}] {m['meeting_id']}: {extract_meeting(conn, m)}", flush=True)
        except llm.QuotaExhausted:
            raise
        except Exception as e:  # one bad meeting must not kill the run; it stays extracted=0
            print(f"  [{i}/{len(todo)}] {m['meeting_id']}: FAILED {type(e).__name__}: {str(e)[:150]}")
    n = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
    print(f"Done. {n} facts in store.")


if __name__ == "__main__":
    try:
        extract_all(db.connect(), sys.argv[1:] or None)
    except llm.QuotaExhausted as e:
        print(f"Stopped cleanly: {e}")
