# Meeting Memory Agent: write-up

## Architecture
Transcripts are a **store the agent retrieves from**; no prompt ever holds more than one meeting.

```
transcripts -> parse (code) -> turns + BM25 | chunks + embeddings | facts per meeting (LLM) | timeline per team (LLM+code)  => memory.db
question    -> agent loop (LLM picks tools, code runs them) -> citation gate (code) -> answers.json
```
**Three memory layers.** *Episodic*: every turn word for word, with keyword and vector search. *Semantic*: 1,476 facts (decisions, changes, actions, follow-ups, numbers, objections, roles), each with a gate-verified quote. *Temporal*: per team and topic, the history and the **current** state (`settled/changed/dropped/open`). This is how the agent knows what is current. For example, `timeline.py --show TS3009` shows case material CHANGED: hard plastic (a) -> titanium back (c) -> titanium-coloured plastic (d). Each meeting is extracted with the team's earlier facts as context, so "that one" resolves to the right topic. The agent starts from the team's timeline, then uses `search` (BM25 + vectors fused with RRF), `read_turns`, `find_facts` and `get_timeline` for up to 8 steps before `submit_answer`. Gate problems (a paraphrase, a quote across two turns, only one meeting) go back to the model, which gets 2 retries. **An unverified quote is never written.**

## Model vs code
| Code: never an LLM | LLM: needs language |
|---|---|
| Parsing, filler flag, acronym fix (`L_C_D_`->`lcd`): fixed format | Extracting facts: "is this a decision or an objection?" |
| BM25, cosine, RRF, team filters: maths, testable | Judging that c replaces a, and references back |
| Meeting order a->d: a fact, not a judgement | Choosing what to search next |
| **Citation gate**: substring is a string check, and LLMs paraphrase | Writing answers and the brief |
| Brief limited to meetings a-c: enforced in SQL, not the prompt | Eval judge (then checked by hand) |
| Cache, rate limits, retries, daily budgets | |

## Tools, and where they broke
Python, SQLite (FTS5 + vector blobs), numpy, pydantic, rapidfuzz, and Gemini on the **free tier** (`3.5-flash-lite` for extraction and the agent, `3.5-flash` for the brief, `embedding-2`). **No framework**: an 80-line loop, visible and tested (39 tests). What broke (NOTES.md has the full log):
- **Embeddings: 1,000 texts/day, 30k tokens/min**, and every text in a batch counts. 250-word chunks would need 3+ days, so I used ~1,000-word chunks (765) and turn-level keyword search for precision. A 100-text batch (36k tokens) could never fit in one minute, so batches are capped by tokens.
- **"LCD" found nothing**: acronyms are spelled `L_C_D_`. Fixed with a normalised search copy; citations use the original text.
- **Models changed mid-build**: 2.5 models were retired (404), and constant 503s (~120 in a day) came with SDK retries that hung silently. I turned those off and wrote my own retry policy. **flash-lite allows 500 requests/day**: when we hit it, `llm.py` fell back automatically to other models (20/day each). Every call is cached, so no crash or quota stop loses work.

## Proof it works
10 of my own questions, checked against the transcripts (`eval/report.md`): **8/9 correct, 1 partial** (an LLM judge, then my own check). All have valid citations from 2+ meetings. One question couldn't run because every free model was exhausted. The official `answers.json` passes every rule in code (0 errors, 1 warning). **Three it got wrong:**
1. **E03, credit to whoever recaps**: it named Speaker B as proposing to drop the LCD; Speaker A proposed it and B, the project manager, summarised it. The recap repeats the keywords, so search ranks it first.
2. **Q22, part of the question missed**: it found lithium (B) and the final choice, but not that C preferred double-A "because most people usually have double A_s around". A code check that each part of the question is answered would catch this.
3. **Q06, one meeting only**: every citation is from ES2008d. With 1,000-word chunks it never found the earlier mention under other words. That's the cost of the quota-driven chunk size.

**1,000 meetings** (measured per meeting: extraction 8.9k in / 1.3k out tokens, 7 s; embeddings 7.1k tokens): about 11M text + 7M embedding tokens, **about $2.70 at list prices** (to verify). Time is **about 3 h sequential or 15-20 min with 10 workers** on paid quota; **about 6 days on the free tier** (embedding cap). About half a cent per question.

## Would it fall over at 10,000 meetings (72x)?
**What holds:** ingest and extraction are independent per-meeting jobs (bounded prompts, linear cost); the agent's context stays the same size; the gate checks one meeting. Storage is about 3.5M turns, 106k facts, ~2 GB. Brute-force vector search, **measured** on 220k vectors: **31 ms/query, 680 MB RAM**, so slow but not broken.
**What breaks, worst first:**
1. **The timeline: one LLM pass per team over all its facts.** That's fine at 4 meetings (~40 facts), but a real project with 500 meetings has ~5k facts, which won't fit in one prompt, and the output would be truncated. Fix: update it **one meeting at a time** (current state + new meeting -> what changed?), sharded by topic. I took this shortcut for the deadline.
2. **Team labels and a-d order.** Every tool filters by team; real meetings arrive unlabelled, with named people, not A-D. Fix: group by project, participants and date; resolve speakers; add cross-project search (today a question like "which projects dropped the LCD?" is unsupported).
3. **Free-tier quotas:** 10k extractions at 500/day is 20 days; ~55k embeddings at 1,000/day is 55 days. Fix: paid quota, which is ~30 min with 10 workers and ~$25-30 (estimated).
4. **SQLite with parallel writers** gives lock contention (seen even with 2 jobs), and each process loads every vector. Fix: Postgres + pgvector and a worker queue.
5. **The agent starts with the whole team timeline**, which bloats at hundreds of topics. Fix: fetch only the relevant topics on demand.
Plus an eval set that grows with every wrong answer.
