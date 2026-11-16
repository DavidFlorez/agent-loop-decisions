"""Unit tests for GuardedDecisionClient, cache stability, dollar guard, and retries."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from agent_loop_decisions.client import (
    CapExceededError,
    DecisionCache,
    GuardedDecisionClient,
    compute_cache_key,
    estimate_request_chars,
    estimate_tokens_from_chars,
    tokens_to_dollars,
)
from agent_loop_decisions.config import MAIN_QUESTIONS


class _FakeError(Exception):
    def __init__(self, code: int, details: dict, message: str = "error") -> None:
        super().__init__(message)
        self.code = code
        self.details = details
        self.message = message


class _FakeBackend:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def create(self, *, model: str, input: object, questions: dict) -> object:
        self.calls.append({"model": model, "input": input, "questions": questions})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _ok_resp(tokens: int = 3000, stuck: float = 0.2, progress: float = 0.8, doomed: float = 0.3) -> object:
    return SimpleNamespace(
        answers={
            "stuck": SimpleNamespace(type="noul", noul=stuck, confidence=max(stuck, 1 - stuck)),
            "progress": SimpleNamespace(type="noul", noul=progress, confidence=max(progress, 1 - progress)),
            "doomed": SimpleNamespace(type="noul", noul=doomed, confidence=max(doomed, 1 - doomed)),
        },
        usage=SimpleNamespace(total_input_tokens=tokens, total_output_tokens=0),
    )


def test_cache_key_stability() -> None:
    inp1 = [{"type": "text", "text": "hello world"}]
    q1 = {
        "stuck": {"type": "noul", "instructions": "Is it stuck?", "criteria": {"true_description": "yes", "false_description": "no"}},
        "doomed": {"type": "noul", "instructions": "Is it doomed?"},
    }
    q2 = {
        "doomed": {"instructions": "Is it doomed?", "type": "noul"},
        "stuck": {"criteria": {"false_description": "no", "true_description": "yes"}, "instructions": "Is it stuck?", "type": "noul"},
    }
    assert compute_cache_key("gemini-decision-1", inp1, q1) == compute_cache_key("gemini-decision-1", inp1, q2)
    assert compute_cache_key("gemini-decision-1", inp1, q1) != compute_cache_key("other-model", inp1, q1)
    assert compute_cache_key("gemini-decision-1", [{"type": "text", "text": "hello world!"}], q1) != compute_cache_key("gemini-decision-1", inp1, q1)


def test_cache_hit_never_pays_twice_and_gz_roundtrip(tmp_path: Path) -> None:
    backend = _FakeBackend([_ok_resp(tokens=10_000)])
    client = GuardedDecisionClient(phase="pilot", cap_usd=0.25, cache_dir=tmp_path, backend=backend)

    r1 = client.decide(shared_text="STEP 1", questions=MAIN_QUESTIONS)
    assert not r1.cached
    assert r1.total_input_tokens == 10_000
    assert r1.cost_usd == pytest.approx(0.001)
    assert len(backend.calls) == 1
    client.flush()

    # Delete uncompressed JSONL to verify reload from gzipped cache alone
    (tmp_path / "decisions.jsonl").unlink()

    backend2 = _FakeBackend([])
    client2 = GuardedDecisionClient(phase="pilot", cap_usd=0.25, cache_dir=tmp_path, backend=backend2)
    r2 = client2.decide(shared_text="STEP 1", questions=MAIN_QUESTIONS)
    assert r2.cached
    assert r2.cache_key == r1.cache_key
    assert r2.answers["stuck"]["noul"] == pytest.approx(0.2)
    assert len(backend2.calls) == 0
    assert client2.spend.total_spent_usd == pytest.approx(0.001)


def test_dollar_guard_refuses_call_over_phase_cap_and_global_cap(tmp_path: Path) -> None:
    # 30,000 chars -> 10,000 est tokens -> $0.001 per call
    big_text = "x" * 30_000
    backend = _FakeBackend([_ok_resp(tokens=10_000), _ok_resp(tokens=10_000)])
    client = GuardedDecisionClient(
        phase="pilot",
        cap_usd=0.0015,
        global_cap_usd=10.0,
        cache_dir=tmp_path,
        backend=backend,
    )
    r1 = client.decide(shared_text=big_text + "1", questions=MAIN_QUESTIONS)
    assert r1.cost_usd == pytest.approx(0.001)

    with pytest.raises(CapExceededError):
        client.decide(shared_text=big_text + "2", questions=MAIN_QUESTIONS)
    assert len(backend.calls) == 1
    assert (tmp_path / "spend.json").exists()
    assert (tmp_path / "decisions.jsonl.gz").exists()

    # Global cap enforcement across phases
    client_tb = GuardedDecisionClient(
        phase="tb",
        cap_usd=3.0,
        global_cap_usd=0.0015,
        cache_dir=tmp_path,
        backend=backend,
    )
    with pytest.raises(CapExceededError):
        client_tb.decide(shared_text=big_text + "3", questions=MAIN_QUESTIONS)
    assert len(backend.calls) == 1


def test_safety_block_not_retried_and_cached(tmp_path: Path) -> None:
    block_err = _FakeError(400, {"error": {"code": "safety", "message": "blocked"}})
    backend = _FakeBackend([block_err])
    client = GuardedDecisionClient(phase="pilot", cap_usd=0.25, cache_dir=tmp_path, backend=backend)
    res = client.decide(shared_text="unsafe text", questions=MAIN_QUESTIONS)
    assert res.status == "blocked"
    assert res.block_code == "safety"
    assert res.cost_usd == 0.0
    assert len(backend.calls) == 1
    assert client.spend.state["total_blocked"] == 1

    # Re-run hits cache without calling backend
    res2 = client.decide(shared_text="unsafe text", questions=MAIN_QUESTIONS)
    assert res2.cached
    assert res2.status == "blocked"
    assert len(backend.calls) == 1


def test_retry_on_429_and_503(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("time.sleep", lambda _s: None)
    e429 = _FakeError(429, {"error": {"code": 429, "message": "rate limit"}})
    e503 = _FakeError(503, {"error": {"code": 503, "message": "unavailable"}})
    backend = _FakeBackend([e429, e503, _ok_resp(tokens=5000)])
    client = GuardedDecisionClient(phase="pilot", cap_usd=0.25, cache_dir=tmp_path, backend=backend)
    res = client.decide(shared_text="retry test", questions=MAIN_QUESTIONS)
    assert res.status == "ok"
    assert res.total_input_tokens == 5000
    assert len(backend.calls) == 3


class _CompatStatusError(Exception):
    def __init__(self, status_code: int, body: object, message: str = "error") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body
        self.message = message


def test_compat_errors_status_code_and_body(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("time.sleep", lambda _s: None)
    e429 = _CompatStatusError(429, {"error": {"code": 429, "message": "rate limit"}})
    block = _CompatStatusError(400, '{"error": {"code": "safety", "message": "blocked"}}')
    backend = _FakeBackend([e429, block])
    client = GuardedDecisionClient(phase="pilot", cap_usd=0.25, cache_dir=tmp_path, backend=backend)
    res = client.decide(shared_text="compat error test", questions=MAIN_QUESTIONS)
    assert res.status == "blocked"
    assert res.block_code == "safety"
    assert len(backend.calls) == 2


def test_concurrent_budget_reservation_prevents_overrun(tmp_path: Path) -> None:
    sample_text = "x" * 600
    est_tok = estimate_tokens_from_chars(
        estimate_request_chars([{"type": "text", "text": sample_text}], MAIN_QUESTIONS)
    )
    est_cost = tokens_to_dollars(est_tok)
    cap_usd = est_cost * 2.4  # fits 2 concurrent calls, rejects a 3rd

    class _SlowBackend:
        def __init__(self) -> None:
            self.calls = 0

        def create(self, *, model: str, input: object, questions: dict) -> object:
            time.sleep(0.05)
            self.calls += 1
            return _ok_resp(tokens=int(est_tok))

    client = GuardedDecisionClient(
        phase="swe",
        cap_usd=cap_usd,
        global_cap_usd=10.0,
        cache_dir=tmp_path,
        max_rpm=10_000,
        max_tpm=10_000_000,
        backend=_SlowBackend(),
    )

    def _worker(idx: int) -> str:
        try:
            res = client.decide(shared_text=f"{sample_text}_{idx}", questions=MAIN_QUESTIONS)
            return res.status
        except CapExceededError:
            return "cap"

    with ThreadPoolExecutor(max_workers=6) as pool:
        outcomes = list(pool.map(_worker, range(6)))

    assert outcomes.count("ok") == 2
    assert outcomes.count("cap") == 4
    assert client.spend.phase_spent_usd("swe") <= cap_usd + 1e-12

