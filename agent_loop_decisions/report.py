"""Reporting, audit sample export, NOTES.md updates, and 1600x900 publication charts."""

from __future__ import annotations

from datetime import datetime, timezone
import importlib.metadata
from pathlib import Path
from typing import Any, Sequence

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import orjson
import pandas as pd

from agent_loop_decisions.client import estimate_request_chars
from agent_loop_decisions.config import (
    AGENT_LOOPS_COMMIT,
    ASSETS_DIR,
    AUDIT_DIR,
    DECISION_MODEL,
    GEMINI_CLI_COMMIT,
    GEMINI_CLI_QUESTIONS,
    MAIN_QUESTIONS,
    MAIN_WINDOW_STEPS,
    PRICE_PER_M_INPUT_TOKENS_USD,
    RANDOM_SEED,
    RESULTS_DIR,
    ROOT_DIR,
    SWE_DATASET_REPO,
    SWE_DATASET_SHA,
    TB_DATASET_REPO,
    TB_DATASET_SHA,
    THRESHOLDS,
)
from agent_loop_decisions.data import (
    PilotPoint,
    PreparedStudyData,
    RunMetadata,
    SelectedTrajectory,
    main_grid_steps,
)
from agent_loop_decisions.metrics import (
    compute_brier_and_ece,
    compute_roc_auc,
    evaluate_hard_negatives,
    evaluate_open_vs_closed_fairness,
    evaluate_outcome_stopper,
    evaluate_strict_loop_detection,
    evaluate_task_split_calibration,
)
from agent_loop_decisions.render import render_decision_window, slice_prefix_steps
from agent_loop_decisions.stoppers import (
    GRID_STOPPER_FAMILIES,
    GeminiCliRunResult,
    GridPointRecord,
    plan_gemini_cli_baseline_turns,
    score_all_grid_stoppers,
    stopper_label,
)

RULE_DETECTORS: tuple[str, ...] = (
    "gemini_cli",
    "cline_style",
    "openhands_stuck",
    "openclaw_10",
    "openclaw_20",
    "step_cap_30",
    "step_cap_50",
    "step_cap_75",
    "step_cap_100",
    "step_cap_150",
    "turn_budget_1_25x_p90",
)


def _fmt_pct(val: float | None, digits: int = 2) -> str:
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return "n/a"
    return f"{val * 100.0:.{digits}f}%"


def _fmt_usd(val: float | None, digits: int = 2) -> str:
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return "n/a"
    return f"${val:,.{digits}f}"


def write_plan_outputs(
    data: PreparedStudyData,
    *,
    results_dir: Path = RESULTS_DIR,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Generate results/plan.csv, sanity_checks.json, and free Sample-A / Full rule-detector baselines."""
    results_dir.mkdir(parents=True, exist_ok=True)
    plan_rows: list[dict[str, Any]] = []

    def _project_grid_rows(
        corpus: str,
        runs: Sequence[RunMetadata],
        assigns: dict[str, dict[str, Any]],
        traces: dict[str, SelectedTrajectory],
    ) -> list[dict[str, Any]]:
        buckets: dict[str, list[RunMetadata]] = {
            "sample_a": [],
            "sample_b_strict": [],
            "sample_b_default_only": [],
        }
        for r in runs:
            a = assigns.get(r.uid)
            if not a:
                continue
            if a.get("in_sample_a"):
                buckets["sample_a"].append(r)
            elif a.get("sample_b_role") == "strict":
                buckets["sample_b_strict"].append(r)
            elif a.get("sample_b_role") == "default_only":
                buckets["sample_b_default_only"].append(r)

        rows_out: list[dict[str, Any]] = []
        # 1. Main grid strata
        for stratum, sr in buckets.items():
            n_pts = 0
            total_prompt_chars = 0
            total_req_chars = 0
            for r in sr:
                tr = traces[r.uid]
                for t in main_grid_steps(r.n_steps):
                    pref = slice_prefix_steps(tr.steps, t)
                    txt = render_decision_window(tr.task_text, pref, step_idx=t, window_steps=MAIN_WINDOW_STEPS)
                    n_pts += 1
                    total_prompt_chars += len(txt)
                    total_req_chars += estimate_request_chars([{"type": "text", "text": txt}], MAIN_QUESTIONS)
            est_tok_30 = float(total_req_chars) / 3.0
            est_tok_35 = float(total_req_chars) / 3.5
            rows_out.append(
                {
                    "phase": corpus,
                    "arm": "main_grid",
                    "stratum": stratum,
                    "n_runs": len(sr),
                    "n_decision_points": n_pts,
                    "rendered_input_chars": total_prompt_chars,
                    "est_tokens_chars_div_3_0": round(est_tok_30),
                    "est_usd_chars_div_3_0": (est_tok_30 / 1_000_000.0) * PRICE_PER_M_INPUT_TOKENS_USD,
                    "est_tokens_chars_div_3_5": round(est_tok_35),
                    "est_usd_chars_div_3_5": (est_tok_35 / 1_000_000.0) * PRICE_PER_M_INPUT_TOKENS_USD,
                }
            )

        # 2. Gemini CLI arm across union of Sample A + Sample B
        selected_runs = buckets["sample_a"] + buckets["sample_b_strict"] + buckets["sample_b_default_only"]
        g_pts = 0
        g_prompt_chars = 0
        g_req_chars = 0
        for r in selected_runs:
            tr = traces[r.uid]
            hash_stop = r.stop_steps.get("gemini_cli")
            n_est = plan_gemini_cli_baseline_turns(r.n_steps, hash_stop)
            if n_est <= 0:
                continue
            # Sample step positions evenly >= 29 before hash_stop for char estimation
            end_s = min(r.n_steps - 1, (hash_stop - 1) if hash_stop is not None else r.n_steps - 1)
            for k in range(n_est):
                t = min(end_s, 29 + k * 10)
                pref = slice_prefix_steps(tr.steps, t)
                txt = render_decision_window(tr.task_text, pref, step_idx=t, window_steps=20)
                g_pts += 1
                g_prompt_chars += len(txt)
                g_req_chars += estimate_request_chars([{"type": "text", "text": txt}], GEMINI_CLI_QUESTIONS)
        est_g_30 = float(g_req_chars) / 3.0
        est_g_35 = float(g_req_chars) / 3.5
        rows_out.append(
            {
                "phase": corpus,
                "arm": "gemini_cli_llm_approx",
                "stratum": "all_sampled",
                "n_runs": len(selected_runs),
                "n_decision_points": g_pts,
                "rendered_input_chars": g_prompt_chars,
                "est_tokens_chars_div_3_0": round(est_g_30),
                "est_usd_chars_div_3_0": (est_g_30 / 1_000_000.0) * PRICE_PER_M_INPUT_TOKENS_USD,
                "est_tokens_chars_div_3_5": round(est_g_35),
                "est_usd_chars_div_3_5": (est_g_35 / 1_000_000.0) * PRICE_PER_M_INPUT_TOKENS_USD,
            }
        )
        return rows_out

    # Pilot row (50 exact rendered points)
    swe_by_uid = {r.uid: r for r in data.swe_runs}
    pilot_prompt_chars = 0
    pilot_req_chars = 0
    for pp in data.pilot_points:
        tr = data.selected_swe[pp.uid]
        pref = slice_prefix_steps(tr.steps, pp.step_idx)
        txt = render_decision_window(tr.task_text, pref, step_idx=pp.step_idx, window_steps=MAIN_WINDOW_STEPS)
        pilot_prompt_chars += len(txt)
        pilot_req_chars += estimate_request_chars([{"type": "text", "text": txt}], MAIN_QUESTIONS)
    plan_rows.append(
        {
            "phase": "pilot",
            "arm": "main_grid",
            "stratum": "pilot_50_points",
            "n_runs": len({pp.uid for pp in data.pilot_points}),
            "n_decision_points": len(data.pilot_points),
            "rendered_input_chars": pilot_prompt_chars,
            "est_tokens_chars_div_3_0": round(pilot_req_chars / 3.0),
            "est_usd_chars_div_3_0": ((pilot_req_chars / 3.0) / 1_000_000.0) * PRICE_PER_M_INPUT_TOKENS_USD,
            "est_tokens_chars_div_3_5": round(pilot_req_chars / 3.5),
            "est_usd_chars_div_3_5": ((pilot_req_chars / 3.5) / 1_000_000.0) * PRICE_PER_M_INPUT_TOKENS_USD,
        }
    )
    plan_rows.extend(_project_grid_rows("swe", data.swe_runs, data.swe_assign, data.selected_swe))
    plan_rows.extend(_project_grid_rows("tb", data.tb_runs, data.tb_assign, data.selected_tb))

    plan_df = pd.DataFrame(plan_rows)
    plan_df.to_csv(results_dir / "plan.csv", index=False)

    # Free rule-detector baselines on Sample A (weighted) & Full Corpus
    def _eval_rules(corpus_runs: Sequence[RunMetadata], assigns: dict[str, dict[str, Any]]) -> pd.DataFrame:
        samp_a = [r for r in corpus_runs if assigns.get(r.uid, {}).get("in_sample_a")]
        w_a = [float(assigns[r.uid]["sample_weight"]) for r in samp_a]
        rows: list[dict[str, Any]] = []
        for scope, rset, wset in [
            ("sample_a_weighted", samp_a, w_a),
            ("full_corpus", list(corpus_runs), [1.0] * len(corpus_runs)),
        ]:
            # On full corpus skip 1000-rep bootstrap if huge? No, vector cluster bootstrap is fast (~0.2s per rule)
            for det in RULE_DETECTORS:
                res = evaluate_outcome_stopper(
                    stopper_name=det,
                    runs=rset,
                    weights=wset,
                    stop_steps=[r.stop_steps.get(det) for r in rset],
                    api_costs_usd=[0.0] * len(rset),
                    n_rand_seeds=200,
                    n_boot=1000,
                    seed=RANDOM_SEED,
                )
                res["scope"] = scope
                rows.append(res)
        return pd.DataFrame(rows)

    swe_rules_df = _eval_rules(data.swe_runs, data.swe_assign)
    tb_rules_df = _eval_rules(data.tb_runs, data.tb_assign)
    swe_rules_df.to_csv(results_dir / "rules_baseline_swe.csv", index=False)
    tb_rules_df.to_csv(results_dir / "rules_baseline_tb.csv", index=False)

    # Free strict-loop & hard-negative rule baselines
    swe_strict_ab = [
        r
        for r in data.swe_runs
        if r.has_strict_loop and (r.uid in data.swe_assign)
    ]
    tb_strict_ab = [
        r
        for r in data.tb_runs
        if r.has_strict_loop and (r.uid in data.tb_assign)
    ]
    swe_stops_rules = {r.uid: {d: r.stop_steps.get(d) for d in RULE_DETECTORS} for r in swe_strict_ab}
    tb_stops_rules = {r.uid: {d: r.stop_steps.get(d) for d in RULE_DETECTORS} for r in tb_strict_ab}
    evaluate_strict_loop_detection(
        strict_runs=swe_strict_ab, stopper_stops_by_uid=swe_stops_rules, n_boot=1000, seed=RANDOM_SEED
    ).to_csv(results_dir / "rules_strict_loops_swe.csv", index=False)
    evaluate_strict_loop_detection(
        strict_runs=tb_strict_ab, stopper_stops_by_uid=tb_stops_rules, n_boot=1000, seed=RANDOM_SEED
    ).to_csv(results_dir / "rules_strict_loops_tb.csv", index=False)

    swe_def_all = [r for r in data.swe_runs if r.default_only]
    tb_def_all = [r for r in data.tb_runs if r.default_only]
    evaluate_hard_negatives(
        default_only_runs=swe_def_all,
        stopper_stops_by_uid={r.uid: {d: r.stop_steps.get(d) for d in RULE_DETECTORS} for r in swe_def_all},
        n_boot=200,
        seed=RANDOM_SEED,
    ).to_csv(results_dir / "rules_hard_negatives_swe_full.csv", index=False)
    evaluate_hard_negatives(
        default_only_runs=tb_def_all,
        stopper_stops_by_uid={r.uid: {d: r.stop_steps.get(d) for d in RULE_DETECTORS} for r in tb_def_all},
        n_boot=200,
        seed=RANDOM_SEED,
    ).to_csv(results_dir / "rules_hard_negatives_tb_full.csv", index=False)

    sanity_payload = {
        "swe_swelike": data.swe_sanity,
        "tb_terminus_2": data.tb_sanity,
        "swe_turn_budgets": data.swe_budgets,
        "tb_turn_budgets": data.tb_budgets,
        "sampling_counts": {
            "swe_sample_a": sum(1 for a in data.swe_assign.values() if a.get("in_sample_a")),
            "swe_sample_b_strict": sum(1 for a in data.swe_assign.values() if a.get("sample_b_role") == "strict"),
            "swe_sample_b_default_only": sum(
                1 for a in data.swe_assign.values() if a.get("sample_b_role") == "default_only"
            ),
            "tb_sample_a": sum(1 for a in data.tb_assign.values() if a.get("in_sample_a")),
            "tb_sample_b_strict": sum(1 for a in data.tb_assign.values() if a.get("sample_b_role") == "strict"),
            "tb_sample_b_default_only": sum(
                1 for a in data.tb_assign.values() if a.get("sample_b_role") == "default_only"
            ),
            "pilot_points": len(data.pilot_points),
        },
    }
    (results_dir / "sanity_checks.json").write_bytes(
        orjson.dumps(sanity_payload, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS)
    )
    return plan_df, swe_rules_df, tb_rules_df


def write_pilot_report(
    *,
    data: PreparedStudyData,
    pilot_records: Sequence[tuple[PilotPoint, GridPointRecord, str]],
    plan_df: pd.DataFrame,
    results_dir: Path = RESULTS_DIR,
) -> dict[str, Any]:
    """Write results/pilot_points.csv, pilot_summary.json, and pilot_report.md with re-projected SWE/TB spend."""
    results_dir.mkdir(parents=True, exist_ok=True)
    run_by_uid = {r.uid: r for r in data.swe_runs}

    rows: list[dict[str, Any]] = []
    ratios: list[float] = []
    latencies: list[float] = []
    examples: list[dict[str, Any]] = []

    for idx, (pp, pt, prompt_text) in enumerate(pilot_records):
        r = run_by_uid[pp.uid]
        cpt = (pt.est_chars / pt.total_input_tokens) if pt.total_input_tokens > 0 else float("nan")
        if pt.status == "ok" and pt.total_input_tokens > 0:
            ratios.append(float(cpt))
            latencies.append(float(pt.latency_ms))
        p_noprog = (1.0 - pt.p_progress) if pt.p_progress is not None else None
        rows.append(
            {
                "pilot_idx": idx,
                "uid": pp.uid,
                "stratum": pp.stratum,
                "model": r.model,
                "is_open_weight": r.is_open_weight,
                "task_name": r.task_name,
                "succeeded": r.succeeded,
                "n_steps": r.n_steps,
                "has_strict_loop": r.has_strict_loop,
                "strict_onset_step": r.strict_onset_step,
                "step_idx": pp.step_idx,
                "active_strict_in_window": pt.active_strict_in_window,
                "status": pt.status,
                "block_code": pt.block_code,
                "p_stuck": pt.p_stuck,
                "p_progress": pt.p_progress,
                "p_no_progress": p_noprog,
                "p_doomed": pt.p_doomed,
                "est_chars": pt.est_chars,
                "est_tokens_chars_div_3_0": pt.est_tokens,
                "actual_input_tokens": pt.total_input_tokens,
                "measured_chars_per_token": cpt,
                "cost_usd": pt.cost_usd,
                "latency_ms": pt.latency_ms,
                "cached": pt.cached,
            }
        )
        examples.append(
            {
                "pilot_idx": idx,
                "uid": pp.uid,
                "stratum": pp.stratum,
                "model": r.model,
                "task_name": r.task_name,
                "succeeded": r.succeeded,
                "strict_onset_step": r.strict_onset_step,
                "step_idx": pp.step_idx,
                "active_strict_in_window": pt.active_strict_in_window,
                "status": pt.status,
                "p_stuck": pt.p_stuck,
                "p_progress": pt.p_progress,
                "p_doomed": pt.p_doomed,
                "actual_input_tokens": pt.total_input_tokens,
                "latency_ms": pt.latency_ms,
                "prompt_excerpt": prompt_text[:700].replace("\n", " "),
            }
        )

    df = pd.DataFrame(rows)
    df.to_csv(results_dir / "pilot_points.csv", index=False)

    n_total = len(df)
    n_ok = int((df["status"] == "ok").sum())
    n_blocked = int((df["status"] == "blocked").sum())
    tot_est_30 = float(df["est_tokens_chars_div_3_0"].sum())
    tot_actual_tok = int(df["actual_input_tokens"].sum())
    tot_cost = float(df["cost_usd"].sum())
    tot_chars_ok = float(df.loc[df["status"] == "ok", "est_chars"].sum())
    measured_cpt = (tot_chars_ok / tot_actual_tok) if tot_actual_tok > 0 else 3.0

    # Pick 10 diverse example answers (4 strict, 3 default-only, 3 Sample A)
    strict_ex = [e for e in examples if e["stratum"] == "strict_onset"][:4]
    def_ex = [e for e in examples if e["stratum"] == "default_only"][:3]
    a_ex = [e for e in examples if e["stratum"] == "sample_a"][:3]
    chosen_examples = strict_ex + def_ex + a_ex

    # Re-project SWE and TB using measured chars_per_token
    reproj: dict[str, Any] = {}
    for ph in ("swe", "tb"):
        sub = plan_df[plan_df["phase"] == ph]
        chars_sum = int(sub["rendered_input_chars"].sum())
        calls_sum = int(sub["n_decision_points"].sum())
        # Include question overhead (~850 main / 650 gcli already reflected in est_tokens * 3.0)
        chars_with_q = float(sub["est_tokens_chars_div_3_0"].sum()) * 3.0
        proj_tok = chars_with_q / measured_cpt
        proj_usd = (proj_tok / 1_000_000.0) * PRICE_PER_M_INPUT_TOKENS_USD
        reproj[ph] = {
            "projected_calls": calls_sum,
            "projected_tokens_at_measured_cpt": round(proj_tok),
            "projected_usd_at_measured_cpt": round(proj_usd, 4),
            "projected_usd_at_chars_div_3_0": round(float(sub["est_usd_chars_div_3_0"].sum()), 4),
        }

    summary = {
        "n_points": n_total,
        "n_ok": n_ok,
        "n_blocked": n_blocked,
        "block_rate": (n_blocked / n_total) if n_total else 0.0,
        "total_chars": int(df["est_chars"].sum()),
        "total_est_tokens_3_0": round(tot_est_30),
        "total_actual_input_tokens": tot_actual_tok,
        "est_vs_actual_token_ratio": (tot_est_30 / tot_actual_tok) if tot_actual_tok > 0 else float("nan"),
        "measured_chars_per_token_aggregate": measured_cpt,
        "measured_chars_per_token_mean": float(np.mean(ratios)) if ratios else float("nan"),
        "measured_chars_per_token_median": float(np.median(ratios)) if ratios else float("nan"),
        "total_cost_usd": tot_cost,
        "mean_latency_ms": float(np.mean(latencies)) if latencies else float("nan"),
        "median_latency_ms": float(np.median(latencies)) if latencies else float("nan"),
        "p95_latency_ms": float(np.percentile(latencies, 95)) if latencies else float("nan"),
        "reprojected_phases": reproj,
        "examples_10": chosen_examples,
    }
    (results_dir / "pilot_summary.json").write_bytes(
        orjson.dumps(summary, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS)
    )

    md: list[str] = [
        "# Pilot Report (50 SWE-rebench Decision Points)\n",
        f"- **Points**: `{n_total}` (`{n_ok}` ok, `{n_blocked}` safety-blocked, block rate `{_fmt_pct(summary['block_rate'])}`)",
        f"- **Tokens (measured vs `chars/3.0` est)**: `{tot_actual_tok:,}` actual vs `{round(tot_est_30):,}` estimated (`{summary['est_vs_actual_token_ratio']:.3f}x` conservative)",
        f"- **Measured chars per input token**: aggregate **`{measured_cpt:.3f}`** (per-call mean `{summary['measured_chars_per_token_mean']:.3f}`, median `{summary['measured_chars_per_token_median']:.3f}`)",
        f"- **Measured Pilot Cost**: **`${tot_cost:.6f}`**",
        f"- **Latency**: mean `{summary['mean_latency_ms']:.1f} ms`, median `{summary['median_latency_ms']:.1f} ms`, P95 `{summary['p95_latency_ms']:.1f} ms`\n",
        "## Re-Projected Phase Costs at Measured Chars/Token\n",
        "| Phase | Projected Calls | Est Tokens (Measured CPT) | Re-Projected USD | Conservative USD (`chars/3.0`) |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for ph in ("swe", "tb"):
        rp = reproj[ph]
        md.append(
            f"| `{ph}` | {rp['projected_calls']:,} | {rp['projected_tokens_at_measured_cpt']:,} | "
            f"**${rp['projected_usd_at_measured_cpt']:.3f}** | ${rp['projected_usd_at_chars_div_3_0']:.3f} |"
        )

    md.extend(["\n## 10 Example Pilot Decision Answers\n", "| # | Stratum | Model | Outcome | Step / Onset | Strict in Win? | `p_stuck` | `p_progress` | `p_doomed` | Tokens | Latency (ms) |", "| ---: | --- | --- | --- | ---: | :---: | ---: | ---: | ---: | ---: | ---: |"])
    for i, ex in enumerate(chosen_examples, 1):
        out_s = "PASS" if ex["succeeded"] else "FAIL"
        on_s = str(ex["strict_onset_step"]) if ex["strict_onset_step"] is not None else "-"
        ps = f"{ex['p_stuck']:.2f}" if ex["p_stuck"] is not None else f"BLK"
        pp = f"{ex['p_progress']:.2f}" if ex["p_progress"] is not None else "BLK"
        pd_v = f"{ex['p_doomed']:.2f}" if ex["p_doomed"] is not None else "BLK"
        md.append(
            f"| {i} | `{ex['stratum']}` | `{ex['model']}` | {out_s} | {ex['step_idx']} / {on_s} | "
            f"{'YES' if ex['active_strict_in_window'] else 'no'} | {ps} | {pp} | {pd_v} | {ex['actual_input_tokens']:,} | {ex['latency_ms']:.0f} |"
        )
    (results_dir / "pilot_report.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    return summary


def _style_ax(ax: plt.Axes) -> None:
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(True, linestyle="--", alpha=0.3)


def generate_example_window_chart(
    *,
    point: GridPointRecord,
    prompt_text: str,
    title_context: str,
    out_path: Path,
) -> Path:
    """Render assets/example_window.png showing a 12-step window and Decision 1's three probabilities."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig = plt.figure(figsize=(16, 9), dpi=100)
    gs = fig.add_gridspec(1, 2, width_ratios=[1.65, 1.0], wspace=0.18)

    ax_txt = fig.add_subplot(gs[0, 0])
    ax_bar = fig.add_subplot(gs[0, 1])

    ax_txt.axis("off")
    # Truncate lines cleanly so text renders legibly in left pane
    wrapped_lines: list[str] = []
    for raw_ln in prompt_text.splitlines():
        ln = raw_ln.strip()
        if not ln:
            wrapped_lines.append("")
            continue
        while len(ln) > 92:
            wrapped_lines.append(ln[:91] + "…")
            ln = ""
        if ln:
            wrapped_lines.append(ln)
    shown = "\n".join(wrapped_lines[:42])
    ax_txt.text(
        0.01,
        0.99,
        shown,
        va="top",
        ha="left",
        family="monospace",
        fontsize=8.2,
        bbox={"boxstyle": "round,pad=0.6", "facecolor": "#f8fafc", "edgecolor": "#cbd5e1"},
    )
    ax_txt.set_title("Input Given to Decision 1 (Causal 12-Step Window)", fontsize=13, fontweight="bold", loc="left")

    p_stuck = float(point.p_stuck or 0.0)
    p_noprog = 1.0 - float(point.p_progress or 0.0)
    p_doomed = float(point.p_doomed or 0.0)
    labels = ["Stuck in loop\nP(stuck)", "No progress\n1 - P(progress)", "Likely to fail\nP(doomed)"]
    vals = [p_stuck, p_noprog, p_doomed]
    colors = ["#dc2626", "#f59e0b", "#4f46e5"]

    bars = ax_bar.barh(labels[::-1], vals[::-1], color=colors[::-1], height=0.48, edgecolor="#1e293b")
    ax_bar.set_xlim(0.0, 1.05)
    ax_bar.axvline(0.5, color="#64748b", linestyle=":", linewidth=1.2)
    ax_bar.axvline(0.9, color="#dc2626", linestyle="--", linewidth=1.2, label="0.90 stop threshold")
    for bar, v in zip(bars, vals[::-1]):
        ax_bar.text(v + 0.02, bar.get_y() + bar.get_height() / 2.0, f"{v:.2f}", va="center", fontsize=12, fontweight="bold")
    ax_bar.set_xlabel("Calibrated Probability (noul)", fontsize=11)
    ax_bar.set_title("Decision 1 Probabilities", fontsize=13, fontweight="bold", loc="left")
    ax_bar.legend(loc="lower right", frameon=False)
    _style_ax(ax_bar)

    fig.suptitle(title_context, fontsize=15, fontweight="bold", y=0.98)
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    return out_path


def _plot_fsr_vs_savings(
    outcome_df: pd.DataFrame,
    *,
    corpus_label: str,
    out_path: Path,
) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(16, 9), dpi=100)

    fam_styles = {
        "d1_stuck": ("#2563eb", "-o", "Decision 1: Stuck"),
        "d1_stuck2": ("#1d4ed8", "--s", "Decision 1: Stuck (2 checks)"),
        "d1_doomed": ("#dc2626", "-^", "Decision 1: Likely to Fail"),
        "d1_noprog": ("#d97706", "-D", "Decision 1: No Progress"),
    }

    for fam, (col, fmt, label) in fam_styles.items():
        sub = outcome_df[outcome_df["family"] == fam].sort_values("fsr_passed")
        if sub.empty:
            continue
        x = sub["fsr_passed"] * 100.0
        y = sub["net_saved_failed_pct"] * 100.0
        ax.plot(x, y, fmt, color=col, linewidth=2.2, markersize=7, label=label)
        for _, row in sub.iterrows():
            if row["threshold"] in (0.5, 0.8, 0.9, 0.98):
                ax.annotate(f"{row['threshold']:.2f}", (row["fsr_passed"] * 100.0, row["net_saved_failed_pct"] * 100.0), textcoords="offset points", xytext=(4, 5), fontsize=8, color=col)

    # Matched-random band from d1_stuck sweep or overall rules sorted by FSR
    band_src = outcome_df[outcome_df["family"].isin(["d1_stuck", "rule"])].sort_values("rand_fsr_passed_mean")
    if not band_src.empty:
        bx = band_src["rand_fsr_passed_mean"] * 100.0
        by_m = band_src["rand_saved_failed_pct_mean"] * 100.0
        by_lo = band_src["rand_saved_failed_pct_ci_low"] * 100.0
        by_hi = band_src["rand_saved_failed_pct_ci_high"] * 100.0
        ax.fill_between(bx, by_lo, by_hi, color="#94a3b8", alpha=0.22, label="Matched Random (95% CI)")
        ax.plot(bx, by_m, ":", color="#475569", linewidth=1.6, label="Matched Random Mean")

    # Gemini CLI arm
    for gname, col, mk in [("gemini_cli_llm", "#059669", "P"), ("gemini_cli_full", "#047857", "*")]:
        gsub = outcome_df[outcome_df["stopper"] == gname]
        if not gsub.empty:
            r = gsub.iloc[0]
            ax.scatter([r["fsr_passed"] * 100.0], [r["net_saved_failed_pct"] * 100.0], s=170, color=col, marker=mk, zorder=6, label=gname)
            ax.annotate(gname, (r["fsr_passed"] * 100.0, r["net_saved_failed_pct"] * 100.0), textcoords="offset points", xytext=(6, -4), fontsize=9, fontweight="bold", color=col)

    # Rule detectors
    rules = outcome_df[outcome_df["kind"] == "rule"]
    for _, r in rules.iterrows():
        rx = r["fsr_passed"] * 100.0
        ry = r["net_saved_failed_pct"] * 100.0
        ax.scatter([rx], [ry], s=95, color="#0f172a", marker="X", zorder=5)
        ax.annotate(str(r["stopper"]), (rx, ry), textcoords="offset points", xytext=(5, 4), fontsize=8.5, color="#0f172a")

    ax.axhline(0.0, color="#64748b", linewidth=1.0, linestyle="-")
    ax.set_xlabel("False-Stop Rate Among Passed Runs (%)", fontsize=12)
    ax.set_ylabel("Net Dollars Saved (% of Valid Evaluation Spend)", fontsize=12)
    ax.set_title(f"{corpus_label}: False-Stop Cost vs. Net Spend Saved Across Stopping Rules", fontsize=15, fontweight="bold")
    ax.legend(loc="best", frameon=True, fontsize=9.5)
    _style_ax(ax)
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    return out_path


def _plot_lead_time(strict_df: pd.DataFrame, *, out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sub = strict_df[strict_df["slice"] == "all_strict"].copy()
    sub = sub.dropna(subset=["median_lead_steps"])
    sub = sub.sort_values("recall", ascending=True)
    fig, ax = plt.subplots(figsize=(16, 9), dpi=100)
    y = np.arange(len(sub))
    labels = [f"{r['corpus'].upper()} | {r['stopper']} (recall {r['recall']*100:.1f}%)" for _, r in sub.iterrows()]
    meds = sub["median_lead_steps"].to_numpy()
    err_lo = meds - sub["p25_lead_steps"].to_numpy()
    err_hi = sub["p75_lead_steps"].to_numpy() - meds
    colors = ["#2563eb" if "d1_" in s or "gemini_cli_" in s else "#475569" for s in sub["stopper"]]
    ax.barh(y, meds, xerr=[np.maximum(0, err_lo), np.maximum(0, err_hi)], color=colors, height=0.62, alpha=0.88, ecolor="#1e293b", capsize=3)
    ax.axvline(0.0, color="#dc2626", linestyle="--", linewidth=1.3, label="Strict loop onset (0)")
    ax.set_yticks(y)
    ax.set_yticklabels(labels, fontsize=9)
    ax.set_xlabel("Median Detection Lead Time vs. Strict Loop Onset (steps; negative = earlier)", fontsize=12)
    ax.set_title("When Each Stopper Fires Relative to Strict Loop Onset (Median & IQR)", fontsize=15, fontweight="bold")
    ax.legend(loc="best", frameon=False)
    _style_ax(ax)
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    return out_path


def _plot_hard_negatives(hn_df: pd.DataFrame, *, out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sub = hn_df.sort_values(["corpus", "fire_rate"], ascending=[True, False]).copy()
    fig, ax = plt.subplots(figsize=(16, 9), dpi=100)
    x = np.arange(len(sub))
    rates = sub["fire_rate"].to_numpy() * 100.0
    lo = np.maximum(0.0, rates - sub["fire_rate_ci_low"].to_numpy() * 100.0)
    hi = np.maximum(0.0, sub["fire_rate_ci_high"].to_numpy() * 100.0 - rates)
    colors = ["#2563eb" if str(c) == "swe" else "#059669" for c in sub["corpus"]]
    ax.bar(x, rates, yerr=[lo, hi], color=colors, capsize=3, alpha=0.88)
    ax.set_xticks(x)
    ax.set_xticklabels([f"{r['corpus'].upper()}: {r['stopper']}" for _, r in sub.iterrows()], rotation=42, ha="right", fontsize=8.5)
    ax.set_ylabel("False Alarm Rate on Harmless Re-Read Runs (%)", fontsize=12)
    ax.set_title("False Alarms on Runs with Only Harmless Re-Reads or Near-Duplicates", fontsize=15, fontweight="bold")
    _style_ax(ax)
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    return out_path


def _plot_calibration(cal_bins_stuck: pd.DataFrame, cal_bins_doomed: pd.DataFrame, meta: dict[str, Any], *, out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig, axes = plt.subplots(1, 2, figsize=(16, 9), dpi=100)
    for ax, df_b, key, title in [
        (axes[0], cal_bins_stuck, "stuck", "P(stuck) vs. Active Strict Loop in Window"),
        (axes[1], cal_bins_doomed, "doomed", "P(doomed) vs. Eventual Run Failure"),
    ]:
        ax.plot([0, 1], [0, 1], "--", color="#64748b", label="Perfect calibration")
        ok = df_b.dropna(subset=["mean_pred", "empirical_rate"])
        if not ok.empty:
            ax.plot(ok["mean_pred"], ok["empirical_rate"], "-o", color="#2563eb", linewidth=2.2, markersize=7, label="Decision 1 (10 bins)")
            for _, r in ok.iterrows():
                ax.annotate(f"n={int(r['count'])}", (r["mean_pred"], r["empirical_rate"]), textcoords="offset points", xytext=(0, 7), ha="center", fontsize=8)
        m = meta.get(key, {})
        ax.set_title(f"{title}\nBrier={m.get('brier', float('nan')):.4f} | ECE={m.get('ece', float('nan')):.4f}", fontsize=13, fontweight="bold")
        ax.set_xlabel("Predicted Probability", fontsize=11)
        ax.set_ylabel("Observed Frequency", fontsize=11)
        ax.set_xlim(0.0, 1.0)
        ax.set_ylim(0.0, 1.0)
        ax.legend(loc="upper left", frameon=False)
        _style_ax(ax)
    fig.suptitle("Calibration Check: Decision 1 Probabilities vs. Observed Outcomes", fontsize=15, fontweight="bold", y=0.98)
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    return out_path


def _plot_open_vs_closed(fair_df: pd.DataFrame, *, out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig, axes = plt.subplots(1, 2, figsize=(16, 9), dpi=100, sharey=True)
    for ax, outc in zip(axes, ["passed", "failed"]):
        sub = fair_df[fair_df["outcome"] == outc].copy()
        if sub.empty:
            continue
        x = np.arange(len(sub))
        w = 0.36
        ax.bar(x - w / 2, sub["open_fire_rate"] * 100.0, width=w, color="#2563eb", label="Open-weight")
        ax.bar(x + w / 2, sub["closed_fire_rate"] * 100.0, width=w, color="#f59e0b", label="Closed")
        ax.set_xticks(x)
        ax.set_xticklabels([f"{r['corpus'].upper()}:{r['stopper']}" for _, r in sub.iterrows()], rotation=38, ha="right", fontsize=8.5)
        ax.set_title(f"Within {outc.capitalize()} Runs", fontsize=13, fontweight="bold")
        ax.legend(loc="upper right", frameon=False)
        _style_ax(ax)
    axes[0].set_ylabel("Stopping Rate (%)", fontsize=12)
    fig.suptitle("Stopping Rates by Model Weight Class at Equal Run Outcome (No Model Name in Prompt)", fontsize=15, fontweight="bold", y=0.98)
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    return out_path


def write_hand_audit_md(
    *,
    runs: Sequence[RunMetadata],
    traces: dict[str, SelectedTrajectory],
    d1_stops: dict[str, int | None],
    rule_stops: dict[str, dict[str, int | None]],
    out_path: Path,
    seed: int = RANDOM_SEED,
) -> Path:
    """Write audit/decision_vs_rules.md with 20 random D1-only fires and 20 random Rule-only fires."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)

    any_rule_fire = {
        r.uid: min([st for st in (rule_stops.get(r.uid) or {}).values() if st is not None], default=None)
        for r in runs
    }
    d1_only = [r for r in runs if d1_stops.get(r.uid) is not None and any_rule_fire.get(r.uid) is None]
    rule_only = [r for r in runs if d1_stops.get(r.uid) is None and any_rule_fire.get(r.uid) is not None]

    def _pick(pool: list[RunMetadata], k: int = 20) -> list[RunMetadata]:
        pool_s = sorted(pool, key=lambda r: (r.corpus, r.model, r.task_name, r.uid))
        if len(pool_s) <= k:
            return pool_s
        idxs = rng.choice(len(pool_s), size=k, replace=False)
        return [pool_s[int(i)] for i in sorted(int(x) for x in idxs)]

    pick_d1 = _pick(d1_only, 20)
    pick_rule = _pick(rule_only, 20)

    lines: list[str] = [
        "# Hand Audit: Decision 1-Only Fires vs. Rule-Only Fires\n",
        f"Sampled with `seed={seed}` (20 Decision 1-only fires, 20 Rule-only fires).\n",
    ]
    for section, subset, kind in [
        ("## Part 1: Fires Only Made by Decision 1 (`d1_stuck@0.80` / calibrated)\n", pick_d1, "d1"),
        ("## Part 2: Fires Only Made by Rule Detectors\n", pick_rule, "rule"),
    ]:
        lines.append(section)
        if not subset:
            lines.append("_No disagreement cases in this subset yet._\n")
            continue
        for i, r in enumerate(subset, 1):
            st = d1_stops.get(r.uid) if kind == "d1" else any_rule_fire.get(r.uid)
            assert st is not None
            tr = traces[r.uid]
            pref = slice_prefix_steps(tr.steps, min(st, len(tr.steps) - 1))
            win = render_decision_window(tr.task_text, pref, step_idx=min(st, len(tr.steps) - 1), window_steps=MAIN_WINDOW_STEPS)
            lines.append(
                f"### Case {i}: `{r.corpus.upper()}` `{r.model}` on `{r.task_name}` (`{r.uid}`)\n"
                f"- **Outcome**: `{'PASS' if r.succeeded else 'FAIL'}` | **Steps**: `{r.n_steps}` | "
                f"**Strict Loop**: `{r.has_strict_loop}` (onset `{r.strict_onset_step}`) | "
                f"**Stop Step**: `{st}` | **Rule Stops**: `{rule_stops.get(r.uid)}`\n"
            )
            lines.append("```text\n" + win + "\n```\n")

    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_path


def run_full_analysis(
    *,
    data: PreparedStudyData,
    swe_grid: dict[str, list[GridPointRecord]],
    swe_gcli: dict[str, GeminiCliRunResult],
    tb_grid: dict[str, list[GridPointRecord]],
    tb_gcli: dict[str, GeminiCliRunResult],
    results_dir: Path = RESULTS_DIR,
    assets_dir: Path = ASSETS_DIR,
    audit_dir: Path = AUDIT_DIR,
) -> dict[str, Any]:
    """Compute all corpus tables, CIs, calibrations, audit samples, and 1600x900 charts from cached results."""
    results_dir.mkdir(parents=True, exist_ok=True)
    assets_dir.mkdir(parents=True, exist_ok=True)
    audit_dir.mkdir(parents=True, exist_ok=True)

    summary_meta: dict[str, Any] = {"corpora_analyzed": []}
    strict_frames: list[pd.DataFrame] = []
    hn_frames: list[pd.DataFrame] = []
    fair_frames: list[pd.DataFrame] = []
    cal_points_all: list[GridPointRecord] = []
    cal_outcomes_by_uid: dict[str, bool] = {}

    audit_runs: list[RunMetadata] = []
    audit_traces: dict[str, SelectedTrajectory] = {}
    audit_d1_stops: dict[str, int | None] = {}
    audit_rule_stops: dict[str, dict[str, int | None]] = {}

    for corpus, runs, assigns, traces, grid_map, gcli_map in [
        ("swe", data.swe_runs, data.swe_assign, data.selected_swe, swe_grid, swe_gcli),
        ("tb", data.tb_runs, data.tb_assign, data.selected_tb, tb_grid, tb_gcli),
    ]:
        if not grid_map:
            continue
        summary_meta["corpora_analyzed"].append(corpus)
        samp_a = [r for r in runs if assigns.get(r.uid, {}).get("in_sample_a")]
        w_a = [float(assigns[r.uid]["sample_weight"]) for r in samp_a]

        # Outcome-based curves on Sample A
        outcome_rows: list[dict[str, Any]] = []
        for fam in GRID_STOPPER_FAMILIES:
            for tau in THRESHOLDS:
                stops: list[int | None] = []
                apis: list[float] = []
                for r in samp_a:
                    pts = grid_map.get(r.uid, [])
                    st, ap = score_all_grid_stoppers(pts, thresholds=(tau,))[stopper_label(fam, tau)]
                    stops.append(st)
                    apis.append(ap)
                row = evaluate_outcome_stopper(
                    stopper_name=stopper_label(fam, tau),
                    runs=samp_a,
                    weights=w_a,
                    stop_steps=stops,
                    api_costs_usd=apis,
                    n_rand_seeds=200,
                    n_boot=1000,
                    seed=RANDOM_SEED,
                )
                row["kind"] = "decision1_grid"
                row["family"] = fam
                row["threshold"] = tau
                outcome_rows.append(row)

        # Gemini CLI arm on Sample A
        if gcli_map:
            for arm_name, get_step, get_api in [
                (
                    "gemini_cli_llm",
                    lambda uid: gcli_map[uid].llm_stop_step if uid in gcli_map else None,
                    lambda uid: gcli_map[uid].api_cost_usd if uid in gcli_map else 0.0,
                ),
                (
                    "gemini_cli_full",
                    lambda uid: gcli_map[uid].full_stop_step if uid in gcli_map else None,
                    lambda uid: gcli_map[uid].api_cost_usd if uid in gcli_map else 0.0,
                ),
            ]:
                row = evaluate_outcome_stopper(
                    stopper_name=arm_name,
                    runs=samp_a,
                    weights=w_a,
                    stop_steps=[get_step(r.uid) for r in samp_a],
                    api_costs_usd=[get_api(r.uid) for r in samp_a],
                    n_rand_seeds=200,
                    n_boot=1000,
                    seed=RANDOM_SEED,
                )
                row["kind"] = "gemini_cli_arm"
                row["family"] = arm_name
                row["threshold"] = np.nan
                outcome_rows.append(row)

        # Rule baselines on Sample A
        for rname in RULE_DETECTORS:
            row = evaluate_outcome_stopper(
                stopper_name=rname,
                runs=samp_a,
                weights=w_a,
                stop_steps=[r.stop_steps.get(rname) for r in samp_a],
                api_costs_usd=[0.0] * len(samp_a),
                n_rand_seeds=200,
                n_boot=1000,
                seed=RANDOM_SEED,
            )
            row["kind"] = "rule"
            row["family"] = "rule"
            row["threshold"] = np.nan
            outcome_rows.append(row)

        outcome_df = pd.DataFrame(outcome_rows)
        outcome_df.to_csv(results_dir / f"outcome_curve_{corpus}.csv", index=False)

        _plot_fsr_vs_savings(
            outcome_df,
            corpus_label="SWE-rebench (swelike)" if corpus == "swe" else "Terminal-Bench 2.0 (terminus-2)",
            out_path=assets_dir / f"fsr_vs_savings_{corpus}.png",
        )

        # 50/50 Task-split Calibration
        cal_df, cal_rules_df = evaluate_task_split_calibration(
            runs=samp_a,
            weights=w_a,
            grid_by_uid=grid_map,
            rule_names=RULE_DETECTORS,
            seed=RANDOM_SEED,
        )
        cal_df.to_csv(results_dir / f"task_split_calibration_{corpus}.csv", index=False)
        cal_rules_df.to_csv(results_dir / f"task_split_rules_{corpus}.csv", index=False)

        # Sample A+B stoppers dictionary for Strict-Loop Detection, Hard Negatives, Cost-per-Catch, and Hand Audit
        all_sampled = [r for r in runs if r.uid in assigns]
        all_stops_by_uid: dict[str, dict[str, int | None]] = {}
        all_apis_by_uid: dict[str, dict[str, float]] = {}
        for r in all_sampled:
            sdict: dict[str, int | None] = {d: r.stop_steps.get(d) for d in RULE_DETECTORS}
            adict: dict[str, float] = {d: 0.0 for d in RULE_DETECTORS}
            pts = grid_map.get(r.uid, [])
            scored = score_all_grid_stoppers(pts)
            for sname, (st, ap) in scored.items():
                sdict[sname] = st
                adict[sname] = ap
            if r.uid in gcli_map:
                sdict["gemini_cli_llm"] = gcli_map[r.uid].llm_stop_step
                sdict["gemini_cli_full"] = gcli_map[r.uid].full_stop_step
                adict["gemini_cli_llm"] = gcli_map[r.uid].api_cost_usd
                adict["gemini_cli_full"] = gcli_map[r.uid].api_cost_usd
            all_stops_by_uid[r.uid] = sdict
            all_apis_by_uid[r.uid] = adict

        # Strict-loop Recall & Lead Time (A+B)
        strict_ab = [r for r in all_sampled if r.has_strict_loop]
        sdf = evaluate_strict_loop_detection(
            strict_runs=strict_ab,
            stopper_stops_by_uid=all_stops_by_uid,
            n_boot=1000,
            seed=RANDOM_SEED,
        )
        sdf["corpus"] = corpus
        sdf.to_csv(results_dir / f"strict_loops_{corpus}.csv", index=False)
        # Keep representative subset for combined lead-time chart
        key_for_plot = {
            "d1_stuck@0.80",
            "d1_stuck2@0.80",
            "d1_doomed@0.80",
            "d1_noprog@0.80",
            "gemini_cli_full",
            "gemini_cli",
            "cline_style",
            "openhands_stuck",
            "openclaw_10",
            "turn_budget_1_25x_p90",
        }
        strict_frames.append(sdf[sdf["stopper"].isin(key_for_plot)])

        # Cost per catch on Sample A+B strict loops vs Sample A
        caught_raw_cost_rows: list[dict[str, Any]] = []
        for fam in GRID_STOPPER_FAMILIES:
            for tau in (0.7, 0.8, 0.9):
                sn = stopper_label(fam, tau)
                tot_api = sum(all_apis_by_uid[r.uid].get(sn, 0.0) for r in all_sampled)
                caught_ab = sum(1 for r in strict_ab if all_stops_by_uid[r.uid].get(sn) is not None)
                row_a = outcome_df[outcome_df["stopper"] == sn].iloc[0]
                caught_raw_cost_rows.append(
                    {
                        "corpus": corpus,
                        "stopper": sn,
                        "api_usd_per_run_sample_a": row_a["api_cost_per_run_usd"],
                        "api_usd_per_caught_strict_ab": (tot_api / caught_ab) if caught_ab > 0 else float("nan"),
                        "api_usd_per_avoided_fail_dollar_a": row_a["api_usd_per_avoided_fail_usd"],
                        "caught_strict_ab": caught_ab,
                        "total_strict_ab": len(strict_ab),
                    }
                )
        pd.DataFrame(caught_raw_cost_rows).to_csv(results_dir / f"cost_per_catch_{corpus}.csv", index=False)

        # Hard Negatives (Sample B default-only)
        def_b = [r for r in all_sampled if assigns[r.uid].get("sample_b_role") == "default_only"]
        hn_df = evaluate_hard_negatives(
            default_only_runs=def_b,
            stopper_stops_by_uid=all_stops_by_uid,
            n_boot=1000,
            seed=RANDOM_SEED,
        )
        hn_df["corpus"] = corpus
        hn_df.to_csv(results_dir / f"hard_negatives_{corpus}.csv", index=False)
        hn_frames.append(hn_df[hn_df["stopper"].isin(key_for_plot)])

        # Open vs Closed fairness (Sample A weighted, within Passed & Failed)
        fair_stoppers = ["d1_stuck@0.80", "d1_stuck2@0.80", "d1_doomed@0.80", "d1_noprog@0.80", "gemini_cli", "openhands_stuck"]
        fair_df = evaluate_open_vs_closed_fairness(
            runs=samp_a,
            weights=w_a,
            stopper_stops_by_uid=all_stops_by_uid,
            stopper_names=[s for s in fair_stoppers if any(s in all_stops_by_uid[r.uid] for r in samp_a)],
            n_boot=1000,
            seed=RANDOM_SEED,
        )
        fair_df["corpus"] = corpus
        fair_df.to_csv(results_dir / f"open_vs_closed_fairness_{corpus}.csv", index=False)
        fair_frames.append(fair_df[fair_df["stopper"].isin(["d1_stuck@0.80", "d1_doomed@0.80", "gemini_cli"])])

        # Gather calibration points (both Sample A weighted & Pooled A+B unweighted)
        for r in all_sampled:
            cal_outcomes_by_uid[r.uid] = bool(r.succeeded)
            for pt in grid_map.get(r.uid, []):
                if pt.status == "ok":
                    cal_points_all.append(pt)

        # Collect for hand audit
        audit_runs.extend(all_sampled)
        audit_traces.update(traces)
        for r in all_sampled:
            audit_d1_stops[r.uid] = all_stops_by_uid[r.uid].get("d1_stuck@0.80")
            audit_rule_stops[r.uid] = {d: all_stops_by_uid[r.uid].get(d) for d in ("gemini_cli", "cline_style", "openhands_stuck", "openclaw_10")}

        summary_meta[corpus] = {
            "n_sample_a": len(samp_a),
            "n_strict_ab": len(strict_ab),
            "n_default_only_b": len(def_b),
            "n_grid_points_evaluated": sum(len(grid_map.get(r.uid, [])) for r in all_sampled),
            "n_grid_blocked": sum(1 for r in all_sampled for p in grid_map.get(r.uid, []) if p.status == "blocked"),
        }

    if strict_frames:
        _plot_lead_time(pd.concat(strict_frames, ignore_index=True), out_path=assets_dir / "lead_time_strict_loops.png")
    if hn_frames:
        _plot_hard_negatives(pd.concat(hn_frames, ignore_index=True), out_path=assets_dir / "hard_negative_fire_rate.png")
    if fair_frames:
        _plot_open_vs_closed(pd.concat(fair_frames, ignore_index=True), out_path=assets_dir / "open_vs_closed_fire_rates.png")

    if cal_points_all:
        y_st = np.array([1.0 if p.active_strict_in_window else 0.0 for p in cal_points_all])
        p_st = np.array([float(p.p_stuck) for p in cal_points_all if p.p_stuck is not None])
        y_dm = np.array([0.0 if cal_outcomes_by_uid[p.uid] else 1.0 for p in cal_points_all])
        p_dm = np.array([float(p.p_doomed) for p in cal_points_all if p.p_doomed is not None])

        b_st, ece_st, bins_st = compute_brier_and_ece(y_st, p_st)
        b_dm, ece_dm, bins_dm = compute_brier_and_ece(y_dm, p_dm)
        auc_st = compute_roc_auc(y_st, p_st)
        auc_dm = compute_roc_auc(y_dm, p_dm)

        bins_st.to_csv(results_dir / "calibration_bins_stuck.csv", index=False)
        bins_dm.to_csv(results_dir / "calibration_bins_doomed.csv", index=False)
        cal_summary = {
            "n_points": len(cal_points_all),
            "stuck": {"brier": b_st, "ece": ece_st, "roc_auc": auc_st, "positive_rate": float(np.mean(y_st))},
            "doomed": {"brier": b_dm, "ece": ece_dm, "roc_auc": auc_dm, "positive_rate": float(np.mean(y_dm))},
        }
        (results_dir / "calibration_summary.json").write_bytes(
            orjson.dumps(cal_summary, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS)
        )
        summary_meta["calibration"] = cal_summary
        _plot_calibration(bins_st, bins_dm, cal_summary, out_path=assets_dir / "calibration_stuck_doomed.png")

    if audit_runs:
        write_hand_audit_md(
            runs=audit_runs,
            traces=audit_traces,
            d1_stops=audit_d1_stops,
            rule_stops=audit_rule_stops,
            out_path=audit_dir / "decision_vs_rules.md",
        )

    return summary_meta


def update_notes_md(
    *,
    data: PreparedStudyData,
    plan_df: pd.DataFrame,
    pilot_summary: dict[str, Any] | None = None,
    analysis_meta: dict[str, Any] | None = None,
    spend_summary: dict[str, Any] | None = None,
    notes_path: Path = ROOT_DIR / "NOTES.md",
) -> Path:
    """Write or refresh NOTES.md strictly from computed artifacts and pinned package/dataset versions."""
    try:
        genai_ver = importlib.metadata.version("google-genai")
    except importlib.metadata.PackageNotFoundError:
        genai_ver = "unknown"

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    swe_s = data.swe_sanity
    tb_s = data.tb_sanity

    lines: list[str] = [
        "# agent-loop-decisions — Engineering Notes\n",
        "## Goal\n",
        "Evaluate whether zero-shot `gemini-decision-1` (Google Decisions API) stops wasteful AI agent trajectories better than rule-based loop detectors (`cline_style`, `openhands_stuck`, `gemini_cli`, `openclaw_10/20`, step caps, and `1.25x P90` turn budget) across SWE-rebench and Terminal-Bench 2.0.\n",
        "## Setup\n",
        f"- **Decision Model**: `{DECISION_MODEL}` (`google-genai=={genai_ver}`, `v1beta` REST / SDK, `$0.10 / 1M` input tokens, `$0` output tokens)",
        f"- **Pinned `agent-loops` commit**: [`{AGENT_LOOPS_COMMIT}`](https://github.com/DavidFlorez/agent-loops/tree/{AGENT_LOOPS_COMMIT})",
        f"- **Pinned `google-gemini/gemini-cli` reference**: commit [`{GEMINI_CLI_COMMIT}`](https://github.com/google-gemini/gemini-cli/blob/{GEMINI_CLI_COMMIT}/packages/core/src/services/loopDetectionService.ts) (`LLM_CHECK_AFTER_TURNS=30`, `DEFAULT_LLM_CHECK_INTERVAL=10`, adaptive bounds `[5, 15]` via `round(5 + 10*(1 - p))`, `LLM_LOOP_CHECK_HISTORY_COUNT=20`, `LLM_CONFIDENCE_THRESHOLD=0.9`; Decision 1 `unproductive_loop` noul stands in for production Gemini CLI's generative Gemini LLM check)",
        f"- **SWE-rebench dataset**: [`{SWE_DATASET_REPO}`](https://huggingface.co/datasets/{SWE_DATASET_REPO}) @ `{SWE_DATASET_SHA}` (`swelike` = `common_scaffold_model`)",
        f"- **Terminal-Bench 2.0 dataset**: [`{TB_DATASET_REPO}`](https://huggingface.co/datasets/{TB_DATASET_REPO}) @ `{TB_DATASET_SHA}` (`terminus-2` harness)",
        "- **Hardware**: Apple MacBook Air (M3, 16 GB RAM, CPU-only); API-bound workload.",
        f"- **Sanity Check (verified against `agent-loops@{AGENT_LOOPS_COMMIT[:7]}`)**:",
        f"  - SWE `swelike`: `{swe_s['runs']:,}` runs (`{swe_s['models']}` models × `{swe_s['runs']//max(1,swe_s['models'])}`), pass rate `{swe_s['pass_rate']*100:.1f}%`, `{swe_s['strict_runs']}` strict loops (`{swe_s['strict_open']}` open, `{swe_s['strict_closed']}` closed, `{swe_s['strict_passed']}` passed), `{swe_s['default_only_runs']:,}` default-only, `{swe_s['cost_valid_runs']:,}` cost-valid worth `${swe_s['total_usd']:,.2f}` (strict `${swe_s['strict_usd']:,.2f}` / `{swe_s['strict_usd_share']*100:.1f}%`, failed `${swe_s['fail_usd']:,.2f}` / `{swe_s['fail_usd_share']*100:.1f}%`), `gemini_cli` hash rules fired `{swe_s['gemini_cli_default_only_fires']}` times on default-only runs (`{swe_s['gemini_cli_fires']}` total; `openhands_stuck` fired `{swe_s['openhands_default_only_fires']}` on default-only and `{swe_s['openhands_strict_fires']}` on strict).",
        f"  - TB `terminus-2`: `{tb_s['runs']:,}` runs (`{tb_s['models']}` models), pass rate `{tb_s['pass_rate']*100:.1f}%`, `{tb_s['strict_runs']}` strict loops (`{tb_s['strict_open']}` open, `{tb_s['strict_closed']}` closed, `{tb_s['strict_passed']}` passed), `{tb_s['default_only_runs']:,}` default-only, `{tb_s['cost_valid_runs']:,}` cost-valid worth `${tb_s['total_usd']:,.2f}` (strict `${tb_s['strict_usd']:,.2f}` / `{tb_s['strict_usd_share']*100:.1f}%`, failed `${tb_s['fail_usd']:,.2f}` / `{tb_s['fail_usd_share']*100:.1f}%`), `gemini_cli` hash rules fired `{tb_s['gemini_cli_fires']}` times (`{tb_s['gemini_cli_strict_fires']}` strict, `{tb_s['gemini_cli_default_only_fires']}` default-only).\n",
        "## Log\n",
        f"- **{now_str}**: Built deterministic sampling + offline causal replay pipeline (`data/parsed_cache/`), ran `plan` (0 API calls), verified all baseline sanity counts against `agent-loops`.",
    ]
    if pilot_summary:
        lines.append(
            f"- **{now_str}**: Ran `pilot` (`{pilot_summary['n_points']}` points, `${pilot_summary['total_cost_usd']:.6f}` spend, "
            f"`{pilot_summary['total_actual_input_tokens']:,}` input tokens, `{pilot_summary['measured_chars_per_token_aggregate']:.3f}` chars/token, "
            f"`{pilot_summary['n_blocked']}` blocked)."
        )
    if analysis_meta and analysis_meta.get("corpora_analyzed"):
        lines.append(
            f"- **{now_str}**: Completed `analyze` over cached responses for corpora `{analysis_meta['corpora_analyzed']}`."
        )

    lines.append("\n## Key Results\n")
    lines.append("### Planning Projections (`results/plan.csv`)\n")
    lines.append("| Phase | Arm | Stratum | Runs | Decision Points | Projected Tokens (`chars/3.0`) | Conservative USD (`chars/3.0`) |")
    lines.append("| --- | --- | --- | ---: | ---: | ---: | ---: |")
    for _, r in plan_df.iterrows():
        lines.append(
            f"| `{r['phase']}` | `{r['arm']}` | `{r['stratum']}` | {int(r['n_runs']):,} | {int(r['n_decision_points']):,} | "
            f"{int(r['est_tokens_chars_div_3_0']):,} | ${float(r['est_usd_chars_div_3_0']):.4f} |"
        )

    if pilot_summary:
        rp_swe = pilot_summary["reprojected_phases"]["swe"]
        rp_tb = pilot_summary["reprojected_phases"]["tb"]
        lines.extend(
            [
                "\n### Pilot Results (`results/pilot_report.md`)\n",
                f"- **Measured Tokens vs Estimate**: `{pilot_summary['total_actual_input_tokens']:,}` actual vs `{pilot_summary['total_est_tokens_3_0']:,}` (`chars/3.0`), measured **`{pilot_summary['measured_chars_per_token_aggregate']:.3f}` chars/token**",
                f"- **Pilot Spend & Latency**: `${pilot_summary['total_cost_usd']:.6f}` total; median `{pilot_summary['median_latency_ms']:.1f} ms` (P95 `{pilot_summary['p95_latency_ms']:.1f} ms`); safety block rate `{_fmt_pct(pilot_summary['block_rate'])}`",
                f"- **Re-Projected Spend at Measured Ratio**: `swe` = **`${rp_swe['projected_usd_at_measured_cpt']:.3f}`** (`{rp_swe['projected_calls']:,}` calls, `{rp_swe['projected_tokens_at_measured_cpt']:,}` tokens); `tb` = **`${rp_tb['projected_usd_at_measured_cpt']:.3f}`** (`{rp_tb['projected_calls']:,}` calls, `{rp_tb['projected_tokens_at_measured_cpt']:,}` tokens)",
            ]
        )

    if spend_summary:
        lines.append(
            f"- **Cumulative Spend (`cache/spend.json`)**: `${spend_summary.get('total_spent_usd', 0.0):.6f}` across `{spend_summary.get('total_calls', 0):,}` uncached API calls (`{spend_summary.get('total_blocked', 0)}` blocked)."
        )

    lines.append("\n## Visuals\n")
    visual_captions = [
        ("assets/example_window.png", "Example causal 12-step trajectory window paired with Decision 1 probabilities."),
        ("assets/fsr_vs_savings_swe.png", "SWE-rebench: False-stop rate among passed runs vs. net dollars saved across Decision 1 thresholds, rule stoppers, and 200-seed matched random."),
        ("assets/fsr_vs_savings_tb.png", "Terminal-Bench 2.0: False-stop rate among passed runs vs. net dollars saved across Decision 1 thresholds, rule stoppers, and 200-seed matched random."),
        ("assets/lead_time_strict_loops.png", "Median detection lead time relative to strict loop onset (and strict-loop recall) per stopper."),
        ("assets/hard_negative_fire_rate.png", "False alarm rates on default-only runs (harmless re-reads / near-duplicates)."),
        ("assets/calibration_stuck_doomed.png", "Reliability diagrams checking Decisions API probability calibration for stuck and doomed."),
        ("assets/open_vs_closed_fire_rates.png", "Open-weight vs. closed stopping rates conditioned on final run outcome."),
    ]
    any_vis = False
    for rel_p, cap in visual_captions:
        if (ROOT_DIR / rel_p).exists():
            lines.append(f"- [`{rel_p}`]({rel_p}): {cap}")
            any_vis = True
    if not any_vis:
        lines.append("- _(Full Decision 1 charts pending `swe` / `tb` live API phases; example pilot window chart generated if pilot cache exists.)_")

    lines.extend(
        [
            "\n## Surprises and Limitations\n",
            "- `gemini-decision-1` currently offers no batch API or context caching, so every overlapping 12-step sliding window is billed at full input token count.",
            "- Terminal-Bench `trial_id` is not unique across `terminus-2` parquet rows (only 3,204 unique `trial_id`s across 15,941 rows); all internal joins use unique `(parquet_file_idx, row_idx)` identifiers (`tb::{file_idx}::{row_idx}`).",
            "- Production Gemini CLI uses a generative Gemini model for its `LLM_LOOP_CHECK`; here Decision 1 `unproductive_loop` (`noul`) acts as a calibrated stand-in.",
            "\n## Open Questions / Next Steps\n",
            "- Review Pilot measurements and re-projected budgets, then authorize `--cap` for `swe` (`python -m agent_loop_decisions swe --cap ...`) and `tb` (`python -m agent_loop_decisions tb --cap ...`).",
        ]
    )
    notes_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return notes_path


def write_summary_md(
    *,
    data: PreparedStudyData,
    plan_df: pd.DataFrame,
    pilot_summary: dict[str, Any] | None = None,
    analysis_meta: dict[str, Any] | None = None,
    spend_summary: dict[str, Any] | None = None,
    results_dir: Path = RESULTS_DIR,
) -> Path:
    """Write results/summary.md strictly from generated CSV and JSON artifacts."""
    results_dir.mkdir(parents=True, exist_ok=True)
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines: list[str] = [
        "# agent-loop-decisions — Results Summary\n",
        f"_Generated: {now_str}_\n",
        "## 1. Corpus Sanity Checks\n",
    ]
    for label, s in [("SWE-rebench (`swelike`)", data.swe_sanity), ("Terminal-Bench 2.0 (`terminus-2`)", data.tb_sanity)]:
        lines.append(
            f"- **{label}**: `{s['runs']:,}` runs (`{s['models']}` models), pass `{_fmt_pct(s['pass_rate'])}`, "
            f"`{s['strict_runs']}` strict (`{s['strict_open']}` open, `{s['strict_closed']}` closed, `{s['strict_passed']}` passed), "
            f"`{s['default_only_runs']:,}` default-only, `{s['cost_valid_runs']:,}` cost-valid worth `{_fmt_usd(s['total_usd'])}` "
            f"(strict `{_fmt_usd(s['strict_usd'])}`, fail `{_fmt_usd(s['fail_usd'])}`), `gemini_cli` hash `{s['gemini_cli_fires']}` fires."
        )

    lines.extend(
        [
            "\n## 2. Planning Projections (`results/plan.csv`)\n",
            "| Phase | Arm | Stratum | Runs | Points | Prompt Chars | Est Tokens (`chars/3.0`) | Est USD (`chars/3.0`) |",
            "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for _, r in plan_df.iterrows():
        lines.append(
            f"| `{r['phase']}` | `{r['arm']}` | `{r['stratum']}` | {int(r['n_runs']):,} | "
            f"{int(r['n_decision_points']):,} | {int(r['rendered_input_chars']):,} | "
            f"{int(r['est_tokens_chars_div_3_0']):,} | ${float(r['est_usd_chars_div_3_0']):.4f} |"
        )

    if pilot_summary:
        rp_swe = pilot_summary["reprojected_phases"]["swe"]
        rp_tb = pilot_summary["reprojected_phases"]["tb"]
        lines.extend(
            [
                "\n## 3. Pilot Calibration (`results/pilot_report.md`)\n",
                f"- **Calls**: `{pilot_summary['n_points']}` (`{pilot_summary['n_ok']}` ok, `{pilot_summary['n_blocked']}` blocked)",
                f"- **Tokens (`actual` vs `chars/3.0`)**: `{pilot_summary['total_actual_input_tokens']:,}` vs `{pilot_summary['total_est_tokens_3_0']:,}` (`{pilot_summary['measured_chars_per_token_aggregate']:.3f}` chars/token)",
                f"- **Spend**: `${pilot_summary['total_cost_usd']:.6f}`; Latency median `{pilot_summary['median_latency_ms']:.1f} ms` (P95 `{pilot_summary['p95_latency_ms']:.1f} ms`)",
                f"- **Re-projected Phase Spend**: `swe` `${rp_swe['projected_usd_at_measured_cpt']:.3f}`, `tb` `${rp_tb['projected_usd_at_measured_cpt']:.3f}`",
            ]
        )

    for corpus in ("swe", "tb"):
        rpath = results_dir / f"rules_baseline_{corpus}.csv"
        if not rpath.exists():
            continue
        rdf = pd.read_csv(rpath)
        sub = rdf[rdf["scope"] == "full_corpus"]
        if sub.empty:
            continue
        lines.extend(
            [
                f"\n## Free Rule Baselines (`{corpus}` full corpus)\n",
                "| Stopper | Fire Rate | FSR (Passed) | Success Loss (pp) | Net Fail Saved (%) | Tail Cut (%) | Matched Rand Saved (%) |",
                "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
            ]
        )
        for _, r in sub.iterrows():
            lines.append(
                f"| `{r['stopper']}` | {_fmt_pct(r['firing_rate'])} | {_fmt_pct(r['fsr_passed'])} | "
                f"{r['success_loss_pp']:.2f} | {_fmt_pct(r['net_saved_failed_pct'])} | "
                f"{_fmt_pct(r['wasted_tail_cut_pct'])} | {_fmt_pct(r['rand_saved_failed_pct_mean'])} |"
            )

    for corpus in (analysis_meta or {}).get("corpora_analyzed", []):
        opath = results_dir / f"outcome_curve_{corpus}.csv"
        if opath.exists():
            odf = pd.read_csv(opath)
            lines.extend(
                [
                    f"\n## Decision 1 vs. Rules (`{corpus}` Sample A weighted)\n",
                    "| Stopper | FSR (Passed) | Net Fail Saved (%) | Gross Fail Saved (%) | API Cost / Run | Matched Rand Saved (%) |",
                    "| --- | ---: | ---: | ---: | ---: | ---: |",
                ]
            )
            key_mask = (
                (odf["kind"] == "rule")
                | (odf["kind"] == "gemini_cli_arm")
                | (odf["threshold"].isin([0.5, 0.8, 0.9, 0.98]))
            )
            for _, r in odf[key_mask].iterrows():
                lines.append(
                    f"| `{r['stopper']}` | {_fmt_pct(r['fsr_passed'])} | {_fmt_pct(r['net_saved_failed_pct'])} | "
                    f"{_fmt_pct(r['gross_saved_failed_pct'])} | ${r['api_cost_per_run_usd']:.5f} | {_fmt_pct(r['rand_saved_failed_pct_mean'])} |"
                )

        cal_path = results_dir / f"task_split_calibration_{corpus}.csv"
        if cal_path.exists():
            cdf = pd.read_csv(cal_path)
            lines.extend(
                [
                    f"\n### Task-Split Calibrated Held-Out Operating Points (`{corpus}`)\n",
                    "| Family | Target FSR | Selected Threshold | Train FSR | Test FSR (Passed) | Test Net Fail Saved (%) | Test Random Saved (%) |",
                    "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
                ]
            )
            for _, r in cdf.iterrows():
                tau_s = f"{r['chosen_tau']:.2f}" if pd.notna(r["chosen_tau"]) else "n/a"
                lines.append(
                    f"| `{r['family']}` | {_fmt_pct(r['target_fsr'], 0)} | {tau_s} | {_fmt_pct(r['dev_fsr_passed'])} | "
                    f"{_fmt_pct(r['eval_fsr_passed'])} | {_fmt_pct(r['eval_net_saved_failed_pct'])} | {_fmt_pct(r['eval_rand_saved_failed_pct_mean'])} |"
                )

    cal_json = results_dir / "calibration_summary.json"
    if cal_json.exists():
        csum = orjson.loads(cal_json.read_bytes())
        lines.extend(
            [
                "\n## Probability Calibration Summary (`results/calibration_summary.json`)\n",
                f"- **Evaluated grid points**: `{csum['n_points']:,}`",
                f"- **`stuck`**: Brier `{csum['stuck']['brier']:.4f}`, ECE `{csum['stuck']['ece']:.4f}`, ROC-AUC `{csum['stuck']['roc_auc']:.4f}`",
                f"- **`doomed`**: Brier `{csum['doomed']['brier']:.4f}`, ECE `{csum['doomed']['ece']:.4f}`, ROC-AUC `{csum['doomed']['roc_auc']:.4f}`",
            ]
        )

    if spend_summary:
        lines.extend(
            [
                "\n## Spend Tracking (`cache/spend.json`)\n",
                f"- **Total Spent**: `${spend_summary.get('total_spent_usd', 0.0):.6f}` / `${spend_summary.get('global_cap_usd', 10.0):.2f}` global cap (`{spend_summary.get('total_calls', 0):,}` calls, `{spend_summary.get('total_blocked', 0)}` blocked)",
            ]
        )

    out_path = results_dir / "summary.md"
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_path

