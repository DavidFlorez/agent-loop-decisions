"""Pipeline tests: causal non-leakage, stopper monotonicity, dollars-saved parity, Gemini CLI interval, and smoke replay."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import orjson
import pytest

from agent_loops.canonicalize import NormalizedStep
from agent_loop_decisions.__main__ import RuntimePaths, main as cli_main
from agent_loop_decisions.client import GuardedDecisionClient
from agent_loop_decisions.config import MAIN_QUESTIONS, THRESHOLDS
from agent_loop_decisions.data import (
    PilotPoint,
    PreparedStudyData,
    RunMetadata,
    SelectedTrajectory,
)
from agent_loop_decisions.metrics import (
    evaluate_outcome_stopper,
    evaluate_rule_fixture_parity,
)
from agent_loop_decisions.render import render_decision_window, slice_prefix_steps
from agent_loop_decisions.stoppers import (
    GRID_STOPPER_FAMILIES,
    GridPointRecord,
    evaluate_gemini_cli_arm,
    evaluate_main_grid_run,
    score_grid_stopper,
    update_gemini_cli_interval,
)


def _make_step(
    step_idx: int,
    *,
    tool_name: str = "bash",
    canonical_call: str | None = None,
    obs_text: str | None = None,
    assistant_text: str | None = None,
    is_error: bool = False,
    error_sig: str | None = None,
    file_op: str | None = None,
    file_path: str | None = None,
) -> NormalizedStep:
    call = canonical_call or f'{tool_name}(cmd="echo step_{step_idx}")'
    obs = obs_text if obs_text is not None else f"output for step {step_idx}"
    asst = assistant_text if assistant_text is not None else f"thinking step {step_idx}"
    return NormalizedStep(
        step_idx=step_idx,
        has_tool=True,
        tool_name=tool_name,
        canonical_call=call,
        call_hash=f"ch_{step_idx}",
        call_tokens=("bash", f"step_{step_idx}"),
        args_known=True,
        obs_text=obs,
        obs_hash=f"oh_{step_idx}",
        obs_norm_hash=f"onh_{step_idx}",
        obs_known=True,
        is_error=is_error,
        error_sig=error_sig,
        file_op=file_op,
        file_path=file_path,
        file_range=None,
        assistant_text=asst,
        msg_chars=len(asst) + len(call),
        obs_chars=len(obs),
        read_range_exact=True,
    )


def test_causal_non_leakage() -> None:
    steps = []
    for i in range(18):
        if i > 10:
            step = _make_step(
                i,
                tool_name="SECRET_FUTURE_TOOL_999",
                canonical_call=f"FUTURE_CALL_MARKER_{i}",
                obs_text=f"FUTURE_OBS_MARKER_{i}",
                assistant_text=f"FUTURE_THOUGHT_OUTCOME_PASS_{i}",
            )
        else:
            step = _make_step(i)
        steps.append(step)

    prefix = slice_prefix_steps(steps, 10)
    assert [s.step_idx for s in prefix] == list(range(11))
    assert all(s.step_idx <= 10 for s in prefix)

    rendered = render_decision_window(
        "Fix bug #42 in parser.py",
        prefix,
        step_idx=10,
        window_steps=12,
    )
    # No future step strings
    assert "SECRET_FUTURE_TOOL_999" not in rendered
    assert "FUTURE_CALL_MARKER" not in rendered
    assert "FUTURE_OBS_MARKER" not in rendered
    assert "FUTURE_THOUGHT_OUTCOME_PASS" not in rendered
    # No run-level leakage (total run length 18, model names, harness, rule labels)
    assert "18" not in rendered
    assert "model=" not in rendered.lower()
    assert "harness" not in rendered.lower()
    assert "succeeded" not in rendered.lower()
    assert "strict_loop" not in rendered.lower()
    assert "cline_style" not in rendered.lower()
    assert "[step 10]" in rendered
    assert "[step 0]" in rendered

    # Window truncation limits to last W steps ending at step_idx
    pref14 = slice_prefix_steps(steps[:11] + [_make_step(k) for k in range(11, 15)], 14)
    win4 = render_decision_window("Fix bug #42", pref14, step_idx=14, window_steps=4)
    assert "[step 11]" in win4
    assert "[step 14]" in win4
    assert "[step 10]" not in win4


def test_grid_stopper_monotonicity_in_threshold() -> None:
    points = [
        GridPointRecord("u1", "swe", 4, "ok", 0.52, 0.45, 0.40, 100, 0.00001, 300, 100.0, 20.0, False, None, False),
        GridPointRecord("u1", "swe", 8, "ok", 0.72, 0.25, 0.65, 110, 0.000011, 330, 110.0, 20.0, False, None, True),
        # Blocked gap at step 12 must reset d1_stuck2 consecutive state and never fire
        GridPointRecord("u1", "swe", 12, "blocked", None, None, None, 0, 0.0, 320, 106.7, 15.0, False, "PROHIBITED_CONTENT", True),
        GridPointRecord("u1", "swe", 16, "ok", 0.85, 0.15, 0.82, 120, 0.000012, 360, 120.0, 20.0, False, None, True),
        GridPointRecord("u1", "swe", 20, "ok", 0.91, 0.04, 0.96, 130, 0.000013, 390, 130.0, 20.0, False, None, True),
        GridPointRecord("u1", "swe", 24, "ok", 0.99, 0.01, 0.99, 140, 0.000014, 420, 140.0, 20.0, False, None, True),
    ]

    # Verify d1_stuck2 reset across blocked gap: step 8 & 16 are NOT consecutive grid stride (diff 8 != 4)
    assert score_grid_stopper(points, family="d1_stuck2", threshold=0.70) == (20, pytest.approx(0.000046))
    # Blocked point never triggers even at 0.50; if all ok removed before 12:
    blk_only = [GridPointRecord("u2", "swe", 4, "blocked", None, None, None, 0, 0.0, 300, 100.0, 10.0, False, "SAFETY", True)]
    for fam in GRID_STOPPER_FAMILIES:
        assert score_grid_stopper(blk_only, family=fam, threshold=0.50)[0] is None

    for fam in GRID_STOPPER_FAMILIES:
        stops_by_tau = [score_grid_stopper(points, family=fam, threshold=tau) for tau in THRESHOLDS]
        for i in range(len(THRESHOLDS) - 1):
            s_lo, cost_lo = stops_by_tau[i]
            s_hi, cost_hi = stops_by_tau[i + 1]
            if s_hi is not None:
                assert s_lo is not None
                assert s_lo <= s_hi
                assert cost_lo <= cost_hi + 1e-12


def _make_run_meta(
    uid: str,
    *,
    harness: str,
    model: str,
    is_open: bool,
    task_name: str,
    succeeded: bool,
    n_steps: int,
    step_costs: list[float],
    cost_included: bool = True,
    stop_steps: dict[str, int | None] | None = None,
    has_strict_loop: bool = False,
    default_only: bool = False,
    strict_onset_step: int | None = None,
    last_productive_step: int = 2,
) -> RunMetadata:
    tot_cost = float(sum(step_costs)) if cost_included else 0.0
    wasted_tail = 0.0
    if (not succeeded) and cost_included and (last_productive_step + 1 < len(step_costs)):
        wasted_tail = float(sum(step_costs[last_productive_step + 1 :]))
    return RunMetadata(
        uid=uid,
        corpus="swe",
        run_id=uid,
        harness=harness,
        model=model,
        is_open_weight=is_open,
        task_name=task_name,
        succeeded=succeeded,
        n_steps=n_steps,
        n_tool_steps=n_steps,
        has_any_loop=has_strict_loop or default_only,
        has_strict_loop=has_strict_loop,
        default_only=default_only,
        strict_onset_step=strict_onset_step,
        default_onset_step=strict_onset_step,
        last_productive_step=last_productive_step,
        wasted_tail_usd=wasted_tail,
        cost_included=cost_included,
        total_cost_usd=tot_cost,
        loop_cost_usd=0.0,
        step_costs=step_costs,
        stop_steps=stop_steps or {},
        source_file="fixture.parquet",
        source_row=0,
    )


def test_dollars_saved_accounting_parity() -> None:
    runs = [
        _make_run_meta(
            "r1",
            harness="hA",
            model="m_closed",
            is_open=False,
            task_name="t1",
            succeeded=True,
            n_steps=10,
            step_costs=[0.1] * 10,
            stop_steps={"cline_style": 5},  # false stop on passed run!
        ),
        _make_run_meta(
            "r2",
            harness="hA",
            model="m_closed",
            is_open=False,
            task_name="t2",
            succeeded=False,
            n_steps=10,
            step_costs=[0.2] * 10,
            stop_steps={"cline_style": 4},  # saves 5 * 0.2 = 1.0 USD on failed run
            has_strict_loop=True,
            strict_onset_step=4,
            last_productive_step=2,
        ),
        _make_run_meta(
            "r3",
            harness="hA",
            model="m_open",
            is_open=True,
            task_name="t3",
            succeeded=False,
            n_steps=12,
            step_costs=[0.5] * 12,
            stop_steps={"cline_style": None},
        ),
        _make_run_meta(
            "r4",
            harness="hB",
            model="m_open",
            is_open=True,
            task_name="t4",
            succeeded=True,
            n_steps=12,
            step_costs=[0.3] * 12,
            stop_steps={"cline_style": None},
        ),
        _make_run_meta(
            "r5",
            harness="hB",
            model="m_open",
            is_open=True,
            task_name="t5",
            succeeded=False,
            n_steps=12,
            step_costs=[0.15] * 12,
            cost_included=False,
            stop_steps={"cline_style": 6},
        ),
    ]

    parity = evaluate_rule_fixture_parity(runs, "cline_style", n_seeds=200, seed=42)
    ours = evaluate_outcome_stopper(
        stopper_name="cline_style",
        runs=runs,
        weights=[1.0] * len(runs),
        stop_steps=[r.stop_steps.get("cline_style") for r in runs],
        api_costs_usd=[0.01] * len(runs),
        n_rand_seeds=200,
        n_boot=20,
        seed=42,
    )

    # Exact parity with agent_loops.detectors.evaluate_detector_with_matched_random
    assert ours["firing_rate"] == pytest.approx(parity["firing_rate"], abs=1e-12)
    assert ours["fsr_fired"] == pytest.approx(parity["false_stop_rate"], abs=1e-12)
    assert ours["success_loss_pp"] == pytest.approx(parity["success_loss_pp"], abs=1e-12)
    assert ours["eval_cost_usd"] == pytest.approx(parity["total_eval_cost_usd"], abs=1e-12)
    assert ours["gross_saved_all_usd"] == pytest.approx(parity["dollars_saved_usd"], abs=1e-12)
    assert ours["gross_saved_all_pct"] == pytest.approx(parity["dollars_saved_pct"], abs=1e-12)
    assert ours["rand_fsr_fired_mean"] == pytest.approx(parity["rand_false_stop_mean"], abs=1e-12)
    assert ours["rand_saved_all_pct_mean"] == pytest.approx(parity["rand_dollars_saved_pct_mean"], abs=1e-12)

    # Outcome-primary metric verification:
    # Passed runs = {r1, r4} (2 runs), false stops = {r1} -> fsr_passed = 0.5
    assert ours["fsr_passed"] == pytest.approx(0.5, abs=1e-12)
    # Gross failed savings = 1.0 USD (from r2 only; r1 passed -> excluded from failed savings; r5 cost_included=False)
    assert ours["gross_saved_failed_usd"] == pytest.approx(1.0, abs=1e-12)
    # API cost across the 4 cost-valid runs = 4 * 0.01 = 0.04 -> net failed saved = 0.96 USD
    assert ours["net_saved_failed_usd"] == pytest.approx(0.96, abs=1e-12)


class _FakeBlockError(Exception):
    def __init__(self, block_code: str = "PROHIBITED_CONTENT", msg: str = "blocked in test") -> None:
        super().__init__(msg)
        self.code = 400
        self.details = {"error": {"code": block_code, "message": msg}}
        self.message = msg


class _AdaptiveCliBackend:
    """Mock backend returning step-indexed p_loop values for Gemini CLI adaptive interval tests."""

    def __init__(self, probs_by_step: dict[int, float | str]) -> None:
        self.probs_by_step = probs_by_step
        self.checked_steps: list[int] = []

    def create(
        self,
        *,
        model: str,
        input: list[dict[str, Any]],
        questions: dict[str, dict[str, Any]],
    ) -> Any:
        del model
        txt = input[0]["text"]
        # Parse final "[step N]" line rendered by render_decision_window
        step_idx = -1
        for line in reversed(txt.splitlines()):
            s = line.strip()
            if s.startswith("[step ") and s.endswith("]"):
                step_idx = int(s[6:-1])
                break
        self.checked_steps.append(step_idx)
        val = self.probs_by_step[step_idx]
        if val == "BLOCKED":
            raise _FakeBlockError("PROHIBITED_CONTENT", "blocked in test")
        p = float(val)
        assert "unproductive_loop" in questions
        return SimpleNamespace(
            answers={
                "unproductive_loop": SimpleNamespace(
                    type="noul", noul=p, confidence=max(p, 1.0 - p)
                )
            },
            usage=SimpleNamespace(total_input_tokens=150, total_output_tokens=0),
        )


def test_gemini_cli_adaptive_interval_and_hash_precedence(tmp_path: Path) -> None:
    # Verify adaptive formula clamp(5, 15, Math.round(5 + 10*(1-p)))
    assert update_gemini_cli_interval(0.0) == 15
    assert update_gemini_cli_interval(0.5) == 10
    assert update_gemini_cli_interval(0.75) == 8  # round(7.5) == 8
    assert update_gemini_cli_interval(0.85) == 7  # round(6.5) == 7
    assert update_gemini_cli_interval(1.0) == 5

    steps = [_make_step(i) for i in range(70)]
    trace = SelectedTrajectory("g1", "swe", "Task", steps, [False] * 70, [False] * 70)

    # Sequence of adaptive turns starting at turn 30 (step 29):
    # - turn 30 (step 29): p=0.5 -> interval 10 -> next turn 40 (step 39)
    # - turn 40 (step 39): p=0.0 -> interval 15 -> next turn 55 (step 54)
    # - turn 55 (step 54): BLOCKED (preserves previous interval 15) -> next turn 70 (step 69)
    # - turn 70 (step 69): p=0.92 >= 0.90 -> llm_stop_step=69
    backend = _AdaptiveCliBackend({29: 0.5, 39: 0.0, 54: "BLOCKED", 69: 0.92})
    client = GuardedDecisionClient(
        phase="test_gcli",
        cap_usd=1.0,
        cache_dir=tmp_path / "c1",
        max_rpm=10_000,
        max_tpm=10_000_000,
        backend=backend,
    )
    run = _make_run_meta(
        "g1",
        harness="h",
        model="m",
        is_open=False,
        task_name="t",
        succeeded=False,
        n_steps=70,
        step_costs=[0.01] * 70,
        stop_steps={"gemini_cli": None},
    )
    res = evaluate_gemini_cli_arm(run, trace, client)
    assert backend.checked_steps == [29, 39, 54, 69]
    assert res.checks_run == 4
    assert res.blocked_checks == 1
    assert res.llm_stop_step == 69
    assert res.full_stop_step == 69

    # Hash stop precedes turn 40 (hash_stop_step=35): LLM only checks at step 29!
    backend2 = _AdaptiveCliBackend({29: 0.5, 39: 0.95})
    client2 = GuardedDecisionClient(
        phase="test_gcli2",
        cap_usd=1.0,
        cache_dir=tmp_path / "c2",
        max_rpm=10_000,
        max_tpm=10_000_000,
        backend=backend2,
    )
    run_hash_first = _make_run_meta(
        "g2",
        harness="h",
        model="m",
        is_open=False,
        task_name="t",
        succeeded=False,
        n_steps=70,
        step_costs=[0.01] * 70,
        stop_steps={"gemini_cli": 35},
    )
    res2 = evaluate_gemini_cli_arm(run_hash_first, trace, client2)
    assert backend2.checked_steps == [29]
    assert res2.hash_stop_step == 35
    assert res2.llm_stop_step is None
    assert res2.full_stop_step == 35


class _SmokeDecisionBackend:
    """Deterministic fast mock backend returning plausible probabilities for synthetic smoke replay."""

    def create(
        self,
        *,
        model: str,
        input: list[dict[str, Any]],
        questions: dict[str, dict[str, Any]],
    ) -> Any:
        del model
        txt = input[0]["text"]
        is_loop = "REPEAT_LOOP_TOKEN" in txt
        toks = max(50, len(txt) // 3)
        if set(questions.keys()) == set(MAIN_QUESTIONS.keys()):
            ps = 0.92 if is_loop else 0.12
            pp = 0.08 if is_loop else 0.85
            pd = 0.88 if is_loop else 0.18
            return SimpleNamespace(
                answers={
                    "stuck": SimpleNamespace(type="noul", noul=ps, confidence=max(ps, 1.0 - ps)),
                    "progress": SimpleNamespace(type="noul", noul=pp, confidence=max(pp, 1.0 - pp)),
                    "doomed": SimpleNamespace(type="noul", noul=pd, confidence=max(pd, 1.0 - pd)),
                },
                usage=SimpleNamespace(total_input_tokens=toks, total_output_tokens=0),
            )
        p_u = 0.93 if is_loop else 0.10
        return SimpleNamespace(
            answers={
                "unproductive_loop": SimpleNamespace(
                    type="noul", noul=p_u, confidence=max(p_u, 1.0 - p_u)
                )
            },
            usage=SimpleNamespace(total_input_tokens=toks, total_output_tokens=0),
        )


def _build_synthetic_study_data() -> PreparedStudyData:
    def _mk_corpus(corpus: str) -> tuple[list[RunMetadata], dict[str, dict[str, Any]], dict[str, SelectedTrajectory], dict[str, Any]]:
        configs = [
            (f"{corpus}_1", "open_model", True, "task_A", True, 20, False, False, None),
            (f"{corpus}_2", "open_model", True, "task_B", False, 34, True, False, 16),
            (f"{corpus}_3", "closed_model", False, "task_C", True, 24, False, True, None),
            (f"{corpus}_4", "closed_model", False, "task_D", False, 34, True, False, 12),
        ]
        runs: list[RunMetadata] = []
        assigns: dict[str, dict[str, Any]] = {}
        traces: dict[str, SelectedTrajectory] = {}
        for idx, (uid, model, is_open, task, succ, n_steps, strict, def_only, onset) in enumerate(configs):
            steps = []
            s_flags = [False] * n_steps
            d_flags = [False] * n_steps
            for i in range(n_steps):
                in_loop = strict and (onset is not None) and (i >= onset)
                if in_loop:
                    s_flags[i] = True
                elif def_only and i >= 12:
                    d_flags[i] = True
                txt = "REPEAT_LOOP_TOKEN same cmd" if in_loop else f"normal progress step {i}"
                steps.append(_make_step(i, obs_text=txt, file_op=None if in_loop else "write"))
            stops = {
                "gemini_cli": 32 if strict else None,
                "cline_style": 18 if strict else None,
                "openhands_stuck": 20 if strict else None,
                "openclaw_10": None,
                "openclaw_20": None,
                "step_cap_30": 30 if n_steps > 30 else None,
                "step_cap_50": None,
                "step_cap_75": None,
                "step_cap_100": None,
                "step_cap_150": None,
                "turn_budget_1_25x_p90": None,
            }
            rm = _make_run_meta(
                uid,
                harness=model,
                model=model,
                is_open=is_open,
                task_name=task,
                succeeded=succ,
                n_steps=n_steps,
                step_costs=[0.05] * n_steps,
                stop_steps=stops,
                has_strict_loop=strict,
                default_only=def_only,
                strict_onset_step=onset,
                last_productive_step=4 if strict else n_steps - 1,
            )
            rm.corpus = corpus
            runs.append(rm)
            role = "strict" if strict else ("default_only" if def_only else "sample_a")
            assigns[uid] = {
                "in_sample_a": True,
                "sample_weight": 1.0,
                "sample_b_role": role if idx in (1, 2) else None,
            }
            traces[uid] = SelectedTrajectory(uid, corpus, f"Solve {task}", steps, d_flags, s_flags)

        tot_usd = sum(r.total_cost_usd for r in runs)
        strict_usd = sum(r.total_cost_usd for r in runs if r.has_strict_loop)
        fail_usd = sum(r.total_cost_usd for r in runs if not r.succeeded)
        sanity = {
            "runs": len(runs),
            "models": 2,
            "passed": sum(1 for r in runs if r.succeeded),
            "pass_rate": 0.5,
            "strict_runs": sum(1 for r in runs if r.has_strict_loop),
            "strict_open": 1,
            "strict_closed": 1,
            "strict_passed": 0,
            "default_only_runs": sum(1 for r in runs if r.default_only),
            "cost_valid_runs": len(runs),
            "total_usd": tot_usd,
            "strict_usd": strict_usd,
            "strict_usd_share": strict_usd / tot_usd,
            "fail_usd": fail_usd,
            "fail_usd_share": fail_usd / tot_usd,
            "gemini_cli_fires": 2,
            "gemini_cli_strict_fires": 2,
            "gemini_cli_default_only_fires": 0,
            "openhands_strict_fires": 2,
            "openhands_default_only_fires": 0,
        }
        return runs, assigns, traces, sanity

    swe_runs, swe_assign, selected_swe, swe_sanity = _mk_corpus("swe")
    tb_runs, tb_assign, selected_tb, tb_sanity = _mk_corpus("tb")
    pilot_points = [
        PilotPoint("swe_2", "swe", "strict_onset", 16),
        PilotPoint("swe_3", "swe", "default_only", 16),
        PilotPoint("swe_1", "swe", "sample_a", 8),
    ]
    return PreparedStudyData(
        swe_runs=swe_runs,
        tb_runs=tb_runs,
        swe_assign=swe_assign,
        tb_assign=tb_assign,
        swe_sanity=swe_sanity,
        tb_sanity=tb_sanity,
        swe_budgets={"open_model": 30, "closed_model": 30},
        tb_budgets={"open_model": 30, "closed_model": 30},
        pilot_points=pilot_points,
        selected_swe=selected_swe,
        selected_tb=selected_tb,
    )


def test_end_to_end_smoke_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    synth = _build_synthetic_study_data()
    monkeypatch.setattr("agent_loop_decisions.__main__.load_or_build_study_data", lambda **_kw: synth)

    paths = RuntimePaths(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        results_dir=tmp_path / "results",
        assets_dir=tmp_path / "assets",
        audit_dir=tmp_path / "audit",
        notes_path=tmp_path / "NOTES.md",
    )
    backend = _SmokeDecisionBackend()

    # 1. Plan (0 API calls)
    assert cli_main(["plan"], paths_override=paths) == 0
    assert (paths.results_dir / "plan.csv").exists()
    assert (paths.results_dir / "sanity_checks.json").exists()
    assert (paths.results_dir / "rules_baseline_swe.csv").exists()
    assert (paths.results_dir / "summary.md").exists()
    assert paths.notes_path.exists()
    assert not (paths.cache_dir / "spend.json").exists()

    # 2. Pilot
    assert cli_main(["pilot", "--cap", "0.25", "--rpm", "10000", "--tpm", "10000000"], backend=backend, paths_override=paths) == 0
    assert (paths.results_dir / "pilot_points.csv").exists()
    assert (paths.results_dir / "pilot_summary.json").exists()
    assert (paths.results_dir / "pilot_report.md").exists()
    assert (paths.assets_dir / "example_window.png").exists()
    assert (paths.cache_dir / "decisions.jsonl.gz").exists()
    assert (paths.cache_dir / "spend.json").exists()

    # 3. Full SWE + TB smoke phases
    assert cli_main(["swe", "--cap", "1.0", "--rpm", "10000", "--tpm", "10000000"], backend=backend, paths_override=paths) == 0
    assert cli_main(["tb", "--cap", "1.0", "--rpm", "10000", "--tpm", "10000000"], backend=backend, paths_override=paths) == 0

    for corpus in ("swe", "tb"):
        assert (paths.results_dir / f"outcome_curve_{corpus}.csv").exists()
        assert (paths.results_dir / f"task_split_calibration_{corpus}.csv").exists()
        assert (paths.results_dir / f"strict_loops_{corpus}.csv").exists()
        assert (paths.results_dir / f"hard_negatives_{corpus}.csv").exists()
        assert (paths.results_dir / f"cost_per_catch_{corpus}.csv").exists()
        assert (paths.results_dir / f"open_vs_closed_fairness_{corpus}.csv").exists()
        assert (paths.assets_dir / f"fsr_vs_savings_{corpus}.png").exists()

    assert (paths.results_dir / "calibration_summary.json").exists()
    assert (paths.assets_dir / "calibration_stuck_doomed.png").exists()
    assert (paths.audit_dir / "decision_vs_rules.md").exists()

    # 4. Re-run analyze strictly from cache (0 extra spend)
    spend_before = orjson.loads((paths.cache_dir / "spend.json").read_bytes())
    (paths.results_dir / "summary.md").unlink()
    (paths.assets_dir / "calibration_stuck_doomed.png").unlink()

    assert cli_main(["analyze"], paths_override=paths) == 0
    spend_after = orjson.loads((paths.cache_dir / "spend.json").read_bytes())
    assert spend_after["total_spent_usd"] == spend_before["total_spent_usd"]
    assert spend_after["total_calls"] == spend_before["total_calls"]
    assert (paths.results_dir / "summary.md").exists()
    assert (paths.assets_dir / "calibration_stuck_doomed.png").exists()
