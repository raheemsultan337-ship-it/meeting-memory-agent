"""One entry point for the whole pipeline. Every stage is resumable and cached.

  python run.py ingest      [1] parse transcripts -> turns + keyword index      (code)
  python run.py index       [2] chunks + embeddings                             (code + embedding API)
  python run.py extract     [3] facts per meeting                               (LLM, flash-lite)
  python run.py timeline    [4] what changed / what is current, per team        (LLM + code)
  python run.py answer      [5] agent answers the 25 questions                  (LLM loop + citation gate)
  python run.py brief       [7] TS3012 brief from meetings a-c                  (LLM + citation gate)
  python run.py assemble        answers.json = answers + brief, then validate   (code)
  python run.py validate        check answers.json against every submission rule (code)
  python run.py eval        [8] score the agent on our own hand-checked questions
  python run.py all             everything above, in order

Graders without a Gemini key: cache.db holds every model response, so `answer`,
`brief` and `eval` replay from cache and reproduce answers.json exactly.
"""
import json
import sys

import config
import db
import llm
from citations import check_citation, norm


def validate(path=config.ROOT / "answers.json") -> list[str]:
    """Every rule from the brief and questions.json, checked in code."""
    conn = db.connect()
    data = json.loads(path.read_text(encoding="utf-8"))
    qs = {q["question_id"]: q for q in json.loads(config.QUESTIONS_PATH.read_text(encoding="utf-8"))["questions"]}
    errs, warns = [], []
    if set(data) != {"answers", "brief"}:
        errs.append(f"top-level keys must be answers + brief, got {sorted(data)}")
    ids = [a.get("question_id") for a in data.get("answers", [])]
    if sorted(ids) != sorted(qs):
        errs.append(f"answers must cover exactly {len(qs)} questions; missing {sorted(set(qs) - set(ids))}")
    for a in data.get("answers", []):
        qid, team = a.get("question_id"), qs.get(a.get("question_id"), {}).get("team")
        if not a.get("answer", "").strip():
            errs.append(f"{qid}: empty answer")
        n_sent = len([s for s in a.get("answer", "").replace("e.g.", "eg").split(". ") if s.strip()])
        if n_sent > 3:
            warns.append(f"{qid}: {n_sent} sentences (rule: 1-3)")
        cites = a.get("citations", [])
        if not 1 <= len(cites) <= 3:
            errs.append(f"{qid}: {len(cites)} citations (rule: 1-3)")
        for c in cites:
            r = check_citation(conn, c["meeting_id"], c["quote"], team)
            if r["status"] != "valid":
                errs.append(f"{qid}: citation fails exact check ({r['status']}): {c['meeting_id']} {c['quote'][:60]!r}")
        if len({c["meeting_id"] for c in cites}) < 2:
            warns.append(f"{qid}: citations from only one meeting")
    brief = data.get("brief", "")
    if not isinstance(brief, str) or len(brief) < 200:
        errs.append("brief must be plain text")
    if any(m in brief for m in ("**", "##", "```")):
        errs.append("brief contains markdown; must be plain text")
    for line in brief.splitlines():
        if line.startswith("- ") and "[TS3012" not in line:
            errs.append(f"brief point without citation: {line[:60]!r}")
    print(f"validate: {len(errs)} errors, {len(warns)} warnings")
    for e in errs:
        print("  ERROR", e)
    for w in warns:
        print("  warn ", w)
    return errs


def assemble() -> None:
    debug = json.loads((config.ROOT / "answers_debug.json").read_text(encoding="utf-8"))
    brief = (config.ROOT / "brief.txt").read_text(encoding="utf-8")
    out = {"answers": [{"question_id": a["question_id"], "answer": a["answer"],
                        "citations": [{"meeting_id": c["meeting_id"], "quote": c["quote"]} for c in a["citations"]]}
                       for a in debug],
           "brief": brief}
    path = config.ROOT / "answers.json"
    path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {path.name}")
    validate(path)


def main(stage: str) -> None:
    conn = db.connect()
    db.init_db(conn)
    if stage in ("ingest", "all"):
        import ingest; ingest.ingest_all(conn)
    if stage in ("index", "all"):
        import index; index.build_index(conn)
    if stage in ("extract", "all"):
        import extract; extract.extract_all(conn)
    if stage in ("timeline", "all"):
        import timeline; timeline.build_all(conn)
    if stage in ("answer", "all"):
        import agent
        results = agent.answer_all(conn)
        (config.ROOT / "answers_debug.json").write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    if stage in ("brief", "all"):
        import brief
        (config.ROOT / "brief.txt").write_text(brief.make_brief(conn), encoding="utf-8")
    if stage in ("assemble", "all"):
        assemble()
    if stage == "validate":
        validate()
    if stage in ("eval", "all"):
        import evaluate; evaluate.main()


if __name__ == "__main__":
    try:
        main(sys.argv[1] if len(sys.argv) > 1 else "all")
    except llm.QuotaExhausted as e:
        print(f"Stopped cleanly (quota): {e}\nEverything finished so far is saved; rerun the same command later.")
