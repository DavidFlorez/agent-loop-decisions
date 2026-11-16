"""Guarded Gemini Decisions API client with disk cache, dollar cap, rate limiter, and retries."""

from __future__ import annotations

import gzip
import hashlib
import os
import random
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import orjson

from agent_loop_decisions.config import (
    API_VERSION,
    CACHE_DIR,
    DECISION_MODEL,
    DEFAULT_GLOBAL_CAP_USD,
    DEFAULT_MAX_RPM,
    DEFAULT_MAX_TPM,
    ESTIMATE_CHARS_PER_TOKEN,
    MAX_RETRIES,
    PRICE_PER_M_INPUT_TOKENS_USD,
    SAFETY_BLOCK_CODES,
)


class CapExceededError(RuntimeError):
    """Raised when an uncached API call would exceed the phase or global dollar cap."""

    def __init__(self, message: str, *, summary: dict[str, Any]) -> None:
        super().__init__(message)
        self.summary = summary


class DecisionsBackend(Protocol):
    """Minimal protocol matching google.genai client.decisions.create."""

    def create(self, *, model: str, input: Any, questions: dict[str, Any]) -> Any: ...


@dataclass(slots=True)
class DecisionCallResult:
    """Normalized result for one Decisions API request (cached, live, or blocked)."""

    cache_key: str
    status: str  # "ok" | "blocked"
    answers: dict[str, dict[str, Any]] | None
    total_input_tokens: int
    cost_usd: float
    est_chars: int
    est_tokens: float
    latency_ms: float
    cached: bool
    block_code: str | None = None
    block_message: str | None = None


def compute_cache_key(model: str, input_payload: Any, questions: dict[str, Any]) -> str:
    """Return deterministic sha256(model + canonical JSON of input + questions)."""
    h = hashlib.sha256()
    h.update(model.encode("utf-8"))
    h.update(b"\x00")
    h.update(orjson.dumps(input_payload, option=orjson.OPT_SORT_KEYS))
    h.update(b"\x00")
    h.update(orjson.dumps(questions, option=orjson.OPT_SORT_KEYS))
    return h.hexdigest()


def _count_text_chars(obj: Any) -> int:
    """Count total string characters across nested input/question structures."""
    if obj is None:
        return 0
    if isinstance(obj, str):
        return len(obj)
    if isinstance(obj, dict):
        # Count string keys (for question/criteria keys) and nested values
        total = 0
        for k, v in obj.items():
            if k not in ("type", "text", "instructions", "criteria", "true_description", "false_description"):
                total += len(str(k))
            total += _count_text_chars(v)
        return total
    if isinstance(obj, (list, tuple)):
        return sum(_count_text_chars(item) for item in obj)
    return len(str(obj))


def estimate_request_chars(input_payload: Any, questions: dict[str, Any]) -> int:
    """Estimate character length of shared input plus question instructions/criteria."""
    return _count_text_chars(input_payload) + _count_text_chars(questions)


def estimate_tokens_from_chars(chars: int) -> float:
    """Conservative token estimate: chars / 3.0."""
    return float(chars) / ESTIMATE_CHARS_PER_TOKEN


def tokens_to_dollars(tokens: float) -> float:
    """Convert input token count to USD at $0.10 / 1M tokens."""
    return (float(tokens) / 1_000_000.0) * PRICE_PER_M_INPUT_TOKENS_USD


def _error_status_code(err: Exception) -> int | None:
    """Return HTTP status code from google-genai compat_errors (.status_code) or mock/SDK (.code)."""
    for attr in ("status_code", "code"):
        val = getattr(err, attr, None)
        if isinstance(val, int):
            return val
    return None


def _extract_safety_block(err: Exception) -> tuple[str | None, str | None]:
    """Return (code, message) if err is an HTTP 400 safety-filter block, else (None, None)."""
    status_code = _error_status_code(err)
    if status_code != 400:
        return None, None
    payload = getattr(err, "details", None)
    if payload is None:
        payload = getattr(err, "body", None)
    if isinstance(payload, str):
        try:
            payload = orjson.loads(payload)
        except orjson.JSONDecodeError:
            payload = None
    err_code: str | None = None
    err_msg: str | None = getattr(err, "message", None)
    if isinstance(payload, dict):
        inner = payload.get("error") if isinstance(payload.get("error"), dict) else payload
        raw_code = inner.get("code")
        if isinstance(raw_code, str):
            err_code = raw_code.strip().lower()
        if isinstance(inner.get("message"), str):
            err_msg = inner["message"]
    if err_code in SAFETY_BLOCK_CODES:
        return err_code, err_msg
    return None, None


def _is_retryable_error(err: Exception) -> bool:
    """Return True for HTTP 429, HTTP 5xx, or transient transport/timeout failures."""
    code = _error_status_code(err)
    if code is not None:
        if code in (408, 429) or 500 <= code < 600:
            return True
        if 400 <= code < 500:
            return False
    # Transport/network exceptions (httpx, requests, TimeoutError, ConnectionError, etc.)
    err_name = type(err).__name__.lower()
    if any(tok in err_name for tok in ("timeout", "connect", "network", "protocol", "readerror", "remote")):
        return True
    if isinstance(err, (TimeoutError, ConnectionError, OSError)):
        return True
    return False


class DecisionCache:
    """Append-only JSONL cache backed by gzipped snapshot for git reproducibility."""

    def __init__(self, cache_dir: Path = CACHE_DIR) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.jsonl_path = self.cache_dir / "decisions.jsonl"
        self.gz_path = self.cache_dir / "decisions.jsonl.gz"
        self._lock = threading.Lock()
        self._records: dict[str, dict[str, Any]] = {}
        self._load_existing()

    def _ingest_lines(self, lines: Any) -> int:
        added = 0
        for raw in lines:
            raw = raw.strip()
            if not raw:
                continue
            rec = orjson.loads(raw)
            key = rec.get("key")
            if isinstance(key, str) and key not in self._records:
                self._records[key] = rec
                added += 1
        return added

    def _load_existing(self) -> None:
        if self.gz_path.exists():
            with gzip.open(self.gz_path, "rb") as fh:
                self._ingest_lines(fh)
        extra_from_jsonl = 0
        if self.jsonl_path.exists():
            with self.jsonl_path.open("rb") as fh:
                extra_from_jsonl = self._ingest_lines(fh)
        # Hydrate local uncompressed append-only JSONL if it wasn't present (e.g. fresh clone)
        if not self.jsonl_path.exists() and self._records:
            tmp = self.jsonl_path.with_suffix(".jsonl.tmp")
            with tmp.open("wb") as fh:
                for rec in self._records.values():
                    fh.write(orjson.dumps(rec))
                    fh.write(b"\n")
            tmp.replace(self.jsonl_path)
        elif extra_from_jsonl > 0 and not self.gz_path.exists():
            self.export_gzip()

    def __len__(self) -> int:
        return len(self._records)

    def get(self, key: str) -> dict[str, Any] | None:
        return self._records.get(key)

    def put(self, record: dict[str, Any]) -> None:
        key = record["key"]
        with self._lock:
            if key in self._records:
                return
            self._records[key] = record
            with self.jsonl_path.open("ab") as fh:
                fh.write(orjson.dumps(record))
                fh.write(b"\n")
                fh.flush()

    def export_gzip(self) -> Path:
        """Atomically write deduplicated cache to decisions.jsonl.gz."""
        with self._lock:
            tmp = self.gz_path.with_suffix(".jsonl.gz.tmp")
            with gzip.open(tmp, "wb", compresslevel=6) as fh:
                for rec in self._records.values():
                    fh.write(orjson.dumps(rec))
                    fh.write(b"\n")
            tmp.replace(self.gz_path)
        return self.gz_path


class SpendTracker:
    """Persistent spend accounting in cache/spend.json."""

    def __init__(self, cache_dir: Path = CACHE_DIR) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.cache_dir / "spend.json"
        self._lock = threading.Lock()
        self.state: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        if self.path.exists():
            data = orjson.loads(self.path.read_bytes())
            data.setdefault("total_spent_usd", 0.0)
            data.setdefault("total_input_tokens", 0)
            data.setdefault("total_calls", 0)
            data.setdefault("total_blocked", 0)
            data.setdefault("by_phase", {})
            return data
        return {
            "total_spent_usd": 0.0,
            "total_input_tokens": 0,
            "total_calls": 0,
            "total_blocked": 0,
            "by_phase": {},
        }

    def phase_spent_usd(self, phase: str) -> float:
        return float(self.state["by_phase"].get(phase, {}).get("spent_usd", 0.0))

    @property
    def total_spent_usd(self) -> float:
        return float(self.state.get("total_spent_usd", 0.0))

    def record_call(self, phase: str, input_tokens: int, cost_usd: float, *, blocked: bool = False) -> None:
        with self._lock:
            self.state["total_input_tokens"] = int(self.state["total_input_tokens"]) + int(input_tokens)
            self.state["total_spent_usd"] = float(self.state["total_spent_usd"]) + float(cost_usd)
            if blocked:
                self.state["total_blocked"] = int(self.state["total_blocked"]) + 1
            else:
                self.state["total_calls"] = int(self.state["total_calls"]) + 1
            ph = self.state["by_phase"].setdefault(
                phase,
                {"spent_usd": 0.0, "input_tokens": 0, "calls": 0, "blocked": 0},
            )
            ph["input_tokens"] = int(ph["input_tokens"]) + int(input_tokens)
            ph["spent_usd"] = float(ph["spent_usd"]) + float(cost_usd)
            if blocked:
                ph["blocked"] = int(ph["blocked"]) + 1
            else:
                ph["calls"] = int(ph["calls"]) + 1
            self.save()

    def save(self) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_bytes(orjson.dumps(self.state, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS))
        tmp.replace(self.path)


class SlidingWindowRateLimiter:
    """Thread-safe 60-second sliding-window RPM and TPM rate limiter."""

    def __init__(self, max_rpm: int = DEFAULT_MAX_RPM, max_tpm: int = DEFAULT_MAX_TPM) -> None:
        self.max_rpm = max(1, int(max_rpm))
        self.max_tpm = max(1, int(max_tpm))
        self._lock = threading.Lock()
        self._events: deque[list[float]] = deque()  # [timestamp, tokens]

    def acquire(self, est_tokens: float) -> list[float]:
        """Block until sending est_tokens stays under max_rpm and max_tpm."""
        tok_need = min(float(est_tokens), float(self.max_tpm))
        while True:
            with self._lock:
                now = time.monotonic()
                cutoff = now - 60.0
                while self._events and self._events[0][0] <= cutoff:
                    self._events.popleft()
                cur_rpm = len(self._events)
                cur_tpm = sum(ev[1] for ev in self._events)
                if cur_rpm < self.max_rpm and (cur_tpm + tok_need <= self.max_tpm or cur_rpm == 0):
                    ev = [now, float(est_tokens)]
                    self._events.append(ev)
                    return ev
                wait_s = max(0.05, (self._events[0][0] + 60.05) - now)
            time.sleep(min(wait_s, 5.0))

    @staticmethod
    def update_tokens(event_ref: list[float], actual_tokens: int) -> None:
        event_ref[1] = float(actual_tokens)


def _read_key_from_dotenv(env_path: Path, var_name: str = "GEMINI_API_KEY") -> str | None:
    if not env_path.is_file():
        return None
    found: str | None = None
    for raw in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k.strip() == var_name:
            val = v.strip().strip('"').strip("'")
            if val:
                found = val
    return found


def _resolve_gemini_api_key() -> str | None:
    repo_root = Path(__file__).resolve().parent.parent
    local_env = _read_key_from_dotenv(repo_root / ".env")
    if local_env:
        return local_env
    env_val = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if env_val:
        return env_val
    return _read_key_from_dotenv(Path.home() / ".env")


class GuardedDecisionClient:
    """Single guarded entry point for all Decisions API calls."""

    def __init__(
        self,
        *,
        phase: str,
        cap_usd: float,
        global_cap_usd: float = DEFAULT_GLOBAL_CAP_USD,
        cache_dir: Path = CACHE_DIR,
        model: str = DECISION_MODEL,
        max_rpm: int = DEFAULT_MAX_RPM,
        max_tpm: int = DEFAULT_MAX_TPM,
        max_retries: int = MAX_RETRIES,
        backend: DecisionsBackend | None = None,
    ) -> None:
        self.phase = phase
        self.cap_usd = float(cap_usd)
        self.global_cap_usd = float(global_cap_usd)
        self.model = model
        self.max_retries = max_retries
        self.cache = DecisionCache(cache_dir=cache_dir)
        self.spend = SpendTracker(cache_dir=cache_dir)
        self.limiter = SlidingWindowRateLimiter(max_rpm=max_rpm, max_tpm=max_tpm)
        self._backend = backend
        self._genai_client: Any = None
        self._backend_lock = threading.Lock()
        self._budget_lock = threading.Lock()
        self.session_api_calls = 0
        self.session_cache_hits = 0
        self.session_blocked = 0
        self.session_spent_usd = 0.0
        self.session_input_tokens = 0
        self.session_latencies_ms: list[float] = []

    def _get_backend(self) -> DecisionsBackend:
        with self._backend_lock:
            if self._backend is not None:
                return self._backend
            api_key = _resolve_gemini_api_key()
            if not api_key:
                raise RuntimeError("GEMINI_API_KEY is not set in .env, environment, or ~/.env.")
            from google import genai

            genai_client = genai.Client(
                api_key=api_key,
                http_options={"api_version": API_VERSION},
            )
            self._genai_client = genai_client
            self._backend = genai_client.decisions
            return self._backend

    def summary(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "phase_cap_usd": self.cap_usd,
            "global_cap_usd": self.global_cap_usd,
            "phase_spent_usd": self.spend.phase_spent_usd(self.phase),
            "total_spent_usd": self.spend.total_spent_usd,
            "session_api_calls": self.session_api_calls,
            "session_cache_hits": self.session_cache_hits,
            "session_blocked": self.session_blocked,
            "session_input_tokens": self.session_input_tokens,
            "session_spent_usd": self.session_spent_usd,
            "cache_size": len(self.cache),
        }

    def flush(self) -> None:
        self.spend.save()
        self.cache.export_gzip()

    def decide(
        self,
        *,
        shared_text: str,
        questions: dict[str, dict[str, Any]],
    ) -> DecisionCallResult:
        """Evaluate questions over shared_text with disk cache, dollar guard, rate limit, and retries."""
        input_payload = [{"type": "text", "text": shared_text}]
        key = compute_cache_key(self.model, input_payload, questions)

        cached = self.cache.get(key)
        if cached is not None:
            self.session_cache_hits += 1
            usage = cached.get("usage") or {}
            return DecisionCallResult(
                cache_key=key,
                status=str(cached.get("status", "ok")),
                answers=cached.get("answers"),
                total_input_tokens=int(usage.get("total_input_tokens", 0)),
                cost_usd=float(cached.get("cost_usd", 0.0)),
                est_chars=int(cached.get("est_chars", 0)),
                est_tokens=float(cached.get("est_tokens", 0.0)),
                latency_ms=float(cached.get("latency_ms", 0.0)),
                cached=True,
                block_code=cached.get("block_code"),
                block_message=cached.get("block_message"),
            )

        est_chars = estimate_request_chars(input_payload, questions)
        est_tokens = estimate_tokens_from_chars(est_chars)
        est_cost = tokens_to_dollars(est_tokens)

        with self._budget_lock:
            phase_spent = self.spend.phase_spent_usd(self.phase)
            total_spent = self.spend.total_spent_usd
            if phase_spent + est_cost > self.cap_usd + 1e-12:
                self.flush()
                raise CapExceededError(
                    f"Phase '{self.phase}' cap exceeded: spent=${phase_spent:.6f} + "
                    f"est=${est_cost:.6f} > cap=${self.cap_usd:.6f}",
                    summary=self.summary(),
                )
            if total_spent + est_cost > self.global_cap_usd + 1e-12:
                self.flush()
                raise CapExceededError(
                    f"Global cap exceeded: total_spent=${total_spent:.6f} + "
                    f"est=${est_cost:.6f} > global_cap=${self.global_cap_usd:.6f}",
                    summary=self.summary(),
                )

        backend = self._get_backend()
        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            ev_ref = self.limiter.acquire(est_tokens)
            t0 = time.perf_counter()
            try:
                res = backend.create(
                    model=self.model,
                    input=input_payload,
                    questions=questions,
                )
                latency_ms = (time.perf_counter() - t0) * 1000.0
                usage_obj = getattr(res, "usage", None)
                total_in_tok = int(getattr(usage_obj, "total_input_tokens", 0) or 0)
                self.limiter.update_tokens(ev_ref, total_in_tok)
                real_cost = tokens_to_dollars(total_in_tok)

                raw_answers = getattr(res, "answers", None) or {}
                norm_answers: dict[str, dict[str, Any]] = {}
                for qk, ans in raw_answers.items():
                    atype = getattr(ans, "type", "noul")
                    entry: dict[str, Any] = {
                        "type": atype,
                        "confidence": float(getattr(ans, "confidence", 0.0)),
                    }
                    if hasattr(ans, "noul") and getattr(ans, "noul") is not None:
                        entry["noul"] = float(getattr(ans, "noul"))
                    if hasattr(ans, "choice") and getattr(ans, "choice") is not None:
                        entry["choice"] = getattr(ans, "choice")
                    if hasattr(ans, "score") and getattr(ans, "score") is not None:
                        entry["score"] = float(getattr(ans, "score"))
                    norm_answers[str(qk)] = entry

                cache_record = {
                    "key": key,
                    "model": self.model,
                    "status": "ok",
                    "answers": norm_answers,
                    "usage": {
                        "total_input_tokens": total_in_tok,
                        "total_output_tokens": 0,
                    },
                    "est_chars": est_chars,
                    "est_tokens": est_tokens,
                    "cost_usd": real_cost,
                    "latency_ms": round(latency_ms, 2),
                }
                self.cache.put(cache_record)
                self.spend.record_call(self.phase, total_in_tok, real_cost, blocked=False)
                self.session_api_calls += 1
                self.session_input_tokens += total_in_tok
                self.session_spent_usd += real_cost
                self.session_latencies_ms.append(latency_ms)

                return DecisionCallResult(
                    cache_key=key,
                    status="ok",
                    answers=norm_answers,
                    total_input_tokens=total_in_tok,
                    cost_usd=real_cost,
                    est_chars=est_chars,
                    est_tokens=est_tokens,
                    latency_ms=latency_ms,
                    cached=False,
                )
            except Exception as exc:
                latency_ms = (time.perf_counter() - t0) * 1000.0
                block_code, block_msg = _extract_safety_block(exc)
                if block_code is not None:
                    cache_record = {
                        "key": key,
                        "model": self.model,
                        "status": "blocked",
                        "block_code": block_code,
                        "block_message": block_msg,
                        "answers": None,
                        "usage": {"total_input_tokens": 0, "total_output_tokens": 0},
                        "est_chars": est_chars,
                        "est_tokens": est_tokens,
                        "cost_usd": 0.0,
                        "latency_ms": round(latency_ms, 2),
                    }
                    self.cache.put(cache_record)
                    self.spend.record_call(self.phase, 0, 0.0, blocked=True)
                    self.session_blocked += 1
                    return DecisionCallResult(
                        cache_key=key,
                        status="blocked",
                        answers=None,
                        total_input_tokens=0,
                        cost_usd=0.0,
                        est_chars=est_chars,
                        est_tokens=est_tokens,
                        latency_ms=latency_ms,
                        cached=False,
                        block_code=block_code,
                        block_message=block_msg,
                    )

                last_err = exc
                if not _is_retryable_error(exc) or attempt >= self.max_retries - 1:
                    self.flush()
                    raise
                backoff = min(60.0, (2.0**attempt) + random.uniform(0.1, 0.8))
                time.sleep(backoff)

        assert last_err is not None
        self.flush()
        raise last_err
