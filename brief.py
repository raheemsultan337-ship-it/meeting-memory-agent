"""Stage [7] BRIEF: what Team TS3012 should read before their final meeting (d).

"Before the final meeting" means only meetings a-c may be used. That is enforced in
CODE (timeline built with upto='c', every citation must be from a TS3012 meeting
with letter < 'd'), not just asked for in the prompt.

Pipeline: timeline as of c (code+LLM) -> LLM writes structured points, each with
citations -> citation gate (code) -> rendered as plain text (code).

Usage:  python brief.py        prints the brief and saves brief.txt
"""
from pydantic import BaseModel, Field

import config
import db
import llm
import timeline
from citations import check_citation

TEAM, UPTO = "TS3012", "c"
KEY = f"{TEAM}@{UPTO}"


class Cite(BaseModel):
    meeting_id: str
    quote: str = Field(description="8-25 words copied exactly from one quote shown")


class Point(BaseModel):
    text: str = Field(description="one or two sentences; name speakers by letter and role")
    citations: list[Cite]


class Brief(BaseModel):
    decided: list[Point]
    changed: list[Point]
    open_actions: list[Point]
    open_disagreements: list[Point]


SYSTEM = """You write a pre-meeting brief for a product-design team (designing a TV remote) before their FINAL meeting.
You get their decision timeline and facts from meetings a-c, each with an exact quote.
Write four sections:
- decided: what has been decided (current state)
- changed: what changed along the way (from X to Y, and why if known)
- open_actions: actions promised and not yet reported back, with the owner (speaker letter + role)
- open_disagreements: objections or disagreements not yet resolved
3-7 points per section, concise. EVERY point needs 1-2 citations: meeting id + 8-25 words copied
exactly from one of the quotes given (you may shorten a quote but never change words). Only use what is given."""


def facts_for_brief(conn) -> str:
    rows = conn.execute("SELECT meeting_id, type, topic, speaker, owner, statement, quote FROM facts "
                        "WHERE team=? AND letter<=? AND type IN ('action','followup','disagreement','decision','change') "
                        "ORDER BY letter, fact_id", (TEAM, UPTO)).fetchall()
    return "\n".join(f"- {r['meeting_id']} {r['type']} ({r['topic']}) speaker {r['speaker']}"
                     f"{' owner ' + r['owner'] if r['owner'] else ''}: {r['statement']}\n"
                     f"    quote: \"{r['quote']}\"" for r in rows)


def roles(conn) -> str:
    return ", ".join(f"{r['speaker']} = {r['role']}" for r in
                     conn.execute("SELECT speaker, role FROM speaker_roles WHERE team=? ORDER BY speaker", (TEAM,)))


def make_brief(conn) -> str:
    timeline.build_team(conn, TEAM, upto=UPTO, key=KEY)
    prompt = (f"Team {TEAM}. Speaker roles: {roles(conn)}\n\n"
              f"TIMELINE after meetings a-c:\n{timeline.render(conn, KEY)}\n\n"
              f"FACTS (actions, follow-ups, disagreements, decisions) from meetings a-c:\n{facts_for_brief(conn)}")
    out = llm.generate_json("smart", prompt, schema=Brief, system=SYSTEM,
                            max_output_tokens=32768, purpose="brief:TS3012")

    sections = [("DECIDED SO FAR", out.decided), ("WHAT CHANGED ALONG THE WAY", out.changed),
                ("OPEN ACTIONS AND OWNERS", out.open_actions), ("OPEN DISAGREEMENTS", out.open_disagreements)]
    lines = [f"Brief for Team {TEAM} before the final meeting (based on meetings {TEAM}a to {TEAM}{UPTO}).", ""]
    dropped = 0
    for title, points in sections:
        lines.append(title)
        for p in points:
            cites = []
            for c in p.citations:
                if not c.meeting_id.startswith(TEAM) or c.meeting_id[-1] > UPTO:
                    continue                      # code-enforced: nothing from meeting d
                r = check_citation(conn, c.meeting_id, c.quote, TEAM)
                if r["ok"]:
                    cites.append(f'[{c.meeting_id}: "{r["quote"]}"]')
            if not cites:
                dropped += 1                      # an uncited point is not published
                continue
            lines.append(f"- {p.text.strip()} {' '.join(cites)}")
        lines.append("")
    if dropped:
        print(f"  ({dropped} points dropped: no citation passed the gate)")
    return "\n".join(lines).strip()


if __name__ == "__main__":
    text = make_brief(db.connect())
    (config.ROOT / "brief.txt").write_text(text, encoding="utf-8")
    print(text)
