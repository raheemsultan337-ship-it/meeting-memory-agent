# Meeting Memory Agent

[![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/raheemsultan337-ship-it/meeting-memory-agent/blob/master/colab_demo.ipynb)

**Try it in your browser, no install:** open the Colab notebook above, then *Runtime → Run all*. Sections 1-5 run with no API key, replaying from the shipped memory store and response cache; section 6 asks a new question with a free Gemini key.

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

## What to expect when you run it

The repo ships the built memory store (`memory.db`) and every API response we received (`cache.db`, keyed by a fingerprint of model + prompt + settings). So:

| You run... | Without an API key | With your own (free) key |
|---|---|---|
| `python run.py answer && python run.py brief && python run.py assemble` | **Reproduces `answers.json` byte for byte, with zero API calls.** Tested on a clean copy with no key. | Same (all cached) |
| `python agent.py ask "<one of the 25 questions>"`, the commands in `DEMO_SCRIPT.md` | Works (cached) | Works |
| `python agent.py ask "<a new question>"` | Clear message: needs a key | Works: about 6 flash-lite calls, about 10-30 s |
| `python run.py validate`, `python -m pytest` | Works (pure code) | Works |
| Rebuild everything from scratch (delete `memory.db` + `cache.db`, `python run.py all`) | Needs a key | About 1.5 h on the free tier, and it just fits in one day's quota (about 330 flash-lite requests of 500/day, about 870 embedding texts of 1,000/day) |

**Honest caveats (all handled in code, see NOTES.md):**
- **Free-tier quotas are small:** flash-lite 500 requests/day, other text models 20/day, embeddings 1,000 texts/day (quotas reset at midnight Pacific). If a daily quota runs out, `llm.py` falls back to the next model in `config.FALLBACKS`; if everything is exhausted, the stage **stops cleanly** and a rerun resumes where it left off. Nothing is lost, because every call is cached as soon as it returns.
- **Google often returns 503 "high demand"** at busy times. You'll see lines like `[gemini-3.5-flash-lite] 503, waiting 4s (attempt 2/8)`. That's the retry logic working, not a failure.
- **Model names change.** `gemini-2.5-*` was retired for new users during this build. If a model isn't available to your key, set `MODEL_FAST` / `MODEL_SMART` / `MODEL_EMBED` in `.env` (list yours with `python llm.py models`). Changing the embedding model requires `python index.py --rechunk`.
- Data location: set `DATA_DIR` in `.env` if the Catalist folder isn't next to this one.

## Ask your own question (live demo)

```bash
python agent.py ask "Team ES2008 wanted a feature to stop the remote getting lost. What happened to it?"
python show_memory.py TS3009 titanium     # the four memory layers for one topic
python timeline.py --show TS3009          # how the agent knows what is current
python search.py --compare "how did they stop the remote getting lost?" ES2005   # keyword vs meaning vs hybrid
```

`ask` prints each tool call the agent makes, the answer, and the verified citations.

**Asking a new question** (locally or in Colab section 6):
1. Get a free key at https://aistudio.google.com/apikey and put it in `.env` as `GEMINI_API_KEY=...` (in Colab: 🔑 Secrets → `GEMINI_API_KEY`, with Notebook access on).
2. Run `python agent.py ask "<question>"`. The question **must name a team** (`ES2002`-`ES2016`, `IS1000`-`IS1009`, `TS3003`-`TS3012`) and must not contain double quotes.
3. About 6 model calls and 10-30 s per question. `503, waiting 4s` lines are the retry logic handling a busy Google. If the daily quota (flash-lite: 500/day per Google project) is used up, you get a clear message; it resets at midnight Pacific.

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
python -m pytest -v      # 40 tests, no API key needed (fake Gemini client + fake clock)
```
