"""Decision 1 loop-stopping rules and Gemini CLI adaptive LLM arm."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Sequence

from agent_loop_decisions.client import DecisionCallResult, GuardedDecisionClient
from agent_loop_decisions.config import (
    GEMINI_CLI_CONFIDENCE_THRESHOLD,
    GEMINI_CLI_DEFAULT_INTERVAL,
    GEMINI_CLI_HISTORY_COUNT,
    GEMINI_CLI_LLM_CHECK_AFTER_TURNS,
    GEMINI_CLI_MAX_INTERVAL,
    GEMINI_CLI_MIN_INTERVAL,
    GEMINI_CLI_QUESTIONS,
    GRID_STRIDE,
    MAIN_QUESTIONS,
    MAIN_WINDOW_STEPS,
    THRESHOLDS,
)
from agent_loop_decisions.data import RunMetadata, SelectedTrajectory, main_grid_steps
from agent_loop_decisions.render import render_decision_window, slice_prefix_steps

GRID_STOPPER_FAMILIES: tuple[str, ...] = ("d1_stuck", "d1_stuck2", "d1_doomed", "d1_noprog")


@dataclass(slots=True)
class GridPointRecord:
    """Evaluated Decision 1 probabilities and billing at one decision step."""

    uid: str
    corpus: str
    step_idx: int
    status: str  # "ok" | "blocked"
    p_stuck: float | None
    p_progress: float | None
    p_doomed: float | None
    total_input_tokens: int
    cost_usd: float
    est_chars: int
    est_tokens: float
    latency_ms: float
    cached: bool
    block_code: str | None = None
    active_strict_in_window: bool = False


@dataclass(slots=True)
class GeminiCliRunResult:
    """Result of replaying Gemini CLI's adaptive LLM loop check stand-in on one run."""

    uid: str
    corpus: str
    hash_stop_step: int | None
    llm_stop_step: int | None
    full_stop_step: int | None
    checks_run: int
    blocked_checks: int
    total_input_tokens: int
    api_cost_usd: float
    latencies_ms: list[float]


def min_non_none(a: int | None, b: int | None) -> int | None:
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def js_math_round(x: float) -> int:
    """Match JavaScript Math.round for non-negative numbers (half rounds up)."""
    return int(math.floor(x + 0.5))


def update_gemini_cli_interval(p_loop: float) -> int:
    """Compute next adaptive check interval matching gemini-cli loopDetectionService.ts."""
    p_clamped = max(0.0, min(1.0, float(p_loop)))
    raw = GEMINI_CLI_MIN_INTERVAL + (GEMINI_CLI_MAX_INTERVAL - GEMINI_CLI_MIN_INTERVAL) * (1.0 - p_clamped)
    return max(GEMINI_CLI_MIN_INTERVAL, min(GEMINI_CLI_MAX_INTERVAL, js_math_round(raw)))


def has_active_strict_loop_in_window(
    strict_flags: Sequence[bool],
    *,
    step_idx: int,
    window_steps: int = MAIN_WINDOW_STEPS,
) -> bool:
    """Return True if any step inside the rendered causal window [max(0, step_idx - W + 1) .. step_idx] is strict-loop."""
    lo = max(0, step_idx - window_steps + 1)
    hi = min(len(strict_flags) - 1, step_idx)
    if hi < lo:
        return False
    return any(strict_flags[i] for i in range(lo, hi + 1))


def evaluate_main_grid_run(
    run: RunMetadata,
    trace: SelectedTrajectory,
    client: GuardedDecisionClient,
    *,
    window_steps: int = MAIN_WINDOW_STEPS,
) -> list[GridPointRecord]:
    """Query/read Decision 1 at all Main Grid points t = 4, 8, ... < min(n_steps, 200)."""
    out: list[GridPointRecord] = []
    for t in main_grid_steps(run.n_steps):
        prefix = slice_prefix_steps(trace.steps, t)
        shared_text = render_decision_window(
            trace.task_text,
            prefix,
            step_idx=t,
            window_steps=window_steps,
        )
        res: DecisionCallResult = client.decide(shared_text=shared_text, questions=MAIN_QUESTIONS)
        p_stuck = res.answers["stuck"]["noul"] if res.status == "ok" and res.answers else None
        p_progress = res.answers["progress"]["noul"] if res.status == "ok" and res.answers else None
        p_doomed = res.answers["doomed"]["noul"] if res.status == "ok" and res.answers else None
        out.append(
            GridPointRecord(
                uid=run.uid,
                corpus=run.corpus,
                step_idx=t,
                status=res.status,
                p_stuck=p_stuck,
                p_progress=p_progress,
                p_doomed=p_doomed,
                total_input_tokens=res.total_input_tokens,
                cost_usd=res.cost_usd,
                est_chars=res.est_chars,
                est_tokens=res.est_tokens,
                latency_ms=res.latency_ms,
                cached=res.cached,
                block_code=res.block_code,
                active_strict_in_window=has_active_strict_loop_in_window(
                    trace.strict_loop_flags, step_idx=t, window_steps=window_steps
                ),
            )
        )
    return out


def score_grid_stopper(
    grid_points: Sequence[GridPointRecord],
    *,
    family: str,
    threshold: float,
) -> tuple[int | None, float]:
    """Apply a Decision 1 grid stopping rule over sorted grid points; return (stop_step, deployment_api_cost_usd).

    Deployment API cost stops accumulating once the stopper fires (or includes all points if never fires).
    Blocked points never trigger a stop and reset consecutive confirmation for d1_stuck2.
    """
    pts = sorted(grid_points, key=lambda p: p.step_idx)
    spent_usd = 0.0
    prev_stuck_ok_step: int | None = None

    for pt in pts:
        spent_usd += float(pt.cost_usd)
        if pt.status != "ok":
            prev_stuck_ok_step = None
            continue

        fired = False
        if family == "d1_stuck":
            assert pt.p_stuck is not None
            fired = pt.p_stuck >= threshold
        elif family == "d1_stuck2":
            assert pt.p_stuck is not None
            if pt.p_stuck >= threshold:
                if prev_stuck_ok_step is not None and (pt.step_idx - prev_stuck_ok_step) == GRID_STRIDE:
                    fired = True
                prev_stuck_ok_step = pt.step_idx
            else:
                prev_stuck_ok_step = None
        elif family == "d1_doomed":
            assert pt.p_doomed is not None
            fired = pt.p_doomed >= threshold
        elif family == "d1_noprog":
            assert pt.p_progress is not None
            p_noprog = round(1.0 - float(pt.p_progress), 9)
            fired = p_noprog >= threshold
        else:
            raise ValueError(f"Unknown Decision 1 grid stopper family: {family}")

        if fired:
            return pt.step_idx, spent_usd

    return None, spent_usd


def stopper_label(family: str, threshold: float) -> str:
    """Canonical string identifier e.g. d1_stuck@0.80."""
    return f"{family}@{threshold:.2f}"


def score_all_grid_stoppers(
    grid_points: Sequence[GridPointRecord],
    thresholds: Sequence[float] = THRESHOLDS,
) -> dict[str, tuple[int | None, float]]:
    """Evaluate all 4 Decision 1 grid stoppers across all thresholds on one trajectory."""
    out: dict[str, tuple[int | None, float]] = {}
    for fam in GRID_STOPPER_FAMILIES:
        for tau in thresholds:
            out[stopper_label(fam, tau)] = score_grid_stopper(grid_points, family=fam, threshold=tau)
    return out


def plan_gemini_cli_baseline_turns(n_steps: int, hash_stop_step: int | None) -> int:
    """Conservative count of worst-case LLM checks (at min interval 5) used for planning before live calls."""
    first_idx = GEMINI_CLI_LLM_CHECK_AFTER_TURNS - 1  # turn 30 -> step_idx 29
    end_idx = n_steps - 1
    if hash_stop_step is not None:
        end_idx = min(end_idx, hash_stop_step - 1)
    if end_idx < first_idx:
        return 0
    # Default check at step_idx=29, then adaptive [5..15] (expected ~12 avg, worst-case 5)
    return 1 + (end_idx - first_idx) // GEMINI_CLI_DEFAULT_INTERVAL


def evaluate_gemini_cli_arm(
    run: RunMetadata,
    trace: SelectedTrajectory,
    client: GuardedDecisionClient,
) -> GeminiCliRunResult:
    """Replay Gemini CLI's adaptive LLM check (turns >= 30, N=20 history) combined with hash rules.

    Mirrors loopDetectionService.ts (commit 4fa64e7):
      - Hash rules are evaluated first each turn; if hash_stop_step fires at <= step_idx,
        the LLM check is not invoked from that turn onward.
      - First LLM check occurs at 1-based turn 30 (step_idx 29).
      - Interval adapts as clamp(5, 15, round(5 + 10 * (1 - p_loop))).
      - LLM check fires when p_loop >= 0.9.
    """
    hash_stop = run.stop_steps.get("gemini_cli")
    llm_stop: int | None = None
    last_check_turn = 0
    check_interval = GEMINI_CLI_DEFAULT_INTERVAL
    checks_run = 0
    blocked_checks = 0
    total_tok = 0
    total_usd = 0.0
    latencies: list[float] = []

    first_step = GEMINI_CLI_LLM_CHECK_AFTER_TURNS - 1  # turn 30 -> step_idx 29
    max_step = run.n_steps - 1
    if hash_stop is not None:
        max_step = min(max_step, hash_stop - 1)

    for step_idx in range(first_step, max_step + 1):
        turn = step_idx + 1
        if last_check_turn == 0 or (turn - last_check_turn) >= check_interval:
            prefix = slice_prefix_steps(trace.steps, step_idx)
            shared_text = render_decision_window(
                trace.task_text,
                prefix,
                step_idx=step_idx,
                window_steps=GEMINI_CLI_HISTORY_COUNT,
            )
            res = client.decide(shared_text=shared_text, questions=GEMINI_CLI_QUESTIONS)
            checks_run += 1
            total_tok += res.total_input_tokens
            total_usd += res.cost_usd
            latencies.append(res.latency_ms)
            last_check_turn = turn

            if res.status == "blocked":
                blocked_checks += 1
                continue

            assert res.answers is not None
            p_loop = float(res.answers["unproductive_loop"]["noul"])
            if p_loop >= GEMINI_CLI_CONFIDENCE_THRESHOLD:
                llm_stop = step_idx
                break
            check_interval = update_gemini_cli_interval(p_loop)

    full_stop = min_non_none(hash_stop, llm_stop)
    return GeminiCliRunResult(
        uid=run.uid,
        corpus=run.corpus,
        hash_stop_step=hash_stop,
        llm_stop_step=llm_stop,
        full_stop_step=full_stop,
        checks_run=checks_run,
        blocked_checks=blocked_checks,
        total_input_tokens=total_tok,
        api_cost_usd=total_usd,
        latencies_ms=latencies,
    )
