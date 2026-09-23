# ASEP — an agentic SDLC orchestrator

Turns a software requirement into a reviewable engineering outcome: a runnable
project, a test suite that was actually executed, and a trace of every decision
taken to get there.

```bash
pip install -e ".[dev]"
python -m asep url_shortener
```

No API key needed. Takes about nine seconds.

---

## Watch it catch its own mistake

The run below builds a URL shortener from one sentence. The interesting part is
in the middle:

```
ok  T-001 normalized into 7 functional and 5 non-functional requirements; 3 ambiguity(ies)
ok  T-002 Modular monolith with cache-aside reads and an async analytics writer
ok  T-003 7 endpoint(s) v1.0.0: POST /api/v1/urls, GET /api/v1/urls/{code}, ...
ok  T-004 planned 5 task(s) for injection; T-020 + T-030 can run concurrently
??  T-010 needs approval (risk=medium)
ok  T-010 wrote 14 file(s), 723 lines
ok  T-040 round 1: fail — 6 check(s), 3 error(s); failed: api_contract, tests
!!  validation round 1 found 3 error(s); 1 are machine-repairable
      contract declares GET /api/v1/analytics/{code} but no route implements it
      failing test: tests/test_analytics.py::test_clicks_are_counted
++  injected T-900-R1 and T-901-R1 into the running graph; moved T-050 behind T-901-R1
ok  T-900-R1 applied 2 edit(s) across 1 file(s)
ok  T-901-R1 round 2: pass — 6 check(s), 0 error(s)
**  run succeeded — 36 tests passed
```

The implementation stage left out an endpoint the API contract declared. Nobody
told the system about that specific mistake:

- a check compared the routes in the AST against the approved contract and found
  one missing
- two generated tests failed on the same gap, independently
- the engine turned that finding into **two new nodes in a graph that was already
  executing** — a repair and a re-validation
- it moved the summary task behind the re-validation, so nothing reported on a
  workspace that was still being rewritten
- the repair applied two edits, and the re-run proved they worked

The whole thing is committed under [`examples/greenfield/`](examples/greenfield/)
— trace, reports, metrics. [`docs/walkthrough.md`](docs/walkthrough.md) follows
that run stage by stage.

## What makes it tick

**A graph that changes shape while it runs.** Tasks are nodes with dependencies;
independent ones execute concurrently. The planner injects the build stages it
decided on, and the validation stage injects repairs — into a graph that is
already executing. Every mutation is cycle-checked before it is accepted. That is
the bit a fixed pipeline cannot express: a late step rewriting the plan for an
earlier step and re-running it.

**Agents never call each other.** They read and write typed keys on a shared
blackboard, and every task declares which keys it reads and writes. The engine
refuses to start a task whose inputs are missing, and fails one that did not
write what it promised. Coordination becomes data you can inspect rather than a
call chain you have to trace.

**The thing that judges the work did not write it.** Validation runs no model at
all. It parses the files on disk, diffs routes against the contract, and executes
the generated suite in a subprocess. Findings that can describe their own fix
carry a repair hint; those are the ones the engine acts on. The rest escalate.

**Two separate limits on what an agent may do.** Approval decides *whether* a
task runs — analysis is unattended, anything writing code is gated. The workspace
sandbox decides *where* its output can land, rejecting absolute paths, parent
traversal and symlink escapes. On top of both sits a list of operations no
approval can authorise.

**It refuses to guess.** "Build a scalable URL shortener" does not say at what
scale. That ambiguity is named, marked blocking, and either answered by a human
or resolved by a default that appears in the final report. Run with `--no-assume`
and it stops rather than inventing an answer.

## Six kinds of work

Building a system, extending one, fixing it, restructuring it, testing it and
documenting it are different jobs, and the graph differs accordingly.

```bash
python -m asep fix_expiry_bug           # or: url_shortener, analytics_upgrade,
                                        # extract_service_layer, raise_test_coverage,
                                        # document_the_service
```

| Work | Shape of the run | What proves it worked |
| --- | --- | --- |
| Build | design → contract → build → test → docs → verify | the generated suite executes and passes |
| Enhancement | impact analysis first, then design the addition | change is additive; the old suite still passes |
| Bug fix | no design stage; **reproduce before repairing** | the regression test failed first, then passed |
| Refactor | no new tests — the old ones are the specification | the existing suite passes **unedited** |
| Test improvement | no architecture, no contract | coverage measurably up; no source file touched |
| Documentation | contract established so coverage is measurable | every published endpoint appears in a document |

### The bug fix is the one worth reading

The defect is real and pre-existing. The read path is cache-aside and the expiry
check sits only on the cache-miss branch, so once a link has been resolved the
cache keeps serving it long after it expired. The target's own 32 tests miss it,
because their expiry test never resolves a link before it expires.

```
ok  T-010 wrote a regression test that resolves a link before it expires
ok  T-020 defect reproduced: 2 regression test(s) fail against the unfixed code
ok  T-030 cached entries now carry the link's expiry
**  run succeeded — 35 passed, 0 failed
```

`T-020` is a node whose job is to **require a failure**. If the regression test
passes against the unfixed code, the run stops — either the defect is not what
was reported or the test does not exercise it, and both need a person.

### Brownfield work reads the actual codebase

Four of the six start from
[`sample_codebase/url_shortener_legacy/`](sample_codebase/url_shortener_legacy/),
a working service with 32 passing tests. Before anything is designed, the AST is
parsed to work out what the change touches:

```
ok  T-002 scanned 15 module(s) across 9 layer(s); 5 matched the requirement,
          2 reached through imports, 3 concept(s) have no home yet
```

That answer is derived, not recalled. Requirement vocabulary is matched against
symbols, routes and tables, then propagated along the import graph in both
directions — so a file nobody named, but which a changed file imports, still
shows up as at risk. Every file it reports carries the reason it was selected.
Ask it about caching instead of analytics and you get a different answer.

The original directory is never modified; each run works on a copy.

## Try to break it

The claim is that the generated suite passes. Check it without the platform:

```bash
python -m asep url_shortener
cd runs/<run-id>/workspace && pytest -q       # 36 passed
```

The claim that the checks mean something is harder to take on trust, so there is
a test that attacks them. It takes a workspace that has already passed, breaks it
in six ways the checks were never written against, and requires each to be
caught:

```bash
pytest -q tests/test_validation_is_independent.py
```

The useful case in there is changing a redirect from 302 to 301. It parses fine
and satisfies the contract — only executing the suite finds it. That is why test
execution is not optional for a trustworthy verdict.

## Where the model fits

Every model call goes through one method that returns a validated Pydantic object
or raises. Agents never see raw text, so a malformed response is a typed failure
the orchestrator retries with the validation error attached, rather than a
parsing bug inside an agent.

The default provider is deterministic, which is what makes the whole thing
runnable with no key and no network. It is not a stub returning empty objects —
it answers with the shape a competent model would, so the orchestration,
validation and repair all execute against real content.

Two things are worth being precise about:

- `app/api/routes.py` is **synthesised from the approved contract**. Remove an
  endpoint and its handler disappears; change a status code and the decorator
  follows; list the endpoints in any order and the catch-all is still registered
  last, because the generator sorts by path specificity. It reproduces the
  hand-written module it replaced byte for byte.
- The modules behind that surface — storage, cache, the analytics writer — are
  library code. A contract says `POST /api/v1/urls` returns 201 or 409, not how
  to allocate a short code. That split is stated rather than blurred.

A real model goes behind the same agents with `--mode openai`. The deterministic
provider **refuses** a requirement it was not scripted for rather than answering
it wrongly.

## What a run leaves behind

```
runs/<run-id>/
├── trace.jsonl              every event, flushed as it happens
├── run.json                 tasks, artifacts, approvals, blackboard
├── graph.mmd                the final graph, including injected nodes
├── summary.md               the run, written for a reviewer
├── metrics.json / .md       derived from the trace
├── validation-approach.md   the test strategy, and what each check cannot see
├── validation-round-N.md    what failed, and what the repair fixed
└── workspace/               the deliverable
```

`summary.md` carries the plan and why each task exists, every artifact with the
requirements it claims to cover, the decisions and their costs, the open risks,
and a limitations section assembled from what actually happened — so it cannot
read the same way regardless of outcome.

`first_pass_yield` in the metrics is the number to watch across runs: did the run
reach a passing verdict without repairing itself? It moves before pass/fail does,
because the repair loop is good enough to rescue a mediocre run.

## Command line

```bash
python -m asep --list                        # scenarios and what they do
python -m asep url_shortener --approve       # decide every gated task yourself
python -m asep url_shortener --no-assume     # refuse to guess; halt and ask
python -m asep url_shortener --resume <id> --answer 'AMB-001=...'
python -m asep url_shortener --inject-failure implementation   # watch it recover
python -m asep url_shortener --json          # trace as JSON lines
```

## Layout

```
asep/
├── orchestration/   the engine, the graph, the policy layer, the trace recorder
├── agents/          twelve narrow agents, each with one job
├── validation/      six checks by default, three more a scenario can opt into
├── models/          typed domain: requirements, design, tasks, artifacts, findings
├── providers/       the Provider interface; OpenAI and deterministic backends
├── tools/           workspace sandbox, AST scanner, impact analysis, test runner
└── scenarios/       six kinds of work; only the opening graph is declared

sample_codebase/     the brownfield target, with one real latent defect
examples/            three committed runs: greenfield, brownfield, ambiguous
tests/               328 tests, 96% coverage of asep/
```

## Running the tests

```bash
pytest -q                      # everything, ~3 minutes
pytest -q -m "not slow"        # ~9s, for the edit-run loop
ruff check asep tests scripts
```

`slow` means "executes a whole orchestration run", which includes running the
generated suite in a subprocess. CI runs all of it on Ubuntu and Windows across
Python 3.10 and 3.12, plus a 90% coverage gate, a dependency audit, and all six
scenarios end to end.

## Limits

- **The generated service is a prototype.** sqlite3 will not sustain the
  throughput its own architecture document targets; the repository interface is
  the seam where Postgres replaces it. That trade-off and its cost are recorded
  in the generated ADR rather than hidden.
- **The repair loop closes a narrow class of defects** — findings specific enough
  to describe their own fix. Everything else escalates, which is correct, but it
  is not a general bug fixer.
- **Impact analysis is a lexical heuristic**, not comprehension. It matches
  requirement vocabulary against the import graph. Every file it lists says why,
  so a wrong answer is visible rather than silent.
- **Static route matching is name-based.** It does not resolve the Python import
  system, so two modules sharing a basename are not told apart.
- **Approval is per-task, not per-diff.** A reviewer approves that implementation
  may run, not the lines it will write.

## Further reading

- [`docs/walkthrough.md`](docs/walkthrough.md) — one requirement traced through
  every stage, citing the artifacts a committed run produced
- [`docs/architecture.md`](docs/architecture.md) — components, execution model,
  and the design decisions with what each one costs
- [`docs/orchestration.md`](docs/orchestration.md) — the execution loop, the
  blackboard, the error taxonomy, how the repair loop rewires the graph
- [`docs/testing.md`](docs/testing.md) — how correctness is established, and where
  that evidence stops
- [`docs/operations.md`](docs/operations.md) — the DevOps and SRE posture
- [`docs/extending.md`](docs/extending.md) — adding a check, an agent, a scenario
  or a provider
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — branching, commits, what CI enforces
