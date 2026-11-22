"""Stage [4] TIMELINE: how each team's decisions changed over time (temporal memory).

This is how the agent "knows what is current". For every team and topic we store
the history (oldest first, each step pointing at a fact with a verified quote), the
CURRENT state as of the team's latest meeting, and a status:
    settled  - decided and never changed
    changed  - replaced/reversed at least once (history shows the chain)
    dropped  - the feature/idea was abandoned
    open     - discussed but never decided

Model vs code:
  LLM  - judging that "rubber" in meeting c REPLACES "wood" from meeting a, or that
         "that one" refers back to an earlier proposal. That is language reasoning.
  code - ordering meetings a->d, checking every fact_id exists and belongs to the team,
         sorting history chronologically, storing. Order is a fact, not a judgement.

One LLM pass per team (35 calls total). Incremental at scale: when a new meeting
arrives, only that team's timeline is rebuilt.

Usage:  python timeline.py [TEAM ...]      python timeline.py --show TS3009
"""
import json
import sqlite3
import sys
from typing import Literal

from pydantic import BaseModel, Field

import db
import llm


class Step(BaseModel):
    meeting_id: str
    state: str = Field(description="what was proposed/decided at this point, short")
    fact_id: int = Field(description="the #id of the fact supporting this step")


class TopicTimeline(BaseModel):
    topic: str
    history: list[Step]
    current: str = Field(description="state as of the team's LAST meeting")
    status: Literal["settled", "changed", "dropped", "open"]
    open_items: str | None = Field(default=None, description="unresolved actions (with owner letter) or disagreements on this topic, if any")


class TeamTimeline(BaseModel):
    topics: list[TopicTimeline]


SYSTEM = """You build the decision timeline for one product-design team from facts extracted from their meetings (a, b, c, d in time order).
Group facts into topics (features, materials, energy source, costs/budget, price, target group, shape/look, components...).
For each topic: list the history oldest first (each step cites one fact #id), then the CURRENT state as of the team's last meeting.
- A later decision replaces an earlier one: status "changed" and the history must show the chain (e.g. "titanium back" -> "titanium-coloured plastic").
- Abandoned features: status "dropped". Never decided: "open". Decided and unchanged: "settled".
- Follow-ups answer earlier actions: put the answer in the history. Actions never answered and objections never resolved go in open_items (with speaker letters).
- Only use the facts given; cite real #ids. Return JSON only. Aim for 6-15 topics."""


def facts_text(conn: sqlite3.Connection, team: str, upto: str = "z") -> str:
    rows = conn.execute("SELECT fact_id, meeting_id, type, topic, speaker, owner, statement, value, unit "
                        "FROM facts WHERE team = ? AND letter <= ? ORDER BY letter, fact_id",
                        (team, upto)).fetchall()
    lines = []
    for r in rows:
        extra = f" owner={r['owner']}" if r["owner"] else ""
        extra += f" value={r['value']:g} {r['unit'] or ''}" if r["value"] is not None else ""
        lines.append(f"#{r['fact_id']} [{r['meeting_id']}] {r['type']} | {r['topic']} | "
                     f"speaker {r['speaker']}{extra}: {r['statement']}")
    return "\n".join(lines)


def build_team(conn: sqlite3.Connection, team: str, upto: str = "z", key: str | None = None) -> int:
    """upto='c' builds the timeline as it stood after meeting c (used for the brief),
    stored under `key` (e.g. 'TS3012@c') so it never mixes with the full timeline."""
    key = key or team
    text = facts_text(conn, team, upto)
    if not text:
        return 0
    out = llm.generate_json("fast", f"Team {team}. Facts in meeting order:\n{text}",
                            schema=TeamTimeline, system=SYSTEM, max_output_tokens=8192,
                            purpose=f"timeline:{key}")
    valid = {r["fact_id"]: r for r in conn.execute(
        "SELECT fact_id, meeting_id, letter FROM facts WHERE team = ? AND letter <= ?", (team, upto))}
    with conn:
        conn.execute("DELETE FROM timeline WHERE team = ?", (key,))
        for t in out.topics:
            # code, not the model, guarantees: real fact ids, this team only, time order
            steps = [s for s in t.history if s.fact_id in valid]
            steps.sort(key=lambda s: (valid[s.fact_id]["letter"], s.fact_id))
            if not steps:
                continue
            hist = [{"meeting_id": valid[s.fact_id]["meeting_id"], "state": s.state,
                     "fact_id": s.fact_id} for s in steps]
            conn.execute("INSERT OR REPLACE INTO timeline VALUES (?,?,?,?,?,?)",
                         (key, t.topic.lower().strip(), t.current, json.dumps(hist), t.status, t.open_items))
    return len(out.topics)


def render(conn: sqlite3.Connection, team: str, topic: str | None = None,
           with_quotes: bool = True) -> str:
    """Human/agent-readable timeline. Each step carries its verified quote, so the
    agent can cite straight from it."""
    q = "SELECT * FROM timeline WHERE team = ?"
    args = [team]
    if topic:
        q += " AND (topic LIKE ? OR current LIKE ?)"
        args += [f"%{topic.lower()}%", f"%{topic.lower()}%"]
    rows = conn.execute(q + " ORDER BY topic", args).fetchall()
    if not rows:
        return f"(no timeline entries for {team}{' / ' + topic if topic else ''})"
    out = []
    for r in rows:
        out.append(f"## {r['topic']}  [{r['status'].upper()}]  current: {r['current']}")
        for s in json.loads(r["history"]):
            f = conn.execute("SELECT speaker, quote FROM facts WHERE fact_id = ?", (s["fact_id"],)).fetchone()
            line = f"   {s['meeting_id']}: {s['state']}"
            if with_quotes and f:
                line += f'  (Speaker {f["speaker"]}: "{f["quote"]}")'
            out.append(line)
        if r["open_items"]:
            out.append(f"   OPEN: {r['open_items']}")
    return "\n".join(out)


def build_all(conn: sqlite3.Connection, teams: list[str] | None = None) -> None:
    db.init_db(conn)
    teams = teams or [r[0] for r in conn.execute("SELECT DISTINCT team FROM facts ORDER BY team")]
    for i, team in enumerate(teams, 1):
        try:
            print(f"  [{i}/{len(teams)}] {team}: {build_team(conn, team)} topics", flush=True)
        except llm.QuotaExhausted:
            raise
        except Exception as e:
            print(f"  [{i}/{len(teams)}] {team}: FAILED {type(e).__name__}: {str(e)[:150]}")


if __name__ == "__main__":
    conn = db.connect()
    if len(sys.argv) > 2 and sys.argv[1] == "--show":
        print(render(conn, sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None))
    else:
        try:
            build_all(conn, sys.argv[1:] or None)
        except llm.QuotaExhausted as e:
            print(f"Stopped cleanly: {e}")
