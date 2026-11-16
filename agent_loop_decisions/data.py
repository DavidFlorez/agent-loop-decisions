"""Dataset loading, step capture via agent-loops, deterministic sampling, and parsed cache."""

from __future__ import annotations

from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass
import gzip
import os
from pathlib import Path
import pickle
from typing import Any

import numpy as np
import orjson
import pandas as pd
import pyarrow.parquet as pq

from agent_loops.canonicalize import NormalizedStep
from agent_loops.labeler import StepLabel, label_trajectory, label_trajectory_strict
from agent_loops.model_catalog import is_open_weight_model
import agent_loops.study as al_study

from agent_loop_decisions.config import (
    DATA_DIR,
    GRID_MAX_STEP,
    GRID_STRIDE,
    RANDOM_SEED,
    SWE_DEFAULT_ONLY_ENRICHMENT_N,
    SWE_SAMPLE_A_PER_MODEL,
    TB_DEFAULT_ONLY_ENRICHMENT_N,
    TB_SAMPLE_A_PER_MODEL,
    TB_STRICT_ENRICHMENT_N,
)

CACHE_VERSION = "v1"
PARSED_CACHE_DIR = DATA_DIR / "parsed_cache"

FILE_CHANGE_OPS: frozenset[str] = frozenset({"write", "write_any", "edit"})


@dataclass(slots=True)
class RunMetadata:
    """Compact per-run metadata aligned 1:1 with agent_loops.CompactRunRecord."""

    uid: str
    corpus: str  # "swe" | "tb"
    run_id: str
    harness: str
    model: str
    is_open_weight: bool
    task_name: str
    succeeded: bool
    n_steps: int
    n_tool_steps: int
    has_any_loop: bool
    has_strict_loop: bool
    default_only: bool
    strict_onset_step: int | None
    default_onset_step: int | None
    last_productive_step: int
    wasted_tail_usd: float
    cost_included: bool
    total_cost_usd: float
    loop_cost_usd: float
    step_costs: list[float]
    stop_steps: dict[str, int | None]
    source_file: str
    source_row: int


@dataclass(slots=True)
class SelectedTrajectory:
    """Full causal trace and ground-truth step annotations for a sampled run."""

    uid: str
    corpus: str
    task_text: str
    steps: list[NormalizedStep]
    default_loop_flags: list[bool]
    strict_loop_flags: list[bool]


@dataclass(slots=True)
class PilotPoint:
    """One decision point selected for the pilot phase."""

    uid: str
    corpus: str
    stratum: str  # "strict_onset" | "default_only" | "sample_a"
    step_idx: int


def compute_last_productive_step(steps: list[NormalizedStep]) -> int:
    """Return index of last step changing a file or producing a never-before-seen error signature."""
    seen_sigs: set[str] = set()
    last_prod = -1
    for s in steps:
        changed_file = s.file_op in FILE_CHANGE_OPS
        new_err = False
        if s.is_error:
            sig = s.error_sig or (f"obs_norm:{s.obs_norm_hash}" if s.obs_known else None)
            if sig and sig not in seen_sigs:
                seen_sigs.add(sig)
                new_err = True
        if changed_file or new_err:
            last_prod = s.step_idx
    return last_prod


def compute_wasted_tail_usd(
    step_costs: list[float] | np.ndarray,
    *,
    last_productive_step: int,
    succeeded: bool,
    cost_included: bool,
) -> float:
    """Return dollars after last_productive_step on failed cost-valid runs (else 0.0)."""
    if succeeded or not cost_included:
        return 0.0
    if not len(step_costs):
        return 0.0
    start = last_productive_step + 1
    if start >= len(step_costs):
        return 0.0
    return float(sum(step_costs[start:]))


def main_grid_steps(n_steps: int) -> list[int]:
    """Return Main Grid decision points t = 4, 8, 12, ... < min(n_steps, 200)."""
    limit = min(int(n_steps), GRID_MAX_STEP)
    if limit <= GRID_STRIDE:
        return []
    return list(range(GRID_STRIDE, limit, GRID_STRIDE))


def extract_swe_task_text(rec: dict[str, Any]) -> str:
    """Extract initial user task description from a SWE-rebench trajectory dict."""
    for ev in rec.get("events") or []:
        if ev.get("type") == "prompt":
            msgs = ev.get("messages") or []
            user_parts = [
                str(m.get("content") or "").strip()
                for m in msgs
                if m.get("role") == "user" and str(m.get("content") or "").strip()
            ]
            if user_parts:
                return "\n\n".join(user_parts)
            all_parts = [str(m.get("content") or "").strip() for m in msgs if str(m.get("content") or "").strip()]
            if all_parts:
                return "\n\n".join(all_parts)
    return ""


def extract_tb_task_text(raw_steps: list[dict[str, Any]]) -> str:
    """Extract first user message from parsed Terminal-Bench raw steps."""
    for s in raw_steps:
        if (s.get("src") or "").lower() == "user":
            msg = str(s.get("msg") or "").strip()
            if msg and not (msg.startswith("$") and len(msg) <= 6 and all(c in "0123456789abcdefABCDEF$" for c in msg)):
                return msg
    for s in raw_steps:
        if (s.get("src") or "").lower() in {"user", "system"}:
            msg = str(s.get("msg") or "").strip()
            if msg and not (msg.startswith("$") and len(msg) <= 6):
                return msg
    return ""


def _parse_swe_shard_worker(shard_str: str) -> list[RunMetadata]:
    """Parse all swelike records in one SWE-rebench .jsonl.gz shard using agent_loops."""
    captured: list[dict[str, Any]] = []
    orig_fn = al_study._analyze_single_trajectory

    def _wrapped_analyze(**kwargs: Any) -> al_study.CompactRunRecord:
        steps: list[NormalizedStep] = kwargs["steps"]
        rec = orig_fn(**kwargs)
        sl_def: list[StepLabel] = label_trajectory(steps)
        sl_str: list[StepLabel] = label_trajectory_strict(steps)
        def_onset = next((s.step_idx for s in sl_def if s.is_loop), None)
        str_onset = next((s.step_idx for s in sl_str if s.is_loop), None)
        last_prod = compute_last_productive_step(steps)
        tail_usd = compute_wasted_tail_usd(
            rec.step_costs,
            last_productive_step=last_prod,
            succeeded=rec.succeeded,
            cost_included=rec.cost_included,
        )
        captured.append(
            {
                "rec": rec,
                "def_onset": def_onset,
                "str_onset": str_onset,
                "last_prod": last_prod,
                "tail_usd": tail_usd,
            }
        )
        return rec

    shard_path = Path(shard_str)
    rel_name = str(shard_path)
    out: list[RunMetadata] = []
    al_study._analyze_single_trajectory = _wrapped_analyze
    try:
        with gzip.open(shard_path, "rt", encoding="utf-8") as fh:
            for row_idx, line in enumerate(fh):
                raw = orjson.loads(line)
                part = raw["participant"]
                if (part.get("type") or "") != "common_scaffold_model":
                    continue
                captured.clear()
                al_study._parse_swe_style_trajectory(
                    raw,
                    dataset="swe_rebench",
                    harness=part["key"],
                    model=part["model"],
                    task_name=raw["instance_id"],
                    traj_id=raw["trajectory_id"],
                    succeeded=bool(raw["evaluation"]["resolved"]),
                    cohort="swe_rebench_swelike",
                )
                cap = captured[0]
                rec: al_study.CompactRunRecord = cap["rec"]
                out.append(
                    RunMetadata(
                        uid=f"swe::{rec.run_id}",
                        corpus="swe",
                        run_id=rec.run_id,
                        harness=rec.harness,
                        model=rec.model,
                        is_open_weight=bool(is_open_weight_model(rec.model)),
                        task_name=rec.task_name,
                        succeeded=bool(rec.succeeded),
                        n_steps=int(rec.n_steps),
                        n_tool_steps=int(rec.n_tool_steps),
                        has_any_loop=bool(rec.has_any_loop),
                        has_strict_loop=bool(rec.has_strict_loop),
                        default_only=bool(rec.has_any_loop and not rec.has_strict_loop),
                        strict_onset_step=cap["str_onset"],
                        default_onset_step=cap["def_onset"],
                        last_productive_step=int(cap["last_prod"]),
                        wasted_tail_usd=float(cap["tail_usd"]),
                        cost_included=bool(rec.cost_included),
                        total_cost_usd=float(rec.total_cost_usd),
                        loop_cost_usd=float(rec.loop_cost_usd),
                        step_costs=[float(x) for x in rec.step_costs.tolist()],
                        stop_steps=dict(rec.stop_steps),
                        source_file=rel_name,
                        source_row=row_idx,
                    )
                )
    finally:
        al_study._analyze_single_trajectory = orig_fn
    return out


def _parse_tb_chunk_worker(args: tuple[str, int, list[tuple[int, dict[str, Any]]]]) -> list[RunMetadata]:
    """Parse one batch of terminus-2 TB rows with step-level onset and productive tail metadata."""
    pf_str, pf_idx, indexed_rows = args
    captured: list[dict[str, Any]] = []
    orig_fn = al_study._analyze_single_trajectory

    def _wrapped_analyze(**kwargs: Any) -> al_study.CompactRunRecord:
        steps: list[NormalizedStep] = kwargs["steps"]
        rec = orig_fn(**kwargs)
        sl_def: list[StepLabel] = label_trajectory(steps)
        sl_str: list[StepLabel] = label_trajectory_strict(steps)
        def_onset = next((s.step_idx for s in sl_def if s.is_loop), None)
        str_onset = next((s.step_idx for s in sl_str if s.is_loop), None)
        last_prod = compute_last_productive_step(steps)
        tail_usd = compute_wasted_tail_usd(
            rec.step_costs,
            last_productive_step=last_prod,
            succeeded=rec.succeeded,
            cost_included=rec.cost_included,
        )
        captured.append(
            {
                "def_onset": def_onset,
                "str_onset": str_onset,
                "last_prod": last_prod,
                "tail_usd": tail_usd,
            }
        )
        return rec

    out: list[RunMetadata] = []
    al_study._analyze_single_trajectory = _wrapped_analyze
    try:
        rows_only = [r for _, r in indexed_rows]
        recs, _ = al_study._process_tb_batch(rows_only)
        assert len(recs) == len(indexed_rows) == len(captured)
        for (orig_row_idx, _), rec, cap in zip(indexed_rows, recs, captured):
            out.append(
                RunMetadata(
                    uid=f"tb::{pf_idx}::{orig_row_idx}",
                    corpus="tb",
                    run_id=rec.run_id,
                    harness=rec.harness,
                    model=rec.model,
                    is_open_weight=bool(is_open_weight_model(rec.model)),
                    task_name=rec.task_name,
                    succeeded=bool(rec.succeeded),
                    n_steps=int(rec.n_steps),
                    n_tool_steps=int(rec.n_tool_steps),
                    has_any_loop=bool(rec.has_any_loop),
                    has_strict_loop=bool(rec.has_strict_loop),
                    default_only=bool(rec.has_any_loop and not rec.has_strict_loop),
                    strict_onset_step=cap["str_onset"],
                    default_onset_step=cap["def_onset"],
                    last_productive_step=int(cap["last_prod"]),
                    wasted_tail_usd=float(cap["tail_usd"]),
                    cost_included=bool(rec.cost_included),
                    total_cost_usd=float(rec.total_cost_usd),
                    loop_cost_usd=float(rec.loop_cost_usd),
                    step_costs=[float(x) for x in rec.step_costs.tolist()],
                    stop_steps=dict(rec.stop_steps),
                    source_file=pf_str,
                    source_row=int(orig_row_idx),
                )
            )
    finally:
        al_study._analyze_single_trajectory = orig_fn
    return out


def _attach_turn_budgets(runs: list[RunMetadata]) -> dict[str, int]:
    """Compute and attach 1.25x P90 turn budget stop steps per harness identically to agent_loops."""
    by_h_succ: dict[str, list[int]] = defaultdict(list)
    all_succ: list[int] = []
    for r in runs:
        if r.succeeded and r.n_steps > 0:
            by_h_succ[r.harness].append(r.n_steps)
            all_succ.append(r.n_steps)
    global_p90 = float(np.percentile(all_succ, 90.0)) if all_succ else 50.0
    budgets: dict[str, int] = {}
    for h in {r.harness for r in runs}:
        vals = by_h_succ.get(h)
        p90 = float(np.percentile(vals, 90.0)) if vals else global_p90
        budgets[h] = max(1, int(np.ceil(1.25 * p90)))
    for r in runs:
        cap = budgets[r.harness]
        r.stop_steps["turn_budget_1_25x_p90"] = (cap - 1) if r.n_steps > cap else None
    return budgets


def compute_corpus_sanity(runs: list[RunMetadata]) -> dict[str, Any]:
    """Compute exact headline counts matching agent-loops sanity check expectations."""
    n = len(runs)
    n_models = len({r.model for r in runs})
    n_pass = sum(1 for r in runs if r.succeeded)
    strict = [r for r in runs if r.has_strict_loop]
    strict_open = sum(1 for r in strict if r.is_open_weight)
    strict_closed = len(strict) - strict_open
    strict_pass = sum(1 for r in strict if r.succeeded)
    def_only = [r for r in runs if r.default_only]
    cv = [r for r in runs if r.cost_included]
    total_usd = sum(r.total_cost_usd for r in cv)
    strict_usd = sum(r.total_cost_usd for r in cv if r.has_strict_loop)
    fail_usd = sum(r.total_cost_usd for r in cv if not r.succeeded)
    gem_fires = sum(1 for r in runs if r.stop_steps.get("gemini_cli") is not None)
    gem_strict = sum(1 for r in strict if r.stop_steps.get("gemini_cli") is not None)
    gem_def_only = sum(1 for r in def_only if r.stop_steps.get("gemini_cli") is not None)
    oh_strict = sum(1 for r in strict if r.stop_steps.get("openhands_stuck") is not None)
    oh_def_only = sum(1 for r in def_only if r.stop_steps.get("openhands_stuck") is not None)
    return {
        "runs": n,
        "models": n_models,
        "passed": n_pass,
        "pass_rate": (n_pass / n) if n else 0.0,
        "strict_runs": len(strict),
        "strict_open": strict_open,
        "strict_closed": strict_closed,
        "strict_passed": strict_pass,
        "default_only_runs": len(def_only),
        "cost_valid_runs": len(cv),
        "total_usd": round(total_usd, 6),
        "strict_usd": round(strict_usd, 6),
        "strict_usd_share": (strict_usd / total_usd) if total_usd > 0 else 0.0,
        "fail_usd": round(fail_usd, 6),
        "fail_usd_share": (fail_usd / total_usd) if total_usd > 0 else 0.0,
        "gemini_cli_fires": gem_fires,
        "gemini_cli_strict_fires": gem_strict,
        "gemini_cli_default_only_fires": gem_def_only,
        "openhands_strict_fires": oh_strict,
        "openhands_default_only_fires": oh_def_only,
    }


def verify_sanity_counts(swe_sanity: dict[str, Any], tb_sanity: dict[str, Any]) -> None:
    """Assert parsed corpus counts match agent-loops ground-truth numbers before proceeding."""
    checks = [
        ("swe.runs", swe_sanity["runs"], 7215),
        ("swe.models", swe_sanity["models"], 13),
        ("swe.strict_runs", swe_sanity["strict_runs"], 152),
        ("swe.strict_open", swe_sanity["strict_open"], 133),
        ("swe.strict_closed", swe_sanity["strict_closed"], 19),
        ("swe.strict_passed", swe_sanity["strict_passed"], 48),
        ("swe.default_only_runs", swe_sanity["default_only_runs"], 4318),
        ("swe.cost_valid_runs", swe_sanity["cost_valid_runs"], 6092),
        ("swe.total_usd", round(swe_sanity["total_usd"], 2), 5845.10),
        ("swe.strict_usd", round(swe_sanity["strict_usd"], 2), 143.37),
        ("swe.fail_usd", round(swe_sanity["fail_usd"], 2), 3011.31),
        ("tb.runs", tb_sanity["runs"], 15941),
        ("tb.strict_runs", tb_sanity["strict_runs"], 265),
        ("tb.strict_open", tb_sanity["strict_open"], 188),
        ("tb.strict_closed", tb_sanity["strict_closed"], 77),
        ("tb.strict_passed", tb_sanity["strict_passed"], 16),
        ("tb.default_only_runs", tb_sanity["default_only_runs"], 3319),
        ("tb.cost_valid_runs", tb_sanity["cost_valid_runs"], 3267),
        ("tb.total_usd", round(tb_sanity["total_usd"], 2), 2249.59),
        ("tb.strict_usd", round(tb_sanity["strict_usd"], 2), 135.30),
        ("tb.fail_usd", round(tb_sanity["fail_usd"], 2), 1959.08),
    ]
    mismatches = [(k, got, exp) for k, got, exp in checks if got != exp]
    if mismatches:
        raise RuntimeError(f"Corpus sanity check mismatch against agent-loops reference: {mismatches}")


def _sample_stratified_default_only(
    candidates: list[RunMetadata],
    n_target: int,
    rng: np.random.Generator,
) -> list[str]:
    """Sample n_target runs stratified by (is_open_weight, succeeded), balanced with overflow."""
    strata: dict[tuple[bool, bool], list[RunMetadata]] = {
        (True, True): [],
        (True, False): [],
        (False, True): [],
        (False, False): [],
    }
    for r in candidates:
        strata[(r.is_open_weight, r.succeeded)].append(r)
    for key in strata:
        strata[key].sort(key=lambda r: (r.model, r.task_name, r.uid))

    keys = sorted(strata.keys())
    selected_uids: set[str] = set()
    # Balanced base allocation (15 per cell for 60 total), redistributing any small-cell shortfall
    remaining = n_target
    alloc: dict[tuple[bool, bool], int] = {k: 0 for k in keys}
    for k in keys:
        take = min(len(strata[k]), n_target // len(keys))
        alloc[k] = take
        remaining -= take
    while remaining > 0:
        progressed = False
        for k in keys:
            if remaining == 0:
                break
            if alloc[k] < len(strata[k]):
                alloc[k] += 1
                remaining -= 1
                progressed = True
        if not progressed:
            break

    out: list[str] = []
    for k in keys:
        pool = strata[k]
        take = alloc[k]
        if take <= 0:
            continue
        idxs = rng.choice(len(pool), size=take, replace=False)
        for i in sorted(int(x) for x in idxs):
            uid = pool[i].uid
            if uid not in selected_uids:
                selected_uids.add(uid)
                out.append(uid)
    return out


def assign_samples_swe(runs: list[RunMetadata], *, seed: int = RANDOM_SEED) -> dict[str, dict[str, Any]]:
    """Assign Sample A (23/model = 299) and Sample B (remaining strict + 60 default-only) for SWE."""
    rng = np.random.default_rng(seed)
    by_model: dict[str, list[RunMetadata]] = defaultdict(list)
    for r in sorted(runs, key=lambda x: (x.model, x.task_name, x.uid)):
        by_model[r.model].append(r)

    assignments: dict[str, dict[str, Any]] = {}
    sample_a_uids: set[str] = set()
    for model in sorted(by_model):
        pool = by_model[model]
        n_pop = len(pool)
        take = min(SWE_SAMPLE_A_PER_MODEL, n_pop)
        idxs = rng.choice(n_pop, size=take, replace=False)
        weight = float(n_pop) / float(take)
        for i in sorted(int(x) for x in idxs):
            r = pool[i]
            sample_a_uids.add(r.uid)
            assignments[r.uid] = {"in_sample_a": True, "sample_b_role": None, "sample_weight": weight}

    # Sample B strict: EVERY strict-loop run not already in A
    for r in sorted(runs, key=lambda x: (x.model, x.task_name, x.uid)):
        if r.has_strict_loop and r.uid not in sample_a_uids:
            assignments[r.uid] = {"in_sample_a": False, "sample_b_role": "strict", "sample_weight": 0.0}

    # Sample B default-only: 60 stratified by (open/closed, pass/fail)
    def_candidates = [r for r in runs if r.default_only and r.uid not in sample_a_uids]
    def_uids = _sample_stratified_default_only(def_candidates, SWE_DEFAULT_ONLY_ENRICHMENT_N, rng)
    for uid in def_uids:
        assignments[uid] = {"in_sample_a": False, "sample_b_role": "default_only", "sample_weight": 0.0}

    return assignments


def assign_samples_tb(runs: list[RunMetadata], *, seed: int = RANDOM_SEED) -> dict[str, dict[str, Any]]:
    """Assign Sample A (10/model = 290) and Sample B (120 strict + 60 default-only) for TB."""
    rng = np.random.default_rng(seed + 1000)
    by_model: dict[str, list[RunMetadata]] = defaultdict(list)
    for r in sorted(runs, key=lambda x: (x.model, x.task_name, x.uid)):
        by_model[r.model].append(r)

    assignments: dict[str, dict[str, Any]] = {}
    sample_a_uids: set[str] = set()
    for model in sorted(by_model):
        pool = by_model[model]
        n_pop = len(pool)
        take = min(TB_SAMPLE_A_PER_MODEL, n_pop)
        idxs = rng.choice(n_pop, size=take, replace=False)
        weight = float(n_pop) / float(take)
        for i in sorted(int(x) for x in idxs):
            r = pool[i]
            sample_a_uids.add(r.uid)
            assignments[r.uid] = {"in_sample_a": True, "sample_b_role": None, "sample_weight": weight}

    # Sample B strict: 120 random strict-loop runs not in A
    strict_pool = sorted(
        [r for r in runs if r.has_strict_loop and r.uid not in sample_a_uids],
        key=lambda x: (x.model, x.task_name, x.uid),
    )
    take_s = min(TB_STRICT_ENRICHMENT_N, len(strict_pool))
    idxs_s = rng.choice(len(strict_pool), size=take_s, replace=False)
    for i in sorted(int(x) for x in idxs_s):
        r = strict_pool[i]
        assignments[r.uid] = {"in_sample_a": False, "sample_b_role": "strict", "sample_weight": 0.0}

    # Sample B default-only: 60 stratified by (open/closed, pass/fail)
    def_candidates = [r for r in runs if r.default_only and r.uid not in sample_a_uids]
    def_uids = _sample_stratified_default_only(def_candidates, TB_DEFAULT_ONLY_ENRICHMENT_N, rng)
    for uid in def_uids:
        assignments[uid] = {"in_sample_a": False, "sample_b_role": "default_only", "sample_weight": 0.0}

    return assignments


def select_pilot_points(
    swe_runs: list[RunMetadata],
    swe_assign: dict[str, dict[str, Any]],
    *,
    seed: int = RANDOM_SEED,
) -> list[PilotPoint]:
    """Select 50 deterministic Main Grid decision points for pilot: 25 strict onset, 15 default-only, 10 Sample A."""
    rng = np.random.default_rng(seed + 2000)
    points: list[PilotPoint] = []
    seen_pairs: set[tuple[str, int]] = set()

    # 1. 25 from SWE Sample B strict runs around strict_onset_step
    strict_b = [
        r
        for r in swe_runs
        if swe_assign.get(r.uid, {}).get("sample_b_role") == "strict"
        and r.strict_onset_step is not None
        and main_grid_steps(r.n_steps)
        and r.strict_onset_step < GRID_MAX_STEP
    ]
    strict_b.sort(key=lambda r: (r.model, r.task_name, r.uid))
    idxs = rng.choice(len(strict_b), size=min(25, len(strict_b)), replace=False)
    for j, idx in enumerate(sorted(int(x) for x in idxs)):
        r = strict_b[idx]
        grid = main_grid_steps(r.n_steps)
        onset = int(r.strict_onset_step or 4)
        # Alternate preferring pre-or-at onset vs at-or-after onset to inspect lead behavior
        prefer_after = (j % 2 == 0)
        step = min(grid, key=lambda t: (abs(t - onset), (t < onset) if prefer_after else (t > onset)))
        points.append(PilotPoint(uid=r.uid, corpus="swe", stratum="strict_onset", step_idx=int(step)))
        seen_pairs.add((r.uid, int(step)))

    # 2. 15 from SWE Sample B default-only runs
    def_b = [
        r
        for r in swe_runs
        if swe_assign.get(r.uid, {}).get("sample_b_role") == "default_only"
        and main_grid_steps(r.n_steps)
    ]
    def_b.sort(key=lambda r: (r.model, r.task_name, r.uid))
    idxs_d = rng.choice(len(def_b), size=min(15, len(def_b)), replace=False)
    for idx in sorted(int(x) for x in idxs_d):
        r = def_b[idx]
        grid = main_grid_steps(r.n_steps)
        target = int(r.default_onset_step) if r.default_onset_step is not None else grid[len(grid) // 2]
        step = min(grid, key=lambda t: (abs(t - target), t))
        if (r.uid, int(step)) not in seen_pairs:
            points.append(PilotPoint(uid=r.uid, corpus="swe", stratum="default_only", step_idx=int(step)))
            seen_pairs.add((r.uid, int(step)))

    # 3. 10 from SWE Sample A (stratified pass/fail + open/closed)
    samp_a = [r for r in swe_runs if swe_assign.get(r.uid, {}).get("in_sample_a") and main_grid_steps(r.n_steps)]
    chosen_a_uids = _sample_stratified_default_only(samp_a, 10, rng)
    by_uid = {r.uid: r for r in samp_a}
    for uid in chosen_a_uids:
        r = by_uid[uid]
        grid = main_grid_steps(r.n_steps)
        target = grid[len(grid) // 2]
        step = int(target)
        if (r.uid, step) not in seen_pairs:
            points.append(PilotPoint(uid=r.uid, corpus="swe", stratum="sample_a", step_idx=step))
            seen_pairs.add((r.uid, step))

    assert len(points) == 50, f"Expected 50 pilot points, got {len(points)}"
    return points


def _extract_selected_swe(runs: list[RunMetadata], needed_uids: set[str]) -> dict[str, SelectedTrajectory]:
    """Extract task_text, NormalizedSteps, and step labels only for selected SWE trajectories."""
    by_file: dict[str, dict[int, str]] = defaultdict(dict)
    for r in runs:
        if r.uid in needed_uids:
            by_file[r.source_file][r.source_row] = r.uid

    captured: list[list[NormalizedStep]] = []
    orig_fn = al_study._analyze_single_trajectory

    def _wrapped(**kwargs: Any) -> al_study.CompactRunRecord:
        captured.append(kwargs["steps"])
        return orig_fn(**kwargs)

    out: dict[str, SelectedTrajectory] = {}
    al_study._analyze_single_trajectory = _wrapped
    try:
        for file_path_str in sorted(by_file):
            wanted_rows = by_file[file_path_str]
            with gzip.open(file_path_str, "rt", encoding="utf-8") as fh:
                for row_idx, line in enumerate(fh):
                    uid = wanted_rows.get(row_idx)
                    if uid is None:
                        continue
                    raw = orjson.loads(line)
                    part = raw["participant"]
                    captured.clear()
                    al_study._parse_swe_style_trajectory(
                        raw,
                        dataset="swe_rebench",
                        harness=part["key"],
                        model=part["model"],
                        task_name=raw["instance_id"],
                        traj_id=raw["trajectory_id"],
                        succeeded=bool(raw["evaluation"]["resolved"]),
                        cohort="swe_rebench_swelike",
                    )
                    steps = captured[0]
                    sl_def = label_trajectory(steps)
                    sl_str = label_trajectory_strict(steps)
                    out[uid] = SelectedTrajectory(
                        uid=uid,
                        corpus="swe",
                        task_text=extract_swe_task_text(raw),
                        steps=steps,
                        default_loop_flags=[bool(x.is_loop) for x in sl_def],
                        strict_loop_flags=[bool(x.is_loop) for x in sl_str],
                    )
    finally:
        al_study._analyze_single_trajectory = orig_fn
    return out


def _extract_selected_tb(runs: list[RunMetadata], needed_uids: set[str]) -> dict[str, SelectedTrajectory]:
    """Extract task_text, NormalizedSteps, and step labels only for selected TB trajectories."""
    by_file: dict[str, dict[int, str]] = defaultdict(dict)
    for r in runs:
        if r.uid in needed_uids:
            by_file[r.source_file][r.source_row] = r.uid

    captured: list[list[NormalizedStep]] = []
    orig_fn = al_study._analyze_single_trajectory

    def _wrapped(**kwargs: Any) -> al_study.CompactRunRecord:
        captured.append(kwargs["steps"])
        return orig_fn(**kwargs)

    out: dict[str, SelectedTrajectory] = {}
    al_study._analyze_single_trajectory = _wrapped
    try:
        for file_path_str in sorted(by_file):
            wanted = by_file[file_path_str]
            pf_reader = pq.ParquetFile(file_path_str)
            global_row = 0
            for batch in pf_reader.iter_batches(batch_size=1000):
                rows = batch.to_pylist()
                batch_targets: list[tuple[str, dict[str, Any]]] = []
                for row in rows:
                    uid = wanted.get(global_row)
                    if uid is not None:
                        batch_targets.append((uid, row))
                    global_row += 1
                if not batch_targets:
                    continue
                captured.clear()
                al_study._process_tb_batch([row for _, row in batch_targets])
                assert len(captured) == len(batch_targets)
                for (uid, row), steps in zip(batch_targets, captured):
                    raw_steps = orjson.loads(row["steps"])
                    sl_def = label_trajectory(steps)
                    sl_str = label_trajectory_strict(steps)
                    out[uid] = SelectedTrajectory(
                        uid=uid,
                        corpus="tb",
                        task_text=extract_tb_task_text(raw_steps),
                        steps=steps,
                        default_loop_flags=[bool(x.is_loop) for x in sl_def],
                        strict_loop_flags=[bool(x.is_loop) for x in sl_str],
                    )
    finally:
        al_study._analyze_single_trajectory = orig_fn
    return out


def _runs_to_df(runs: list[RunMetadata], assigns: dict[str, dict[str, Any]]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for r in runs:
        a = assigns.get(r.uid, {})
        rows.append(
            {
                "uid": r.uid,
                "corpus": r.corpus,
                "run_id": r.run_id,
                "harness": r.harness,
                "model": r.model,
                "is_open_weight": r.is_open_weight,
                "task_name": r.task_name,
                "succeeded": r.succeeded,
                "n_steps": r.n_steps,
                "n_tool_steps": r.n_tool_steps,
                "has_any_loop": r.has_any_loop,
                "has_strict_loop": r.has_strict_loop,
                "default_only": r.default_only,
                "strict_onset_step": r.strict_onset_step,
                "default_onset_step": r.default_onset_step,
                "last_productive_step": r.last_productive_step,
                "wasted_tail_usd": r.wasted_tail_usd,
                "cost_included": r.cost_included,
                "total_cost_usd": r.total_cost_usd,
                "loop_cost_usd": r.loop_cost_usd,
                "step_costs_json": orjson.dumps(r.step_costs).decode("utf-8"),
                "stop_steps_json": orjson.dumps(r.stop_steps).decode("utf-8"),
                "source_file": r.source_file,
                "source_row": r.source_row,
                "in_sample_a": bool(a.get("in_sample_a", False)),
                "sample_b_role": a.get("sample_b_role"),
                "sample_weight": float(a.get("sample_weight", 0.0)),
            }
        )
    return pd.DataFrame(rows)


def _df_to_runs(df: pd.DataFrame) -> tuple[list[RunMetadata], dict[str, dict[str, Any]]]:
    runs: list[RunMetadata] = []
    assigns: dict[str, dict[str, Any]] = {}
    for row in df.to_dict("records"):
        uid = str(row["uid"])
        str_onset = None if pd.isna(row["strict_onset_step"]) else int(row["strict_onset_step"])
        def_onset = None if pd.isna(row["default_onset_step"]) else int(row["default_onset_step"])
        r = RunMetadata(
            uid=uid,
            corpus=str(row["corpus"]),
            run_id=str(row["run_id"]),
            harness=str(row["harness"]),
            model=str(row["model"]),
            is_open_weight=bool(row["is_open_weight"]),
            task_name=str(row["task_name"]),
            succeeded=bool(row["succeeded"]),
            n_steps=int(row["n_steps"]),
            n_tool_steps=int(row["n_tool_steps"]),
            has_any_loop=bool(row["has_any_loop"]),
            has_strict_loop=bool(row["has_strict_loop"]),
            default_only=bool(row["default_only"]),
            strict_onset_step=str_onset,
            default_onset_step=def_onset,
            last_productive_step=int(row["last_productive_step"]),
            wasted_tail_usd=float(row["wasted_tail_usd"]),
            cost_included=bool(row["cost_included"]),
            total_cost_usd=float(row["total_cost_usd"]),
            loop_cost_usd=float(row["loop_cost_usd"]),
            step_costs=[float(x) for x in orjson.loads(row["step_costs_json"])],
            stop_steps=dict(orjson.loads(row["stop_steps_json"])),
            source_file=str(row["source_file"]),
            source_row=int(row["source_row"]),
        )
        runs.append(r)
        if bool(row["in_sample_a"]) or (row["sample_b_role"] is not None and not pd.isna(row["sample_b_role"])):
            srole = None if pd.isna(row["sample_b_role"]) else str(row["sample_b_role"])
            assigns[uid] = {
                "in_sample_a": bool(row["in_sample_a"]),
                "sample_b_role": srole,
                "sample_weight": float(row["sample_weight"]),
            }
    return runs, assigns


@dataclass(slots=True)
class PreparedStudyData:
    """Loaded corpora, sanity statistics, sample assignments, pilot points, and selected traces."""

    swe_runs: list[RunMetadata]
    tb_runs: list[RunMetadata]
    swe_assign: dict[str, dict[str, Any]]
    tb_assign: dict[str, dict[str, Any]]
    swe_sanity: dict[str, Any]
    tb_sanity: dict[str, Any]
    swe_budgets: dict[str, int]
    tb_budgets: dict[str, int]
    pilot_points: list[PilotPoint]
    selected_swe: dict[str, SelectedTrajectory]
    selected_tb: dict[str, SelectedTrajectory]


def load_or_build_study_data(
    *,
    data_dir: Path = DATA_DIR,
    force_rebuild: bool = False,
) -> PreparedStudyData:
    """Load parsed datasets and deterministic samples from data/parsed_cache/, building once if needed."""
    cache_dir = data_dir / "parsed_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    manifest_p = cache_dir / "manifest.json"
    swe_pq = cache_dir / "swe_swelike_runs.parquet"
    tb_pq = cache_dir / "tb_terminus2_runs.parquet"
    swe_traces_p = cache_dir / "selected_swe.pkl.gz"
    tb_traces_p = cache_dir / "selected_tb.pkl.gz"

    if (
        not force_rebuild
        and manifest_p.exists()
        and swe_pq.exists()
        and tb_pq.exists()
        and swe_traces_p.exists()
        and tb_traces_p.exists()
    ):
        manifest = orjson.loads(manifest_p.read_bytes())
        if manifest.get("version") == CACHE_VERSION:
            swe_runs, swe_assign = _df_to_runs(pd.read_parquet(swe_pq))
            tb_runs, tb_assign = _df_to_runs(pd.read_parquet(tb_pq))
            with gzip.open(swe_traces_p, "rb") as fh:
                selected_swe = pickle.load(fh)
            with gzip.open(tb_traces_p, "rb") as fh:
                selected_tb = pickle.load(fh)
            pilot_points = [PilotPoint(**p) for p in manifest["pilot_points"]]
            return PreparedStudyData(
                swe_runs=swe_runs,
                tb_runs=tb_runs,
                swe_assign=swe_assign,
                tb_assign=tb_assign,
                swe_sanity=manifest["swe_sanity"],
                tb_sanity=manifest["tb_sanity"],
                swe_budgets=manifest["swe_budgets"],
                tb_budgets=manifest["tb_budgets"],
                pilot_points=pilot_points,
                selected_swe=selected_swe,
                selected_tb=selected_tb,
            )

    workers = min(6, max(1, (os.cpu_count() or 4) - 1))

    # 1. SWE-rebench swelike
    swe_shards = sorted((data_dir / "swe_rebench_07_2026" / "trajectories").glob("*/*.jsonl.gz"))
    if not swe_shards:
        raise FileNotFoundError(f"No SWE shards under {data_dir / 'swe_rebench_07_2026'}")
    swe_runs: list[RunMetadata] = []
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for chunk in ex.map(_parse_swe_shard_worker, [str(p) for p in swe_shards]):
            swe_runs.extend(chunk)
    swe_runs.sort(key=lambda r: (r.harness, r.model, r.task_name, r.uid))
    swe_budgets = _attach_turn_budgets(swe_runs)

    # 2. Terminal-Bench terminus-2
    tb_parquets = sorted((data_dir / "terminalbench" / "data").glob("*.parquet"))
    if not tb_parquets:
        raise FileNotFoundError(f"No TB parquets under {data_dir / 'terminalbench' / 'data'}")
    tb_chunks: list[tuple[str, int, list[tuple[int, dict[str, Any]]]]] = []
    for pf_idx, pf in enumerate(tb_parquets):
        pf_reader = pq.ParquetFile(pf)
        global_row = 0
        cur_chunk: list[tuple[int, dict[str, Any]]] = []
        for batch in pf_reader.iter_batches(batch_size=1000):
            for row in batch.to_pylist():
                row_i = global_row
                global_row += 1
                if (row.get("agent") or "") != "terminus-2":
                    continue
                st_str = row.get("steps")
                if not st_str or not orjson.loads(st_str):
                    continue
                cur_chunk.append((row_i, row))
                if len(cur_chunk) >= 500:
                    tb_chunks.append((str(pf), pf_idx, cur_chunk))
                    cur_chunk = []
        if cur_chunk:
            tb_chunks.append((str(pf), pf_idx, cur_chunk))

    tb_runs: list[RunMetadata] = []
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for res_chunk in ex.map(_parse_tb_chunk_worker, tb_chunks):
            tb_runs.extend(res_chunk)
    tb_runs.sort(key=lambda r: (r.harness, r.model, r.task_name, r.uid))
    tb_budgets = _attach_turn_budgets(tb_runs)

    # 3. Verify baseline sanity counts against agent-loops reference
    swe_sanity = compute_corpus_sanity(swe_runs)
    tb_sanity = compute_corpus_sanity(tb_runs)
    verify_sanity_counts(swe_sanity, tb_sanity)

    # 4. Deterministic stratified sampling & pilot selection
    swe_assign = assign_samples_swe(swe_runs, seed=RANDOM_SEED)
    tb_assign = assign_samples_tb(tb_runs, seed=RANDOM_SEED)
    pilot_points = select_pilot_points(swe_runs, swe_assign, seed=RANDOM_SEED)

    # 5. Extract full NormalizedSteps & task_text ONLY for sampled runs
    selected_swe = _extract_selected_swe(swe_runs, set(swe_assign.keys()))
    selected_tb = _extract_selected_tb(tb_runs, set(tb_assign.keys()))

    # 6. Persist parsed cache atomically under gitignored data/parsed_cache/
    _runs_to_df(swe_runs, swe_assign).to_parquet(swe_pq, index=False)
    _runs_to_df(tb_runs, tb_assign).to_parquet(tb_pq, index=False)
    with gzip.open(swe_traces_p, "wb", compresslevel=6) as fh:
        pickle.dump(selected_swe, fh)
    with gzip.open(tb_traces_p, "wb", compresslevel=6) as fh:
        pickle.dump(selected_tb, fh)

    manifest_data = {
        "version": CACHE_VERSION,
        "swe_sanity": swe_sanity,
        "tb_sanity": tb_sanity,
        "swe_budgets": swe_budgets,
        "tb_budgets": tb_budgets,
        "pilot_points": [asdict(p) for p in pilot_points],
    }
    manifest_p.write_bytes(orjson.dumps(manifest_data, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS))

    return PreparedStudyData(
        swe_runs=swe_runs,
        tb_runs=tb_runs,
        swe_assign=swe_assign,
        tb_assign=tb_assign,
        swe_sanity=swe_sanity,
        tb_sanity=tb_sanity,
        swe_budgets=swe_budgets,
        tb_budgets=tb_budgets,
        pilot_points=pilot_points,
        selected_swe=selected_swe,
        selected_tb=selected_tb,
    )
