# Run metrics — 879f1abc

`analytics_upgrade` finished **succeeded** in 10209ms.

| Signal | Value |
| --- | --- |
| first-pass yield | no |
| tasks planned / injected | 10 / 2 |
| retries | 0 |
| repair rounds | 1 |
| escalations | 0 |
| approvals requested / denied | 2 / 0 |
| validation rounds | 2 |
| checks run / failed | 6 / 0 |
| artifacts / lines | 22 / 1246 |

## Where the time went

| Agent | Calls | Total | Mean | Retries |
| --- | --- | --- | --- | --- |
| `validation` | 2 | 9864ms | 4932ms | 0 |
| `codebase` | 1 | 77ms | 77ms | 0 |
| `implementation` | 1 | 38ms | 38ms | 0 |

`first-pass yield` is the one to watch across runs: it says whether the platform got to a passing verdict without needing to repair itself. A fall in that number is the signal that output quality is drifting, and it moves before the pass/fail result does.
