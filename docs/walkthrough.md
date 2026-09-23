# One requirement, traced end to end

The other documents describe the system. This one follows a single sentence
through every stage and shows what each produced, so the claims are checkable
rather than asserted.

Everything below comes from a real run. Reproduce it in one command:

```bash
python -m asep url_shortener
```

The committed copy is in [`examples/greenfield/`](../examples/greenfield/); file
references point there where the artifact is committed, and to
`runs/<id>/workspace/` where it is produced rather than stored.

---

## The input

> Build a scalable URL shortener service with APIs, persistence, and analytics.

One sentence. No schema, no acceptance criteria, no scale figures.

---

## 1 · Decomposed into an engineering problem

**Agent:** `requirement` · **Output:** `workspace/docs/requirements.md`

The sentence becomes **7 functional** and **5 non-functional** requirements, plus
**3 named ambiguities** — the part that matters, because it is what the sentence
did not say:

| | Question | Blocking | Resolution |
| --- | --- | --- | --- |
| AMB-001 | What read/write volume, over what corpus? | **yes** | default recorded |
| AMB-002 | Is creation authenticated, and by whom? | no | default recorded |
| AMB-003 | How long are click events retained? | no | default recorded |

`AMB-001` is blocking because it decides the storage engine and whether the
cache is in-process or shared — getting it wrong is a rewrite, not a tuning
exercise.

**Controlled autonomy, first instance.** By default the recorded default is
applied and the run continues, and every such default appears in the final
summary under *Assumptions this run made*. Told not to assume, it stops:

```bash
python -m asep url_shortener --no-assume     # halts, exits non-zero
```

That run is committed at [`examples/ambiguous/`](../examples/ambiguous/) — note
`"artifacts": {}` in its `run.json`. **It refuses to produce code it cannot
justify.** Answer and it continues from where it stopped, without repeating
work:

```bash
python -m asep url_shortener --resume <run-id> --answer 'AMB-001=5k rps, 200k links'
```

---

## 2 · Designed, two stages running concurrently

**Agents:** `architecture` ‖ `api_design`

Neither depends on the other — both consume the normalized requirement — so the
engine dispatches them together. Visible in the trace as `parallel_with`:

```json
{"type":"task_started","task_id":"T-002","data":{"parallel_with":["T-003"]}}
```

`architecture` is rejected if it declares no trade-offs: *a design with no
rejected alternatives has not been designed.* It produced four, each with an
accepted cost — for instance choosing random base62 codes over a counter,
because a counter makes every link enumerable, at the cost of collision retry.

`api_design` is rejected if any endpoint lacks a requirement id. Seven endpoints,
all traced. `openapi.yaml` is **derived** from that contract via
`contract.openapi()`, so the published spec cannot drift from what validation
enforces.

---

## 3 · Decomposed into executable graph nodes

**Agent:** `planner` · **Output:** `workspace/docs/plan.md`

The planner returns **tasks, not prose**, and the engine injects them into the
graph that is already running:

| ID | Task | Agent | Depends on | Reads | Writes |
| --- | --- | --- | --- | --- | --- |
| T-010 | Implement from architecture and contract | `implementation` | T-004 | `api_contract`, `architecture` | `artifacts_index` |
| T-020 | Generate an executable test suite | `test` | T-010 | `artifacts_index`, `api_contract` | `test_suite_index` |
| T-030 | Write the ADR and README | `documentation` | T-010 | `architecture`, `api_contract` | `documentation_index` |
| T-040 | Validate against the contract, run the suite | `validation` | T-020, T-030 | `artifacts_index` | `validation_report` |
| T-050 | Summarize for a reviewer | `summary` | T-040 | `requirement` | `run_summary` |

Because the plan is **data**, it is checked before execution: a task naming an
unregistered agent, or depending on something that does not exist, is rejected
here rather than discovered at dispatch.

`T-020` and `T-030` share a dependency and each other's absence, so they run
concurrently too.

**The graph started with 4 tasks and now has 9.** It is not a pipeline.

---

## 4 · Built, under a human gate

**Agents:** `implementation` ‖ then `test` ‖ `documentation`

```
??  T-010 needs approval (risk=medium)
```

**Controlled autonomy, second instance.** Analysis runs unattended; anything
that writes code stops for a decision. With `--approve` a human answers each
one, and a rejection stops everything downstream — asserted in
`test_engine.py::test_a_rejected_task_stops_everything_behind_it`.

Two independent limits apply here, and they are not the same thing:

- **Approval** decides *whether* the task runs.
- **`Workspace`** decides *where* its output can land — absolute paths, parent
  traversal and symlink escapes are refused before any write.
- On top of both, a list of operations no approval can authorise (`rm -rf /`,
  destructive DDL, `terraform destroy`).

The HTTP layer is **synthesised from the approved contract**, not stored: remove
an endpoint and its handler disappears, and the catch-all `/{code}` is
registered last because the generator sorts by path specificity — a correctness
rule the contract cannot express.

---

## 5 · Validated by something that did not write it

**Agent:** `validation` · **Output:**
[`validation-round-1.md`](../examples/greenfield/validation-round-1.md)

No model is consulted. The stage parses the files on disk, compares routes
against the approved contract, and **executes the generated suite in a
subprocess**.

It fails:

```
validation round 1 found 3 error(s); 1 are machine-repairable
```

| Check | Result |
| --- | --- |
| structure, syntax, guardrails, traceability | pass |
| **api_contract** | **fail** — 6/7 declared endpoints implemented |
| **tests** | **fail** — 34 passed, 2 failed |

The contract declares `GET /api/v1/analytics/{code}`; no route implements it.
Two generated tests fail on the same gap, found independently.

That gap is **emergent, not scripted**: the operation library has no
implementation for that endpoint, so the generator omitted it — and omitted
rather than stubbed, because a stub would satisfy the structural check while
lying about working.

---

## 6 · Repaired, then re-validated

One finding carries a `repair_hint`, so the engine turns it into **two new graph
nodes** and rewires what was waiting:

```
injected T-900-R1 and T-901-R1 into the running graph; moved T-050 behind T-901-R1
```

That rewiring is the part worth pausing on. `T-050` depended on `T-040`, so it
became ready the moment validation *finished* — including finishing by failing.
Without re-pointing it at the re-validation node it would have run beside the
repair and reported on a workspace being rewritten underneath it.

The repair is gated too (`?? T-900-R1 needs approval`), applies two anchored
edits, and is **not trusted**:
[`validation-round-2.md`](../examples/greenfield/validation-round-2.md) re-runs
every check.

```
validation passed after 1 repair round(s) (6 checks, 0 warning(s))
tests: 36 passed, 0 failed
```

A finding with no actionable hint would have escalated to a human instead, and
the repair budget is finite — a loop that is not converging stops.

---

## 7 · Reported for a reviewer

**Output:** [`summary.md`](../examples/greenfield/summary.md) ·
[`metrics.md`](../examples/greenfield/metrics.md) ·
[`validation-approach.md`](../examples/greenfield/validation-approach.md)

Assembled from the run's own state rather than generated, because a model asked
to summarize its own run rounds the edges off the parts a reviewer needs.

`summary.md` carries the implementation plan with each task's rationale, an
inventory of every artifact with the requirements it claims to cover, the
decisions and their accepted costs, the open risks, the verification results,
the assumptions — and a **Limitations** section built from what actually
happened, so it cannot read the same way regardless of outcome.

`validation-approach.md` states the test strategy and, for every check, **what
it cannot catch**.

---

## Verify it yourself

The claim is that the generated suite passes. Check it without the platform:

```bash
python -m asep url_shortener
cd runs/<run-id>/workspace && pytest -q      # 36 passed
```

And that the checks are not decoration — this breaks a passing workspace six
ways they were never written against, and requires each to be caught:

```bash
pytest -q tests/test_validation_is_independent.py
```

---

## What the run cost

From [`metrics.md`](../examples/greenfield/metrics.md), derived from the trace:

| Signal | Value |
| --- | --- |
| first-pass yield | no — needed one repair |
| tasks planned / injected | 9 / 2 |
| approvals requested / denied | 2 / 0 |
| validation rounds | 2 |
| artifacts / lines | 28 / ~1500 |

`first_pass_yield` is the leading indicator: pass/fail only reports the final
state, and the repair loop is good enough to rescue a mediocre run.
