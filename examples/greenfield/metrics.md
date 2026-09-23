# Run metrics — 7f194be3

`url_shortener` finished **succeeded** in 10796ms.

| Signal | Value |
| --- | --- |
| first-pass yield | no |
| tasks planned / injected | 9 / 2 |
| retries | 0 |
| repair rounds | 1 |
| escalations | 0 |
| approvals requested / denied | 2 / 0 |
| validation rounds | 2 |
| checks run / failed | 6 / 0 |
| artifacts / lines | 28 / 1508 |

## Where the time went

| Agent | Calls | Total | Mean | Retries |
| --- | --- | --- | --- | --- |
| `validation` | 2 | 10699ms | 5349ms | 0 |
| `implementation` | 1 | 26ms | 26ms | 0 |
| `test` | 1 | 10ms | 10ms | 0 |

`first-pass yield` is the one to watch across runs: it says whether the platform got to a passing verdict without needing to repair itself. A fall in that number is the signal that output quality is drifting, and it moves before the pass/fail result does.
