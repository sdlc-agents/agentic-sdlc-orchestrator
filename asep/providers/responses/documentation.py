"""Documentation improvement: write down what the service does and why.

The target has a thin README and nothing else — no API reference, no record of
why anything was built the way it was, and nothing an on-call engineer can use
at three in the morning. The documentation check holds the result to the API
contract rather than to a word count, so "we wrote more docs" cannot pass while
the endpoint a caller actually needs stays unmentioned.
"""

from __future__ import annotations

from ...agents.schemas import CodeBundle, GeneratedFile, TaskSpec, WorkPlan
from ...models import (
    Ambiguity,
    ApiContract,
    ArtifactKind,
    Clarity,
    Endpoint,
    FunctionalRequirement,
    NonFunctionalRequirement,
    NormalizedRequirement,
    RequirementKind,
)

API_REFERENCE = """# API reference

Base URL: the service root. All request and response bodies are JSON, except the
redirect, which is an HTTP redirect and has no body.

## POST /api/v1/urls

Create a short link.

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `long_url` | string | yes | Must be `http` or `https`. Other schemes are rejected, because a short link to `javascript:` is a redirect gadget. |
| `custom_code` | string | no | 4–16 characters, base62. Reserved paths such as `healthz` are refused. |
| `ttl_seconds` | integer | no | 60 to 31,536,000. Omit for a link that never expires. |
| `owner` | string | no | Recorded but not yet enforced; there is no authentication. |

| Status | Meaning |
| --- | --- |
| 201 | Created. Body is the link metadata. |
| 409 | `custom_code` is already in use. |
| 422 | `custom_code` is reserved or malformed, or `long_url` is not http(s). |

## GET /api/v1/urls/{code}

Read a link's metadata **without** resolving it and without recording a click.

| Status | Meaning |
| --- | --- |
| 200 | Body is the link metadata. |
| 404 | No link with that code. |

Note that this returns 200 for an expired or deactivated link. It answers "does
this record exist", not "does this link work" — use the redirect for the latter.

## DELETE /api/v1/urls/{code}

Deactivate a link. The record is kept; only its ability to resolve is removed,
so the code is never handed out again to someone else.

| Status | Meaning |
| --- | --- |
| 204 | Deactivated. No body. |
| 404 | No link with that code. |

## GET /healthz

Liveness. Returns 200 whenever the process is running. It deliberately touches
no dependency, so a database outage does not cause the orchestrator to kill
otherwise healthy instances.

## GET /readyz

Readiness, including a database round trip.

| Status | Meaning |
| --- | --- |
| 200 | Ready to serve. |
| 503 | The database is unreachable; take this instance out of rotation. |

## GET /{code}

Resolve a short code.

| Status | Meaning |
| --- | --- |
| 302 | Redirect to the target. |
| 404 | Unknown, expired or deactivated code. |

302 rather than 301 is deliberate. A permanent redirect is cached by the
browser, so the service never sees the request again — which is fine until you
need to know whether anyone is using a link.

This route is registered last. FastAPI resolves routes in declaration order, and
a single-segment catch-all registered earlier would shadow `/healthz` and every
`/api/*` path.
"""

ADR_STORAGE = """# ADR-001: sqlite behind a repository interface

## Status

Accepted, with a known expiry date.

## Context

The service needs durable storage for links. It also needs to be runnable by
anyone who has just cloned it, with no infrastructure to stand up first, or the
cost of contributing to it rises sharply.

## Decision

Persist to sqlite3 in WAL mode, reached only through `UrlRepository`. No module
outside the repository imports `sqlite3`.

## Consequences

The system runs with no dependencies to install and no services to start, and
the seam that lets Postgres replace sqlite is exercised from the first day
rather than designed in later and hoped for.

The cost is real and accepted: sqlite has a single writer, so it will not reach
the throughput target in the architecture document. It is a development and
evaluation engine. Moving to Postgres is required before production traffic, and
the repository interface is the only thing that should need to change.
"""

ADR_CODES = """# ADR-002: random base62 codes, not a counter

## Status

Accepted.

## Context

Every short link needs a code. The obvious cheap option is a counter encoded to
base62, which never collides and needs no retry logic.

## Decision

Generate codes randomly from a 62-character alphabet, detect collisions on
insert, and retry a bounded number of times.

## Consequences

Links are not enumerable. A counter would let anyone who can increment an
integer walk every link in the system, including every other customer's — an
information leak that no amount of later access control undoes, because the
identifiers themselves carry the ordering.

Hashing the target URL was also rejected: identical targets would collide into
one code, so deleting one link would silently break unrelated ones.

The accepted cost is collision handling, and a code length that has to grow with
the corpus to keep the collision rate negligible.
"""

RUNBOOK = """# Runbook

For whoever is holding the pager. Symptoms first, because that is what you have.

## Readiness is failing, liveness is fine

`/readyz` returns 503 while `/healthz` returns 200: the process is alive and the
database is not reachable. Check disk space first — sqlite in WAL mode needs
room for the write-ahead log, and a full disk presents as a database error
rather than a disk error.

Instances in this state are removed from rotation automatically. They will
recover on their own once the database is reachable; no restart is needed and a
restart will not help.

## Redirects are slow

The read path is cache-aside, so slow redirects usually mean the cache is not
being hit. Either the process was recently restarted and the cache is cold —
which resolves itself — or the working set has outgrown the cache capacity, in
which case `URLS_CACHE_CAPACITY` is the knob.

A redirect should never wait on a write. If redirect latency tracks write
volume, something has been added to the read path that does not belong there.

## A link that should have expired still redirects

Known defect. The expiry check sits on the cache-miss branch, so a link that was
resolved before it expired keeps resolving until its cache entry ages out —
up to `URLS_CACHE_TTL_S` seconds, five minutes by default.

To force it immediately, `DELETE /api/v1/urls/{code}`, which invalidates the
cache entry as well as deactivating the record.

## A link needs to be taken down now

`DELETE /api/v1/urls/{code}`. This deactivates the record and invalidates the
cache in the same request, so it takes effect immediately. The record is kept,
which matters: the code is never reissued to someone else.

There is no authentication on this endpoint. Anyone who can reach the service
can delete any link.

## Configuration

Every value is an environment variable, read once at startup.

| Variable | Default | What it changes |
| --- | --- | --- |
| `URLS_DB_PATH` | `urls.db` | Where sqlite stores data |
| `URLS_BASE_URL` | `http://localhost:8000` | The prefix in returned short URLs |
| `URLS_CODE_LENGTH` | `7` | Generated code length |
| `URLS_CACHE_CAPACITY` | `10000` | Entries held in the read cache |
| `URLS_CACHE_TTL_S` | `300` | How long a cached entry lives |

## What this service does not do

No authentication, no rate limiting, and no reputation check on submitted
targets. Anyone who can reach it can create links to anywhere and delete
anyone's. Treat it as an internal service until those exist.
"""

README = """# URL Shortener

Create short links and resolve them quickly.

    pip install -r requirements.txt
    uvicorn app.main:app --reload
    pytest -q

## Documentation

- [API reference](docs/api-reference.md) — every endpoint, every status code
- [Runbook](docs/runbook.md) — symptoms, causes and what to do about them
- [ADR-001](docs/adr/001-sqlite-behind-a-repository.md) — why sqlite, and when it stops being the answer
- [ADR-002](docs/adr/002-random-base62-codes.md) — why codes are random rather than sequential

## How it fits together

    app/config.py      runtime settings, all environment-overridable
    app/models.py      domain entities
    app/db.py          sqlite3 wrapper with per-thread connections
    app/shortcode.py   base62 code generation and validation
    app/cache.py       LRU + TTL cache in front of the read path
    app/repository.py  persistence for short URLs
    app/schemas.py     request/response transport models
    app/api/routes.py  HTTP surface
    app/main.py        composition root

The read path is cache-aside: a redirect checks the cache first and falls
through to storage only on a miss. That is the only path with a latency budget
anyone feels, and it is why nothing synchronous has been added behind it.

## Known limits

Documented rather than hidden, because whoever inherits this will find them
anyway — ideally not during an incident.

- An expired link can keep resolving until its cache entry ages out. See the
  runbook.
- sqlite has a single writer and will not sustain high write volume.
- No authentication on link creation or deletion.
- No rate limiting, and no check on whether a target is malicious.
"""


def requirement(raw: str) -> NormalizedRequirement:
    return NormalizedRequirement(
        raw=raw,
        intent=(
            "The service has no API reference, no record of why it was built the "
            "way it was, and nothing usable during an incident. Write the "
            "documentation an engineer inheriting this would actually need."
        ),
        kind=RequirementKind.BROWNFIELD,
        clarity=Clarity.CLEAR,
        functional=[
            FunctionalRequirement(
                id="FR-001",
                statement="Every published endpoint is documented with its status codes",
            ),
            FunctionalRequirement(
                id="FR-002",
                statement="The decisions that are expensive to reverse are recorded with their costs",
            ),
            FunctionalRequirement(
                id="FR-003",
                statement="A runbook maps observable symptoms to causes and actions",
            ),
            FunctionalRequirement(
                id="FR-004",
                statement="Known limits and defects are stated rather than omitted",
                priority="should",
            ),
        ],
        non_functional=[
            NonFunctionalRequirement(
                id="NFR-001",
                category="maintainability",
                statement=(
                    "Documentation describes decisions and behaviour, not a "
                    "restatement of the code, so it does not go stale on every edit"
                ),
            ),
            NonFunctionalRequirement(
                id="NFR-002",
                category="observability",
                statement="The runbook is organised by symptom, which is what an on-call engineer has",
            ),
        ],
        ambiguities=[
            Ambiguity(
                id="AMB-001",
                question="Who is the audience — callers of the API, or maintainers of the service?",
                why_it_matters=(
                    "A caller needs the contract and nothing about sqlite; a "
                    "maintainer needs the reverse. Writing one document for both "
                    "serves neither."
                ),
                blocking=False,
                default_assumption=(
                    "Both, in separate documents: an API reference for callers, "
                    "ADRs and a runbook for maintainers"
                ),
            )
        ],
        out_of_scope=[
            "Changing any code, including fixing the expiry defect the runbook describes",
            "A generated documentation site",
            "Client libraries or usage tutorials",
        ],
    )


def api_contract() -> ApiContract:
    """The surface that has to end up documented.

    Stated as a contract so the documentation check can verify each endpoint
    actually appears somewhere, instead of trusting that more prose means more
    coverage.
    """
    return ApiContract(
        title="URL Shortener — documented surface",
        version="1.0.0",
        endpoints=[
            Endpoint(
                method="POST",
                path="/api/v1/urls",
                summary="Create a short link",
                covers=["FR-001"],
                status_codes=[201, 409, 422],
            ),
            Endpoint(
                method="GET",
                path="/api/v1/urls/{code}",
                summary="Read link metadata",
                covers=["FR-001"],
                status_codes=[200, 404],
            ),
            Endpoint(
                method="DELETE",
                path="/api/v1/urls/{code}",
                summary="Deactivate a link",
                covers=["FR-001"],
                status_codes=[204, 404],
            ),
            Endpoint(
                method="GET",
                path="/healthz",
                summary="Liveness",
                covers=["FR-001"],
                status_codes=[200],
            ),
            Endpoint(
                method="GET",
                path="/readyz",
                summary="Readiness",
                covers=["FR-001"],
                status_codes=[200, 503],
            ),
            Endpoint(
                method="GET",
                path="/{code}",
                summary="Resolve a short code and redirect",
                covers=["FR-001"],
                status_codes=[302, 404],
            ),
        ],
    )


def plan() -> WorkPlan:
    return WorkPlan(
        strategy=(
            "One documentation task, then verification against the contract. The "
            "check that matters asks whether every published endpoint appears in "
            "some document — otherwise a run can add three pages about the parts "
            "that were already well understood and pass."
        ),
        parallelism_note="One author, one set of documents; nothing to parallelise.",
        tasks=[
            TaskSpec(
                id="T-010",
                title="Write the API reference, the ADRs and the runbook",
                agent="documentation",
                depends_on=[],
                covers=["FR-001", "FR-002", "FR-003", "FR-004"],
                reads=["api_contract", "impact_analysis"],
                writes=["documentation_index"],
                rationale="Writes documents only; no source file is touched",
            ),
            TaskSpec(
                id="T-020",
                title="Verify every published endpoint is documented",
                agent="validation",
                depends_on=["T-010"],
                reads=["documentation_index"],
                writes=["validation_report"],
                rationale="Held to the contract rather than to a word count",
            ),
            TaskSpec(
                id="T-030",
                title="Summarize what is now documented",
                agent="summary",
                depends_on=["T-020"],
                reads=["requirement"],
                writes=["run_summary"],
            ),
        ],
    )


def docs() -> CodeBundle:
    return CodeBundle(
        summary=(
            "Wrote an API reference covering every endpoint and status code, two "
            "ADRs recording the decisions that are expensive to reverse, a "
            "symptom-led runbook, and a README that points at them and states the "
            "known limits."
        ),
        files=[
            GeneratedFile(
                path="docs/api-reference.md",
                content=API_REFERENCE,
                kind=ArtifactKind.DOC,
                language="markdown",
                covers=["FR-001"],
            ),
            GeneratedFile(
                path="docs/adr/001-sqlite-behind-a-repository.md",
                content=ADR_STORAGE,
                kind=ArtifactKind.DOC,
                language="markdown",
                covers=["FR-002"],
            ),
            GeneratedFile(
                path="docs/adr/002-random-base62-codes.md",
                content=ADR_CODES,
                kind=ArtifactKind.DOC,
                language="markdown",
                covers=["FR-002"],
            ),
            GeneratedFile(
                path="docs/runbook.md",
                content=RUNBOOK,
                kind=ArtifactKind.DOC,
                language="markdown",
                covers=["FR-003", "FR-004"],
            ),
            GeneratedFile(
                path="README.md",
                content=README,
                kind=ArtifactKind.DOC,
                language="markdown",
                covers=["FR-004"],
            ),
        ],
        notes=[
            "The runbook is organised by symptom rather than by component, because "
            "a symptom is what the person reading it at 3am actually has.",
            "The known expiry defect is documented, with the workaround, rather "
            "than left for someone to rediscover during an incident.",
            "Each ADR records what the decision cost, not only what was chosen.",
        ],
    )


__all__ = ["api_contract", "docs", "plan", "requirement"]
