"""Metrics, matched-random baselines, calibration, fairness, task-split calibration, and bootstrap CIs."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from typing import Any, Callable, Sequence

import numpy as np
import pandas as pd
from scipy.stats import rankdata

from agent_loops.detectors import ReplayTrajectory, evaluate_detector_with_matched_random, run_matched_random_baseline

from agent_loop_decisions.config import RANDOM_SEED, TARGET_FSR_LEVELS, THRESHOLDS
from agent_loop_decisions.data import RunMetadata
from agent_loop_decisions.stoppers import (
    GRID_STOPPER_FAMILIES,
    GeminiCliRunResult,
    GridPointRecord,
    score_grid_stopper,
    stopper_label,
)


def compute_roc_auc(y_true: np.ndarray, y_score: np.ndarray) -> float | None:
    """Compute exact ROC-AUC via average rank sum; return None if only one class exists."""
    y = np.asarray(y_true, dtype=np.int32)
    s = np.asarray(y_score, dtype=np.float64)
    n_pos = int(np.sum(y == 1))
    n_neg = int(np.sum(y == 0))
    if n_pos == 0 or n_neg == 0:
        return None
    ranks = rankdata(s, method="average")
    pos_rank_sum = float(np.sum(ranks[y == 1]))
    auc = (pos_rank_sum - (n_pos * (n_pos + 1)) / 2.0) / (n_pos * n_neg)
    return float(auc)


def compute_brier_and_ece(
    y_true: np.ndarray,
    probs: np.ndarray,
    *,
    weights: np.ndarray | None = None,
    n_bins: int = 10,
) -> tuple[float, float, pd.DataFrame]:
    """Compute (possibly weighted) Brier score, 10-bin ECE, and reliability diagram table."""
    y = np.asarray(y_true, dtype=np.float64)
    p = np.asarray(probs, dtype=np.float64)
    n = len(y)
    if n == 0:
        return float("nan"), float("nan"), pd.DataFrame()
    w = np.ones(n, dtype=np.float64) if weights is None else np.asarray(weights, dtype=np.float64)
    w_sum = float(np.sum(w))
    if w_sum <= 0:
        return float("nan"), float("nan"), pd.DataFrame()

    brier = float(np.sum(w * ((p - y) ** 2)) / w_sum)
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    ece = 0.0
    bin_rows: list[dict[str, Any]] = []
    for b in range(n_bins):
        lo, hi = float(edges[b]), float(edges[b + 1])
        if b == n_bins - 1:
            mask = (p >= lo) & (p <= hi)
        else:
            mask = (p >= lo) & (p < hi)
        wb = float(np.sum(w[mask]))
        cnt = int(np.sum(mask))
        if wb > 0 and cnt > 0:
            mean_p = float(np.sum(w[mask] * p[mask]) / wb)
            emp_y = float(np.sum(w[mask] * y[mask]) / wb)
            ece += (wb / w_sum) * abs(emp_y - mean_p)
        else:
            mean_p = float("nan")
            emp_y = float("nan")
        bin_rows.append(
            {
                "bin": b,
                "bin_low": lo,
                "bin_high": hi,
                "count": cnt,
                "weight_sum": wb,
                "mean_pred": mean_p,
                "empirical_rate": emp_y,
            }
        )
    return brier, float(ece), pd.DataFrame(bin_rows)


def evaluate_rule_fixture_parity(
    runs: Sequence[RunMetadata],
    detector_name: str,
    *,
    n_seeds: int = 200,
    seed: int = RANDOM_SEED,
) -> dict[str, Any]:
    """Evaluate a rule detector directly via agent_loops.evaluate_detector_with_matched_random for exact parity."""
    trajs = [
        ReplayTrajectory(
            run_id=r.uid,
            harness=r.harness,
            model=r.model,
            task_name=r.task_name,
            succeeded=r.succeeded,
            steps=[None] * r.n_steps,  # type: ignore[list-item]
            step_costs_usd=r.step_costs,
            cost_included=r.cost_included,
            has_any_loop=r.has_any_loop,
            has_rotation_loop=r.has_strict_loop,
        )
        for r in runs
    ]
    stops = {r.uid: r.stop_steps.get(detector_name) for r in runs}
    res = evaluate_detector_with_matched_random(
        detector_name,
        lambda t: stops[t.run_id],
        trajs,
        n_seeds=n_seeds,
        seed=seed,
    )
    return asdict(res)


def _weighted_matched_random(
    *,
    runs: Sequence[RunMetadata],
    weights: Sequence[float],
    stop_steps: Sequence[int | None],
    n_seeds: int = 200,
    seed: int = RANDOM_SEED,
) -> dict[str, float]:
    """Run 200-seed per-harness step-matched random baseline returning both weighted & agent-loops metrics."""
    n = len(runs)
    harness_indices: dict[str, list[int]] = defaultdict(list)
    for i, r in enumerate(runs):
        harness_indices[r.harness].append(i)

    n_steps_arr = np.array([r.n_steps for r in runs], dtype=np.int32)
    succ_arr = np.array([r.succeeded for r in runs], dtype=bool)
    cost_inc_arr = np.array([r.cost_included for r in runs], dtype=bool)
    w_arr = np.array(weights, dtype=np.float64)

    # suffix[i][st] = sum(step_costs[st + 1 :])
    suffix_costs: list[np.ndarray | None] = [None] * n
    for i, r in enumerate(runs):
        if r.cost_included and r.n_steps > 0:
            c = np.array(r.step_costs, dtype=np.float64)
            rev = np.cumsum(c[::-1])[::-1]
            suf = np.zeros(len(c), dtype=np.float64)
            if len(c) > 1:
                suf[:-1] = rev[1:]
            suffix_costs[i] = suf

    total_w_pass = float(np.sum(w_arr[succ_arr]))
    total_w_eval_cost = float(np.sum(w_arr[cost_inc_arr] * np.array([r.total_cost_usd for r in runs])[cost_inc_arr]))

    prepared: list[tuple[np.ndarray, np.ndarray, np.ndarray]] = []
    for h, idxs in harness_indices.items():
        h_stops = [stop_steps[i] for i in idxs if stop_steps[i] is not None]
        if not h_stops:
            continue
        r_arr = np.array(idxs, dtype=np.int32)
        order = np.argsort(-n_steps_arr[r_arr], kind="mergesort")
        r_sorted = r_arr[order]
        l_sorted = n_steps_arr[r_sorted]
        stops_desc = np.sort(np.array(h_stops, dtype=np.int32))[::-1]
        p_counts = np.searchsorted(-l_sorted, -stops_desc, side="left")
        prepared.append((r_sorted, stops_desc, p_counts))

    rng = np.random.default_rng(seed)
    fsr_passed = np.zeros(n_seeds, dtype=np.float64)
    fsr_fired = np.zeros(n_seeds, dtype=np.float64)
    saved_failed_pct = np.zeros(n_seeds, dtype=np.float64)
    saved_all_pct = np.zeros(n_seeds, dtype=np.float64)

    for s_idx in range(n_seeds):
        w_false = 0.0
        w_fired = 0.0
        w_saved_fail = 0.0
        w_saved_all = 0.0
        for r_sorted, stops_desc, p_counts in prepared:
            active: list[int] = []
            prev_p = 0
            for j in range(len(stops_desc)):
                new_p = int(p_counts[j])
                if new_p > prev_p:
                    active.extend(range(prev_p, new_p))
                    prev_p = new_p
                pick_pos = int(rng.integers(len(active)))
                chosen_sorted_idx = active[pick_pos]
                active[pick_pos] = active[-1]
                active.pop()

                run_i = int(r_sorted[chosen_sorted_idx])
                st = int(stops_desc[j])
                wi = float(w_arr[run_i])
                w_fired += wi
                if succ_arr[run_i]:
                    w_false += wi
                if cost_inc_arr[run_i]:
                    suf = suffix_costs[run_i]
                    if suf is not None:
                        sv = wi * float(suf[st])
                        w_saved_all += sv
                        if not succ_arr[run_i]:
                            w_saved_fail += sv

        fsr_passed[s_idx] = (w_false / total_w_pass) if total_w_pass > 0 else 0.0
        fsr_fired[s_idx] = (w_false / w_fired) if w_fired > 0 else 0.0
        saved_failed_pct[s_idx] = (w_saved_fail / total_w_eval_cost) if total_w_eval_cost > 0 else 0.0
        saved_all_pct[s_idx] = (w_saved_all / total_w_eval_cost) if total_w_eval_cost > 0 else 0.0

    def _stats(arr: np.ndarray, prefix: str) -> dict[str, float]:
        return {
            f"{prefix}_mean": float(np.mean(arr)),
            f"{prefix}_ci_low": float(np.percentile(arr, 2.5)),
            f"{prefix}_ci_high": float(np.percentile(arr, 97.5)),
        }

    out: dict[str, float] = {}
    out.update(_stats(fsr_passed, "rand_fsr_passed"))
    out.update(_stats(fsr_fired, "rand_fsr_fired"))
    out.update(_stats(saved_failed_pct, "rand_saved_failed_pct"))
    out.update(_stats(saved_all_pct, "rand_saved_all_pct"))
    return out


def _prepare_outcome_arrays(
    runs: Sequence[RunMetadata],
    weights: Sequence[float],
    stop_steps: Sequence[int | None],
    api_costs_usd: Sequence[float],
) -> dict[str, np.ndarray]:
    n = len(runs)
    w = np.array(weights, dtype=np.float64)
    fired = np.array([st is not None for st in stop_steps], dtype=np.float64)
    succ = np.array([r.succeeded for r in runs], dtype=np.float64)
    fail = 1.0 - succ
    cv = np.array([r.cost_included for r in runs], dtype=np.float64)
    tot_cost = np.array([r.total_cost_usd if r.cost_included else 0.0 for r in runs], dtype=np.float64)
    api = np.array(api_costs_usd, dtype=np.float64)

    gross_all = np.zeros(n, dtype=np.float64)
    gross_fail = np.zeros(n, dtype=np.float64)
    tail_total = np.zeros(n, dtype=np.float64)
    tail_cut = np.zeros(n, dtype=np.float64)

    for i, r in enumerate(runs):
        if r.cost_included:
            if not r.succeeded:
                tail_total[i] = float(r.wasted_tail_usd)
            st = stop_steps[i]
            if st is not None:
                suf_all = float(sum(r.step_costs[st + 1 :]))
                gross_all[i] = suf_all
                if not r.succeeded:
                    gross_fail[i] = suf_all
                    cut_start = max(st + 1, r.last_productive_step + 1)
                    if cut_start < len(r.step_costs):
                        tail_cut[i] = float(sum(r.step_costs[cut_start:]))

    return {
        "w": w,
        "fired": fired,
        "succ": succ,
        "fail": fail,
        "cv": cv,
        "tot_cost": tot_cost,
        "api": api,
        "gross_all": gross_all,
        "gross_fail": gross_fail,
        "tail_total": tail_total,
        "tail_cut": tail_cut,
    }


def _compute_metrics_from_mask(arrs: dict[str, np.ndarray], mult: np.ndarray) -> dict[str, float]:
    """Compute scalar outcome metrics on runs multiplied by cluster multiplicity."""
    w = arrs["w"] * mult
    w_tot = float(np.sum(w))
    w_pass = float(np.sum(w * arrs["succ"]))
    w_fired = float(np.sum(w * arrs["fired"]))
    w_false = float(np.sum(w * arrs["fired"] * arrs["succ"]))

    firing_rate = (w_fired / w_tot) if w_tot > 0 else 0.0
    fsr_passed = (w_false / w_pass) if w_pass > 0 else 0.0
    fsr_fired = (w_false / w_fired) if w_fired > 0 else 0.0
    success_loss_pp = (100.0 * w_false / w_tot) if w_tot > 0 else 0.0

    cv_mask = arrs["cv"] > 0
    w_cv = float(np.sum(w[cv_mask]))
    w_eval_usd = float(np.sum(w[cv_mask] * arrs["tot_cost"][cv_mask]))
    w_gross_fail = float(np.sum(w[cv_mask] * arrs["gross_fail"][cv_mask]))
    w_gross_all = float(np.sum(w[cv_mask] * arrs["gross_all"][cv_mask]))
    w_api_cv = float(np.sum(w[cv_mask] * arrs["api"][cv_mask]))
    w_api_all = float(np.sum(w * arrs["api"]))

    w_net_fail = w_gross_fail - w_api_cv
    w_net_all = w_gross_all - w_api_cv

    gross_fail_pct = (w_gross_fail / w_eval_usd) if w_eval_usd > 0 else 0.0
    gross_all_pct = (w_gross_all / w_eval_usd) if w_eval_usd > 0 else 0.0
    net_fail_pct = (w_net_fail / w_eval_usd) if w_eval_usd > 0 else 0.0
    net_all_pct = (w_net_all / w_eval_usd) if w_eval_usd > 0 else 0.0

    w_tail_tot = float(np.sum(w[cv_mask] * arrs["tail_total"][cv_mask]))
    w_tail_cut = float(np.sum(w[cv_mask] * arrs["tail_cut"][cv_mask]))
    tail_cut_pct = (w_tail_cut / w_tail_tot) if w_tail_tot > 0 else 0.0

    return {
        "weighted_runs": w_tot,
        "weighted_fired": w_fired,
        "firing_rate": firing_rate,
        "fsr_passed": fsr_passed,
        "fsr_fired": fsr_fired,
        "success_loss_pp": success_loss_pp,
        "eval_cost_usd": w_eval_usd,
        "gross_saved_failed_usd": w_gross_fail,
        "gross_saved_failed_pct": gross_fail_pct,
        "gross_saved_all_usd": w_gross_all,
        "gross_saved_all_pct": gross_all_pct,
        "api_cost_cv_usd": w_api_cv,
        "api_cost_all_usd": w_api_all,
        "api_cost_per_run_usd": (w_api_all / w_tot) if w_tot > 0 else 0.0,
        "net_saved_failed_usd": w_net_fail,
        "net_saved_failed_pct": net_fail_pct,
        "net_saved_all_usd": w_net_all,
        "net_saved_all_pct": net_all_pct,
        "wasted_tail_total_usd": w_tail_tot,
        "wasted_tail_cut_usd": w_tail_cut,
        "wasted_tail_cut_pct": tail_cut_pct,
        "cv_weighted_runs": w_cv,
    }


def evaluate_outcome_stopper(
    *,
    stopper_name: str,
    runs: Sequence[RunMetadata],
    weights: Sequence[float],
    stop_steps: Sequence[int | None],
    api_costs_usd: Sequence[float],
    n_rand_seeds: int = 200,
    n_boot: int = 1000,
    seed: int = RANDOM_SEED,
) -> dict[str, Any]:
    """Evaluate one stopper on outcome metrics with task-clustered bootstrap CIs and matched-random baseline."""
    arrs = _prepare_outcome_arrays(runs, weights, stop_steps, api_costs_usd)
    point = _compute_metrics_from_mask(arrs, np.ones(len(runs), dtype=np.float64))

    # Task-cluster bootstrap (1,000 resamples by unique task_name)
    task_to_idxs: dict[str, list[int]] = defaultdict(list)
    for i, r in enumerate(runs):
        task_to_idxs[r.task_name].append(i)
    unique_tasks = sorted(task_to_idxs.keys())
    task_clusters = [np.array(task_to_idxs[t], dtype=np.int32) for t in unique_tasks]
    n_tasks = len(task_clusters)

    rng = np.random.default_rng(seed)
    boot_keys = (
        "fsr_passed",
        "fsr_fired",
        "success_loss_pp",
        "net_saved_failed_pct",
        "net_saved_all_pct",
        "gross_saved_failed_pct",
        "wasted_tail_cut_pct",
        "firing_rate",
    )
    boot_vals: dict[str, np.ndarray] = {k: np.zeros(n_boot, dtype=np.float64) for k in boot_keys}

    if n_tasks > 0 and n_boot > 0:
        counts_mat = rng.multinomial(n_tasks, np.full(n_tasks, 1.0 / n_tasks), size=n_boot)
        run_mult = np.zeros(len(runs), dtype=np.float64)
        for b in range(n_boot):
            run_mult.fill(0.0)
            cnts = counts_mat[b]
            for tidx in np.flatnonzero(cnts):
                run_mult[task_clusters[int(tidx)]] = float(cnts[int(tidx)])
            bres = _compute_metrics_from_mask(arrs, run_mult)
            for k in boot_keys:
                boot_vals[k][b] = bres[k]

    cis: dict[str, float] = {}
    for k, arr in boot_vals.items():
        cis[f"{k}_ci_low"] = float(np.percentile(arr, 2.5)) if len(arr) else 0.0
        cis[f"{k}_ci_high"] = float(np.percentile(arr, 97.5)) if len(arr) else 0.0

    rand = _weighted_matched_random(
        runs=runs,
        weights=weights,
        stop_steps=stop_steps,
        n_seeds=n_rand_seeds,
        seed=seed,
    )

    # Cost per catch on Sample A (also evaluated separately on A+B strict loops)
    w = arrs["w"]
    strict_mask = np.array([r.has_strict_loop for r in runs], dtype=bool)
    w_strict_caught = float(np.sum(w[strict_mask] * arrs["fired"][strict_mask]))
    api_per_caught_strict = (point["api_cost_all_usd"] / w_strict_caught) if w_strict_caught > 0 else float("nan")
    api_per_avoided_fail_usd = (
        (point["api_cost_cv_usd"] / point["gross_saved_failed_usd"])
        if point["gross_saved_failed_usd"] > 0
        else float("nan")
    )

    return {
        "stopper": stopper_name,
        **point,
        **cis,
        **rand,
        "api_usd_per_caught_strict_sample_a": api_per_caught_strict,
        "api_usd_per_avoided_fail_usd": api_per_avoided_fail_usd,
    }


def evaluate_task_split_calibration(
    *,
    runs: Sequence[RunMetadata],
    weights: Sequence[float],
    grid_by_uid: dict[str, list[GridPointRecord]],
    rule_names: Sequence[str],
    target_fsr_levels: Sequence[float] = TARGET_FSR_LEVELS,
    seed: int = RANDOM_SEED,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """50/50 task-level split (seed 42): pick threshold tau on dev half at 1/2/5% FSR, score on eval half."""
    tasks = sorted({r.task_name for r in runs})
    rng = np.random.default_rng(seed)
    perm = rng.permutation(len(tasks))
    half = len(tasks) // 2
    dev_tasks = {tasks[int(i)] for i in perm[:half]}
    eval_tasks = {tasks[int(i)] for i in perm[half:]}

    dev_runs = [r for r in runs if r.task_name in dev_tasks]
    dev_w = [w for r, w in zip(runs, weights) if r.task_name in dev_tasks]
    eval_runs = [r for r in runs if r.task_name in eval_tasks]
    eval_w = [w for r, w in zip(runs, weights) if r.task_name in eval_tasks]

    fine_taus = [round(0.01 * k, 2) for k in range(50, 100)]

    cal_rows: list[dict[str, Any]] = []
    for fam in GRID_STOPPER_FAMILIES:
        # Evaluate fine curve on dev half
        dev_curve: list[tuple[float, float, float]] = []
        for tau in fine_taus:
            stops_dev: list[int | None] = []
            apis_dev: list[float] = []
            for r in dev_runs:
                st, ap = score_grid_stopper(grid_by_uid.get(r.uid, []), family=fam, threshold=tau)
                stops_dev.append(st)
                apis_dev.append(ap)
            arrs_dev = _prepare_outcome_arrays(dev_runs, dev_w, stops_dev, apis_dev)
            m_dev = _compute_metrics_from_mask(arrs_dev, np.ones(len(dev_runs), dtype=np.float64))
            dev_curve.append((tau, m_dev["fsr_passed"], m_dev["net_saved_failed_pct"]))

        for target_fsr in target_fsr_levels:
            feasible = [row for row in dev_curve if row[1] <= target_fsr + 1e-12]
            if feasible:
                # Choose smallest tau (most aggressive stopper) meeting train FSR <= target_fsr
                chosen_tau, dev_fsr, dev_net = feasible[0]
                exact_feasible = True
            else:
                chosen_tau, dev_fsr, dev_net = dev_curve[-1]
                exact_feasible = False

            stops_ev: list[int | None] = []
            apis_ev: list[float] = []
            for r in eval_runs:
                st, ap = score_grid_stopper(grid_by_uid.get(r.uid, []), family=fam, threshold=chosen_tau)
                stops_ev.append(st)
                apis_ev.append(ap)
            ev_res = evaluate_outcome_stopper(
                stopper_name=f"{fam}@cal_{int(round(target_fsr * 100))}pct",
                runs=eval_runs,
                weights=eval_w,
                stop_steps=stops_ev,
                api_costs_usd=apis_ev,
                n_rand_seeds=200,
                n_boot=1000,
                seed=seed,
            )
            cal_rows.append(
                {
                    "family": fam,
                    "target_fsr": target_fsr,
                    "chosen_tau": chosen_tau,
                    "dev_feasible": exact_feasible,
                    "dev_fsr_passed": dev_fsr,
                    "dev_net_saved_failed_pct": dev_net,
                    "eval_fsr_passed": ev_res["fsr_passed"],
                    "eval_fsr_passed_ci_low": ev_res["fsr_passed_ci_low"],
                    "eval_fsr_passed_ci_high": ev_res["fsr_passed_ci_high"],
                    "eval_fsr_fired": ev_res["fsr_fired"],
                    "eval_net_saved_failed_pct": ev_res["net_saved_failed_pct"],
                    "eval_net_saved_failed_ci_low": ev_res["net_saved_failed_pct_ci_low"],
                    "eval_net_saved_failed_ci_high": ev_res["net_saved_failed_pct_ci_high"],
                    "eval_net_saved_all_pct": ev_res["net_saved_all_pct"],
                    "eval_gross_saved_failed_pct": ev_res["gross_saved_failed_pct"],
                    "eval_success_loss_pp": ev_res["success_loss_pp"],
                    "eval_firing_rate": ev_res["firing_rate"],
                    "eval_wasted_tail_cut_pct": ev_res["wasted_tail_cut_pct"],
                    "eval_rand_fsr_passed_mean": ev_res["rand_fsr_passed_mean"],
                    "eval_rand_saved_failed_pct_mean": ev_res["rand_saved_failed_pct_mean"],
                }
            )

    rule_rows: list[dict[str, Any]] = []
    for rname in rule_names:
        stops_ev = [r.stop_steps.get(rname) for r in eval_runs]
        ev_res = evaluate_outcome_stopper(
            stopper_name=rname,
            runs=eval_runs,
            weights=eval_w,
            stop_steps=stops_ev,
            api_costs_usd=[0.0] * len(eval_runs),
            n_rand_seeds=200,
            n_boot=1000,
            seed=seed,
        )
        rule_rows.append(ev_res)

    return pd.DataFrame(cal_rows), pd.DataFrame(rule_rows)


def evaluate_strict_loop_detection(
    *,
    strict_runs: Sequence[RunMetadata],
    stopper_stops_by_uid: dict[str, dict[str, int | None]],
    n_boot: int = 1000,
    seed: int = RANDOM_SEED,
) -> pd.DataFrame:
    """Compute recall and lead time (stop_step - strict_onset_step) across All / Passed / Failed strict-loop runs."""
    slices = [
        ("all_strict", [r for r in strict_runs if r.strict_onset_step is not None]),
        ("strict_passed", [r for r in strict_runs if r.strict_onset_step is not None and r.succeeded]),
        ("strict_failed", [r for r in strict_runs if r.strict_onset_step is not None and not r.succeeded]),
    ]
    stopper_names = sorted({k for d in stopper_stops_by_uid.values() for k in d})
    rng = np.random.default_rng(seed)

    rows: list[dict[str, Any]] = []
    for slice_name, sub in slices:
        n_s = len(sub)
        if n_s == 0:
            continue
        task_to_idxs: dict[str, list[int]] = defaultdict(list)
        for i, r in enumerate(sub):
            task_to_idxs[r.task_name].append(i)
        task_clusters = [np.array(task_to_idxs[t], dtype=np.int32) for t in sorted(task_to_idxs)]
        n_tasks = len(task_clusters)
        counts_mat = rng.multinomial(n_tasks, np.full(n_tasks, 1.0 / n_tasks), size=n_boot) if n_boot > 0 else None

        for sname in stopper_names:
            caught = np.array(
                [1.0 if stopper_stops_by_uid.get(r.uid, {}).get(sname) is not None else 0.0 for r in sub],
                dtype=np.float64,
            )
            leads = [
                int(stopper_stops_by_uid[r.uid][sname]) - int(r.strict_onset_step)  # type: ignore[arg-type]
                for r in sub
                if stopper_stops_by_uid.get(r.uid, {}).get(sname) is not None
            ]
            recall = float(np.mean(caught))
            if counts_mat is not None and n_tasks > 0:
                boot_rec = np.zeros(n_boot, dtype=np.float64)
                boot_med_lead = []
                for b in range(n_boot):
                    mult = np.zeros(n_s, dtype=np.float64)
                    cnts = counts_mat[b]
                    for tidx in np.flatnonzero(cnts):
                        mult[task_clusters[int(tidx)]] = float(cnts[int(tidx)])
                    denom = float(np.sum(mult))
                    boot_rec[b] = float(np.sum(mult * caught) / denom) if denom > 0 else 0.0
                    # Weighted lead samples in bootstrap replicate
                    rep_leads: list[int] = []
                    for i, r in enumerate(sub):
                        st = stopper_stops_by_uid.get(r.uid, {}).get(sname)
                        m = int(mult[i])
                        if st is not None and m > 0:
                            rep_leads.extend([int(st) - int(r.strict_onset_step)] * m)  # type: ignore[arg-type]
                    if rep_leads:
                        boot_med_lead.append(float(np.median(rep_leads)))
                rec_lo, rec_hi = float(np.percentile(boot_rec, 2.5)), float(np.percentile(boot_rec, 97.5))
                lead_ci_lo = float(np.percentile(boot_med_lead, 2.5)) if boot_med_lead else float("nan")
                lead_ci_hi = float(np.percentile(boot_med_lead, 97.5)) if boot_med_lead else float("nan")
            else:
                rec_lo, rec_hi = recall, recall
                lead_ci_lo, lead_ci_hi = float("nan"), float("nan")

            rows.append(
                {
                    "slice": slice_name,
                    "stopper": sname,
                    "n_strict": n_s,
                    "caught_runs": int(np.sum(caught)),
                    "recall": recall,
                    "recall_ci_low": rec_lo,
                    "recall_ci_high": rec_hi,
                    "mean_lead_steps": float(np.mean(leads)) if leads else float("nan"),
                    "median_lead_steps": float(np.median(leads)) if leads else float("nan"),
                    "median_lead_ci_low": lead_ci_lo,
                    "median_lead_ci_high": lead_ci_hi,
                    "p25_lead_steps": float(np.percentile(leads, 25)) if leads else float("nan"),
                    "p75_lead_steps": float(np.percentile(leads, 75)) if leads else float("nan"),
                }
            )
    return pd.DataFrame(rows)


def evaluate_hard_negatives(
    *,
    default_only_runs: Sequence[RunMetadata],
    stopper_stops_by_uid: dict[str, dict[str, int | None]],
    n_boot: int = 1000,
    seed: int = RANDOM_SEED,
) -> pd.DataFrame:
    """Compute firing rate on default-only (harmless reread / near-duplicate) runs with task-cluster CIs."""
    n = len(default_only_runs)
    if n == 0:
        return pd.DataFrame()
    stopper_names = sorted({k for d in stopper_stops_by_uid.values() for k in d})
    task_to_idxs: dict[str, list[int]] = defaultdict(list)
    for i, r in enumerate(default_only_runs):
        task_to_idxs[r.task_name].append(i)
    task_clusters = [np.array(task_to_idxs[t], dtype=np.int32) for t in sorted(task_to_idxs)]
    n_tasks = len(task_clusters)
    rng = np.random.default_rng(seed)
    counts_mat = rng.multinomial(n_tasks, np.full(n_tasks, 1.0 / n_tasks), size=n_boot) if n_boot > 0 else None

    rows: list[dict[str, Any]] = []
    for sname in stopper_names:
        fired = np.array(
            [1.0 if stopper_stops_by_uid.get(r.uid, {}).get(sname) is not None else 0.0 for r in default_only_runs],
            dtype=np.float64,
        )
        rate = float(np.mean(fired))
        if counts_mat is not None and n_tasks > 0:
            boot_r = np.zeros(n_boot, dtype=np.float64)
            for b in range(n_boot):
                mult = np.zeros(n, dtype=np.float64)
                cnts = counts_mat[b]
                for tidx in np.flatnonzero(cnts):
                    mult[task_clusters[int(tidx)]] = float(cnts[int(tidx)])
                denom = float(np.sum(mult))
                boot_r[b] = float(np.sum(mult * fired) / denom) if denom > 0 else 0.0
            lo, hi = float(np.percentile(boot_r, 2.5)), float(np.percentile(boot_r, 97.5))
        else:
            lo, hi = rate, rate
        rows.append(
            {
                "stopper": sname,
                "n_default_only": n,
                "fired_runs": int(np.sum(fired)),
                "fire_rate": rate,
                "fire_rate_ci_low": lo,
                "fire_rate_ci_high": hi,
            }
        )
    return pd.DataFrame(rows)


def evaluate_open_vs_closed_fairness(
    *,
    runs: Sequence[RunMetadata],
    weights: Sequence[float],
    stopper_stops_by_uid: dict[str, dict[str, int | None]],
    stopper_names: Sequence[str],
    n_boot: int = 1000,
    seed: int = RANDOM_SEED,
) -> pd.DataFrame:
    """Compare weighted fire rates for Open vs Closed models within Passed and Failed runs separately."""
    n = len(runs)
    w = np.array(weights, dtype=np.float64)
    is_open = np.array([r.is_open_weight for r in runs], dtype=bool)
    succ = np.array([r.succeeded for r in runs], dtype=bool)

    task_to_idxs: dict[str, list[int]] = defaultdict(list)
    for i, r in enumerate(runs):
        task_to_idxs[r.task_name].append(i)
    task_clusters = [np.array(task_to_idxs[t], dtype=np.int32) for t in sorted(task_to_idxs)]
    n_tasks = len(task_clusters)
    rng = np.random.default_rng(seed)
    counts_mat = rng.multinomial(n_tasks, np.full(n_tasks, 1.0 / n_tasks), size=n_boot) if n_boot > 0 else None

    rows: list[dict[str, Any]] = []
    for outcome_label, out_mask in [("passed", succ), ("failed", ~succ)]:
        m_open = out_mask & is_open
        m_closed = out_mask & (~is_open)
        for sname in stopper_names:
            fired = np.array(
                [1.0 if stopper_stops_by_uid.get(r.uid, {}).get(sname) is not None else 0.0 for r in runs],
                dtype=np.float64,
            )
            wo_d = float(np.sum(w[m_open]))
            wc_d = float(np.sum(w[m_closed]))
            ro = float(np.sum(w[m_open] * fired[m_open]) / wo_d) if wo_d > 0 else float("nan")
            rc = float(np.sum(w[m_closed] * fired[m_closed]) / wc_d) if wc_d > 0 else float("nan")
            diff = ro - rc if (not np.isnan(ro) and not np.isnan(rc)) else float("nan")

            if counts_mat is not None and n_tasks > 0 and not np.isnan(diff):
                boot_ro = []
                boot_rc = []
                boot_d = []
                for b in range(n_boot):
                    mult = np.zeros(n, dtype=np.float64)
                    cnts = counts_mat[b]
                    for tidx in np.flatnonzero(cnts):
                        mult[task_clusters[int(tidx)]] = float(cnts[int(tidx)])
                    wm = w * mult
                    d1 = float(np.sum(wm[m_open]))
                    d2 = float(np.sum(wm[m_closed]))
                    if d1 > 0 and d2 > 0:
                        r1 = float(np.sum(wm[m_open] * fired[m_open]) / d1)
                        r2 = float(np.sum(wm[m_closed] * fired[m_closed]) / d2)
                        boot_ro.append(r1)
                        boot_rc.append(r2)
                        boot_d.append(r1 - r2)
                ro_lo, ro_hi = (
                    (float(np.percentile(boot_ro, 2.5)), float(np.percentile(boot_ro, 97.5)))
                    if boot_ro
                    else (ro, ro)
                )
                rc_lo, rc_hi = (
                    (float(np.percentile(boot_rc, 2.5)), float(np.percentile(boot_rc, 97.5)))
                    if boot_rc
                    else (rc, rc)
                )
                d_lo, d_hi = (
                    (float(np.percentile(boot_d, 2.5)), float(np.percentile(boot_d, 97.5)))
                    if boot_d
                    else (diff, diff)
                )
            else:
                ro_lo = ro_hi = ro
                rc_lo = rc_hi = rc
                d_lo = d_hi = diff

            rows.append(
                {
                    "outcome": outcome_label,
                    "stopper": sname,
                    "n_open_raw": int(np.sum(m_open)),
                    "n_closed_raw": int(np.sum(m_closed)),
                    "open_fire_rate": ro,
                    "open_ci_low": ro_lo,
                    "open_ci_high": ro_hi,
                    "closed_fire_rate": rc,
                    "closed_ci_low": rc_lo,
                    "closed_ci_high": rc_hi,
                    "diff_open_minus_closed": diff,
                    "diff_ci_low": d_lo,
                    "diff_ci_high": d_hi,
                }
            )
    return pd.DataFrame(rows)
