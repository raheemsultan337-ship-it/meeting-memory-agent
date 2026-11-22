# Meeting Memory Agent

An agent that turns 138 real meeting transcripts (AMI corpus) into searchable memory, answers questions about what was decided, what changed and who said what (with word-for-word citations), and writes a pre-meeting brief.

- **Answers:** [`answers.json`](answers.json) (25 answers + TS3012 brief), produced by `python run.py all`
- **Write-up:** [`WRITEUP.md`](WRITEUP.md) (architecture, model-vs-code line, where things broke, production at scale)
- **Evaluation:** [`eval/report.md`](eval/report.md) (10 of our own hand-checked questions, scores, failures, cost)
- **Build log:** [`NOTES.md`](NOTES.md) (every problem we hit, with numbers)

## Quick start

Requires Python 3.12+ (built on 3.14, Windows).

```bash
cd meeting-memory
python -m venv .venv
.venv\Scripts\activate          # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env          # then put your GEMINI_API_KEY in .env (free key: aistudio.google.com/apikey)
```

The transcripts are expected at `../Catalist_AI_Intern_Build_Test_Data/transcripts/` (change `DATA_DIR` in `config.py` if needed).

```bash
python run.py all               # every stage, in order (resumable; safe to re-run)
python run.py validate          # check answers.json against every submission rule
```

**No API key?** `cache.db` contains every model and embedding response we got. Each request is fingerprinted (model + prompt + settings), so running the stages replays from the cache and reproduces `answers.json` without calling Gemini. (`memory.db` itself is rebuilt from the transcripts by `python run.py ingest`, which is pure code.)

## Ask your own question (live demo)

```bash
python agent.py ask "Team ES2008 wanted a feature to stop the remote getting lost. What happened to it?"
python timeline.py --show TS3009          # how the agent knows what is current
python search.py --compare "how did they stop the remote getting lost?" ES2005   # keyword vs meaning vs hybrid
```

The question must name a team (e.g. `TS3009`). `ask` prints each tool call the agent makes, the answer, and the verified citations.

## Stages

| # | Stage | File | Model or code? |
|---|---|---|---|
| 1 | Parse transcripts into turns, keyword index (BM25) | `ingest.py` | code |
| 2 | Chunks (~1,000 words, 2-turn overlap) + embeddings | `index.py` | code + embedding API |
| 3 | Extract facts per meeting (decisions, changes, actions, follow-ups, numbers, objections, roles) | `extract.py` | LLM (flash-lite) + citation gate |
| 4 | Timeline per team: history and current state of each topic | `timeline.py` | LLM + code |
| 5 | Agent loop: search / read_turns / find_facts / get_timeline, then submit_answer | `agent.py` | LLM (flash-lite) + code tools |
| 6 | Citation gate: 6+ words, inside one turn, exact (case/spacing ignored) | `citations.py` | code |
| 7 | TS3012 brief from meetings a-c only | `brief.py` | LLM (flash) + citation gate |
| 8 | Evaluation on our own questions | `evaluate.py` | code + LLM judge + human review |
| - | All Gemini calls: cache, rate limits, daily budget, 429/503 retries, usage log | `llm.py` | code |

## Files produced

| File | What | In git? |
|---|---|---|
| `memory.db` | the memory store (turns, chunks, vectors, facts, timeline) | no, rebuildable |
| `cache.db` | every API response, for free re-runs and reproducibility | yes |
| `usage.jsonl` | one line per real API call: tokens, time, purpose, errors | yes |
| `answers_debug.json` | answers plus each agent's tool trace and gate problems | yes |

## Tests

```bash
python -m pytest -v      # 39 tests, no API key needed (fake Gemini client + fake clock)
```
