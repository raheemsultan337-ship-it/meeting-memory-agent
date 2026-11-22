"""Step 2 tests: cache, rate limiter, daily budget, 429 handling, embedding batching.

These use a FAKE Gemini client and a FAKE clock, so they need no API key, cost no
quota, and run in milliseconds. Run:  python -m pytest -v tests/test_llm.py
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.genai import errors, types
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import llm  # noqa: E402


class FakeClock:
    """time that only moves when someone 'sleeps' - lets us test waiting instantly."""
    def __init__(self):
        self.t = 0.0
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def fake_response(text: str, in_tok=10, out_tok=5) -> types.GenerateContentResponse:
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(role="model", parts=[types.Part(text=text)]))],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=in_tok, candidates_token_count=out_tok),
    )


def err_429(per_day=False, delay="7s"):
    quota = "GenerateRequestsPerDayPerProjectPerModel-FreeTier" if per_day \
        else "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"
    return errors.ClientError(429, {"error": {
        "code": 429, "status": "RESOURCE_EXHAUSTED", "message": "quota",
        "details": [
            {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{"quotaId": quota}]},
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay},
        ]}})


class FakeModels:
    """Stands in for client.models. Pops scripted results; counts real calls."""
    def __init__(self):
        self.script = []          # items: a response, or an exception to raise
        self.gen_calls = 0
        self.embed_batches = []

    def generate_content(self, model, contents, config):
        self.gen_calls += 1
        item = self.script.pop(0) if self.script else fake_response("ok")
        if isinstance(item, Exception):
            raise item
        return item

    def embed_content(self, model, contents, config):
        # llm.embed sends each text as its own Content; unwrap back to strings
        texts = [c.parts[0].text if hasattr(c, "parts") else c for c in contents]
        self.embed_batches.append(texts)
        return SimpleNamespace(embeddings=[SimpleNamespace(values=[float(len(t))] * 3) for t in texts])


@pytest.fixture
def fake(tmp_path, monkeypatch):
    """Isolated llm module: temp cache/log, fake client, fake clock, small limits."""
    monkeypatch.setattr(config, "CACHE_DB", tmp_path / "cache.db")
    monkeypatch.setattr(config, "USAGE_LOG", tmp_path / "usage.jsonl")
    monkeypatch.setattr(config, "LIMITS", {
        "fast": {"rpm": 3, "tpm": 1_000_000, "rpd": 5},
        "smart": {"rpm": 3, "tpm": 1_000_000, "rpd": 5},
        "embed": {"rpm": 100, "tpm": 1_000_000, "rpd": 100},
    })
    monkeypatch.setattr(config, "EMBED_BATCH", 2)
    monkeypatch.setattr(config, "MODELS", dict(config.MODELS))   # tests may change it; don't leak
    monkeypatch.setattr(config, "FALLBACKS", {})                 # off unless a test turns it on
    monkeypatch.setattr(llm, "_used_fallbacks", set())
    clock = FakeClock()
    models = FakeModels()
    monkeypatch.setattr(llm, "_now", clock.now)
    monkeypatch.setattr(llm, "_sleep", clock.sleep)
    monkeypatch.setattr(llm, "_client", SimpleNamespace(models=models))
    monkeypatch.setattr(llm, "_cache_conn", None)
    monkeypatch.setattr(llm, "_limiters", {})
    yield SimpleNamespace(models=models, clock=clock, tmp=tmp_path)
    if llm._cache_conn:
        llm._cache_conn.close()


def test_second_identical_call_comes_from_cache(fake):
    fake.models.script = [fake_response("pong")]
    assert llm.generate_text("fast", "say pong") == "pong"
    assert llm.generate_text("fast", "say pong") == "pong"
    assert fake.models.gen_calls == 1                      # API hit only once
    assert llm.generate_text("fast", "say ping") == "ok"   # different prompt -> new call
    assert fake.models.gen_calls == 2


def test_generate_json_validates_schema(fake):
    class Answer(BaseModel):
        answer: str
        n: int
    fake.models.script = [fake_response('{"answer": "rubber", "n": 3}')]
    out = llm.generate_json("fast", "q", schema=Answer)
    assert out == Answer(answer="rubber", n=3)


def test_rate_limiter_waits_instead_of_erroring(fake):
    for i in range(4):                     # rpm=3, so the 4th call must wait
        llm.generate_text("fast", f"prompt {i}")
    assert fake.models.gen_calls == 4
    assert fake.clock.slept and sum(fake.clock.slept) >= 59


def test_per_minute_429_waits_retry_delay_then_succeeds(fake):
    fake.models.script = [err_429(delay="7s"), fake_response("done")]
    assert llm.generate_text("fast", "x") == "done"
    assert 7.0 in fake.clock.slept         # honoured Google's retryDelay


def test_per_day_429_raises_quota_exhausted(fake):
    fake.models.script = [err_429(per_day=True)]
    with pytest.raises(llm.QuotaExhausted):
        llm.generate_text("fast", "x")


def test_local_daily_budget_stops_before_google_does(fake):
    for i in range(5):                     # rpd=5
        llm.generate_text("fast", f"p{i}")
    with pytest.raises(llm.QuotaExhausted):
        llm.generate_text("fast", "one too many")
    assert fake.models.gen_calls == 5      # the 6th never reached the API


def test_server_error_backs_off_and_retries(fake):
    fake.models.script = [errors.ServerError(503, {"error": {"code": 503, "message": "busy"}}),
                          fake_response("recovered")]
    assert llm.generate_text("fast", "x") == "recovered"


def test_bad_request_is_not_retried(fake):
    fake.models.script = [errors.ClientError(400, {"error": {"code": 400, "message": "bad"}})]
    with pytest.raises(errors.ClientError):
        llm.generate_text("fast", "x")
    assert fake.models.gen_calls == 1


def test_embed_batches_and_caches_per_text(fake):
    v1 = llm.embed(["a", "bb", "ccc"])                  # EMBED_BATCH=2 -> 2 requests
    assert fake.models.embed_batches == [["a", "bb"], ["ccc"]]
    assert v1[2] == pytest.approx([3.0, 3.0, 3.0])
    llm.embed(["bb", "dddd"])                           # only the new text is sent
    assert fake.models.embed_batches[-1] == ["dddd"]


def test_usage_log_and_report(fake, capsys):
    llm.generate_text("fast", "x")
    lines = (fake.tmp / "usage.jsonl").read_text().splitlines()
    assert '"event": "call"' in lines[0] and '"latency_s"' in lines[0]
    llm.report()
    assert "Estimated paid-tier cost" in capsys.readouterr().out


def test_timeout_is_retried(fake):
    import httpx2 as httpx
    fake.models.script = [httpx.ReadTimeout("slow"), fake_response("after timeout")]
    assert llm.generate_text("fast", "x") == "after timeout"


def test_embed_fails_loudly_if_model_merges_batch(fake, monkeypatch):
    # Simulates gemini-embedding-2: 2 texts in, 1 vector out.
    monkeypatch.setattr(fake.models, "embed_content",
                        lambda model, contents, config: SimpleNamespace(
                            embeddings=[SimpleNamespace(values=[1.0, 2.0, 3.0])]))
    with pytest.raises(RuntimeError, match="can't batch"):
        llm.embed(["one", "two"])


def test_embed_batches_are_capped_by_tokens(fake, monkeypatch):
    # The real bug: 100 chunks = 36k tokens > the 30k/min embedding limit.
    monkeypatch.setattr(config, "EMBED_BATCH", 100)
    monkeypatch.setattr(config, "EMBED_BATCH_TOKENS", 1_000)
    texts = [f"{i} " + "word " * 300 for i in range(10)]   # ~380 tokens each
    llm.embed(texts)
    sizes = [len(b) for b in fake.models.embed_batches]
    assert sum(sizes) == 10 and max(sizes) <= 2           # ~2 texts per 1k-token request


def test_error_log_records_which_quota(fake):
    fake.models.script = [err_429(delay="1s"), fake_response("ok")]
    llm.generate_text("fast", "x")
    log = (fake.tmp / "usage.jsonl").read_text()
    assert "GenerateRequestsPerMinutePerProjectPerModel-FreeTier" in log


def test_embed_daily_budget_counts_texts_not_calls(fake, monkeypatch):
    # Google counts every text in a batch as one request toward the daily cap.
    monkeypatch.setattr(config, "EMBED_BATCH", 100)
    monkeypatch.setitem(config.LIMITS, "embed", {"rpm": 100, "tpm": 1_000_000, "rpd": 5})
    llm.embed(["a", "b", "c"])                 # 1 call, but 3 of the 5 used
    with pytest.raises(llm.QuotaExhausted):
        llm.embed(["d", "e", "f"])             # would make 6 > 5: refused up front
    assert len(fake.models.embed_batches) == 1
    llm.embed(["a", "b"])                      # cached texts cost nothing


def test_daily_quota_falls_back_to_next_model(fake, monkeypatch):
    monkeypatch.setattr(config, "MODELS", {**config.MODELS, "fast": "primary"})
    monkeypatch.setattr(config, "FALLBACKS", {"fast": [{"model": "backup", "rpm": 3, "tpm": 1_000_000, "rpd": 5}]})
    monkeypatch.setattr(llm, "_used_fallbacks", set())
    fake.models.script = [err_429(per_day=True), fake_response("from backup")]
    assert llm.generate_text("fast", "x") == "from backup"
    assert config.MODELS["fast"] == "backup"
    assert llm.generate_text("fast", "x") == "from backup"   # cached under backup
    assert fake.models.gen_calls == 2


def test_daily_budget_is_per_api_key(fake, monkeypatch):
    # Quotas belong to a key/project: a new key must not inherit another key's usage.
    monkeypatch.setattr(config, "GEMINI_API_KEY", "key-one")
    for i in range(5):                      # rpd=5 in the fixture
        llm.generate_text("fast", f"k1-{i}")
    with pytest.raises(llm.QuotaExhausted):
        llm.generate_text("fast", "k1-over")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "key-two")
    assert llm.generate_text("fast", "k2-first") == "ok"   # fresh budget for the new key
