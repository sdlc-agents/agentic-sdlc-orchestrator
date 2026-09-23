# Operating this platform

Where it sits against DevOps and SRE practice, what it deliberately does not do,
and why one commonly-requested item is not here.

## DevOps

| Practice | State |
| --- | --- |
| CI on every push and PR | Ubuntu + Windows, Python 3.10 + 3.12 |
| Lint | `ruff`, blocking |
| Coverage | ≥ 90% of `asep/`, **enforced** not reported |
| Dependency scanning | `pip-audit`, blocking; Dependabot weekly |
| Containerised | `Dockerfile`, non-root |
| Trunk-based development | short-lived branches, `main` protected behind all checks |
| Reproducibility gates | sample codebase and examples must regenerate byte-identically |

**Not here, on purpose:** no CD, no infrastructure-as-code, no environment
promotion. Nothing about this project deploys anywhere — it is a tool that runs
locally or in CI. Adding a Terraform module to satisfy a checklist would be
scenery.

**Genuinely missing:** release automation. There are no tagged versions and no
published package. For a tool at this stage that is a reasonable gap, but it is
a gap rather than a decision.

## SRE

The platform produces a structured record of every run, and that record is the
observability surface.

| Signal | Where |
| --- | --- |
| Events | `trace.jsonl` — every scheduling decision, approval, failure, mutation, flushed as it happens |
| Metrics | `metrics.json` and `metrics.md` — derived from the trace |
| Run state | `run.json` — tasks, artifacts, approvals, blackboard |
| Verdicts | `validation-round-N.md`, `validation-approach.md` |

Metrics are **derived rather than instrumented**: every figure comes from events
the engine already emits, so nothing depends on a counter someone forgets to
increment.

### The signal worth watching

`first_pass_yield` — did the run reach a passing verdict without needing to
repair itself?

It is a leading indicator. Pass/fail only tells you the final state, and the
repair loop is good enough to rescue a mediocre run. A fall in first-pass yield
means output quality is drifting while the run still ends green, which is
exactly the failure you want to catch before a human does.

Alongside it: `retries` (provider instability), `escalations` (defects the
platform could not act on) and per-agent `total_ms` (where time actually goes —
currently ~99% validation, because it executes the generated suite).

### Reliability behaviours already in the engine

- **Bounded retry** with the failure message attached to the next attempt, so a
  retry is informed rather than a repeat.
- **Failure classification** — the error class decides recovery, never a string
  match. Transient retries; guardrail violations never do.
- **Finite repair budget** — a loop that is not converging stops rather than
  running forever.
- **Graceful degradation of scope** — findings that cannot describe a fix are
  escalated rather than guessed at.
- **Timeout on generated code**, executed in a subprocess.

### What is not here

No SLOs or error budgets. The generated architecture states targets
(`p99 < 50ms`, `10k redirects/sec`) but nothing defines them as SLIs or measures
against them — they are design intent, and the validation report says so. No
alerting, because there is no long-running service to alert on.

The **generated** service has `/healthz` and `/readyz` and sheds load
deliberately under pressure, but it has no `/metrics` endpoint and no structured
logging. That is a real gap in the deliverable, documented in its own runbook
rather than hidden.

## AIOps

**Not implemented, and mostly not applicable** — worth stating plainly rather
than claiming otherwise.

AIOps means AI applied to *operations*: anomaly detection over production
telemetry, alert correlation, automated incident remediation. This platform
applies AI to the *SDLC* — dev-time, before anything is running. There is no
production signal for it to act on, and no incident surface. Adding an "AIOps
module" here would be a label on something that does not exist.

The honest connection is a shared **control pattern**, not a shared domain. The
validation → repair → re-validation loop is closed-loop automated remediation
driven by structured machine-readable signals, with a bounded budget and
escalation to a human when confidence runs out. That is the same shape as
automated remediation in production, applied to the build instead:

```
signal        production AIOps          this platform
-----------   ----------------------    ---------------------------
detect        anomaly in telemetry      validation check fails
diagnose      correlate alerts          structured Finding + repair_hint
act           runbook automation        repair agent applies edits
verify        confirm metric recovered  re-validation task re-runs the checks
give up       page a human              escalate; repair budget is finite
```

If this platform ever grew a production-facing component, the metrics above are
the substrate AIOps would need. Today it would be a name without a system behind
it.
