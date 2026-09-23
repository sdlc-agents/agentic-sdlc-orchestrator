# Architecture overview

The platform's own architecture — not the URL shortener it generates. Written
for someone deciding whether the design is sound before reading the code.

[`orchestration.md`](orchestration.md) goes deeper on the engine;
[`extending.md`](extending.md) covers adding to it.

## Components

```
                        ┌──────────────────────────────┐
   requirement ────────►│            CLI               │
                        │  parses flags, prints events │
                        └──────────────┬───────────────┘
                                       ▼
                        ┌──────────────────────────────┐
                        │           Runner             │
                        │ seeds workspace, fingerprints│
                        │ it, assembles the engine     │
                        └──────────────┬───────────────┘
                                       ▼
   ┌───────────────────────────────────────────────────────────────┐
   │                          Engine                               │
   │   ready → gate → dispatch → react        (loop, not pipeline) │
   └───┬──────────────┬──────────────┬──────────────┬──────────────┘
       ▼              ▼              ▼              ▼
  ┌─────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐
  │  Graph  │   │  Policy  │   │  Agents  │   │ Recorder │
  │ mutable │   │ approval │   │ 12, each │   │  trace   │
  │   DAG   │   │ + guard  │   │  one job │   │ + reports│
  └─────────┘   └──────────┘   └────┬─────┘   └──────────┘
                                    │
                 ┌──────────────────┼──────────────────┐
                 ▼                  ▼                  ▼
          ┌────────────┐     ┌────────────┐     ┌────────────┐
          │ Blackboard │     │  Provider  │     │   Tools    │
          │ typed      │     │ mock /     │     │ workspace  │
          │ shared     │     │ openai     │     │ scanner    │
          │ state      │     │            │     │ test runner│
          └────────────┘     └────────────┘     └────────────┘
                                    │
                                    ▼
                             ┌────────────┐
                             │ Validation │
                             │ 6 default  │
                             │ +3 opt-in  │
                             │ no model   │
                             └────────────┘
```

| Component | Module | Responsibility |
| --- | --- | --- |
| **Engine** | `orchestration/engine.py` | Scheduling, concurrency, contract enforcement, failure classification, the repair loop |
| **Graph** | `orchestration/graph.py` | A mutable DAG. Cycle-checked on every mutation, including mutations made mid-run |
| **Policy** | `orchestration/policy.py` | Which tasks need a human; which operations nothing may perform |
| **Recorder** | `orchestration/state.py` | Flushes every event to `trace.jsonl` as it happens |
| **Blackboard** | `models/run.py` | Typed shared state. The only way agents communicate |
| **Agents** | `agents/` | Twelve narrow roles. Nine call a model; three deliberately do not |
| **Providers** | `providers/` | One method: return a validated object or raise a typed error |
| **Validation** | `validation/` | Six checks every run, three a scenario opts into. No model involved |
| **Tools** | `tools/` | Workspace sandbox, AST scanner, subprocess test runner |
| **Scenarios** | `scenarios/` | Six kinds of work; only the *opening* graph is declared |

## Execution model

The engine does not walk a list of stages. It loops:

```python
while True:
    ready = graph.ready()            # dependencies all succeeded
    if not ready:
        if skip_unreachable(): continue
        break
    approved = gate(ready)           # sequential — a reviewer answers one at a time
    dispatch(approved)               # concurrent — independent tasks run together
    react_to_validation()            # may add nodes to the graph
```

Re-reading the graph each iteration is what lets it grow while running. A
greenfield run starts with 4 tasks and ends with 11: five injected by the
planner, two by the validation stage.

### Control flow

```
requirement ──► architecture ──┐
    │                          ├──► planner ──► [graph grows here]
    └──────────► api design ───┘                      │
                                                      ▼
                          implementation ──► tests ───┬──► validation ──► summary
                                   └──────► docs ─────┘        │
                                                               ▼
                                              repair ◄── findings with repair hints
                                                 └──► re-validation ──► (rewires dependents)
```

The shape differs per kind of work. A bug fix has no architecture stage and
inserts a **reproduction** node that requires the regression test to *fail*
before a fix is allowed. A refactor writes no tests. A test-improvement change
has neither architecture nor contract.

### Coordination

Agents never call each other. They read and write typed keys on the blackboard,
and each task declares which keys it reads and writes. The engine refuses to
start a task whose inputs are absent, and fails one that did not write what it
promised — so coordination is inspectable data rather than an implicit call
chain, and a planning mistake surfaces as an error instead of an agent
improvising around missing input.

## Key design decisions

### A mutable graph, not a pipeline

**Chosen:** tasks are nodes; the graph is modified while executing.
**Rejected:** a fixed sequence of stages.

A pipeline cannot express "a late step rewrites the plan for an earlier step and
re-runs it", which is exactly what fixing a defect requires. **Cost:** every
mutation must be cycle-checked, and reasoning about a graph that changes shape is
harder than reading a list.

### Verification by something that is not the author

**Chosen:** a separate validation stage that runs no model — it parses files,
compares routes against the contract, and executes the suite in a subprocess.
**Rejected:** agents self-reporting success.

An agent asked to design, build, test and sign off agrees with itself. **Cost:**
the checks are mechanical, so they catch structural defects well and semantic
ones only where a test happens to cover them. Each check documents what it cannot
catch, in `validation-approach.md`.

### Typed provider responses

**Chosen:** every model call returns a validated Pydantic object or raises.
**Rejected:** agents parsing raw text.

A malformed response becomes a retryable `ContractError` the orchestrator handles
— with the validation message attached to the next attempt — rather than a
parsing bug inside an agent. **Cost:** every agent output needs a schema, and a
schema constrains what the model can say.

### Two independent limits on autonomy

**Chosen:** approval decides *whether* work happens; the workspace decides
*where* it can land; a forbidden-operation list overrides both.
**Rejected:** approval alone.

Review is not a capability boundary — an approved task can still do something
nobody reviewed. **Cost:** the forbidden list is pattern matching over text, so
it is blunt. It once refused a design document for containing the word
"shutdown"; the pattern was narrowed to the command form, and both cases are in
the test suite.

### Facts read, not recalled

**Chosen:** impact analysis, validation and the run summary call no model.
**Rejected:** asking a model what imports what.

Which modules import which is a fact on disk, and a design built on a
recollection of it sends the implementation stage after the wrong files.
Impact analysis derives its answer: requirement vocabulary is matched against
symbols, routes and tables, then propagated along the import graph in both
directions. Every file it reports carries the reason it was selected.

**Cost:** it is a lexical heuristic, not comprehension. Import resolution is
name-based, so two modules sharing a basename are not told apart, and a
requirement whose vocabulary does not overlap the code will under-report.

### Deterministic default provider

**Chosen:** `--mode mock` replays a fixed, high-quality script; `--mode openai`
puts a real model behind the same agents.
**Rejected:** requiring an API key to run anything.

The orchestration, validation and evidence are what this project is about, and
they can be evaluated end to end without a key or network access. **Cost:** the
default mode cannot answer a requirement it was not scripted for — so it
*refuses* rather than confidently returning the wrong design.

### One deliberate defect in the script

The greenfield implementation response omits an endpoint the contract declares.
Nothing downstream special-cases it. A demo where everything succeeds on the
first pass would hide the repair loop, which is the part worth looking at.

## Where the risk actually is

| Risk | Mitigation |
| --- | --- |
| Checks pass while the code is semantically wrong | The suite is executed, not just generated; `tests/test_validation_is_independent.py` injects unseen defects and requires each to be caught |
| The repair loop never converges | Repair budget is finite; findings with no actionable hint escalate rather than retry |
| An agent writes outside its sandbox | Every write goes through `Workspace`, which rejects absolute paths, traversal and symlink escapes |
| A reviewer approves without understanding | The approval prompt carries the task's rationale, what it covers and what it depends on |
| The report flatters the run | The summary is assembled from state, not generated, and its limitations section is built from what actually happened |
