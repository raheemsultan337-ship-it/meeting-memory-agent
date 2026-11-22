# Meeting Memory Agent: write-up

## Architecture
Transcripts are a **store the agent retrieves from**; no prompt ever holds more than one meeting.

```
transcripts -> parse (code) -> turns + BM25 | chunks + embeddings | facts per meeting (LLM) | timeline per team (LLM+code)  => memory.db
question    -> agent loop (LLM picks tools, code runs them) -> citation gate (code) -> answers.json
```
**Three memory layers.** *Episodic*: every turn word for word, with keyword and vector search. *Semantic*: 1,476 facts (decisions, changes, actions, follow-ups, numbers, objections, roles), each with a gate-verified quote. *Temporal*: per team and topic, the history and the **current** state (`settled/changed/dropped/open`). This is how the agent knows what is current: e.g. TS3009's case material CHANGED from hard plastic (a) to a titanium back (c) to titanium-coloured plastic (d). Each meeting is extracted with the team's earlier facts as context, so "that one" resolves correctly. The agent starts from the timeline and calls `search` (BM25 + vectors, fused with RRF), `read_turns`, `find_facts` and `get_timeline` for up to 8 steps, then `submit_answer`. Gate problems go back to the model, which gets 2 retries. **An unverified quote is never written.**

## Model-vs-code decisions, and why
| Code: never an LLM | LLM: needs language |
|---|---|
| Parsing, filler flag, acronym fix (`L_C_D_`->`lcd`): fixed format | Extracting facts: "is this a decision or an objection?" |
| BM25, cosine, RRF, team filters: maths, testable | Judging that c replaces a, and references back |
| Meeting order a->d: a fact, not a judgement | Choosing what to search next |
| **Citation gate**: substring is a string check, and LLMs paraphrase | Writing answers and the brief |
| Brief limited to meetings a-c: enforced in SQL, not the prompt | Eval judge (then checked by hand) |
| Cache, rate limits, retries, daily budgets | |

## Tools used, and where they broke
Python, SQLite (FTS5 + vectors), numpy, pydantic, rapidfuzz, Gemini free tier (`3.5-flash-lite`, `3.5-flash`, `embedding-2`). **No framework**: an 80-line loop, visible and tested (40 tests). What broke (full log in NOTES.md):
- **Embedding quota (1,000 texts/day)** forced ~1,000-word chunks instead of ~250 (3+ days of quota). That cost retrieval precision, and it's behind failure 3 below.
- **Keyword search found nothing for "LCD"** (it's spelled `L_C_D_`), so I added a normalised search copy.
- **Models and quotas shifted mid-build**: retired models (404), constant 503s, flash-lite capped at 500/day. Every call is cached, retried and resumable, and falls back to another model, so no failure lost work.

## Proof it works
10 of my own questions, checked against the transcripts (`eval/report.md`): **8/9 correct, 1 partial** (an LLM judge, then my own check). All have valid citations from 2+ meetings. One question couldn't run because every free model was exhausted. The official `answers.json` passes every rule in code (0 errors, 1 warning).

## Three questions it got wrong, and why
1. **E03 (ES2002): the wrong speaker got the credit.** It said Speaker B (project manager) proposed removing the LCD. In fact **Speaker A** proposed it (ES2002d#149), and B only **summarised** it ("what's our reviewed suggestion? Um take away the L_C_ display?"). *Why:* the recap repeats the key words, so it ranks above the original proposal, and the agent cites the first good match. Project managers recap constantly, so this is systematic, not a one-off. *Fix:* prefer the earliest turn that introduces an idea, and check against the extracted `proposal` fact's speaker.
2. **Q22 (ES2010): part of the question left out.** It got the lithium suggestion (Speaker B) and the final choice (lithium), but **left out** that Speaker C preferred double-A batteries "because most people usually have double A_s around" (ES2010a). *Why:* nothing checks that every part of a multi-part question is answered; the agent stops once it has valid citations. *Fix:* split the question into parts (code or LLM) and have the gate reject answers that skip a part.
3. **Q06 (ES2008): only one meeting cited.** All its citations are from ES2008d, which breaks the "at least two meetings" rule. The gate warned three times, and even a retry with 12 steps didn't find an earlier mention. *Why:* the free embedding quota forced ~1,000-word chunks, so an earlier, differently worded mention ("some sort of device to help you find it") gets diluted in a big chunk. A live run of a **reworded** question did find it (ES2008a#39), so it's retrieval variance, not missing data. *Fix:* ~250-word chunks on paid quota, plus a find_facts sweep of each meeting before giving up.

## Cost and time to ingest 1,000 meetings
**1,000 meetings** (measured per meeting: extraction 8.9k in / 1.3k out tokens, 7 s; embeddings 7.1k tokens): about 11M text + 7M embedding tokens, **about $2.70 at list prices** (to verify). Time is **about 3 h sequential or 15-20 min with 10 workers** on paid quota; **about 6 days on the free tier** (embedding cap). About half a cent per question.

## What production would need at real scale (10,000 meetings, 72x)
**What holds:** per-meeting jobs (bounded prompts, linear cost), a constant-size agent context, and a gate that checks one meeting. ~3.5M turns, ~2 GB. Brute-force vector search, **measured on 220k vectors: 31 ms/query, 680 MB RAM**.
**What breaks, worst first:**
1. **The timeline: one LLM pass per team.** That's fine for 4 meetings (~40 facts), but 500 meetings (~5k facts) overflows the prompt and the output. Fix: update **one meeting at a time** (current state + new meeting -> what changed?). A shortcut I took for the deadline.
2. **Team labels and a-d order.** Real meetings come unlabelled, with named people. Fix: group by project, participants and date; resolve speakers; add cross-project search (unsupported today).
3. **Free-tier quotas:** 20 days of extraction and 55 days of embeddings. Fix: paid quota, ~30 min with 10 workers, ~$25-30 (estimated).
4. **SQLite with parallel writers** gives lock contention. Fix: Postgres + pgvector and a worker queue.
5. **The whole team timeline in the starting prompt** bloats at hundreds of topics. Fix: fetch only relevant topics on demand.
