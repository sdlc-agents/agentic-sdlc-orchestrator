# Run metrics — d8fce4fe

`url_shortener` finished **succeeded** in 9423ms.

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
| `validation` | 2 | 9339ms | 4669ms | 0 |
| `implementation` | 1 | 20ms | 20ms | 0 |
| `test` | 1 | 12ms | 12ms | 0 |

`first-pass yield` is the one to watch across runs: it says whether the platform got to a passing verdict without needing to repair itself. A fall in that number is the signal that output quality is drifting, and it moves before the pass/fail result does.
