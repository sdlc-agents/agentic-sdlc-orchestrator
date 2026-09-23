# Testing approach

How this project establishes that it works, and where that evidence stops.

There are two distinct things being tested and it is worth keeping them apart:
the **platform** (does the orchestration behave correctly?) and the **output**
(is what it generated any good?). They need different evidence.

## Testing the platform

287 tests, 96% line coverage of `asep/`.

```bash
pytest -q                     # everything, ~150s
pytest -q -m "not slow"       # ~9s, for the edit-run-edit loop
coverage run --source=asep -m pytest && coverage report
```

`slow` means "executes a whole orchestration run", which includes running the
generated suite in a subprocess. The marker follows what a test *does*, not
which file it sits in — the CLI tests that drive a full run are marked slow, and
mislabelling them once turned the fast loop into a 30-second one.

| Suite | Establishes |
| --- | --- |
| `test_graph.py` | The DAG stays correct while being mutated: cycles rejected and rolled back, transitive blocking, topological levels |
| `test_engine.py` | Contracts enforced, failures classified rather than retried blindly, rejection stops downstream work, the graph grows safely |
| `test_guardrails.py` | The workspace refuses traversal and absolute paths; forbidden operations are caught; prose is *not* falsely caught |
| `test_validation.py` | Each check fires on the defect it exists for |
| `test_agents.py` | Agents refuse bad input — an architecture with no trade-offs, an untraceable endpoint, a plan naming an unregistered agent |
| `test_tools.py` | Route extraction, import resolution, test-runner parsing, timeout enforcement |
| `test_scenarios.py` | Greenfield and brownfield end to end, asserting on the *shape* of the run |
| `test_work_kinds.py` | The other four kinds of work, and that the graph genuinely differs per kind |
| `test_deliverables.py` | The documents a reviewer is handed actually say something |
| `test_cli.py` | Exit codes CI depends on, argument parsing, and that the observer never crashes while reporting a finished run |
| `test_openai_provider.py` | The live-model path — prompt construction, schema round-tripping for every agent, and failure classification — without a key or a network |
| `test_impact_derivation.py` | Impact analysis answers differently for different requirements — the test that stops it being re-hard-coded |
| `test_resume.py` | A halted run continues without repeating work; the blackboard rehydrates as models |
| `test_metrics.py` | Derived run metrics agree with the trace they came from |
| `test_validation_is_independent.py` | **The checks are not decoration** |

### The one that matters most

`test_validation_is_independent.py` exists because the rest of the suite has a
blind spot. The platform ships with a deliberate defect — an omitted endpoint —
and every end-to-end test watches it get caught and repaired. A check
hard-coded to look for exactly that defect would pass all of them.

So that file takes a workspace that has already passed, breaks it in ways the
checks were never written against, and requires a FAIL each time:

| Injected defect | Caught by |
| --- | --- |
| An endpoint silently deleted from the code | `api_contract` |
| 302 changed to 301 — parses fine, satisfies the contract | `tests` only |
| A syntax error | `syntax` |
| `kubectl delete` smuggled into a shell script | `guardrails` |
| `requirements.txt` deleted | `structure` |
| The entire test suite deleted | `tests` — "no tests ran" must not read as success |

The 301 case is the useful one. Syntax and contract checks both pass it; only
*executing* the suite finds it. That is why test execution is not optional for a
trustworthy verdict.

The same principle applies to the opt-in checks: editing a pre-existing test
file must fail `behaviour_preserved`, and removing an endpoint's documentation
must fail `documentation`. Both are asserted in `test_work_kinds.py`.

## Testing the generated output

The platform's claim is not "a model produced plausible files". It is that the
output was independently verified, so the verification has to be real.

**The generated suite is executed**, in a subprocess, with a timeout. When a run
reports "36 passed", pytest ran and 36 tests passed. You can confirm it yourself
without the platform:

```bash
cd runs/<run-id>/workspace
pytest -q
```

**Checks parse what is on disk**, never an agent's claim about it. Routes come
from the AST and are compared against the approved contract; the guardrail check
re-screens every file rather than trusting that the commit path screened it.

**Every run publishes what it could not check.** `validation-approach.md` lists
the test strategy — which tests are unit, which are integration, what each
exercises — then, for each check, what a pass establishes *and what it cannot
catch*. A report that only lists passes invites the reader to assume the gaps
were covered by something else.

## Reproducibility

Two things are checked in CI because they silently drift otherwise:

- **The brownfield sample is derived, not maintained.**
  `scripts/derive_legacy_sample.py` regenerates it from the same blueprint the
  greenfield scenario builds, with analytics removed. Every removal asserts that
  it matched, so a blueprint change fails loudly instead of leaving two
  scenarios describing different systems. CI regenerates it and fails on any
  diff.
- **Every kind of work runs end to end.** All six scenarios execute in CI, each
  exiting non-zero unless its own validation passed, plus a well-defined
  requirement under `--no-assume` to prove it does not stop for a human without
  cause.

## Known limitations and trade-offs

Stated plainly, because a testing document that only describes what is covered is
not useful to whoever inherits it.

**The default provider is deterministic.** The orchestration, validation,
guardrails and test execution are fully real, but `--mode mock` returns scripted
content instead of calling a model. The mock refuses a requirement it was not
scripted for rather than answering it wrongly.

The `--mode openai` path is tested with an injected stub client, so everything
this project controls — prompt construction, schema round-tripping for all seven
agent schemas, and whether a failure is classified transient or a contract
violation — is covered. What is *not* covered is the SDK itself and the quality
of a real model's reasoning; both need a key and neither is something a test here
could honestly assert.

**Coverage is measured by imports, not lines.** `test_coverage` answers "does
anything test this module at all", which is the useful signal when the answer is
no. It is not a substitute for a coverage tool, and a module with one trivial
test counts as covered.

**`behaviour_preserved` compares digests.** It catches an edited or deleted test
file. It cannot catch a test weakened in a way that leaves the bytes identical —
nothing can, short of running the old suite against the new code, which is what
the `tests` check already does.

**Import resolution is name-based.** The scanner does not resolve the Python
import system, so two modules with the same basename in different packages are
not told apart. Data flows come from the import graph, which shows what *can*
happen rather than what does — weaker than a call graph, but derived from the
source rather than remembered about it.

**Nothing is deployed.** Migrations, startup, shutdown and real failure modes are
untested outside the harness. No load testing and no security scanning, so any
latency, throughput or security target in a generated requirement is design
intent rather than a measured result.

**Approval is per-task, not per-diff.** A reviewer approves that implementation
may run, not the specific lines it will write.
