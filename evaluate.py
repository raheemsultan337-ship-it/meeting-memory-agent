"""Stage [8] EVAL: score the agent on our own hand-checked questions.

Three scores per question:
  citations  (code)  every citation passes the exact-match gate AND >= 2 meetings
  must       (code)  share of required facts (regex) present in the answer
  judge      (LLM)   correct / partial / wrong vs the hand-checked gold answer.
                     An LLM judge compares MEANING, which code can't. Its verdicts
                     are then reviewed by hand (see report.md, "human check").

Also reports measured cost/time from cache.db for the 1,000-meeting estimate.
Usage:  python evaluate.py
"""
import json
import re
from typing import Literal

from pydantic import BaseModel

import agent
import config
import db
import llm
from citations import check_citation

EVAL = config.ROOT / "eval" / "my_questions.json"
REPORT = config.ROOT / "eval" / "report.md"
RESULTS = config.ROOT / "eval" / "results.json"


class Verdict(BaseModel):
    verdict: Literal["correct", "partial", "wrong"]
    reason: str


JUDGE = """You grade an answer against a gold answer that was checked by a human.
correct = all parts of the question answered and consistent with the gold answer.
partial = some parts right, some missing or wrong. wrong = the main point is wrong or missing.
Speaker letters must match. Numbers may be in words or digits. Return JSON."""


def must_score(answer: str, must: list[str]) -> float:
    hits = 0
    for m in must:
        if len(m) == 1:   # a speaker letter: match as a whole word, e.g. "Speaker D" / "D ("
            hits += bool(re.search(rf"\b{m}\b", answer))
        else:
            hits += bool(re.search(m, answer, re.IGNORECASE))
    return hits / len(must)


def main() -> None:
    conn = db.connect()
    qs = json.loads(EVAL.read_text(encoding="utf-8"))["questions"]
    rows = []
    for q in qs:
        try:
            r = agent.answer_question(conn, q["team"], q["question"], qid=q["id"])
        except (llm.QuotaExhausted, RuntimeError) as ex:
            # Report honestly instead of crashing: this question could not be run today.
            rows.append({**q, "answer": "(not run: daily API quota exhausted / model unavailable)",
                         "citations": [], "steps": 0, "trace": [], "citations_ok": False, "must": 0.0,
                         "verdict": "not run", "judge_reason": str(ex)[:200]})
            print(f"  {q['id']}: NOT RUN ({type(ex).__name__})", flush=True)
            continue
        cites_ok = all(check_citation(conn, c["meeting_id"], c["quote"], q["team"])["status"] == "valid"
                       for c in r["citations"]) and len({c["meeting_id"] for c in r["citations"]}) >= 2
        try:
            v = llm.generate_json("fast", f"QUESTION: {q['question']}\nGOLD: {q['gold']}\nANSWER: {r['answer']}",
                                  schema=Verdict, system=JUDGE, purpose=f"judge:{q['id']}")
        except (llm.QuotaExhausted, RuntimeError):
            v = Verdict(verdict="partial", reason="LLM judge not run (quota); verdict to be set by human check")
        rows.append({**q, "answer": r["answer"], "citations": r["citations"], "steps": r["steps"],
                     "trace": r["trace"], "citations_ok": cites_ok,
                     "must": round(must_score(r["answer"], q["must"]), 2),
                     "verdict": v.verdict, "judge_reason": v.reason})
        print(f"  {q['id']}: {v.verdict:<8} must={rows[-1]['must']:.2f} citations_ok={cites_ok} steps={r['steps']}", flush=True)
    RESULTS.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    write_report(rows)


def cost_section() -> str:
    """Per-stage totals of real API calls, read from usage.jsonl (each call logs its purpose)."""
    out = ["| stage | calls | input tok | output tok | model time (s) |", "|---|---|---|---|---|"]
    stats: dict[str, list] = {}
    for line in config.USAGE_LOG.read_text(encoding="utf-8").splitlines():
        e = json.loads(line)
        if e.get("event") != "call":
            continue
        stage = (e.get("purpose") or "other").split(":")[0]
        s = stats.setdefault(stage, [0, 0, 0, 0.0])
        s[0] += 1; s[1] += e.get("in_tok", 0); s[2] += e.get("out_tok", 0); s[3] += e.get("latency_s", 0)
    for stage, (n, i, o, t) in sorted(stats.items()):
        out.append(f"| {stage} | {n} | {i:,} | {o:,} | {t:,.0f} |")
    return "\n".join(out)


def write_report(rows: list[dict]) -> None:
    n = len([r for r in rows if r["verdict"] != "not run"]) or 1
    correct = sum(r["verdict"] == "correct" for r in rows)
    partial = sum(r["verdict"] == "partial" for r in rows)
    cites = sum(r["citations_ok"] for r in rows)
    lines = ["# Evaluation on our own questions", "",
             f"**{correct}/{n} correct, {partial} partial, {n - correct - partial} wrong** (LLM judge vs hand-checked gold; "
             f"verdicts reviewed by hand below). Citations valid and from 2+ meetings: {cites}/{n}. "
             f"Average required-fact coverage: {sum(r['must'] for r in rows) / n:.0%}.", "",
             "| id | kind | verdict | must | citations ok | steps |", "|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['id']} | {r['kind']} | {r['verdict']} | {r['must']:.2f} | {r['citations_ok']} | {r['steps']} |")
    lines += ["", "## Per question", ""]
    for r in rows:
        lines += [f"### {r['id']} ({r['team']}): {r['question']}", "",
                  f"- **Gold (hand-checked):** {r['gold']}",
                  f"- **Agent:** {r['answer']}",
                  f"- **Citations:** " + "; ".join(f"{c['meeting_id']}: \"{c['quote']}\"" for c in r["citations"]),
                  f"- **Judge:** {r['verdict']}: {r['judge_reason']}",
                  f"- **Tool trace:** {' -> '.join(t.split('(')[0] for t in r['trace'])}", ""]
    lines += ["## Measured usage (all real API calls so far, from usage.jsonl)", "", cost_section(), ""]
    REPORT.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {REPORT.relative_to(config.ROOT)}")


if __name__ == "__main__":
    main()
