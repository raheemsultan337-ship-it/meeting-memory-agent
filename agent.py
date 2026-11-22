"""Stage [5] AGENT: answers questions by retrieving from memory in a loop.

The loop (hand-written, ~80 lines, no framework, so every step is visible):
  1. Start context: the question + the team's speaker roles + the team's TIMELINE
     (bounded per-team memory, never the corpus).
  2. The model picks a tool: search / read_turns / find_facts / get_timeline.
     Code runs it and returns the result. Repeat (max MAX_STEPS).
  3. The model calls submit_answer(answer, citations).
  4. The CITATION GATE (code) checks every quote. Problems (paraphrase, one meeting
     only, wrong team) go back to the model as the tool result; it gets RETRIES
     chances to fix them. Only gate-approved citations are ever saved.

Usage:
  python agent.py answer            all 25 questions -> answers.json (+ brief)
  python agent.py ask "question"    one question, live (for the demo)
"""
import json
import re
import sqlite3
import sys

from google.genai import types

import config
import db
import llm
import search
import timeline
from citations import gate

MAX_STEPS = 8
RETRIES = 2
ROLE = "fast"     # flash-lite: 15 RPM free and ~2s/call. Flash "thinks" 30-45s per call.

SYSTEM = """You answer questions about recorded product-design meetings (teams of four designing a TV remote; meetings a, b, c, d in time order).
You have tools to search the meeting memory. Work in steps:
1. Read the TIMELINE given to you: it shows how decisions changed and what is current, with quotes.
2. Use search / read_turns / find_facts to find the exact passages, in AT LEAST TWO different meetings of the team.
3. Call submit_answer.

Answer rules:
- 1 or 2 plain sentences that answer EVERY part of the question.
- Name speakers by letter, plus role if known, e.g. "Speaker C (industrial designer)".
- Numbers as spoken are fine ("twelve Euros fifty" = 12.50 Euros).
- 2 or 3 citations from at least two different meetings of the team.
- Each quote: 8-25 consecutive words copied EXACTLY from ONE line shown to you after [meeting#turn] (keep um/uh, spelling and punctuation). Never paraphrase. Never join two turns.
- If the tool rejects a citation, fix it using the exact text of a turn you have seen."""

# ------------------------------------------------------------------ tools
TOOLS = [types.Tool(function_declarations=[
    types.FunctionDeclaration(
        name="search",
        description="Hybrid search (keywords + meaning) over the team's meetings. Returns the best "
                    "matching single turns and passages, each line labelled [meeting#turn].",
        parameters_json_schema={"type": "object", "properties": {
            "query": {"type": "string", "description": "what to look for, in plain words"},
            "meeting_id": {"type": "string", "description": "optional: limit to one meeting, e.g. ES2008c"}},
            "required": ["query"]}),
    types.FunctionDeclaration(
        name="read_turns",
        description="Read consecutive turns of one meeting (max 40), to see exact words and context.",
        parameters_json_schema={"type": "object", "properties": {
            "meeting_id": {"type": "string"},
            "from_turn": {"type": "integer"}, "to_turn": {"type": "integer"}},
            "required": ["meeting_id", "from_turn", "to_turn"]}),
    types.FunctionDeclaration(
        name="find_facts",
        description="Search the extracted facts (decisions, proposals, changes, actions, follow-ups, "
                    "numbers, disagreements) of the team by keyword. Each has a verified quote.",
        parameters_json_schema={"type": "object", "properties": {
            "keyword": {"type": "string"}}, "required": ["keyword"]}),
    types.FunctionDeclaration(
        name="get_timeline",
        description="Timeline of one topic for the team: history oldest first and the current state.",
        parameters_json_schema={"type": "object", "properties": {
            "topic": {"type": "string"}}, "required": ["topic"]}),
    types.FunctionDeclaration(
        name="submit_answer",
        description="Submit the final answer with 2-3 citations from at least two meetings.",
        parameters_json_schema={"type": "object", "properties": {
            "answer": {"type": "string"},
            "citations": {"type": "array", "items": {"type": "object", "properties": {
                "meeting_id": {"type": "string"}, "quote": {"type": "string"}},
                "required": ["meeting_id", "quote"]}}},
            "required": ["answer", "citations"]}),
])]


def tool_search(conn, team, query, meeting_id=None):
    out = []
    turns = search.keyword_search(conn, query, team=team, meeting=meeting_id, limit=8)
    if turns:
        out.append("Best matching single turns:")
        out += [f"[{t['meeting_id']}#{t['turn_idx']}] {t['speaker']}: {t['text']}" for t in turns]
    for p in search.hybrid_search(conn, query, team=team, meeting=meeting_id, limit=3):
        hit_words = set(re.findall(r"\w+", query.lower()))
        lines = p["turns"]
        # keep passages readable: the 25 turns around the best keyword overlap
        if len(lines) > 25:
            scores = [len(hit_words & set(re.findall(r"\w+", t["text"].lower()))) for t in lines]
            best = max(range(len(lines)), key=lambda i: scores[i])
            lines = lines[max(0, best - 12): best + 13]
        out.append(f"\nPassage from {p['meeting_id']} (found by {', '.join(p['ranks'])}):")
        out += [f"[{p['meeting_id']}#{t['turn_idx']}] {t['speaker']}: {t['text']}" for t in lines]
    return "\n".join(out) or "no results"


def tool_read_turns(conn, team, meeting_id, from_turn, to_turn):
    if not meeting_id.startswith(team):
        return f"{meeting_id} is not a meeting of team {team}"
    to_turn = min(int(to_turn), int(from_turn) + 39)
    rows = conn.execute("SELECT turn_idx, speaker, text FROM turns WHERE meeting_id=? AND turn_idx "
                        "BETWEEN ? AND ? ORDER BY turn_idx", (meeting_id, int(from_turn), to_turn)).fetchall()
    return "\n".join(f"[{meeting_id}#{r['turn_idx']}] {r['speaker']}: {r['text']}" for r in rows) or "no turns"


def tool_find_facts(conn, team, keyword):
    like = f"%{keyword.lower()}%"
    rows = conn.execute("SELECT meeting_id, type, topic, speaker, statement, turn_idx, quote FROM facts "
                        "WHERE team=? AND (lower(statement) LIKE ? OR topic LIKE ? OR lower(quote) LIKE ?) "
                        "ORDER BY letter LIMIT 15", (team, like, like, like)).fetchall()
    return "\n".join(f"{r['meeting_id']} {r['type']} ({r['topic']}): {r['statement']}\n"
                     f"   [{r['meeting_id']}#{r['turn_idx']}] {r['speaker']}: {r['quote']}" for r in rows) or "no facts"


def roles_text(conn, team):
    rows = conn.execute("SELECT speaker, role FROM speaker_roles WHERE team=? ORDER BY speaker", (team,)).fetchall()
    return ", ".join(f"Speaker {r['speaker']} = {r['role']}" for r in rows) or "unknown"


def run_tool(conn, team, name, args):
    try:
        if name == "search":
            return tool_search(conn, team, args.get("query", ""), args.get("meeting_id"))
        if name == "read_turns":
            return tool_read_turns(conn, team, args["meeting_id"], args["from_turn"], args["to_turn"])
        if name == "find_facts":
            return tool_find_facts(conn, team, args.get("keyword", ""))
        if name == "get_timeline":
            return timeline.render(conn, team, args.get("topic"))
        return f"unknown tool {name}"
    except Exception as e:  # a bad tool call becomes feedback, not a crash
        return f"tool error: {type(e).__name__}: {e}"


# ------------------------------------------------------------------ the loop
def answer_question(conn: sqlite3.Connection, team: str, question: str, qid: str = "ask",
                    verbose: bool = False) -> dict:
    start = (f"Team: {team}\nSpeaker roles: {roles_text(conn, team)}\n\n"
             f"TIMELINE of team {team} (how decisions changed; quotes are verified):\n"
             f"{timeline.render(conn, team)}\n\nQUESTION: {question}")
    contents = [types.Content(role="user", parts=[types.Part(text=start)])]
    best: dict = {"answer": "", "citations": []}
    retries, trace = 0, []

    for step in range(1, MAX_STEPS + 2):
        force = step > MAX_STEPS   # out of steps: force a submit_answer call
        cfg = types.ToolConfig(function_calling_config=types.FunctionCallingConfig(
            mode="ANY", allowed_function_names=["submit_answer"])) if force else None
        resp = llm.call(ROLE, contents, system=SYSTEM, tools=TOOLS, tool_config=cfg,
                        purpose=f"agent:{qid}:step{step}")
        content = resp.candidates[0].content if resp.candidates else None
        calls = resp.function_calls or []
        if content is None or not calls:
            # model replied with text instead of a tool call: nudge it
            contents.append(content or types.Content(role="model", parts=[types.Part(text="")]))
            contents.append(types.Content(role="user", parts=[types.Part(
                text="Use the tools, then call submit_answer.")]))
            continue
        contents.append(content)
        responses = []
        for fc in calls:
            args = dict(fc.args or {})
            trace.append(f"{fc.name}({json.dumps(args)[:120]})")
            if verbose:
                print(f"  step {step}: {fc.name} {json.dumps(args)[:150]}")
            if fc.name == "submit_answer":
                accepted, problems = gate(conn, args.get("citations", []), team=team)
                cand = {"answer": args.get("answer", "").strip(), "citations": accepted}
                if len(accepted) >= len(best["citations"]) or not best["answer"]:
                    best = cand
                if not problems or retries >= RETRIES or force:
                    return {**best, "steps": step, "trace": trace, "problems": problems}
                retries += 1
                result = ("Not accepted yet. Fix these and call submit_answer again:\n- "
                          + "\n- ".join(problems)
                          + f"\nCitations accepted so far: {json.dumps(accepted)}")
            else:
                result = run_tool(conn, team, fc.name, args)
            responses.append(types.Part.from_function_response(name=fc.name, response={"result": result[:12000]}))
        contents.append(types.Content(role="user", parts=responses))
    return {**best, "steps": MAX_STEPS, "trace": trace, "problems": ["ran out of steps"]}


def guess_team(conn, question: str) -> str | None:
    m = re.search(r"\b(ES|IS|TS)\d{4}\b", question)
    return m.group(0) if m else None


def answer_with_escalation(conn: sqlite3.Connection, team: str, question: str, qid: str) -> dict:
    """Normal budget first. If the gate still reports problems (e.g. only one meeting
    cited), retry once with a bigger step/retry budget and keep the better answer."""
    global MAX_STEPS, RETRIES
    r = answer_question(conn, team, question, qid)
    if not r["problems"]:
        return r
    saved = MAX_STEPS, RETRIES
    MAX_STEPS, RETRIES = 12, 3
    try:
        r2 = answer_question(conn, team, question, f"{qid}-redo")
    finally:
        MAX_STEPS, RETRIES = saved
    better = r2["citations"] and (not r2["problems"] or len(r2["problems"]) < len(r["problems"]))
    return r2 if better else r


def answer_all(conn: sqlite3.Connection) -> list[dict]:
    qs = json.loads(config.QUESTIONS_PATH.read_text(encoding="utf-8"))["questions"]
    results = []
    for q in qs:
        r = answer_with_escalation(conn, q["team"], q["question"], q["question_id"])
        results.append({"question_id": q["question_id"], "answer": r["answer"], "citations": r["citations"],
                        "_steps": r["steps"], "_problems": r["problems"], "_trace": r["trace"]})
        print(f"  {q['question_id']}: {r['steps']} steps, {len(r['citations'])} citations"
              f"{' PROBLEMS: ' + '; '.join(r['problems']) if r['problems'] else ''}", flush=True)
    return results


if __name__ == "__main__":
    conn = db.connect()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "answer"
    if cmd == "ask":
        question = " ".join(sys.argv[2:])
        team = guess_team(conn, question)
        if not team:
            sys.exit("Name the team in the question, e.g. 'Team ES2008 ...'")
        try:
            r = answer_question(conn, team, question, qid="ask", verbose=True)
        except (llm.MissingKey, llm.QuotaExhausted) as e:
            sys.exit(f"\nCan't answer this new question right now: {e}")
        print(f"\nANSWER: {r['answer']}")
        for c in r["citations"]:
            print(f'  [{c["meeting_id"]}] "{c["quote"]}"')
        if r["problems"]:
            print("  (problems:", "; ".join(r["problems"]), ")")
    elif cmd == "redo":
        # Re-run chosen questions with a bigger step/retry budget; keep the better result.
        MAX_STEPS, RETRIES = 12, 3
        path = config.ROOT / "answers_debug.json"
        results = json.loads(path.read_text(encoding="utf-8"))
        qs = {q["question_id"]: q for q in json.loads(config.QUESTIONS_PATH.read_text(encoding="utf-8"))["questions"]}
        for qid in sys.argv[2:]:
            q = qs[qid]
            r = answer_question(conn, q["team"], q["question"], f"{qid}-redo", verbose=True)
            old = next(x for x in results if x["question_id"] == qid)
            better = (not r["problems"] or len(r["problems"]) < len(old["_problems"])) and r["citations"]
            print(f"{qid}: {'REPLACED' if better else 'kept old'} -> {r['answer']}\n   problems: {r['problems']}")
            if better:
                old.update(answer=r["answer"], citations=r["citations"], _steps=r["steps"],
                           _problems=r["problems"], _trace=r["trace"])
        path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    elif cmd == "answer":
        results = answer_all(conn)
        out = config.ROOT / "answers_debug.json"
        out.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Saved {len(results)} answers with traces to {out.name}")
