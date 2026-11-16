# agent-loop-decisions — Engineering Notes

## Goal

Evaluate whether zero-shot `gemini-decision-1` (Google Decisions API) stops wasteful AI agent trajectories better than rule-based loop detectors (`cline_style`, `openhands_stuck`, `gemini_cli`, `openclaw_10/20`, step caps, and `1.25x P90` turn budget) across SWE-rebench and Terminal-Bench 2.0.

## Setup

- **Decision Model**: `gemini-decision-1` (`google-genai==3.3.0`, `v1beta` REST / SDK, `$0.10 / 1M` input tokens, `$0` output tokens)
- **Pinned `agent-loops` commit**: [`5e29d428f4e80403f982b7bbf4671fa9b696880e`](https://github.com/DavidFlorez/agent-loops/tree/5e29d428f4e80403f982b7bbf4671fa9b696880e)
- **Pinned `google-gemini/gemini-cli` reference**: commit [`4fa64e71b9b4e1bb65a86cc2863574f561b82340`](https://github.com/google-gemini/gemini-cli/blob/4fa64e71b9b4e1bb65a86cc2863574f561b82340/packages/core/src/services/loopDetectionService.ts) (`LLM_CHECK_AFTER_TURNS=30`, `DEFAULT_LLM_CHECK_INTERVAL=10`, adaptive bounds `[5, 15]` via `round(5 + 10*(1 - p))`, `LLM_LOOP_CHECK_HISTORY_COUNT=20`, `LLM_CONFIDENCE_THRESHOLD=0.9`; Decision 1 `unproductive_loop` noul stands in for production Gemini CLI's generative Gemini LLM check)
- **SWE-rebench dataset**: [`ibragim-bad/swe_rebench_07_2026_trajectories`](https://huggingface.co/datasets/ibragim-bad/swe_rebench_07_2026_trajectories) @ `cdae27cdd16673f0c682871ad55d325f24cc7020` (`swelike` = `common_scaffold_model`)
- **Terminal-Bench 2.0 dataset**: [`yoonholee/terminalbench-trajectories`](https://huggingface.co/datasets/yoonholee/terminalbench-trajectories) @ `04e8940f5b6736a7ce8d22224fe2f2af74163ed2` (`terminus-2` harness)
- **Hardware**: Apple MacBook Air (M3, 16 GB RAM, CPU-only); API-bound workload.
- **Sanity Check (verified against `agent-loops@5e29d42`)**:
  - SWE `swelike`: `7,215` runs (`13` models × `555`), pass rate `48.0%`, `152` strict loops (`133` open, `19` closed, `48` passed), `4,318` default-only, `6,092` cost-valid worth `$5,845.10` (strict `$143.37` / `2.5%`, failed `$3,011.31` / `51.5%`), `gemini_cli` hash rules fired `340` times on default-only runs (`402` total; `openhands_stuck` fired `0` on default-only and `49` on strict).
  - TB `terminus-2`: `15,941` runs (`29` models), pass rate `32.5%`, `265` strict loops (`188` open, `77` closed, `16` passed), `3,319` default-only, `3,267` cost-valid worth `$2,249.59` (strict `$135.30` / `6.0%`, failed `$1,959.08` / `87.1%`), `gemini_cli` hash rules fired `248` times (`146` strict, `58` default-only).

## Log

- **2026-11-16 21:27 UTC**: Built deterministic sampling + offline causal replay pipeline (`data/parsed_cache/`), ran `plan` (0 API calls), verified all baseline sanity counts against `agent-loops`.

## Key Results

### Planning Projections (`results/plan.csv`)

| Phase | Arm | Stratum | Runs | Decision Points | Projected Tokens (`chars/3.0`) | Conservative USD (`chars/3.0`) |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| `pilot` | `main_grid` | `pilot_50_points` | 50 | 50 | 180,509 | $0.0181 |
| `swe` | `main_grid` | `sample_a` | 299 | 5,040 | 20,182,255 | $2.0182 |
| `swe` | `main_grid` | `sample_b_strict` | 147 | 3,793 | 15,143,553 | $1.5144 |
| `swe` | `main_grid` | `sample_b_default_only` | 60 | 1,104 | 4,261,862 | $0.4262 |
| `swe` | `gemini_cli_llm_approx` | `all_sampled` | 506 | 2,685 | 16,762,233 | $1.6762 |
| `tb` | `main_grid` | `sample_a` | 290 | 1,451 | 4,003,656 | $0.4004 |
| `tb` | `main_grid` | `sample_b_strict` | 120 | 2,543 | 8,489,336 | $0.8489 |
| `tb` | `main_grid` | `sample_b_default_only` | 60 | 671 | 2,195,294 | $0.2195 |
| `tb` | `gemini_cli_llm_approx` | `all_sampled` | 470 | 914 | 4,861,091 | $0.4861 |
- **Cumulative Spend (`cache/spend.json`)**: `$0.000000` across `0` uncached API calls (`0` blocked).

## Visuals

- _(Full Decision 1 charts pending `swe` / `tb` live API phases; example pilot window chart generated if pilot cache exists.)_

## Surprises and Limitations

- `gemini-decision-1` currently offers no batch API or context caching, so every overlapping 12-step sliding window is billed at full input token count.
- Terminal-Bench `trial_id` is not unique across `terminus-2` parquet rows (only 3,204 unique `trial_id`s across 15,941 rows); all internal joins use unique `(parquet_file_idx, row_idx)` identifiers (`tb::{file_idx}::{row_idx}`).
- Production Gemini CLI uses a generative Gemini model for its `LLM_LOOP_CHECK`; here Decision 1 `unproductive_loop` (`noul`) acts as a calibrated stand-in.

## Open Questions / Next Steps

- Review Pilot measurements and re-projected budgets, then authorize `--cap` for `swe` (`python -m agent_loop_decisions swe --cap ...`) and `tb` (`python -m agent_loop_decisions tb --cap ...`).
