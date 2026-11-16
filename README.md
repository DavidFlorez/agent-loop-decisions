# agent-loop-decisions

Offline step-by-step replay evaluating Google's **`gemini-decision-1`** (Decisions API) as a calibrated, zero-shot loop-stopper across saved AI coding agent trajectories, benchmarked directly against rule-based stoppers from [`DavidFlorez/agent-loops`](https://github.com/DavidFlorez/agent-loops/tree/5e29d428f4e80403f982b7bbf4671fa9b696880e) (frozen at commit `5e29d42`).

## Pinned Inputs & References

- **Decision model**: `gemini-decision-1` (`v1beta` Decisions API, `$0.10 / 1M` input tokens, `$0` output)
- **Frozen `agent-loops` baseline**: [`5e29d428f4e80403f982b7bbf4671fa9b696880e`](https://github.com/DavidFlorez/agent-loops/tree/5e29d428f4e80403f982b7bbf4671fa9b696880e)
- **Gemini CLI reference (`loopDetectionService.ts`)**: [`4fa64e71b9b4e1bb65a86cc2863574f561b82340`](https://github.com/google-gemini/gemini-cli/blob/4fa64e71b9b4e1bb65a86cc2863574f561b82340/packages/core/src/services/loopDetectionService.ts)
- **SWE-rebench (`swelike`)**: `ibragim-bad/swe_rebench_07_2026_trajectories` @ `cdae27cdd16673f0c682871ad55d325f24cc7020` (`7,215` runs, `13` models)
- **Terminal-Bench 2.0 (`terminus-2`)**: `yoonholee/terminalbench-trajectories` @ `04e8940f5b6736a7ce8d22224fe2f2af74163ed2` (`15,941` runs, `29` models)

## Stopping Rules Compared

- **Decision 1 Grid Stoppers** (12-step causal window every 4 steps `t = 4, 8, ... < min(n_steps, 200)` across `τ ∈ {0.50, 0.60, 0.70, 0.80, 0.90, 0.95, 0.98}`):
  - `d1_stuck`: single-point `p_stuck >= τ`
  - `d1_stuck2`: two consecutive grid points `p_stuck >= τ`
  - `d1_doomed`: `p_doomed >= τ` (terminal futility)
  - `d1_noprog`: `1 - p_progress >= τ`
- **Gemini CLI Arm** (`gemini_cli_llm` / `gemini_cli_full`): replays Gemini CLI hash rules plus adaptive LLM check (`start turn 30`, `interval clamp(5, 15, round(5 + 10*(1 - p)))`, `20`-step history, `p >= 0.90`).
- **Rule & Budget Baselines**: `cline_style`, `openhands_stuck`, `gemini_cli` (hash rules), `openclaw_10`, `openclaw_20`, `step_cap_{30,50,75,100,150}`, `turn_budget_1_25x_p90`, and 200-seed step-matched random baselines.

## Quickstart & CLI

```bash
# 1. Verify sanity counts & generate zero-API budget plan + free rule baselines
.venv/bin/python -m agent_loop_decisions plan

# 2. Run 50-point calibration pilot (guarded cap $0.25)
.venv/bin/python -m agent_loop_decisions pilot --cap 0.25

# 3. Full corpora (requires explicit --cap authorization after pilot)
.venv/bin/python -m agent_loop_decisions swe --cap 7.0
.venv/bin/python -m agent_loop_decisions tb --cap 3.0

# 4. Recompute all metrics, charts, audit tables, and summaries strictly from local cache (0 API calls)
.venv/bin/python -m agent_loop_decisions analyze
```

Run tests:

```bash
.venv/bin/pytest -v
```

## Safeguards & Reproducibility

- **Causal non-leakage**: every input at step `t` contains only steps `<= t` (excluding total length, outcome, model name, harness, loop labels, and rule counters).
- **Pre-call dollar guard & deterministic cache**: every API call passes through `GuardedDecisionClient` (pre-call `chars/3.0` phase & global budget checks, persistent append-only `cache/decisions.jsonl`, gzipped snapshot `cache/decisions.jsonl.gz`, and `cache/spend.json`).
- Raw parquet datasets (`data/`) and API credentials (`.env`) are gitignored and never committed. See [`NOTES.md`](NOTES.md) and [`results/summary.md`](results/summary.md) for current phase status and outputs.
