# The orchestration layer

Written for someone reading the engine for the first time, or deciding whether
its guarantees are the ones they need. The README describes what the platform
does; this describes how the engine makes it hold.

## The execution loop

The engine does not walk a list of stages. It loops:

```python
while True:
    ready = graph.ready()              # pending tasks whose dependencies all succeeded
    if not ready:
        if skip_unreachable(): continue  # mark tasks stranded behind a failure
        break
    approved = gate(ready)             # sequential: approvals resolve before concurrency
    dispatch(approved)                 # concurrent: independent tasks run together
    react_to_validation()              # may add nodes to the graph
```

Three properties follow from the shape.

**The graph is re-read every iteration**, so anything injected during the
previous pass is picked up without special handling. This is what lets the
planner decide the build stages at runtime and the validation stage schedule
repairs.

**Approvals are resolved before dispatch, sequentially.** Concurrency would make
the prompts interleave, and a reviewer cannot answer two questions at once.

**`skip_unreachable` runs only when nothing is ready.** Tasks stranded behind a
failure are marked transitively, so the loop terminates rather than spinning on
work that can never become ready.

## The blackboard

Agents never call each other. They read and write keys in `RunState.blackboard`.

| Key | Written by | Holds |
| --- | --- | --- |
| `requirement` | requirement | the normalized problem, with ambiguities and assumptions |
| `impact_analysis` | codebase | what an existing codebase change touches (brownfield) |
| `codebase_summary` | codebase | module, route and table counts |
| `architecture` | architecture | components, data flows, trade-offs, risks |
| `api_contract` | api_design | endpoints, each traced to a requirement id |
| `work_plan` | planner | the decomposition that was injected into the graph |
| `artifacts_index` | implementation, repair | path → id, hash, line count, coverage |
| `test_suite_index` | test | the generated suite |
| `documentation_index` | documentation | ADRs and README |
| `validation_report` | validation | the current round's verdict |
| `validation_rounds` | validation | how many rounds have run |
| `open_findings` | *the engine* | repairable findings handed to the repair agent |
| `final_validation_report` | *the engine* | the verdict the run ended on |
| `repair_summary` | repair | edits applied, and the finding each one addresses |
| `run_summary` | summary | the reviewer-facing report |

Two of these are written by the engine rather than by an agent, and that is the
point: `open_findings` is how a finding produced by one stage reaches a stage
that did not exist when it was produced.

## Contracts

Each task declares `reads` and `writes`. The engine enforces both:

- **Before dispatch** — a task whose `reads` are not all present raises
  `DependencyError` and the agent is never invoked. An agent that cannot get its
  input should not be given the chance to improvise around it.
- **After the agent returns** — a task that did not write everything in `writes`
  raises `ContractError`.

Declared contracts are also what make the plan checkable. The planner validates
that every task names a registered agent and depends only on tasks that exist,
so a bad plan fails at planning time rather than at dispatch.

## The error taxonomy

The engine never inspects an error message. The class decides the recovery:

| Error | Retryable | Means |
| --- | --- | --- |
| `TransientError` | yes | provider timeout, rate limit, flaky tool |
| `ContractError` | yes | output violated its schema or contract |
| `DependencyError` | no | a task ran without its declared inputs — a planning defect |
| `GuardrailViolation` | no | the agent attempted something policy forbids |
| `FatalError` | no | unrecoverable; escalate |
| `ClarificationRequired` | — | not a failure; the run halts and waits for a person |

A retry is not a repeat. The failing message is attached to the next attempt's
input, so the second try is informed rather than identical.

`ContractError` being retryable is the load-bearing one: it is how a model that
returned malformed output gets a second chance with the validation error in
front of it, instead of the run dying on a parsing problem.

## The repair loop

When a validation report comes back failing, the engine decides between three
outcomes:

1. **No repairable findings** — nothing carries a `repair_hint`. Escalate. A
   defect no agent can act on is not improved by trying again.
2. **Repair budget exhausted** — `max_repair_rounds` reached. Escalate. An
   unbounded loop is worse than an honest stop.
3. **Otherwise** — inject a repair task and a re-validation task, put the
   repairable findings on the blackboard under `open_findings`, and continue.

The repair is not trusted. The re-validation node that follows it is what decides
whether the defect is actually gone, and a repair that fixed nothing fails the
next round.

One subtlety is easy to miss. Tasks that already depended on the validation node
become ready the moment it *finishes* — including when it finishes by failing.
They would then run concurrently with the repair that failure triggered, and
report on a workspace being rewritten underneath them. So when a repair round is
injected, every pending dependent of the old validation node is re-pointed at the
new one:

```
before:  T-040 (validation) ──► T-050 (summary)

after:   T-040 ──► T-900-R1 (repair) ──► T-901-R1 (re-validate) ──► T-050
```

`TaskGraph.add_dependency` checks for cycles and reverts the edge if adding it
would create one.

## Events

Every decision is an event, appended to `RunState.events` and flushed to
`trace.jsonl` as it happens — so a run killed halfway still leaves a readable
record.

`run_started`, `run_finished`, `task_started`, `task_succeeded`, `task_failed`,
`task_retried`, `task_skipped`, `approval_requested`, `approval_granted`,
`approval_denied`, `validation_passed`, `validation_failed`, `repair_scheduled`,
`graph_mutated`, `escalated`, `clarification_requested`.

Events carry structured data alongside the message: which tasks a task ran
concurrently with, which keys it wrote, which findings a validation round
produced, which tasks a repair round rewired.

## Run outcomes

| Status | When |
| --- | --- |
| `succeeded` | nothing failed, nothing was rejected, validation passed |
| `failed` | a task failed or was stranded, or validation ended failing |
| `rejected_by_human` | a human declined a gated task |
| `needs_clarification` | a blocking ambiguity had no answer |

`needs_clarification` is a deliberate outcome, not an error. The run halts with
the questions in the trace, and no task is left recorded as in flight — they go
back to pending, because the work is waiting on a person rather than broken.
