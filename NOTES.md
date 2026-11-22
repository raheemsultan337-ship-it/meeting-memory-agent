# Build notes: where things broke

Running log for the write-up section "where your tools broke at this size".

## Step 1: parse + keyword index
- **Spelled-out acronyms broke keyword search.** Transcripts write `L_C_D_`, `T_V_` (1,934 lines). FTS5 splits on `_`, so "LCD" found nothing. Fix: a `search_text` copy with acronyms joined; original `text` kept for citations.
- **Not every team has meetings a to d.** IS1002 has no a; IS1005 has no d. The letter must come from the header, not from file order.
- **BM25 can't match meaning.** "how did they stop the remote getting lost?" (ES2005) returns "Did you get my email?" as the top hit, because it matches on "did"/"get". The real answer ("if you lose it and you can whistle") uses different words. That's why we add embeddings (step 3).
- 24% of turns (11,756 / 48,148) are filler ("Mm-hmm.", "Okay."). They're kept for citations and skipped when sending text to the LLM.

## Step 2: LLM wrapper
- Free tier: each model has its own RPM / TPM / RPD. The daily cap (RPD) is the real constraint, and it resets at midnight **Pacific**.
- **Model names changed under us.** `gemini-2.5-flash-lite` returned 404 "no longer available to new users". Switched to `gemini-3.5-flash-lite` (fast) and `gemini-3.5-flash` (smart). Code uses roles, so this was a 2-line config change.
- **The SDK's built-in retries hid a hang.** `gemini-3.8-flash` returned 503 "high demand", and the SDK silently retried for 3+ minutes with no output. Fix: SDK retries off (`attempts=1`), a 90s timeout per request, and our own logged retry policy.
- **`gemini-embedding-2` can't batch.** It returned 1 vector for 2 texts (it merges a list into one input). Silently zipping texts with vectors would have mismatched every chunk. Fix: use `gemini-embedding-001`, plus a hard check that the vector count equals the text count.
- **The thinking model is slow.** `gemini-3.5-flash` took 27–45s to answer "pong" (it spends ~66 tokens "thinking" first). Flash-Lite took about 1.3s. This matters for the agent loop (up to 6 calls per question): we may need to cap the thinking budget or use Flash-Lite for simple steps.
- The cache works: a second identical `ping` (3 calls) took 1.7s total and used zero quota.

## Step 3: chunks + embeddings + hybrid search
- **The embedding free tier is 30k tokens/minute, not 250k.** AI Studio shows 250k TPM for the text models; Google's 429 error said `EmbedContentTokensPerMinutePerProjectPerModel-FreeTier = 30000`. Our first batch (100 chunks, about 36k tokens) was bigger than a whole minute's budget, so it failed on every retry, however long we waited. Fix: batches are capped by **tokens** (20k), not just count, and error logs now record the exact quota id and value.
- The full corpus is 3,334 chunks of about 270 words, about 1.2M tokens. At 27k/min that's about 45 min, once; after that it's cached. Overlap (2 turns) makes up about 37% of those tokens (905k chunk words vs 662k unique), the price of never cutting an exchange in half.
- **A test caught a chunking bug:** a 900-word turn made the overlap logic emit the same turns twice and build a 1,050-word chunk. Fix: track "fresh" (not yet chunked) turns, and drop the overlap if it would overflow.
- **A Python escaping bug in the SQL schema:** `"\n"` inside a SQL comment in a normal Python string became a real newline and broke the schema.
- **Correction:** `gemini-embedding-2` *can* batch. A plain list of strings is read as ONE multi-part input, so each text has to be sent as its own `Content`. We now use embedding-2 that way.
- **Embedding quota is 1,000 TEXTS/day.** 35 batch calls carrying 989 texts used up the whole day (`EmbedContentRequestsPerDayPerUserPerProjectPerModel-FreeTier=1000`). 250-word chunks (3,334) would take 3+ days, so chunks are now about 1,000 words (765) to leave about 200/day for query embeddings. The trade-off is that a vector hit is about 6 minutes of meeting, so the agent narrows down with turn-level keyword search and read_turns. Our local budget now counts texts, not calls. The stop was clean: QuotaExhausted, nothing lost.
- Search degrades gracefully: if the embedding quota is gone, hybrid search falls back to keyword-only.

## Steps 4-8: citations, extraction, timeline, agent, brief, eval
- `gemini-3.5-flash-lite` returns many 503 "high demand" errors at peak times (most meetings needed 1-4 retries). The retry/backoff in llm.py absorbs them; meetings that fail all retries stay `extracted=0` and are picked up on rerun.
- The extraction quote gate repaired about 10% of quotes (the model paraphrased or pointed at the wrong turn) and dropped facts it couldn't verify. Speaker letters are taken from the turns table, not from the model.
- The agent first tried a 33-word quote mixing two sentences; the gate rejected it and the agent fixed it on retry (Q03 test).
- **flash-lite free tier = 500 requests/day** (`GenerateRequestsPerDayPerProjectPerModel-FreeTier=500`), hit during the eval at 19:27. The automatic fallback switched to `gemini-3.6-flash` (own quota), which was itself returning 503s, so retries were raised from 5 to 8.
- **A truncated JSON response got cached** (the brief: the thinking model spent its output tokens thinking). Fix: responses with finish_reason MAX_TOKENS are never cached, and the brief's budget is raised to 32k.
- Measured per meeting: extraction 8.9k in / 1.3k out tokens, 7.0 s; embeddings 7.1k tokens. About 120 503/504 errors over the day, all absorbed by retries.
