"""Central configuration. No other module hard-codes a path or a tunable."""
import os
from pathlib import Path

from dotenv import load_dotenv

# Folder this file lives in, i.e. the project root (meeting-memory/).
ROOT = Path(__file__).resolve().parent

# Secrets live in .env (git-ignored), never in code.
load_dotenv(ROOT / ".env")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Catalist's data sits next to the project; we read it in place, never copy it.
DATA_DIR = ROOT.parent / "Catalist_AI_Intern_Build_Test_Data"
TRANSCRIPTS_DIR = DATA_DIR / "transcripts"
QUESTIONS_PATH = DATA_DIR / "questions.json"

# The memory store: one SQLite file. Rebuildable from the transcripts, so not in git.
DB_PATH = ROOT / "memory.db"

# Words that carry no content on their own. A short turn made only of these
# ("Mm-hmm.", "Okay, yeah.") is flagged as filler: kept in the DB for exact
# citations, but skipped when we send text to the LLM or rank search results.
FILLER_WORDS = {
    "mm", "mm-hmm", "mmm", "hmm", "um", "uh", "uh-huh", "huh", "ah", "oh",
    "okay", "ok", "yeah", "yep", "right", "so", "well", "alright", "cool",
}
# Deliberately NOT filler: "yes", "no", "sure", "fine" — these can be someone
# agreeing to or rejecting a decision, which the extractor must see.
FILLER_MAX_WORDS = 4

# ---------------------------------------------------------------- LLM (step 2)
# Code refers to models by ROLE, never by name, so swapping a model is one line here
# (or one line in .env). Verify current names at aistudio.google.com/docs/models,
# or run:  python llm.py models
# Tested 2026-11-22 on the free tier (see NOTES.md):
#   2.5-flash-lite -> 404 retired for new users; 3.8-flash -> 503 overloaded;
#   embedding-2 -> a plain list of strings is read as ONE multi-part input; send
#                  each text as its own Content (llm.embed does).
MODELS = {
    "fast":  os.getenv("MODEL_FAST",  "gemini-3.5-flash-lite"),   # extraction, timeline (~1.5s/call)
    "smart": os.getenv("MODEL_SMART", "gemini-3.5-flash"),        # agent, brief (thinks; slower)
    "embed": os.getenv("MODEL_EMBED", "gemini-embedding-2"),      # vectors (newest; texts must be sent as separate Contents)
}
HTTP_TIMEOUT_S = 90      # one request may not hang longer than this
EMBED_DIM = 768          # shorter vectors: less storage, nearly the same quality
EMBED_BATCH = 100        # max texts per embedding request
EMBED_BATCH_TOKENS = 13_000  # max est. tokens per request: 2 requests (26k) fit in one minute of the 27k embed TPM

# Free-tier limits per model ROLE. TPM = 250k for all three (user checked AI Studio
# 2026-11-22). RPM: flash-lite 15, flash ~5 (user saw 3.6-flash=5). RPD still estimates. Google changes these: check yours in AI Studio
# (Usage / Rate limits) and edit. We stay ~10% under so we never hit the wall.
#   rpm = requests/minute, tpm = tokens/minute, rpd = requests/day (resets midnight Pacific)
LIMITS = {
    "fast":  {"rpm": 13, "tpm": 225_000, "rpd": 490},   # RPD is 500 (Google 429 error, 2026-11-22)
    "smart": {"rpm": 4,  "tpm": 225_000, "rpd": 225},
    "embed": {"rpm": 90, "tpm": 27_000,  "rpd": 990},   # TPM 30k, RPD 1000 TEXTS (from Google 429 errors)
}

# Paid-tier prices in USD per 1M tokens, ONLY used to estimate "cost to ingest
# 1,000 meetings" for the write-up. Verify at aistudio.google.com/docs/pricing.
PRICES = {
    "fast":  {"in": 0.10, "out": 0.40},
    "smart": {"in": 0.30, "out": 2.50},
    "embed": {"in": 0.15, "out": 0.0},
}

MAX_RETRIES = 8          # per call, for per-minute 429s and 5xx (fallback models are often overloaded)
CACHE_DB = ROOT / "cache.db"       # every LLM/embedding response, committed to git
USAGE_LOG = ROOT / "usage.jsonl"   # one line per real API call (and per error)

# If a role's model runs out of DAILY quota, llm.py switches to the next model here
# (each model has its own free-tier quota). Limits are conservative guesses.
FALLBACKS = {
    "fast":  [{"model": "gemini-3.6-flash", "rpm": 4, "tpm": 225_000, "rpd": 20},   # free RPD = 20
              {"model": "gemini-3.5-flash", "rpm": 4, "tpm": 225_000, "rpd": 20},
              {"model": "gemini-3.8-flash", "rpm": 4, "tpm": 225_000, "rpd": 20}],
    "smart": [{"model": "gemini-3.6-flash", "rpm": 4, "tpm": 225_000, "rpd": 200}],
}
