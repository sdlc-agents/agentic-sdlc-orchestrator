# Validation approach

What this run checks, how, and what it cannot tell you. Derived from the workspace and the configured checks rather than declared in advance, so it describes the suite that exists rather than the one that was intended.

## Test strategy

20 test case(s) across 3 file(s): 1 unit file(s) and 2 integration file(s).

**Unit tests** import their subject directly and assert its behaviour in isolation — the cheap, fast layer that pins down edge cases which are awkward to provoke through the front door.

**Integration tests** drive the assembled application through HTTP. They are what catches wiring mistakes: a component can be individually correct and never connected.

| File | Kind | Cases | Exercises |
| --- | --- | --- | --- |
| `tests/test_analytics.py` | integration | 4 | `app.analytics`, `app.db`, `app.models`, `app.repository` |
| `tests/test_api.py` | integration | 10 | the assembled application over HTTP |
| `tests/test_shortcode.py` | unit | 6 | `app.shortcode` |

## Verification steps

Each check runs against the files on disk. None of them asks an agent whether its own work was correct.

| # | Check | What a pass establishes | What it cannot catch |
| --- | --- | --- | --- |
| 1 | `structure` | The deliverable is a runnable project, not a pile of snippets | Says nothing about whether the code in those files is correct |
| 2 | `syntax` | Every generated module parses | A module can parse perfectly and still be wrong in every other way |
| 3 | `guardrails` | No file on disk contains a forbidden destructive operation | Pattern matching over text; it does not understand intent, and novel phrasings of a dangerous action are not in the list |
| 4 | `api_contract` | Every endpoint the approved contract declares exists in the code | Checks that a route exists, not that it behaves as the contract describes |
| 5 | `traceability` | Every must-have requirement is claimed by some artifact | An artifact claiming to cover a requirement is not evidence that it does |
| 6 | `tests` | The generated suite executed and passed in a subprocess | Only covers what the suite asserts; a passing suite bounds risk, it does not eliminate it |

## Failure handling

A failing check produces structured findings. A finding specific enough to describe its own fix is repaired and then **re-validated** — a repair is never trusted on the strength of having been applied. A finding that cannot describe a fix is escalated to a human rather than retried, and the repair budget is finite so a loop that is not converging stops instead of running forever.

## What is not verified

- Performance. No load test runs, so the latency and throughput targets in the requirements are unmeasured — they are design intent, not results.
- Security. There is no dependency audit, no static security analysis and no penetration testing. The guardrail check refuses a list of known destructive operations; that is not a security review.
- Concurrency under real load. Thread-safety is argued for in the design and exercised only incidentally by the suite.
- Operational behaviour. Nothing is deployed, so migrations, startup, shutdown and failure modes are untested outside the test harness.
- Out of scope by decision: Geographic or device breakdown of clicks
- Out of scope by decision: Backfilling analytics for redirects served before this change
- Out of scope by decision: A reporting UI or scheduled exports
- Contract conformance is structural: the checks confirm each declared endpoint exists, and the tests assert the behaviour they happen to cover. Request and response bodies are not schema-validated against the published spec.
