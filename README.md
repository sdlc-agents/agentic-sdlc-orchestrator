# ASEP — Agentic Software Engineering Platform

A requirement goes in as one sentence of English. What comes out is a runnable
project, a test suite that was actually executed, and a trace of every decision
made along the way — including the ones a human was asked to approve and the
defect the platform found in its own output and fixed.

```bash
pip install -e ".[dev]"
python -m asep url_shortener
```

No API key required. The default provider is deterministic, which is explained
in [What is real and what is simulated](#what-is-real-and-what-is-simulated).

## Setup and evaluation

Python 3.10 or newer. Nothing to configure and no services to start.

```bash
git clone <this repo> && cd agentic-software-engineering-platform
pip install -e ".[dev]"          # pydantic, fastapi, httpx, pytest, ruff
python -m asep --list            # what it can be asked to do
```

Or in a container, which is the safer option — the validation stage executes
generated code, and in a container it runs as a non-root user with nothing
mounted rather than as you:

```bash
docker build -t asep .
docker run --rm asep url_shortener
```

**Evaluate it in four commands**, in the order a sceptical reviewer would:

```bash
# 1. Watch the mandatory use case run end to end.
python -m asep url_shortener

# 2. Check the deliverable yourself, outside the platform.
cd runs/<run-id>/workspace && pytest -q     # 36 passed
cd -

# 3. Confirm the checks are not decoration — break the output and watch them fail.
pytest -q tests/test_validation_is_independent.py

# 4. Confirm it refuses to guess.
python -m asep url_shortener --no-assume    # halts, exits non-zero
```

Step 2 is the one worth doing. The platform claims the generated suite passes;
step 2 is you verifying that without taking its word for it.

Then read [`runs/<run-id>/summary.md`](examples/greenfield/summary.md) — the
implementation plan and why each task exists, every artifact with the
requirements it claims to cover, the decisions and what they cost, the open
risks, and what the run does **not** establish.

Everything else: `pytest -q` (287 tests, ~150s), `ruff check asep tests scripts`.

---

## The problem this is built around

Getting a language model to emit a plausible file is not the hard part any more.
The hard part is everything around it: knowing what was actually asked, noticing
what the request failed to say, deciding what to build before building it,
keeping the pieces consistent with each other, finding out that the result is
wrong, and fixing it — without a person having to hold the whole thing together
by hand.

A single agent asked to design, build, test and sign off produces work that is
internally consistent and externally unverified. It agrees with itself. That is
the failure mode this platform is designed against, and most of what follows is
a consequence of taking it seriously.

## What a run actually does

```
  **  run 0ee33fc1 (mock mode, 4 tasks)
  ok  T-001 normalized into 7 functional and 5 non-functional requirements; 3 ambiguity(ies), 3 resolved
  ok  T-002 Modular monolith with cache-aside reads and an asynchronous analytics writer: 7 component(s), 4 trade-off(s), 4 risk(s), high severity: RISK-002, RISK-003
  ok  T-003 7 endpoint(s) v1.0.0: POST /api/v1/urls, GET /api/v1/urls/{code}, ...
  ok  T-004 planned 5 task(s) for injection; T-020 + T-030 can run concurrently
  ??  T-010 needs approval (risk=medium)
  ok  T-010 wrote 14 file(s), 723 lines
  ok  T-020 wrote 4 file(s), 202 lines
  ok  T-030 wrote 2 file(s), 83 lines
  ok  T-040 round 1: fail — 6 check(s), 3 error(s), 0 warning(s); failed: api_contract, tests
  !!  validation round 1 found 3 error(s); 1 are machine-repairable
        findings: contract declares GET /api/v1/analytics/{code} but no route implements it
        findings: failing test: tests/test_analytics.py::test_clicks_are_counted
  ++  injected T-900-R1 and T-901-R1 into the running graph; moved T-050 behind T-901-R1
  ok  T-900-R1 applied 2 edit(s) across 1 file(s) — Added the analytics route the contract declares
  ok  T-901-R1 round 2: pass — 6 check(s), 0 error(s), 0 warning(s)
  ok  validation passed after 1 repair round(s)
  **  run succeeded in 7472ms
```

The interesting part is the middle. The implementation stage omitted an endpoint
that the API contract declared. Nothing in the system was told about that
specific mistake:

- the **contract check** compared the routes in the AST against the approved
  contract and found one missing,
- the **generated test suite** failed on it independently,
- the engine turned the finding into **two new nodes in a graph that was already
  running** — a repair and a re-validation,
- it **moved the summary task behind the re-validation**, so nothing reported on
  a workspace that was still being rewritten,
- the repair applied two edits, and the re-run proved they worked: **36 tests,
  0 failures.**

Three full runs — trace, reports and console output — are committed under
[`examples/`](examples/): [greenfield](examples/greenfield/),
[brownfield](examples/brownfield/), and an
[ambiguous requirement](examples/ambiguous/) that halts rather than guessing.

## Six kinds of engineering work

Real requirements are not all "build me a thing". Extending a system, fixing it,
restructuring it, testing it and documenting it are different jobs with
different evidence of success, and a platform that runs one pipeline for all of
them has only really implemented the first.

```bash
python -m asep url_shortener           # build a URL shortener from nothing
python -m asep analytics_upgrade       # add a feature to a service that exists
python -m asep fix_expiry_bug          # fix a real latent defect
python -m asep extract_service_layer   # refactor without changing behaviour
python -m asep raise_test_coverage     # put untested modules under test
python -m asep document_the_service    # write the reference, ADRs and runbook
```

The graphs differ because the jobs differ:

| Work | Shape of the run | What proves it worked |
| --- | --- | --- |
| **Build** | design → contract → build → test → docs → verify | the generated suite executes and passes |
| **Enhancement** | impact analysis first, then design the addition | the change is additive; the old suite still passes |
| **Bug fix** | no design stage; **reproduce before repairing** | the regression test failed first, then passed |
| **Refactor** | no new tests — the old ones are the specification | the existing suite passes **unedited** |
| **Test improvement** | no architecture, no contract | measured coverage went up; no source file changed |
| **Documentation** | contract established so coverage is measurable | every published endpoint appears in a document |

Each declares the check that makes its own kind of success measurable
(`behaviour_preserved`, `test_coverage`, `documentation`), so the verdict fits
the work instead of the work being bent to fit one verdict.

### The bug fix is the one worth reading

The defect is real and pre-existing, not planted. The read path is cache-aside
and the expiry check sits only on the cache-miss branch, so once a link has been
resolved once the cache answers on its behalf long after the link expired. The
target's own 32 passing tests miss it, because their expiry test never resolves
a link before it expires.

```
  ok  T-010 wrote 1 file(s) — a regression test that resolves a link before it expires
  ok  T-020 defect reproduced: 2 regression test(s) fail against the unfixed code
  ??  T-030 needs approval (risk=medium)
  ok  T-030 wrote 1 file(s) — cached entries now carry the link's expiry
  ok  T-040 round 1: pass — 6 check(s), 0 error(s)
  **  run succeeded — 35 passed, 0 failed
```

`T-020` is a graph node whose job is to **require a failure**. If the regression
test passes against the unfixed code, the run stops: either the defect is not
what was reported or the test does not exercise it, and both need a person. A
bug-fix run that skips this can produce a test that was green all along, a fix
that changed nothing, and a green final verdict — a sequence indistinguishable
from success that contains no evidence of anything.

### Brownfield work starts from a real codebase

Four of the six start from
[`sample_codebase/url_shortener_legacy/`](sample_codebase/url_shortener_legacy/),
a working service with 32 passing tests. Before anything is designed, a
static-analysis stage parses that tree and computes what the change touches and
what it might break:

```
  ok  T-002 scanned 15 module(s) across 9 layer(s); 8 impacted, 1 API(s) affected,
           blast radius 10 file(s), 7 data flow(s) run through changed code
```

That answer comes from the AST, not from a model. Which modules import which is
a fact, and a design built on a recollection of it sends the implementation
stage after the wrong files — a mistake that only surfaces three steps later.

It reports more than a file list. The layers, entry points and data flows are
reconstructed from the import graph, and each flow is marked according to
whether it passes through something the change touches:

| From | To | Direction | Touched |
| --- | --- | --- | --- |
| `app/api/routes.py` | `app/repository.py` | transport → persistence | **changed** |
| `app/repository.py` | `app/db.py` | persistence → storage | **changed** |
| `app/main.py` | `app/cache.py` | composition → cache | no |

A file is safe or dangerous to modify depending on what runs through it, and
that is a property of the architecture rather than of the file.

The existing suite is the constraint every brownfield change is measured
against, and it is still passing at the end of all four. The original directory
is never modified: each run works on a copy.

## How it works

```
requirement ──► architecture ──┐
    │                          ├──► planner ──► [graph grows here]
    └──────────► api design ───┘                      │
                                                      ▼
                          implementation ──► tests ───┬──► validation ──► summary
                                   └──────► docs ─────┘        │
                                                               ▼
                                              repair ◄── findings with repair hints
                                                 └──► re-validation
```

Five ideas do the work.

**A mutable task graph, not a pipeline.** Tasks are nodes with dependencies.
Independent nodes run concurrently. Crucially the graph can be *modified while it
is executing* — the planner injects the build stages it decided on, and the
validation stage injects repairs. Every mutation is checked for cycles before it
is accepted, and rejected if it would create one. A fixed pipeline cannot express
"a late step rewrites the plan for an earlier step and re-runs it", which is
exactly what fixing a defect requires.

**A blackboard, not a call chain.** Agents never call each other. They read and
write typed keys in shared state. Coordination becomes inspectable data rather
than an implicit chain of calls, and any agent can be run in isolation.

**Declared contracts, enforced.** Every task declares the keys it reads and
writes. The engine refuses to start a task whose inputs are absent, and fails a
task that did not write what it promised. A planning mistake becomes an explicit
error instead of an agent improvising around missing input.

**Verification by something other than the author.** The validation stage runs no
model at all. It parses the files on disk, compares routes against the contract,
screens for forbidden operations, and executes the generated suite in a
subprocess. An agent cannot talk its way past any of it. Findings that can
describe their own fix carry a `repair_hint`, and those are the ones the engine
can act on; findings that cannot are escalated to a human rather than looped on.

**Bounded autonomy.** Two independent limits:

| | |
| --- | --- |
| **Approval** decides *whether* work happens | Analysis runs unattended. Anything that writes code — implementation, repair — is gated. `--approve` puts a human on every gate; `--threshold` moves the line. |
| **The workspace** decides *where* it can land | Every write goes through one object that rejects absolute paths, parent traversal, and anything resolving outside the root. |

On top of both sits a list of operations no approval can authorise — `rm -rf /`,
destructive DDL, `terraform destroy`, `kubectl delete`, piping curl into a shell.
Content matching them never reaches disk.

That last rule has teeth. The shipped migration originally carried a commented-out
`DROP TABLE` rollback; the guardrail refused it, and the migration now describes
its rollback in prose instead. Conversely, the `shutdown` pattern used to match
the English word and refused a design document that said "flush on shutdown" —
a guardrail that fires on documentation is a guardrail that gets switched off, so
it was narrowed to the command form. Both cases are in the test suite.

### Handling ambiguity

"Build a scalable URL shortener with analytics" does not say what scale, whether
creation is authenticated, or how long to keep click data. The requirement stage
names each of these, decides which are blocking, and attaches an explicit default
to the rest.

```bash
python -m asep url_shortener --no-assume
#   ??  run halted: blocking ambiguities require a human answer
#       AMB-001: What read and write volume must this sustain, and over what
#                link corpus? (matters because it decides the storage engine and
#                whether the cache is in-process or shared)

python -m asep url_shortener --answer "AMB-001=5k redirects/sec over 200k links"
```

By default the recorded defaults are applied and the run continues — but every
one of them appears in the run summary under *Assumptions this run made*, so a
reviewer sees what was guessed. A default that is not written down is just a
silent guess.

## What is real and what is simulated

Worth being precise about, because it is the first question a reader should ask.

**Real.** The orchestration engine, the dependency graph and its mutation, the
concurrency, the contract enforcement, retry classification, the approval gates,
the workspace sandbox, the forbidden-operation screening, the static analysis of
the brownfield codebase, all six validation checks, the repair loop, and the
execution of the generated test suite. When the run says 36 tests passed, pytest
ran in a subprocess and 36 tests passed.

**Deterministic.** The default `--mode mock` provider returns fixed, high-quality
content for each agent instead of calling a model. This is not a stub that
returns empty objects — it answers with the same shape a competent model would,
which is what makes the rest meaningful. It also deliberately gets one thing
wrong: the implementation response omits an endpoint the contract requires.
Nothing downstream special-cases that gap. A demo where everything succeeds on
the first pass would hide the part of the system worth looking at.

`--mode openai` swaps in a real model through the same `Provider` interface.
Every model call in the system goes through one method that returns a validated
Pydantic object or raises — agents never see raw text, so a malformed response is
a typed failure the orchestrator retries with the validation error attached,
rather than a parsing bug inside an agent.

```bash
cp .env.example .env    # add OPENAI_API_KEY
python -m asep url_shortener --mode openai --model gpt-4o-mini
```

## Output

Each run writes a directory:

```
runs/<run-id>/
├── trace.jsonl              every event, flushed as it happens
├── run.json                 tasks, artifacts, approvals, timings
├── graph.mmd                the final graph, including injected nodes
├── summary.md               the run, written for a reviewer
├── validation-approach.md   the test strategy, and what each check cannot see
├── validation-round-1.md    what failed, and which findings were repairable
├── validation-round-2.md    what the repair actually fixed
└── workspace/               the deliverable
```

`summary.md` is the reviewer-facing document: the implementation plan and why
each task exists, an inventory of every artifact with the requirements it claims
to cover, the decisions and what they cost, the open risks, the verification
results, the assumptions the run made — and a **limitations** section assembled
from what actually happened, so it does not read the same way whatever the
outcome was.

`validation-approach.md` states the test strategy (which tests are unit, which
are integration, what each exercises), what each check establishes, and — the
part that matters — what each check *cannot* catch.

`trace.jsonl` is flushed as events occur, so a run killed halfway still leaves a
readable record of how far it got. The trace is the evidence that the system
orchestrated rather than merely generated: every scheduling decision, approval,
failure and graph mutation is replayable without re-running a model.

## Command line

```bash
python -m asep --list                        # scenarios and what they do
python -m asep url_shortener                 # greenfield
python -m asep analytics_upgrade             # brownfield
python -m asep url_shortener --approve       # decide every gated task yourself
python -m asep url_shortener --threshold low # gate everything, including analysis
python -m asep url_shortener --no-assume     # refuse to guess; halt and ask
python -m asep url_shortener --json          # trace as JSON lines, for piping
python -m asep url_shortener --no-tests      # skip executing the generated suite
python -m asep url_shortener --inject-failure implementation   # watch it retry and recover
python -m asep url_shortener --resume <run-id> --answer 'AMB-001=...'  # continue a halted run
```

## Further reading

- [`docs/architecture.md`](docs/architecture.md) — components, execution model,
  control flow, and the key design decisions with what each one costs
- [`docs/orchestration.md`](docs/orchestration.md) — the execution loop, the
  blackboard keys, the error taxonomy, and how the repair loop rewires the graph
- [`docs/testing.md`](docs/testing.md) — how correctness is established, and the
  known limitations and trade-offs
- [`docs/extending.md`](docs/extending.md) — adding a check, an agent, a scenario
  or a provider
- [`examples/`](examples/) — three committed runs: greenfield, brownfield, and an
  ambiguous requirement that halts

## Layout

```
asep/
├── models/          typed domain: requirements, design, tasks, artifacts, findings
├── orchestration/   the engine, the graph, the policy layer, the trace recorder
├── agents/          twelve narrow agents, each with one job
├── validation/      six default checks plus three the scenarios opt into
├── providers/       the Provider interface, the OpenAI and deterministic backends
│   └── scripts/     deterministic content, one module per kind of work
├── tools/           workspace sandbox, AST scanner, subprocess test runner
└── scenarios/       the opening task graphs; everything else is injected at runtime

sample_codebase/     the brownfield target: a working service, 32 passing tests,
                     and one real latent defect
examples/            a committed run record
scripts/             regenerates the brownfield sample from the blueprint
tests/               287 tests, 96% coverage of asep/
```

## Tests

```bash
pytest -q                      # everything, ~150s
pytest -q -m "not slow"        # ~9s, for the edit-run loop
pytest -q -m "not slow"        # unit only, ~5s
ruff check asep tests scripts
```

96% line coverage of `asep/` — `coverage run --source=asep -m pytest && coverage report`.

The slow ones are the end-to-end runs, and they are slow because they are real:
each executes the generated suite in a subprocess. They assert on the shape of
the run — what was gated, what failed first, what repaired it — rather than on
generated prose.

Several tests exist because they caught something. `importers_of` silently
matched nothing for `from .module import Symbol`, which is how most of a Python
codebase imports, so every dependency edge in the brownfield impact analysis was
empty. The concern map was keyed on a label rather than a module name, so the
redirect handler — the one file on the latency-critical path — was never flagged
as impacted. Both are pinned now.

## Limits

Worth stating plainly, since a system that only describes what works is not
useful to whoever inherits it.

- **The generated service is a prototype.** sqlite3 will not sustain the
  throughput its own architecture document targets; the repository interface is
  the seam where Postgres replaces it. That trade-off, and the cost it accepts,
  is recorded in the generated ADR rather than hidden.
- **The repair loop closes a narrow class of defects** — findings specific enough
  to describe their own fix. Anything else escalates, which is correct, but it
  means the loop is not a general bug fixer.
- **`test_coverage` counts imports, not lines.** It answers "does anything test
  this module at all", which is the useful signal when the answer is no. It is
  not a substitute for a coverage tool.
- **`behaviour_preserved` compares file digests.** It catches an edited or
  deleted test, not a test weakened in a way that leaves the bytes the same
  length — nothing does, short of running the old suite against the new code.
- **The impact-analysis concern map is domain-specific.** It knows what a click
  analytics feature touches. A different change needs a different map, or a model
  call to build one.
- **Static route matching is name-based.** It does not resolve the Python import
  system, so two modules with the same basename in different packages are not
  told apart.
- **Approval is per-task, not per-diff.** A reviewer approves that
  implementation may run, not the specific lines it will write.
- **Impact analysis is a heuristic**, not a semantic understanding of the change.
  It matches requirement vocabulary against symbols, routes and tables, then
  propagates along the import graph. Every file it lists says why it is there,
  so a wrong answer is visible rather than silent.
