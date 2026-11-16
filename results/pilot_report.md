# Pilot Report (50 SWE-rebench Decision Points)

- **Points**: `50` (`50` ok, `0` safety-blocked, block rate `0.00%`)
- **Tokens (measured vs `chars/3.0` est)**: `174,501` actual vs `180,509` estimated (`1.034x` conservative)
- **Measured chars per input token**: aggregate **`3.103`** (per-call mean `3.102`, median `3.096`)
- **Measured Pilot Cost**: **`$0.017450`**
- **Latency**: mean `338.2 ms`, median `315.4 ms`, P95 `505.7 ms`

## Re-Projected Phase Costs at Measured Chars/Token

| Phase | Projected Calls | Est Tokens (Measured CPT) | Re-Projected USD | Conservative USD (`chars/3.0`) |
| --- | ---: | ---: | ---: | ---: |
| `swe` | 12,622 | 54,474,473 | **$5.447** | $5.635 |
| `tb` | 5,579 | 18,898,737 | **$1.890** | $1.955 |

## 10 Example Pilot Decision Answers

| # | Stratum | Model | Outcome | Step / Onset | Strict in Win? | `p_stuck` | `p_progress` | `p_doomed` | Tokens | Latency (ms) |
| ---: | --- | --- | --- | ---: | :---: | ---: | ---: | ---: | ---: | ---: |
| 1 | `strict_onset` | `Qwen/Qwen3.5-35B-A3B` | PASS | 68 / 68 | YES | 0.54 | 0.55 | 0.27 | 3,582 | 459 |
| 2 | `strict_onset` | `Qwen/Qwen3.5-35B-A3B` | FAIL | 36 / 35 | YES | 0.88 | 0.17 | 0.51 | 2,875 | 314 |
| 3 | `strict_onset` | `Qwen/Qwen3.5-35B-A3B` | FAIL | 188 / 187 | YES | 0.91 | 0.05 | 0.87 | 3,085 | 338 |
| 4 | `strict_onset` | `Qwen/Qwen3.6-27B` | FAIL | 80 / 80 | YES | 0.26 | 0.76 | 0.22 | 3,278 | 261 |
| 5 | `default_only` | `Qwen/Qwen3.6-27B` | PASS | 4 / - | no | 0.48 | 0.19 | 0.72 | 1,425 | 357 |
| 6 | `default_only` | `Qwen/Qwen3.6-27B` | FAIL | 32 / - | no | 0.13 | 0.75 | 0.36 | 3,084 | 344 |
| 7 | `default_only` | `Qwen/Qwen3.6-35B-A3B` | FAIL | 4 / - | no | 0.09 | 0.88 | 0.49 | 2,887 | 283 |
| 8 | `sample_a` | `claude-sonnet-5` | FAIL | 16 / - | no | 0.12 | 0.54 | 0.19 | 3,664 | 308 |
| 9 | `sample_a` | `gpt-5.6-sol` | FAIL | 8 / - | no | 0.01 | 0.96 | 0.01 | 3,897 | 341 |
| 10 | `sample_a` | `x-ai/grok-4.5` | FAIL | 28 / - | no | 0.11 | 0.80 | 0.12 | 4,807 | 315 |
