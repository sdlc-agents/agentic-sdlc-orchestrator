# Example runs

Three committed runs, one per kind of input a reviewer should see. Each folder
holds the full evidence: the console output, every event in order, the reports,
and the final graph including nodes that were injected while it ran.

Regenerate all three with `python scripts/generate_examples.py`. Workspaces are
not committed — they are large and reproducible by running the command each
example prints at the top of its `console.txt`.

| Example | Input | Outcome |
| --- | --- | --- |
| [`greenfield/`](greenfield/) | *"Build a scalable URL shortener service with APIs, persistence, and analytics."* | succeeded — 36 tests pass after one repair round |
| [`brownfield/`](brownfield/) | *"Add click analytics to the existing URL shortener without slowing down redirects or breaking the current test suite."* | succeeded — 36 tests pass against a real codebase |
| [`ambiguous/`](ambiguous/) | the greenfield requirement, run with `--no-assume` | **halted** — refused to guess at a blocking question |

---

## Greenfield — `python -m asep url_shortener`

Nothing exists. One sentence has to become a runnable service.

**Task decomposition.** The requirement becomes 7 functional and 5
non-functional requirements with 3 named ambiguities. The planner turns the
approved design into graph nodes — `T-010` implementation, `T-020` tests,
`T-030` documentation, `T-040` validation, `T-050` summary — each declaring the
blackboard keys it reads and writes. See the *Implementation plan and rationale*
section of [`summary.md`](greenfield/summary.md), which records why each task
exists rather than only what it did.

**Multi-step orchestration.** The run starts with 4 tasks and finishes with 11.
Five are injected by the planner at runtime; two more by the validation stage.
`T-002` and `T-003` run concurrently, and so do `T-020` and `T-030` — visible in
the `parallel_with` field on `task_started` events in
[`trace.jsonl`](greenfield/trace.jsonl).

**Output validation.** [`validation-round-1.md`](greenfield/validation-round-1.md)
fails: the implementation omitted an endpoint the contract declares, and two
generated tests fail on it independently. One finding carries a repair hint, so
the engine injects a repair and a re-validation, and moves `T-050` behind the
re-validation so nothing reports on a workspace still being rewritten.
[`validation-round-2.md`](greenfield/validation-round-2.md) passes with 36 tests
executed.

## Brownfield — `python -m asep analytics_upgrade`

A working service already exists, with 32 passing tests that must still pass.

**Task decomposition.** An extra opening node the greenfield graph has no use
for: impact analysis. Before anything is designed, the AST is parsed to find
what the change touches — 8 impacted files, 10 in the blast radius, the data
flows that run through changed code, and which published API the change reaches.

**Multi-step orchestration.** The design stage reads the impact analysis rather
than guessing, and the plan that follows is shaped by it. Same repair loop as
greenfield.

**Output validation.** The existing suite is the constraint, and it still
passes. [`validation-approach.md`](brownfield/validation-approach.md) states the
test strategy — which tests are unit, which are integration, what each exercises
— and what each check *cannot* catch.

## Ambiguous — `python -m asep url_shortener --no-assume`

The same requirement, with the platform told not to apply its own defaults.

It halts. `AMB-001` — *"What read and write volume must this sustain, and over
what link corpus?"* — is marked blocking, because it decides the storage engine
and whether the cache is in-process or shared; getting it wrong is a rewrite,
not a tuning exercise. No code is generated, and the run exits non-zero.

This is the deliberate case. By default the platform applies a recorded default
and continues, and every such default appears in the summary under *Assumptions
this run made* — but a default that cannot be defended should stop the run
instead of quietly shaping the design. Answer and re-run:

    python -m asep url_shortener --answer "AMB-001=5k redirects/sec over 200k links"

The answer is then recorded with `source: human` rather than `source: default`,
so a reviewer can tell which decisions had a person behind them.

**No task is left recorded as in flight.** The halted task goes back to pending,
because the work is waiting on an answer rather than broken — visible in
[`run.json`](ambiguous/run.json).
