"""FOUNDATION: every Gemini call in the project goes through this file.

What it gives every other stage, for free:
  1. Disk cache    - an identical request is answered from cache.db, never the API.
                     Re-running anything costs nothing; crashes lose nothing.
  2. Rate limiter  - stays under requests/min and tokens/min, by waiting.
  3. Daily budget  - counts requests per day; stops CLEANLY before the daily cap.
  4. 429 handling  - per-minute limit: wait (as long as Google says) and retry.
                     per-day limit: raise QuotaExhausted so the stage stops; its
                     progress is already saved, so the next run resumes.
  5. Usage log     - tokens + wall-clock time per real call -> cost/time estimates.

Public API:
  generate_text(role, prompt, system=..., purpose=...)          -> str
  generate_json(role, prompt, schema=PydanticModel, ...)         -> PydanticModel instance
  call(role, contents, system=, schema=, tools=, ...)            -> raw GenerateContentResponse (agent loop)
  embed(texts, task_type="RETRIEVAL_DOCUMENT" | "RETRIEVAL_QUERY") -> list[list[float]]

CLI:
  python llm.py ping      one tiny real call, to check your key and model names
  python llm.py models    list the models your key can use
  python llm.py report    tokens, time and estimated paid cost so far
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
import time
from array import array
from collections import deque
from datetime import datetime
from typing import Any, TypeVar
from zoneinfo import ZoneInfo

from google import genai
from google.genai import errors, types
from pydantic import BaseModel

import config

T = TypeVar("T", bound=BaseModel)


def _network_errors() -> tuple[type[Exception], ...]:
    """Timeout / dropped-connection exception classes of whatever HTTP library the
    Google SDK uses (google-genai 3.x uses httpx2; older versions use httpx)."""
    found: list[type[Exception]] = [TimeoutError, ConnectionError]
    for name in ("httpx2", "httpx"):
        try:
            mod = __import__(name)
            found += [mod.TimeoutException, mod.TransportError]
        except (ImportError, AttributeError):
            pass
    return tuple(found)


NETWORK_ERRORS = _network_errors()

# Indirection so tests can swap in a fake clock and a fake API client.
_now = time.monotonic
_sleep = time.sleep
_client: Any = None


class QuotaExhausted(Exception):
    """Daily quota for a model is used up. Stop the stage; rerun after the reset."""


class MissingKey(RuntimeError):
    """No GEMINI_API_KEY and the request isn't in cache.db. Cached work still replays."""


# ======================================================================= cache
_cache_conn: sqlite3.Connection | None = None


def _cache() -> sqlite3.Connection:
    """One SQLite file holds every cached response plus the daily request counter.

    Why SQLite and not thousands of small JSON files: writes are atomic (a crash
    can't leave a half-written entry), lookups are fast, and it's ONE file to commit
    so a grader can reproduce answers.json without a Gemini key.
    """
    global _cache_conn
    if _cache_conn is None:
        _cache_conn = sqlite3.connect(config.CACHE_DB)
        _cache_conn.executescript("""
            CREATE TABLE IF NOT EXISTS cache (
                key        TEXT PRIMARY KEY,  -- sha256 of the full request
                kind       TEXT NOT NULL,     -- 'gen' | 'emb'
                role       TEXT NOT NULL,
                model      TEXT NOT NULL,
                value      BLOB NOT NULL,     -- response JSON, or float32 vector bytes
                in_tok     INTEGER, out_tok INTEGER, latency_s REAL,
                created    TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS daily_requests (
                day TEXT NOT NULL, model TEXT NOT NULL, n INTEGER NOT NULL,
                PRIMARY KEY (day, model)
            );
        """)
    return _cache_conn


def _key(kind: str, payload: dict) -> str:
    """Same request -> same key. sort_keys makes dict order irrelevant."""
    blob = json.dumps({"kind": kind, **payload}, sort_keys=True, default=_jsonable)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _jsonable(x: Any) -> Any:
    """Make SDK/pydantic objects hashable-as-JSON (for cache keys)."""
    if isinstance(x, BaseModel):
        return x.model_dump(mode="json", exclude_none=True)
    if isinstance(x, type) and issubclass(x, BaseModel):
        return x.model_json_schema()
    if callable(x):  # e.g. a Python function passed as a tool: use its name, not its memory address
        return f"{x.__module__}.{getattr(x, '__qualname__', repr(x))}"
    return repr(x)


def _cache_get(key: str) -> bytes | None:
    row = _cache().execute("SELECT value FROM cache WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _cache_put(key, kind, role, value, in_tok, out_tok, latency) -> None:
    with _cache():
        _cache().execute(
            "INSERT OR REPLACE INTO cache VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (key, kind, role, config.MODELS[role], value, in_tok, out_tok,
             round(latency, 3), datetime.now().isoformat(timespec="seconds")),
        )


# ============================================================ rate limiting
class RateLimiter:
    """Sliding 60-second window per model role: count requests and tokens.

    Before each call: if one more request (or its tokens) would break the per-minute
    limit, sleep until the oldest call in the window is >60s old. Waiting a few
    seconds is cheap; a 429 error is slower and wastes an attempt.
    """

    def __init__(self, rpm: int, tpm: int):
        self.rpm, self.tpm = rpm, tpm
        self.window: deque[tuple[float, int]] = deque()  # (timestamp, tokens)

    def acquire(self, est_tokens: int) -> None:
        est_tokens = min(est_tokens, self.tpm)  # a single huge call must still pass
        while True:
            now = _now()
            while self.window and now - self.window[0][0] >= 60:
                self.window.popleft()
            used_tok = sum(t for _, t in self.window)
            if len(self.window) < self.rpm and used_tok + est_tokens <= self.tpm:
                self.window.append((now, est_tokens))
                return
            _sleep(max(0.05, 60 - (now - self.window[0][0])))

    def correct_last(self, actual_tokens: int) -> None:
        """Swap our estimate for the real token count once the API tells us."""
        if self.window:
            ts, _ = self.window.pop()
            self.window.append((ts, actual_tokens))


_limiters: dict[str, RateLimiter] = {}


def _limiter(role: str) -> RateLimiter:
    if role not in _limiters:
        lim = config.LIMITS[role]
        _limiters[role] = RateLimiter(lim["rpm"], lim["tpm"])
    return _limiters[role]


def _today_pacific() -> str:
    # Gemini's daily quotas reset at midnight Pacific time, not your local midnight.
    return datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()


def _check_and_count_daily(role: str, n: int = 1) -> None:
    """Refuse to start a call that would go over our daily budget.

    n = how many 'requests' Google will count. For generate calls that's 1. For
    embeddings Google counts EVERY TEXT in a batch as one request (we learned this
    when 35 batch calls carrying 989 texts used up a 1,000/day quota).
    """
    model, day = config.MODELS[role], _today_pacific()
    c = _cache()
    row = c.execute("SELECT n FROM daily_requests WHERE day=? AND model=?", (day, model)).fetchone()
    used = row[0] if row else 0
    budget = config.LIMITS[role]["rpd"]
    if used + n > budget:
        raise QuotaExhausted(f"{model}: {used} of {budget} daily requests used, this call needs {n}. "
                             f"Resets at midnight Pacific. Progress is saved; just rerun later.")
    with c:
        c.execute("INSERT INTO daily_requests VALUES (?, ?, ?) ON CONFLICT(day, model) "
                  "DO UPDATE SET n = n + excluded.n", (day, model, n))


def _estimate_tokens(obj: Any) -> int:
    # Rough rule for English: ~4 characters per token. Only used for the limiter.
    return len(json.dumps(obj, default=_jsonable)) // 4 + 1


# ================================================================ the API call
def _get_client():
    global _client
    if _client is None:
        if not config.GEMINI_API_KEY:
            raise MissingKey("This request is not in cache.db and no GEMINI_API_KEY is set. "
                             "Cached questions replay without a key; new ones need a key in .env "
                             "(free: aistudio.google.com/apikey).")
        # attempts=1 turns OFF the SDK's own silent retries: we found they retried a
        # 503 for minutes with no output. Our _with_retries owns retrying, logs every
        # failure, and knows per-minute 429 (wait) from per-day 429 (stop).
        _client = genai.Client(
            api_key=config.GEMINI_API_KEY,
            http_options=types.HttpOptions(
                timeout=config.HTTP_TIMEOUT_S * 1000,  # milliseconds
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        )
    return _client


def _log(entry: dict) -> None:
    entry = {"ts": datetime.now().isoformat(timespec="seconds"), **entry}
    with open(config.USAGE_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def _retry_delay(err: errors.APIError) -> float | None:
    """Google puts 'retryDelay': '37s' in 429 errors. Use it if present."""
    m = re.search(r"retryDelay['\"]?\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)s", json.dumps(err.details, default=str))
    return float(m.group(1)) if m else None


def _quota_info(err: errors.APIError) -> str | None:
    """'EmbedContentTokensPerMinutePerProjectPerModel-FreeTier=30000': WHICH limit was hit."""
    found: list[str] = []

    def walk(x: Any) -> None:
        if isinstance(x, dict):
            if "quotaId" in x:
                found.append(f"{x['quotaId']}={x.get('quotaValue', '?')}")
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(err.details)
    return ", ".join(found) or None


def _is_daily_quota(err: errors.APIError) -> bool:
    """Per-day quota errors mention 'PerDay' in the violated quota id."""
    return "perday" in json.dumps(err.details, default=str).lower()


def _with_retries(role: str, purpose: str, est_tokens: int, fn, daily_cost: int = 1):
    """Run one API request with limiter, daily budget and retry policy.

    Returns (result, seconds_taken). Policy:
      429 per-day   -> QuotaExhausted (no point retrying today)
      429 per-min   -> sleep Google's retryDelay (or backoff), retry
      5xx / network -> exponential backoff (2, 4, 8, 16s...), retry
      other 4xx     -> raise immediately (it's a bug in our request, retrying won't help)
    """
    model = config.MODELS[role]
    for attempt in range(1, config.MAX_RETRIES + 1):
        _limiter(role).acquire(est_tokens)
        _check_and_count_daily(role, daily_cost)
        start = time.perf_counter()
        try:
            return fn(), time.perf_counter() - start
        except errors.APIError as e:
            code = getattr(e, "code", None)
            _log({"event": "error", "role": role, "model": model, "purpose": purpose,
                  "code": code, "attempt": attempt, "quota": _quota_info(e),
                  "message": (getattr(e, "message", None) or str(e))[:200]})
            if code == 429 and _is_daily_quota(e):
                raise QuotaExhausted(f"{model}: Google says the daily quota is used up.") from e
            if code == 429 or (code is not None and code >= 500):
                wait = _retry_delay(e) or min(60, 2 ** attempt)
                print(f"  [{model}] {code}, waiting {wait:.0f}s (attempt {attempt}/{config.MAX_RETRIES})")
                _sleep(wait)
                continue
            raise
        except NETWORK_ERRORS as e:
            # Timeout or dropped connection: not our request's fault, so retry.
            wait = min(60, 2 ** attempt)
            _log({"event": "error", "role": role, "model": model, "purpose": purpose,
                  "code": type(e).__name__, "attempt": attempt, "message": str(e)[:300]})
            print(f"  [{model}] {type(e).__name__}, waiting {wait}s (attempt {attempt}/{config.MAX_RETRIES})")
            _sleep(wait)
    raise RuntimeError(f"{model}: still failing after {config.MAX_RETRIES} attempts ({purpose})")


def call(role: str, contents: Any, *, system: str | None = None,
         schema: type[BaseModel] | None = None, tools: list | None = None,
         tool_config: types.ToolConfig | None = None,
         temperature: float = 0.0, max_output_tokens: int | None = None,
         purpose: str = "") -> types.GenerateContentResponse:
    """Low-level cached generate call. Returns the full SDK response object.

    temperature=0 -> the most repeatable output for the same input, which is what
    you want for extraction and evaluation.
    """
    request = {"role": role, "model": config.MODELS[role], "contents": contents,
               "system": system, "schema": schema, "tools": tools, "tool_config": tool_config,
               "temperature": temperature, "max_output_tokens": max_output_tokens}

    # Cache lookup under the primary model AND every fallback model, so an answer we
    # already paid for (by whichever model gave it) is always reused.
    for model in _models_for(role):
        cached = _cache_get(_key("gen", {**request, "model": model}))
        if cached is not None:
            return types.GenerateContentResponse.model_validate_json(cached)

    while True:
        try:
            return _generate(role, request, contents, system, schema, tools, tool_config,
                             temperature, max_output_tokens, purpose)
        except QuotaExhausted as e:
            if not _switch_to_fallback(role, str(e)):
                raise


def _models_for(role: str) -> list[str]:
    return [config.MODELS[role]] + [f["model"] for f in config.FALLBACKS.get(role, [])]


_used_fallbacks: set[str] = set()


def _switch_to_fallback(role: str, why: str) -> bool:
    """Daily quota gone for this role's model: switch to the next model, which has its
    OWN separate quota. Returns False when no fallback is left."""
    for fb in config.FALLBACKS.get(role, []):
        if fb["model"] not in _used_fallbacks and fb["model"] != config.MODELS[role]:
            _used_fallbacks.add(fb["model"])
            print(f"  [quota] {why}")
            print(f"  [quota] '{role}' now falls back to {fb['model']}")
            _log({"event": "fallback", "role": role, "from": config.MODELS[role], "to": fb["model"]})
            config.MODELS[role] = fb["model"]
            config.LIMITS[role] = {k: fb[k] for k in ("rpm", "tpm", "rpd")}
            _limiters.pop(role, None)
            return True
    return False


def _generate(role, request, contents, system, schema, tools, tool_config,
              temperature, max_output_tokens, purpose) -> types.GenerateContentResponse:
    request = {**request, "model": config.MODELS[role]}   # model may have changed (fallback)
    key = _key("gen", request)

    cfg = types.GenerateContentConfig(
        system_instruction=system,
        temperature=temperature,
        max_output_tokens=max_output_tokens,
        tools=tools,
        tool_config=tool_config,
        response_mime_type="application/json" if schema else None,
        response_schema=schema,
    )
    resp, secs = _with_retries(
        role, purpose, _estimate_tokens([contents, system]),
        lambda: _get_client().models.generate_content(
            model=config.MODELS[role], contents=contents, config=cfg),
    )

    u = resp.usage_metadata
    in_tok = (u.prompt_token_count or 0) if u else 0
    out_tok = ((u.candidates_token_count or 0) + (u.thoughts_token_count or 0)) if u else 0
    _limiter(role).correct_last(in_tok + out_tok)
    _log({"event": "call", "kind": "gen", "role": role, "model": config.MODELS[role],
          "purpose": purpose, "in_tok": in_tok, "out_tok": out_tok, "latency_s": round(secs, 3)})
    finish = str(resp.candidates[0].finish_reason) if resp.candidates else ""
    if "MAX_TOKENS" in finish:
        # Cut off mid-answer (thinking models spend output tokens on thinking first).
        # Never cache a truncated response, or every rerun would replay the broken one.
        _log({"event": "truncated", "role": role, "model": config.MODELS[role], "purpose": purpose})
        print(f"  [{config.MODELS[role]}] response hit max_output_tokens; not cached")
    else:
        _cache_put(key, "gen", role, resp.model_dump_json(exclude_none=True), in_tok, out_tok, secs)
    return resp


def generate_text(role: str, prompt: Any, *, system: str | None = None,
                  purpose: str = "", **kw) -> str:
    return call(role, prompt, system=system, purpose=purpose, **kw).text or ""


def generate_json(role: str, prompt: Any, *, schema: type[T], system: str | None = None,
                  purpose: str = "", **kw) -> T:
    """Ask for JSON matching a pydantic schema, and validate it.

    Gemini's structured output makes the JSON shape very reliable, but we still
    validate with pydantic: never trust model output blindly.
    """
    resp = call(role, prompt, system=system, schema=schema, purpose=purpose, **kw)
    return schema.model_validate_json(resp.text or "")


# ================================================================ embeddings
def _pack(vec: list[float]) -> bytes:
    return array("f", vec).tobytes()          # float32: 768 dims -> 3 KB


def _unpack(blob: bytes) -> list[float]:
    a = array("f")
    a.frombytes(blob)
    return a.tolist()


def _token_batches(indices: list[int], texts: list[str]) -> list[list[int]]:
    """Group texts into requests capped by BOTH count and estimated tokens.

    Lesson learned: the embedding free tier allows 30k tokens/minute. A batch of
    100 chunks is ~36k tokens - bigger than a whole minute's budget - so it failed
    forever no matter how long we waited. Capping tokens per request fixes that.
    """
    batches: list[list[int]] = []
    cur: list[int] = []
    cur_tok = 0
    for i in indices:
        tok = _estimate_tokens(texts[i])
        if cur and (len(cur) >= config.EMBED_BATCH or cur_tok + tok > config.EMBED_BATCH_TOKENS):
            batches.append(cur)
            cur, cur_tok = [], 0
        cur.append(i)
        cur_tok += tok
    if cur:
        batches.append(cur)
    return batches


def embed(texts: list[str], *, task_type: str = "RETRIEVAL_DOCUMENT",
          role: str = "embed", purpose: str = "") -> list[list[float]]:
    """Embed many texts. Cached PER TEXT, and only the misses are sent, in batches.

    task_type matters: documents are embedded as RETRIEVAL_DOCUMENT and questions as
    RETRIEVAL_QUERY. The model places a question near the passages that ANSWER it,
    not near passages that merely look similar to the question.
    """
    model = config.MODELS[role]
    keys = [_key("emb", {"model": model, "text": t, "task": task_type, "dim": config.EMBED_DIM})
            for t in texts]
    out: list[list[float] | None] = [None] * len(texts)
    missing: list[int] = []
    for i, k in enumerate(keys):
        blob = _cache_get(k)
        if blob is None:
            missing.append(i)
        else:
            out[i] = _unpack(blob)

    for idx in _token_batches(missing, texts):
        batch = [texts[i] for i in idx]
        est = _estimate_tokens(batch)
        # Each text wrapped in its OWN Content. A plain list of strings is read by
        # gemini-embedding-2 as one multi-part input -> one vector for the whole batch.
        contents = [types.Content(parts=[types.Part(text=t)]) for t in batch]
        resp, secs = _with_retries(
            role, purpose, est,
            lambda: _get_client().models.embed_content(
                model=model, contents=contents,
                config=types.EmbedContentConfig(task_type=task_type,
                                                output_dimensionality=config.EMBED_DIM)),
            daily_cost=len(batch),
        )
        _log({"event": "call", "kind": "emb", "role": role, "model": model, "purpose": purpose,
              "n_texts": len(batch), "in_tok": est, "out_tok": 0, "latency_s": round(secs, 3)})
        if len(resp.embeddings) != len(batch):
            # Happens if texts get merged into one input. Silently zipping would pair
            # texts with the wrong vectors, so fail loudly instead.
            raise RuntimeError(f"{model} returned {len(resp.embeddings)} vectors for "
                               f"{len(batch)} texts; inputs were merged, can't batch this way.")
        per_text_secs = secs / len(batch)
        for i, e in zip(idx, resp.embeddings):
            out[i] = list(e.values)
            _cache_put(keys[i], "emb", role, _pack(out[i]),
                       _estimate_tokens(texts[i]), 0, per_text_secs)
    return out  # type: ignore[return-value]


# ===================================================================== report
def report() -> None:
    """Totals of all UNIQUE work done (from the cache), for the write-up's cost/time."""
    rows = _cache().execute("""
        SELECT role, kind, COUNT(*), SUM(in_tok), SUM(out_tok), SUM(latency_s)
        FROM cache GROUP BY role, kind ORDER BY role""").fetchall()
    if not rows:
        print("Cache is empty: no calls made yet.")
        return
    total = 0.0
    print(f"{'role':<6} {'kind':<4} {'items':>7} {'in_tok':>10} {'out_tok':>9} {'time_s':>8} {'paid $':>8}")
    for role, kind, n, i, o, s in rows:
        p = config.PRICES.get(role, {"in": 0, "out": 0})
        cost = (i or 0) / 1e6 * p["in"] + (o or 0) / 1e6 * p["out"]
        total += cost
        print(f"{role:<6} {kind:<4} {n:>7} {i or 0:>10} {o or 0:>9} {s or 0:>8.1f} {cost:>8.4f}")
    print(f"Estimated paid-tier cost of all work so far: ${total:.4f} "
          f"(time excludes rate-limit waiting)")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    if cmd == "ping":
        for role in ("fast", "smart"):
            print(role, config.MODELS[role], "->",
                  generate_text(role, "Reply with exactly: pong", purpose="ping").strip())
        print("embed", config.MODELS["embed"], "->",
              len(embed(["hello world"], purpose="ping")[0]), "dims")
    elif cmd == "models":
        for m in _get_client().models.list():
            print(m.name, getattr(m, "supported_actions", ""))
    else:
        report()
