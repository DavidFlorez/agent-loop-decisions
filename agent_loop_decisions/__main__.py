"""CLI entrypoint for agent-loop-decisions (plan, pilot, swe, tb, analyze)."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
import sys
import threading
import time
from typing import Any, Sequence

import orjson

from agent_loop_decisions.client import (
    CapExceededError,
    DecisionCache,
    DecisionCallResult,
    DecisionsBackend,
    GuardedDecisionClient,
    SpendTracker,
    compute_cache_key,
)
from agent_loop_decisions.config import (
    ASSETS_DIR,
    AUDIT_DIR,
    CACHE_DIR,
    DATA_DIR,
    DECISION_MODEL,
    DEFAULT_GLOBAL_CAP_USD,
    DEFAULT_MAX_RPM,
    DEFAULT_MAX_TPM,
    DEFAULT_PHASE_CAPS_USD,
    MAIN_QUESTIONS,
    MAIN_WINDOW_STEPS,
    RESULTS_DIR,
    ROOT_DIR,
)
from agent_loop_decisions.data import (
    PilotPoint,
    PreparedStudyData,
    RunMetadata,
    SelectedTrajectory,
    load_or_build_study_data,
)
from agent_loop_decisions.render import render_decision_window, slice_prefix_steps
from agent_loop_decisions.report import (
    generate_example_window_chart,
    run_full_analysis,
    update_notes_md,
    write_pilot_report,
    write_plan_outputs,
    write_summary_md,
)
from agent_loop_decisions.stoppers import (
    GeminiCliRunResult,
    GridPointRecord,
    evaluate_gemini_cli_arm,
    evaluate_main_grid_run,
    has_active_strict_loop_in_window,
)


class _UncachedDecisionError(RuntimeError):
    """Raised during read-only analyze replay when a decision point is missing from cache."""


class _CacheOnlyClient:
    """Read-only DecisionCache adapter for analyze mode (never invokes API or mutates spend.json)."""

    def __init__(self, cache_dir: Path, model: str = DECISION_MODEL) -> None:
        self.model = model
        self.cache = DecisionCache(cache_dir=cache_dir)

    def decide(
        self,
        *,
        shared_text: str,
        questions: dict[str, dict[str, Any]],
    ) -> DecisionCallResult:
        input_payload = [{"type": "text", "text": shared_text}]
        key = compute_cache_key(model=self.model, input_payload=input_payload, questions=questions)
        cached = self.cache.get(key)
        if cached is None:
            raise _UncachedDecisionError("Decision point missing from local cache during analyze.")
        return DecisionCallResult(
            cache_key=key,
            status=str(cached.get("status", "ok")),
            answers=cached.get("answers"),
            total_input_tokens=int((cached.get("usage") or {}).get("total_input_tokens", 0)),
            cost_usd=float(cached.get("cost_usd", 0.0)),
            est_chars=int(cached.get("est_chars", 0)),
            est_tokens=float(cached.get("est_tokens", 0.0)),
            latency_ms=float(cached.get("latency_ms", 0.0)),
            cached=True,
            block_code=cached.get("block_code"),
            block_message=cached.get("block_message"),
        )


@dataclass(slots=True)
class RuntimePaths:
    """Filesystem paths for CLI execution (overridable in tests)."""

    data_dir: Path = DATA_DIR
    cache_dir: Path = CACHE_DIR
    results_dir: Path = RESULTS_DIR
    assets_dir: Path = ASSETS_DIR
    audit_dir: Path = AUDIT_DIR
    notes_path: Path = ROOT_DIR / "NOTES.md"


def _ordered_sampled_runs(
    runs: Sequence[RunMetadata],
    assigns: dict[str, dict[str, Any]],
) -> list[RunMetadata]:
    """Order sampled runs cleanly with Sample A first, then Sample B enrichment runs."""
    sample_a = [r for r in runs if assigns.get(r.uid, {}).get("in_sample_a")]
    sample_b_only = [
        r for r in runs if (r.uid in assigns) and not assigns[r.uid].get("in_sample_a")
    ]
    return sample_a + sample_b_only


def _evaluate_pilot_points(
    data: PreparedStudyData,
    client: GuardedDecisionClient,
) -> list[tuple[PilotPoint, GridPointRecord, str]]:
    """Query or read cached Decision 1 answers for all 50 pilot decision points."""
    swe_by_uid = {r.uid: r for r in data.swe_runs}
    records: list[tuple[PilotPoint, GridPointRecord, str]] = []
    for pp in data.pilot_points:
        r = swe_by_uid[pp.uid]
        tr = data.selected_swe[pp.uid]
        prefix = slice_prefix_steps(tr.steps, pp.step_idx)
        prompt_text = render_decision_window(
            tr.task_text,
            prefix,
            step_idx=pp.step_idx,
            window_steps=MAIN_WINDOW_STEPS,
        )
        res = client.decide(shared_text=prompt_text, questions=MAIN_QUESTIONS)
        pt = GridPointRecord(
            uid=r.uid,
            corpus=r.corpus,
            step_idx=pp.step_idx,
            status=res.status,
            p_stuck=res.answers["stuck"]["noul"] if res.status == "ok" and res.answers else None,
            p_progress=res.answers["progress"]["noul"] if res.status == "ok" and res.answers else None,
            p_doomed=res.answers["doomed"]["noul"] if res.status == "ok" and res.answers else None,
            total_input_tokens=res.total_input_tokens,
            cost_usd=res.cost_usd,
            est_chars=res.est_chars,
            est_tokens=res.est_tokens,
            latency_ms=res.latency_ms,
            cached=res.cached,
            block_code=res.block_code,
            active_strict_in_window=has_active_strict_loop_in_window(
                tr.strict_loop_flags, step_idx=pp.step_idx, window_steps=MAIN_WINDOW_STEPS
            ),
        )
        records.append((pp, pt, prompt_text))
    return records


def _render_best_example_chart(
    data: PreparedStudyData,
    pilot_records: Sequence[tuple[PilotPoint, GridPointRecord, str]],
    out_path: Path,
) -> Path | None:
    """Render an illustrative Decision 1 trajectory window from pilot records."""
    swe_by_uid = {r.uid: r for r in data.swe_runs}
    ok_recs = [(pp, pt, txt) for pp, pt, txt in pilot_records if pt.status == "ok"]
    if not ok_recs:
        return None
    strict_ok = [x for x in ok_recs if x[0].stratum == "strict_onset" and x[1].active_strict_in_window]
    pool = strict_ok or [x for x in ok_recs if x[0].stratum == "strict_onset"] or ok_recs
    pp, pt, prompt_text = max(pool, key=lambda x: float(x[1].p_stuck or 0.0))
    r = swe_by_uid[pp.uid]
    title_ctx = (
        f"Pilot Example ({pp.stratum}): {r.model} | step={pp.step_idx} "
        f"(strict onset={r.strict_onset_step}, succeeded={r.succeeded})"
    )
    return generate_example_window_chart(
        point=pt,
        prompt_text=prompt_text,
        title_context=title_ctx,
        out_path=out_path,
    )


def _read_spend_if_exists(
    cache_dir: Path, global_cap_usd: float = DEFAULT_GLOBAL_CAP_USD
) -> dict[str, Any] | None:
    spend_path = cache_dir / "spend.json"
    if not spend_path.exists():
        return None
    state = dict(SpendTracker(cache_dir=cache_dir).state)
    state["global_cap_usd"] = global_cap_usd
    return state


def _reconstruct_corpus_from_cache(
    runs: Sequence[RunMetadata],
    assigns: dict[str, dict[str, Any]],
    traces: dict[str, SelectedTrajectory],
    ro_client: Any,
) -> tuple[dict[str, list[GridPointRecord]], dict[str, GeminiCliRunResult]]:
    """Read complete sampled corpus results strictly from cache; return empty dicts if uncached."""
    ordered = _ordered_sampled_runs(runs, assigns)
    grid_map: dict[str, list[GridPointRecord]] = {}
    try:
        for r in ordered:
            grid_map[r.uid] = evaluate_main_grid_run(
                r, traces[r.uid], ro_client, window_steps=MAIN_WINDOW_STEPS
            )
    except _UncachedDecisionError:
        return {}, {}

    gcli_map: dict[str, GeminiCliRunResult] = {}
    try:
        for r in ordered:
            gcli_map[r.uid] = evaluate_gemini_cli_arm(r, traces[r.uid], ro_client)
    except _UncachedDecisionError:
        gcli_map = {}

    return grid_map, gcli_map


def _run_analyze_phase(
    data: PreparedStudyData,
    plan_df: Any,
    paths: RuntimePaths,
) -> dict[str, Any]:
    """Recompute reports, charts, audits, summary.md, and NOTES.md strictly from local cache."""
    has_cache = (paths.cache_dir / "decisions.jsonl").exists() or (
        paths.cache_dir / "decisions.jsonl.gz"
    ).exists()
    ro_client = _CacheOnlyClient(cache_dir=paths.cache_dir) if has_cache else None

    pilot_summary: dict[str, Any] | None = None
    if ro_client is not None:
        try:
            pilot_records = _evaluate_pilot_points(data, ro_client)
            pilot_summary = write_pilot_report(
                data=data,
                pilot_records=pilot_records,
                plan_df=plan_df,
                results_dir=paths.results_dir,
            )
            _render_best_example_chart(
                data, pilot_records, paths.assets_dir / "example_window.png"
            )
        except _UncachedDecisionError:
            p_json = paths.results_dir / "pilot_summary.json"
            if p_json.exists():
                pilot_summary = orjson.loads(p_json.read_bytes())

    swe_grid, swe_gcli = (
        _reconstruct_corpus_from_cache(
            data.swe_runs, data.swe_assign, data.selected_swe, ro_client
        )
        if ro_client is not None
        else ({}, {})
    )
    tb_grid, tb_gcli = (
        _reconstruct_corpus_from_cache(
            data.tb_runs, data.tb_assign, data.selected_tb, ro_client
        )
        if ro_client is not None
        else ({}, {})
    )

    analysis_meta = run_full_analysis(
        data=data,
        swe_grid=swe_grid,
        swe_gcli=swe_gcli,
        tb_grid=tb_grid,
        tb_gcli=tb_gcli,
        results_dir=paths.results_dir,
        assets_dir=paths.assets_dir,
        audit_dir=paths.audit_dir,
    )
    spend_summary = _read_spend_if_exists(paths.cache_dir)
    write_summary_md(
        data=data,
        plan_df=plan_df,
        pilot_summary=pilot_summary,
        analysis_meta=analysis_meta,
        spend_summary=spend_summary,
        results_dir=paths.results_dir,
    )
    update_notes_md(
        data=data,
        plan_df=plan_df,
        pilot_summary=pilot_summary,
        analysis_meta=analysis_meta,
        spend_summary=spend_summary,
        notes_path=paths.notes_path,
    )
    return {
        "pilot_summary": pilot_summary,
        "analysis_meta": analysis_meta,
        "spend_summary": spend_summary,
    }


def _print_plan_summary(data: PreparedStudyData, plan_df: Any) -> None:
    for label, s in [("SWE swelike", data.swe_sanity), ("TB terminus-2", data.tb_sanity)]:
        print(
            f"[SANITY] {label}: runs={s['runs']} models={s['models']} "
            f"pass={s['passed']} ({s['pass_rate']:.4f}) strict={s['strict_runs']} "
            f"(open={s['strict_open']}, closed={s['strict_closed']}, pass={s['strict_passed']}) "
            f"default_only={s['default_only_runs']} cost_valid={s['cost_valid_runs']} "
            f"total_usd=${s['total_usd']:.2f} strict_usd=${s['strict_usd']:.2f} "
            f"fail_usd=${s['fail_usd']:.2f} gemini_cli={s['gemini_cli_fires']}"
        )
    print("\n[PLAN PROJECTIONS]")
    cols = [
        "phase",
        "arm",
        "stratum",
        "n_runs",
        "n_decision_points",
        "est_tokens_chars_div_3_0",
        "est_usd_chars_div_3_0",
    ]
    print(plan_df[cols].to_string(index=False))


def _run_corpus_phase(
    *,
    corpus: str,
    data: PreparedStudyData,
    plan_df: Any,
    args: argparse.Namespace,
    paths: RuntimePaths,
    backend: DecisionsBackend | None,
) -> int:
    runs = data.swe_runs if corpus == "swe" else data.tb_runs
    assigns = data.swe_assign if corpus == "swe" else data.tb_assign
    traces = data.selected_swe if corpus == "swe" else data.selected_tb
    ordered = _ordered_sampled_runs(runs, assigns)

    client = GuardedDecisionClient(
        phase=corpus,
        cap_usd=float(args.cap),
        global_cap_usd=float(args.global_cap),
        cache_dir=paths.cache_dir,
        max_rpm=int(args.rpm),
        max_tpm=int(args.tpm),
        max_retries=int(args.max_retries),
        backend=backend,
    )
    workers = max(1, int(getattr(args, "workers", 1)))
    t_start = time.monotonic()
    try:
        if workers == 1:
            for idx, r in enumerate(ordered, start=1):
                evaluate_main_grid_run(r, traces[r.uid], client, window_steps=MAIN_WINDOW_STEPS)
                evaluate_gemini_cli_arm(r, traces[r.uid], client)
                if idx % 50 == 0 or idx == len(ordered):
                    s = client.summary()
                    print(
                        f"[{corpus.upper()} {idx}/{len(ordered)} runs] "
                        f"calls={s['session_api_calls']} hits={s['session_cache_hits']} "
                        f"spent=${s['phase_spent_usd']:.4f} elapsed={time.monotonic() - t_start:.1f}s",
                        flush=True,
                    )
        else:
            tasks = [
                (r, arm)
                for r in sorted(ordered, key=lambda x: x.n_steps, reverse=True)
                for arm in ("grid", "gcli")
            ]
            stop_event = threading.Event()

            def _run_task(r: RunMetadata, arm: str) -> None:
                if stop_event.is_set():
                    return
                tr = traces[r.uid]
                if arm == "grid":
                    evaluate_main_grid_run(r, tr, client, window_steps=MAIN_WINDOW_STEPS)
                else:
                    evaluate_gemini_cli_arm(r, tr, client)

            with ThreadPoolExecutor(max_workers=workers) as pool:
                futs = {pool.submit(_run_task, r, arm): (r.uid, arm) for r, arm in tasks}
                done_cnt = 0
                try:
                    for fut in as_completed(futs):
                        fut.result()
                        done_cnt += 1
                        if done_cnt % 100 == 0 or done_cnt == len(futs):
                            s = client.summary()
                            print(
                                f"[{corpus.upper()} {done_cnt}/{len(futs)} tasks] "
                                f"calls={s['session_api_calls']} hits={s['session_cache_hits']} "
                                f"spent=${s['phase_spent_usd']:.4f} elapsed={time.monotonic() - t_start:.1f}s",
                                flush=True,
                            )
                except BaseException:
                    stop_event.set()
                    for pending in futs:
                        pending.cancel()
                    raise
    except CapExceededError as exc:
        client.flush()
        print(f"[CAP EXCEEDED] {exc}", file=sys.stderr)
        print(orjson.dumps(exc.summary, option=orjson.OPT_INDENT_2).decode("utf-8"), file=sys.stderr)
        return 2

    client.flush()
    out = _run_analyze_phase(data, plan_df, paths)
    print(
        f"[{corpus.upper()} DONE] spend={orjson.dumps(client.summary()).decode('utf-8')} "
        f"analyzed={out['analysis_meta']['corpora_analyzed']}"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-loop-decisions",
        description="Offline replay of Gemini Decision 1 vs. rule-based loop stoppers.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def _add_common(p: argparse.ArgumentParser, *, default_cap: float | None = None) -> None:
        p.add_argument("--data-dir", type=Path, default=DATA_DIR)
        p.add_argument("--cache-dir", type=Path, default=CACHE_DIR)
        p.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
        p.add_argument("--assets-dir", type=Path, default=ASSETS_DIR)
        p.add_argument("--audit-dir", type=Path, default=AUDIT_DIR)
        p.add_argument("--notes-path", type=Path, default=ROOT_DIR / "NOTES.md")
        p.add_argument("--rebuild-cache", action="store_true")
        if default_cap is not None:
            p.add_argument("--cap", type=float, default=default_cap)
            p.add_argument("--global-cap", type=float, default=DEFAULT_GLOBAL_CAP_USD)
            p.add_argument("--rpm", type=int, default=DEFAULT_MAX_RPM)
            p.add_argument("--tpm", type=int, default=DEFAULT_MAX_TPM)
            p.add_argument("--max-retries", type=int, default=5)
            p.add_argument("--workers", type=int, default=1)

    _add_common(sub.add_parser("plan", help="Verify corpus sanity and project costs (0 API calls)."))
    _add_common(sub.add_parser("pilot", help="Run 50-point pilot phase."), default_cap=DEFAULT_PHASE_CAPS_USD["pilot"])
    _add_common(sub.add_parser("swe", help="Run SWE-rebench Decision 1 replay."), default_cap=DEFAULT_PHASE_CAPS_USD["swe"])
    _add_common(sub.add_parser("tb", help="Run Terminal-Bench 2.0 Decision 1 replay."), default_cap=DEFAULT_PHASE_CAPS_USD["tb"])
    _add_common(sub.add_parser("analyze", help="Compute metrics, charts, audit, and summary from cache (0 API calls)."))
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    backend: DecisionsBackend | None = None,
    paths_override: RuntimePaths | None = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    paths = paths_override or RuntimePaths(
        data_dir=args.data_dir,
        cache_dir=args.cache_dir,
        results_dir=args.results_dir,
        assets_dir=args.assets_dir,
        audit_dir=args.audit_dir,
        notes_path=args.notes_path,
    )

    data = load_or_build_study_data(
        data_dir=paths.data_dir,
        force_rebuild=bool(args.rebuild_cache),
    )
    plan_df, _, _ = write_plan_outputs(data, results_dir=paths.results_dir)

    if args.command == "plan":
        spend_summary = _read_spend_if_exists(paths.cache_dir)
        write_summary_md(
            data=data,
            plan_df=plan_df,
            spend_summary=spend_summary,
            results_dir=paths.results_dir,
        )
        update_notes_md(
            data=data,
            plan_df=plan_df,
            spend_summary=spend_summary,
            notes_path=paths.notes_path,
        )
        _print_plan_summary(data, plan_df)
        return 0

    if args.command == "pilot":
        client = GuardedDecisionClient(
            phase="pilot",
            cap_usd=float(args.cap),
            global_cap_usd=float(args.global_cap),
            cache_dir=paths.cache_dir,
            max_rpm=int(args.rpm),
            max_tpm=int(args.tpm),
            max_retries=int(args.max_retries),
            backend=backend,
        )
        try:
            pilot_records = _evaluate_pilot_points(data, client)
        except CapExceededError as exc:
            client.flush()
            print(f"[CAP EXCEEDED] {exc}", file=sys.stderr)
            print(orjson.dumps(exc.summary, option=orjson.OPT_INDENT_2).decode("utf-8"), file=sys.stderr)
            return 2

        client.flush()
        pilot_summary = write_pilot_report(
            data=data,
            pilot_records=pilot_records,
            plan_df=plan_df,
            results_dir=paths.results_dir,
        )
        _render_best_example_chart(
            data, pilot_records, paths.assets_dir / "example_window.png"
        )
        spend_summary = _read_spend_if_exists(
            paths.cache_dir, global_cap_usd=float(args.global_cap)
        )
        write_summary_md(
            data=data,
            plan_df=plan_df,
            pilot_summary=pilot_summary,
            spend_summary=spend_summary,
            results_dir=paths.results_dir,
        )
        update_notes_md(
            data=data,
            plan_df=plan_df,
            pilot_summary=pilot_summary,
            spend_summary=spend_summary,
            notes_path=paths.notes_path,
        )
        rp_swe = pilot_summary["reprojected_phases"]["swe"]
        rp_tb = pilot_summary["reprojected_phases"]["tb"]
        print(
            f"[PILOT DONE] points={pilot_summary['n_points']} ok={pilot_summary['n_ok']} "
            f"blocked={pilot_summary['n_blocked']} actual_tok={pilot_summary['total_actual_input_tokens']} "
            f"est_tok_3_0={pilot_summary['total_est_tokens_3_0']} "
            f"chars_per_token={pilot_summary['measured_chars_per_token_aggregate']:.3f} "
            f"cost=${pilot_summary['total_cost_usd']:.6f} "
            f"median_ms={pilot_summary['median_latency_ms']:.1f} "
            f"p95_ms={pilot_summary['p95_latency_ms']:.1f} "
            f"reproj_swe=${rp_swe['projected_usd_at_measured_cpt']:.3f} "
            f"reproj_tb=${rp_tb['projected_usd_at_measured_cpt']:.3f}"
        )
        return 0

    if args.command in ("swe", "tb"):
        return _run_corpus_phase(
            corpus=args.command,
            data=data,
            plan_df=plan_df,
            args=args,
            paths=paths,
            backend=backend,
        )

    if args.command == "analyze":
        out = _run_analyze_phase(data, plan_df, paths)
        print(
            f"[ANALYZE DONE] corpora={out['analysis_meta']['corpora_analyzed']} "
            f"pilot_cached={out['pilot_summary'] is not None}"
        )
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
