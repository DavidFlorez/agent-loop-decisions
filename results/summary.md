# agent-loop-decisions — Results Summary

_Generated: 2026-11-16 21:53 UTC_

## 1. Corpus Sanity Checks

- **SWE-rebench (`swelike`)**: `7,215` runs (`13` models), pass `48.01%`, `152` strict (`133` open, `19` closed, `48` passed), `4,318` default-only, `6,092` cost-valid worth `$5,845.10` (strict `$143.37`, fail `$3,011.31`), `gemini_cli` hash `402` fires.
- **Terminal-Bench 2.0 (`terminus-2`)**: `15,941` runs (`29` models), pass `32.48%`, `265` strict (`188` open, `77` closed, `16` passed), `3,319` default-only, `3,267` cost-valid worth `$2,249.59` (strict `$135.30`, fail `$1,959.08`), `gemini_cli` hash `248` fires.

## 2. Planning Projections (`results/plan.csv`)

| Phase | Arm | Stratum | Runs | Points | Prompt Chars | Est Tokens (`chars/3.0`) | Est USD (`chars/3.0`) |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |
| `pilot` | `main_grid` | `pilot_50_points` | 50 | 50 | 510,726 | 180,509 | $0.0181 |
| `swe` | `main_grid` | `sample_a` | 299 | 5,040 | 57,442,124 | 20,182,255 | $2.0182 |
| `swe` | `main_grid` | `sample_b_strict` | 147 | 3,793 | 43,094,172 | 15,143,553 | $1.5144 |
| `swe` | `main_grid` | `sample_b_default_only` | 60 | 1,104 | 12,105,522 | 4,261,862 | $0.4262 |
| `swe` | `gemini_cli_llm_approx` | `all_sampled` | 506 | 2,685 | 48,613,943 | 16,762,233 | $1.6762 |
| `tb` | `main_grid` | `sample_a` | 290 | 1,451 | 11,117,152 | 4,003,656 | $0.4004 |
| `tb` | `main_grid` | `sample_b_strict` | 120 | 2,543 | 23,901,520 | 8,489,336 | $0.8489 |
| `tb` | `main_grid` | `sample_b_default_only` | 60 | 671 | 6,172,546 | 2,195,294 | $0.2195 |
| `tb` | `gemini_cli_llm_approx` | `all_sampled` | 470 | 914 | 14,013,851 | 4,861,091 | $0.4861 |

## 3. Pilot Calibration (`results/pilot_report.md`)

- **Calls**: `50` (`50` ok, `0` blocked)
- **Tokens (`actual` vs `chars/3.0`)**: `174,501` vs `180,509` (`3.103` chars/token)
- **Spend**: `$0.017450`; Latency median `315.4 ms` (P95 `505.7 ms`)
- **Re-projected Phase Spend**: `swe` `$5.447`, `tb` `$1.890`

## Free Rule Baselines (`swe` full corpus)

| Stopper | Fire Rate | FSR (Passed) | Success Loss (pp) | Net Fail Saved (%) | Tail Cut (%) | Matched Rand Saved (%) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `gemini_cli` | 5.57% | 2.48% | 1.19 | 1.43% | 2.49% | 1.14% |
| `cline_style` | 0.26% | 0.17% | 0.08 | 0.18% | 0.20% | 0.09% |
| `openhands_stuck` | 0.68% | 0.52% | 0.25 | 0.53% | 1.78% | 0.23% |
| `openclaw_10` | 0.08% | 0.06% | 0.03 | 0.03% | 0.05% | 0.01% |
| `openclaw_20` | 0.00% | 0.00% | 0.00 | 0.00% | 0.00% | 0.00% |
| `step_cap_30` | 78.45% | 74.34% | 35.69 | 35.07% | 86.74% | 34.54% |
| `step_cap_50` | 60.68% | 53.90% | 25.88 | 24.12% | 68.38% | 23.71% |
| `step_cap_75` | 36.13% | 29.71% | 14.26 | 14.28% | 41.15% | 14.04% |
| `step_cap_100` | 20.37% | 16.08% | 7.72 | 8.85% | 22.89% | 8.63% |
| `step_cap_150` | 7.47% | 6.12% | 2.94 | 3.82% | 7.75% | 3.76% |
| `turn_budget_1_25x_p90` | 4.89% | 2.86% | 1.37 | 2.90% | 7.96% | 2.83% |

## Free Rule Baselines (`tb` full corpus)

| Stopper | Fire Rate | FSR (Passed) | Success Loss (pp) | Net Fail Saved (%) | Tail Cut (%) | Matched Rand Saved (%) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `gemini_cli` | 1.56% | 0.46% | 0.15 | 6.07% | 10.22% | 4.12% |
| `cline_style` | 1.55% | 0.46% | 0.15 | 6.00% | 10.16% | 4.12% |
| `openhands_stuck` | 1.04% | 0.10% | 0.03 | 1.39% | 5.08% | 2.82% |
| `openclaw_10` | 1.21% | 0.21% | 0.07 | 3.96% | 5.99% | 3.85% |
| `openclaw_20` | 0.29% | 0.00% | 0.00 | 0.58% | 2.41% | 1.01% |
| `step_cap_30` | 24.61% | 11.36% | 3.69 | 65.93% | 80.95% | 63.57% |
| `step_cap_50` | 13.08% | 3.80% | 1.24 | 48.43% | 62.06% | 47.24% |
| `step_cap_75` | 6.79% | 1.43% | 0.46 | 31.76% | 45.04% | 30.62% |
| `step_cap_100` | 4.06% | 0.37% | 0.12 | 20.00% | 31.77% | 19.71% |
| `step_cap_150` | 2.01% | 0.06% | 0.02 | 7.32% | 12.61% | 7.26% |
| `turn_budget_1_25x_p90` | 17.65% | 6.26% | 2.03 | 56.90% | 71.15% | 55.28% |

## Spend Tracking (`cache/spend.json`)

- **Total Spent**: `$0.017450` / `$10.00` global cap (`50` calls, `0` blocked)
