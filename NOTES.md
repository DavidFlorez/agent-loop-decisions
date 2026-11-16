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

- **2026-11-16 22:27 UTC**: Built deterministic sampling + offline causal replay pipeline (`data/parsed_cache/`), ran `plan` (0 API calls), verified all baseline sanity counts against `agent-loops`.
- **2026-11-16 22:27 UTC**: Ran `pilot` (`50` points, `$0.017450` spend, `174,501` input tokens, `3.103` chars/token, `0` blocked).
- **2026-11-16 22:27 UTC**: Completed `analyze` over cached responses for corpora `['swe', 'tb']`.

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

### Pilot Results (`results/pilot_report.md`)

- **Measured Tokens vs Estimate**: `174,501` actual vs `180,509` (`chars/3.0`), measured **`3.103` chars/token**
- **Pilot Spend & Latency**: `$0.017450` total; median `315.4 ms` (P95 `505.7 ms`); safety block rate `0.00%`
- **Re-Projected Spend at Measured Ratio**: `swe` = **`$5.447`** (`12,622` calls, `54,474,473` tokens); `tb` = **`$1.890`** (`5,579` calls, `18,898,737` tokens)
- **Cumulative Spend (`cache/spend.json`)**: `$6.932495` across `17,252` uncached API calls (`0` blocked).

## Visuals

- [`assets/example_window.png`](assets/example_window.png): Example causal 12-step trajectory window paired with Decision 1 probabilities.
- [`assets/fsr_vs_savings_swe.png`](assets/fsr_vs_savings_swe.png): SWE-rebench: False-stop rate among passed runs vs. net dollars saved across Decision 1 thresholds, rule stoppers, and 200-seed matched random.
- [`assets/fsr_vs_savings_tb.png`](assets/fsr_vs_savings_tb.png): Terminal-Bench 2.0: False-stop rate among passed runs vs. net dollars saved across Decision 1 thresholds, rule stoppers, and 200-seed matched random.
- [`assets/lead_time_strict_loops.png`](assets/lead_time_strict_loops.png): Median detection lead time relative to strict loop onset (and strict-loop recall) per stopper.
- [`assets/hard_negative_fire_rate.png`](assets/hard_negative_fire_rate.png): False alarm rates on default-only runs (harmless re-reads / near-duplicates).
- [`assets/calibration_stuck_doomed.png`](assets/calibration_stuck_doomed.png): Reliability diagrams checking Decisions API probability calibration for stuck and doomed.
- [`assets/open_vs_closed_fire_rates.png`](assets/open_vs_closed_fire_rates.png): Open-weight vs. closed stopping rates conditioned on final run outcome.

## Surprises and Limitations

- `gemini-decision-1` currently offers no batch API or context caching, so every overlapping 12-step sliding window is billed at full input token count.
- Terminal-Bench `trial_id` is not unique across `terminus-2` parquet rows (only 3,204 unique `trial_id`s across 15,941 rows); all internal joins use unique `(parquet_file_idx, row_idx)` identifiers (`tb::{file_idx}::{row_idx}`).
- Production Gemini CLI uses a generative Gemini model for its `LLM_LOOP_CHECK`; here Decision 1 `unproductive_loop` (`noul`) acts as a calibrated stand-in.

## Open Questions / Next Steps

- All planned replay phases (`pilot`, `swe`, `tb`, `analyze`) are complete; see `results/summary.md`, `audit/decision_vs_rules.md`, and `results/calibration_summary.json`. Follow-up: test post-hoc Platt/isotonic probability recalibration or combined multi-question rules (`stuck` + `doomed`).
