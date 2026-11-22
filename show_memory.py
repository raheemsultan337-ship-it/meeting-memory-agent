"""Demo helper: show the memory layers for one team and topic, side by side.

Read-only, pure code, no API calls.
Usage:  python show_memory.py TS3009 titanium
"""
import json
import sys

import db
import timeline
from search import keyword_search


def line(title: str) -> None:
    print("\n" + "=" * 78 + f"\n{title}\n" + "=" * 78)


def main(team: str, topic: str) -> None:
    conn = db.connect()
    counts = {
        "turns": conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0],
        "chunks": conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0],
        "vectors": conn.execute("SELECT COUNT(*) FROM chunk_vectors").fetchone()[0],
        "facts": conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0],
        "topics": conn.execute("SELECT COUNT(*) FROM timeline WHERE team NOT LIKE '%@%'").fetchone()[0],
    }
    line("MEMORY STORE: memory.db (one SQLite file, written once at ingest, read through tools)")
    print(f"  Episodic : {counts['turns']:,} speaker turns word for word, {counts['chunks']} chunks, {counts['vectors']} embeddings")
    print(f"  Semantic : {counts['facts']:,} extracted facts, each with a verified quote")
    print(f"  Temporal : {counts['topics']} team/topic timelines (history + current state)")
    roles = ", ".join(f"{r['speaker']} = {r['role']}" for r in
                      conn.execute("SELECT speaker, role FROM speaker_roles WHERE team=? ORDER BY speaker", (team,)))
    print(f"  Roles ({team}): {roles}")

    line(f"1. EPISODIC: the raw words. Keyword search for '{topic}' in {team}")
    for t in sorted(keyword_search(conn, topic, team=team, limit=6), key=lambda t: (t["meeting_id"], t["turn_idx"])):
        print(f"  [{t['meeting_id']}#{t['turn_idx']}] {t['speaker']}: {t['text'][:110]}")
    print("  -> exact, but it can't tell you which of these is the CURRENT decision.")

    line(f"2. SEMANTIC: facts the LLM extracted (quotes verified by code) mentioning '{topic}'")
    like = f"%{topic.lower()}%"
    for f in conn.execute("SELECT meeting_id, type, speaker, statement FROM facts WHERE team=? AND "
                          "(lower(statement) LIKE ? OR topic LIKE ?) ORDER BY letter, fact_id LIMIT 8",
                          (team, like, like)):
        print(f"  {f['meeting_id']} [{f['type']:<12}] Speaker {f['speaker']}: {f['statement'][:95]}")
    print("  -> clean, searchable statements: decisions, changes, objections, who said them.")

    line(f"3. TEMPORAL: the timeline, i.e. how the decision changed and what is current")
    rows = conn.execute("SELECT topic FROM timeline WHERE team=? AND (topic LIKE ? OR current LIKE ? OR history LIKE ?)",
                        (team, like, like, like)).fetchall()
    for r in rows[:2]:
        print(timeline.render(conn, team, r["topic"]))
    print("  -> the LLM judged 'd replaces c'; code ordered the meetings and checked every fact id.")

    line("4. WORKING: the agent's context for one question (same size at 138 or 10,000 meetings)")
    print("  question + team roles + team timeline + whatever its tools retrieve, then discarded.")
    print("  See it live:  python agent.py ask \"Team TS3009 planned a titanium back ...\"")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit("usage: python show_memory.py TEAM TOPIC   e.g.  python show_memory.py TS3009 titanium")
    main(sys.argv[1], sys.argv[2])
