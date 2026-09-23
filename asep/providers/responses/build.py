"""Greenfield: build a URL shortener from nothing.

The implementation response deliberately omits the analytics endpoint that the
contract declares. Nothing downstream special-cases the gap — the contract check
finds it, two generated tests fail on it, and the repair loop closes it. A demo
where everything succeeds on the first pass would hide the part of the system
worth looking at.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...agents.schemas import CodeBundle, GeneratedFile, RepairEdit, RepairPlan, TaskSpec, WorkPlan
from ...models import (
    Ambiguity,
    ApiContract,
    Architecture,
    ArtifactKind,
    Clarity,
    Component,
    DataFlow,
    Endpoint,
    FunctionalRequirement,
    NonFunctionalRequirement,
    NormalizedRequirement,
    RequirementKind,
    Risk,
    RiskLevel,
    TradeOff,
)
from ..blueprints import url_shortener as bp
from . import codegen

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..base import GenerationRequest

# The greenfield and enhancement scripts share their plan, test and doc shapes
# and branch on the scenario name, so it is named here rather than imported from
# the dispatcher that imports this module.
GREENFIELD = "url_shortener"

LEGACY_IMPORT = "from ..schemas import CreateUrlRequest, HealthResponse, ShortUrlResponse"
REPAIRED_IMPORT = (
    "from ..schemas import AnalyticsResponse, CreateUrlRequest, HealthResponse, "
    "ShortUrlResponse"
)

CLICK_MIGRATION = """-- 002_add_click_events.sql
-- Additive migration: the redirect path keeps working while this is applied,
-- and applying it twice is harmless.

CREATE TABLE IF NOT EXISTS click_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT NOT NULL,
    occurred_at REAL NOT NULL,
    referrer    TEXT,
    user_agent  TEXT
);

CREATE INDEX IF NOT EXISTS idx_click_events_code_time
    ON click_events(code, occurred_at);

-- Rollback, described rather than executable: the platform's policy layer
-- forbids generated artifacts from containing destructive DDL, so reversing
-- this is a deliberate human step. Reverse order: index
-- idx_click_events_code_time, then table click_events.
"""

ADR = """# ADR-001: Analytics is written off the redirect path

## Status

Accepted.

## Context

The redirect is the only endpoint with a latency budget that users feel, and it
is the one endpoint every click passes through. Recording a click is a write.
Putting a write in front of the redirect means the slowest component in the
system — the database under load — decides how fast links resolve.

## Decision

The redirect handler enqueues a click event onto a bounded in-process queue and
returns. A background worker drains the queue in batches and writes them.

When the queue is full, events are dropped and counted rather than blocking the
producer. The counter is exposed so the loss is visible instead of silent.

## Consequences

Redirect latency no longer depends on write throughput, and an analytics outage
degrades reporting instead of taking down redirects.

The cost is real and accepted: events buffered in memory are lost if the process
dies, and analytics are eventually consistent, so a click may not appear in a
report for up to one flush interval. Under sustained overload the system reports
fewer clicks than occurred — deliberately, because the alternative is reporting
all of them slowly and failing the latency budget for every user.

Moving the queue onto a durable broker removes the loss window and is the
expected next step; it was not done here because it adds an operational
dependency this stage does not yet justify.
"""


def _greenfield_requirement(raw: str) -> NormalizedRequirement:
    return NormalizedRequirement(
        raw=raw,
        intent=(
            "A link shortening service: create short codes for long URLs, resolve "
            "them quickly, and report how often each one is used."
        ),
        kind=RequirementKind.GREENFIELD,
        clarity=Clarity.NEEDS_CLARIFICATION,
        functional=[
            FunctionalRequirement(
                id="FR-001", statement="Create a short code for a submitted long URL"
            ),
            FunctionalRequirement(
                id="FR-002",
                statement="Accept an optional caller-supplied custom alias",
                priority="should",
            ),
            FunctionalRequirement(
                id="FR-003", statement="Resolve a short code to its target with an HTTP redirect"
            ),
            FunctionalRequirement(
                id="FR-004",
                statement="Support an optional expiry after which a link stops resolving",
                priority="should",
            ),
            FunctionalRequirement(
                id="FR-005",
                statement="Report per-link click totals, recent activity and top referrers",
            ),
            FunctionalRequirement(
                id="FR-006", statement="Deactivate a link so it no longer resolves"
            ),
            FunctionalRequirement(
                id="FR-007",
                statement="Expose liveness and readiness endpoints for operation",
                priority="should",
            ),
        ],
        non_functional=[
            NonFunctionalRequirement(
                id="NFR-001",
                category="performance",
                statement="Redirect latency must stay within a fixed budget at the tail",
                target="p99 < 50ms server-side, excluding network",
            ),
            NonFunctionalRequirement(
                id="NFR-002",
                category="scalability",
                statement="Sustain the target redirect rate on commodity hardware",
                target="10k redirects/sec across horizontally scaled instances",
            ),
            NonFunctionalRequirement(
                id="NFR-003",
                category="availability",
                statement=(
                    "Analytics recording must never block or fail a redirect; "
                    "losing a click is preferable to delaying a user"
                ),
            ),
            NonFunctionalRequirement(
                id="NFR-004",
                category="security",
                statement=(
                    "Reject non-http(s) targets and reserved aliases so short links "
                    "cannot be used as redirect gadgets"
                ),
            ),
            NonFunctionalRequirement(
                id="NFR-005",
                category="observability",
                statement="Expose health and readiness suitable for a load balancer",
            ),
        ],
        ambiguities=[
            Ambiguity(
                id="AMB-001",
                question="What read and write volume must this sustain, and over what link corpus?",
                why_it_matters=(
                    "It decides the storage engine and whether the cache is in-process "
                    "or shared. Getting it wrong is a rewrite, not a tuning exercise."
                ),
                blocking=True,
                default_assumption=(
                    "10k redirects/sec and 1M links, read-dominant by roughly 100:1"
                ),
            ),
            Ambiguity(
                id="AMB-002",
                question="Are link creation and deletion authenticated, and by whom?",
                why_it_matters=(
                    "An unauthenticated create endpoint is an open redirect factory "
                    "and a spam vector."
                ),
                blocking=False,
                default_assumption=(
                    "No authentication in this stage; an owner field is recorded so "
                    "authorization can be added without a migration"
                ),
            ),
            Ambiguity(
                id="AMB-003",
                question="How long must click events be retained, and at what granularity?",
                why_it_matters="It sets the storage growth rate and the aggregation strategy.",
                blocking=False,
                default_assumption=(
                    "Raw events retained indefinitely at this scale; daily rollups "
                    "served from a 30-day window"
                ),
            ),
        ],
        out_of_scope=[
            "A web UI; the deliverable is an HTTP API",
            "Custom domains per tenant",
            "Malware and phishing classification of submitted targets",
            "Billing, quotas and rate limiting",
        ],
    )


def _greenfield_architecture() -> Architecture:
    return Architecture(
        style="Modular monolith with cache-aside reads and an asynchronous analytics writer",
        summary=(
            "One deployable with hard internal seams. The redirect path is the only "
            "latency-critical path, so it is the one that gets a cache in front of it "
            "and nothing synchronous behind it. Everything the redirect does not need "
            "— recording the click, aggregating it, serving reports — happens off that "
            "path. Storage and cache are reached through narrow interfaces so the "
            "sqlite and in-process implementations can be replaced by Postgres and "
            "Redis without touching the handlers."
        ),
        components=[
            Component(
                name="HTTP API",
                responsibility="Validate input, map domain errors to status codes, serve redirects",
                technology="FastAPI",
            ),
            Component(
                name="Shortcode generator",
                responsibility="Produce and validate base62 codes; reject reserved paths",
                technology="Python stdlib secrets",
            ),
            Component(
                name="Read cache",
                responsibility="Serve code to target lookups without touching storage",
                technology="In-process LRU with TTL",
                scales="stateful",
            ),
            Component(
                name="URL repository",
                responsibility="Persist links, handle alias collisions, enforce expiry",
                technology="sqlite3 behind a repository interface",
            ),
            Component(
                name="Analytics recorder",
                responsibility=(
                    "Buffer click events on a bounded queue and flush them in batches "
                    "from a background thread"
                ),
                technology="Python threading and queue",
                scales="stateful",
            ),
            Component(
                name="Click repository",
                responsibility="Persist click events and answer aggregate queries",
                technology="sqlite3",
            ),
            Component(
                name="Database",
                responsibility="Durable storage for links and clicks",
                technology="sqlite3 in WAL mode",
                scales="managed",
            ),
        ],
        data_flows=[
            DataFlow(
                source="Client",
                target="HTTP API",
                description="POST a long URL, receive a short code",
            ),
            DataFlow(
                source="HTTP API",
                target="Read cache",
                description="Resolve a code, falling through to storage on a miss",
            ),
            DataFlow(
                source="HTTP API",
                target="Analytics recorder",
                description="Enqueue a click event and return immediately",
                synchronous=False,
            ),
            DataFlow(
                source="Analytics recorder",
                target="Click repository",
                description="Flush buffered events in batches on an interval",
                synchronous=False,
            ),
        ],
        trade_offs=[
            TradeOff(
                decision="How short codes are generated",
                chosen="Random base62 with a bounded collision retry",
                alternatives=[
                    "Monotonic counter encoded to base62",
                    "Hash of the target URL truncated to n characters",
                ],
                rationale=(
                    "A counter makes every link enumerable, which exposes every "
                    "customer's links to anyone who can count. A hash makes identical "
                    "targets collide into one code, so one deletion breaks unrelated links."
                ),
                accepted_cost=(
                    "Collisions must be detected and retried, and code length has to "
                    "grow with the corpus to keep the collision rate negligible."
                ),
            ),
            TradeOff(
                decision="Where click recording happens",
                chosen="Off the redirect path, on a bounded queue with a background flusher",
                alternatives=[
                    "Write the click inline before redirecting",
                    "Publish to an external durable queue",
                ],
                rationale=(
                    "An inline write makes redirect latency a function of database load, "
                    "which violates the one budget users actually feel. A durable broker "
                    "is the right end state but adds an operational dependency this stage "
                    "does not justify."
                ),
                accepted_cost=(
                    "Clicks buffered in memory are lost on an unclean shutdown, and under "
                    "sustained overload events are dropped rather than queued without bound."
                ),
            ),
            TradeOff(
                decision="Storage engine",
                chosen="sqlite3 behind a repository interface",
                alternatives=["PostgreSQL from the start", "A managed key-value store"],
                rationale=(
                    "The interface, not the engine, is the durable decision. sqlite keeps "
                    "the system runnable with no infrastructure while the seam that allows "
                    "Postgres to replace it is exercised from day one."
                ),
                accepted_cost=(
                    "Single-writer contention means sqlite will not reach the stated "
                    "throughput target; it is a development and evaluation engine, and "
                    "the migration is required before production."
                ),
            ),
            TradeOff(
                decision="Redirect status code",
                chosen="302 Found",
                alternatives=["301 Moved Permanently"],
                rationale=(
                    "A 301 is cached by the browser, so subsequent clicks never reach the "
                    "service and are never counted. That silently destroys the analytics "
                    "product the requirement asks for."
                ),
                accepted_cost=(
                    "Every click costs a round trip; the service cannot shed that load to "
                    "browser caches."
                ),
            ),
        ],
        risks=[
            Risk(
                id="RISK-001",
                description="Buffered click events are lost if the process dies",
                likelihood="medium",
                impact="medium",
                mitigation=(
                    "Flush on shutdown, keep the interval short, and expose the dropped "
                    "counter; move to a durable queue before analytics become billable"
                ),
            ),
            Risk(
                id="RISK-002",
                description="sqlite write contention will not sustain the target redirect rate",
                likelihood="high",
                impact="high",
                mitigation=(
                    "Repository interface is the seam; migrate to Postgres with a shared "
                    "Redis cache before any load beyond evaluation"
                ),
            ),
            Risk(
                id="RISK-003",
                description="The service can be used to launder links to malicious targets",
                likelihood="medium",
                impact="high",
                mitigation=(
                    "Scheme validation rejects non-http(s) targets today; reputation "
                    "checking and authenticated creation are required before public use"
                ),
            ),
            Risk(
                id="RISK-004",
                description="A deleted link keeps resolving from a stale cache entry",
                likelihood="low",
                impact="medium",
                mitigation="Deletion invalidates the cache entry in the same request",
            ),
        ],
        diagram=(
            "graph LR\n"
            "    Client -->|POST /api/v1/urls| API[HTTP API]\n"
            "    Client -->|GET /{code}| API\n"
            "    API --> Cache[LRU + TTL cache]\n"
            "    Cache -.miss.-> Urls[URL repository]\n"
            "    Urls --> DB[(sqlite WAL)]\n"
            "    API -.enqueue, non-blocking.-> Recorder[Analytics recorder]\n"
            "    Recorder -.batched flush.-> Clicks[Click repository]\n"
            "    Clicks --> DB"
        ),
    )


def _greenfield_contract() -> ApiContract:
    return ApiContract(
        title="URL Shortener",
        version="1.0.0",
        endpoints=[
            Endpoint(
                method="POST",
                path="/api/v1/urls",
                summary="Create a short link",
                covers=["FR-001", "FR-002", "FR-004"],
                request_model="CreateUrlRequest",
                response_model="ShortUrlResponse",
                status_codes=[201, 409, 422],
            ),
            Endpoint(
                method="GET",
                path="/api/v1/urls/{code}",
                summary="Read link metadata without resolving it",
                covers=["FR-001"],
                response_model="ShortUrlResponse",
                status_codes=[200, 404],
            ),
            Endpoint(
                method="DELETE",
                path="/api/v1/urls/{code}",
                summary="Deactivate a link",
                covers=["FR-006"],
                status_codes=[204, 404],
            ),
            Endpoint(
                method="GET",
                path="/api/v1/analytics/{code}",
                summary="Click analytics for a short link",
                covers=["FR-005"],
                response_model="AnalyticsResponse",
                status_codes=[200, 404],
            ),
            Endpoint(
                method="GET",
                path="/healthz",
                summary="Liveness",
                covers=["FR-007"],
                response_model="HealthResponse",
            ),
            Endpoint(
                method="GET",
                path="/readyz",
                summary="Readiness, including database reachability",
                covers=["FR-007"],
                response_model="HealthResponse",
                status_codes=[200, 503],
            ),
            Endpoint(
                method="GET",
                path="/{code}",
                summary="Resolve a short code and redirect",
                covers=["FR-003"],
                status_codes=[302, 404],
            ),
        ],
    )


def _plan(scenario: str) -> WorkPlan:
    greenfield = scenario == GREENFIELD
    build_title = (
        "Implement the service from the architecture and contract"
        if greenfield
        else "Add click recording, storage and the analytics read model"
    )
    return WorkPlan(
        strategy=(
            "Build, test and document from the approved contract. Implementation is "
            "one task rather than several because the modules share a composition root "
            "and splitting them would create tasks that cannot be validated "
            "independently. Tests and documentation do not depend on each other, so "
            "they are scheduled as siblings and run concurrently. Validation is a "
            "separate node, not a step inside implementation, because an agent must "
            "not be the judge of its own output."
        ),
        parallelism_note=(
            "T-020 and T-030 are independent; the critical path is "
            "implementation -> tests -> validation."
        ),
        tasks=[
            TaskSpec(
                id="T-010",
                title=build_title,
                agent="implementation",
                depends_on=[],
                covers=["FR-001", "FR-002", "FR-003"] if greenfield else ["FR-001", "FR-002"],
                reads=["api_contract", "architecture"],
                writes=["artifacts_index", "implementation_summary"],
                risk=RiskLevel.MEDIUM,
                rationale=(
                    "Writes source into the workspace, so it is gated for human approval"
                ),
            ),
            TaskSpec(
                id="T-020",
                title="Generate an executable test suite from the contract",
                agent="test",
                depends_on=["T-010"],
                covers=["FR-005"] if greenfield else ["FR-003", "FR-004"],
                reads=["artifacts_index", "api_contract"],
                writes=["test_suite_index"],
                rationale="Tests are written against the contract, not against the implementation",
            ),
            TaskSpec(
                id="T-030",
                title="Write the architecture decision record and update the README",
                agent="documentation",
                depends_on=["T-010"],
                covers=["FR-007"] if greenfield else [],
                reads=["architecture", "api_contract"],
                writes=["documentation_index"],
                rationale="Independent of the test suite, so it runs alongside it",
            ),
            TaskSpec(
                id="T-040",
                title="Validate the workspace against the contract and run the suite",
                agent="validation",
                depends_on=["T-020", "T-030"],
                reads=["artifacts_index"],
                writes=["validation_report"],
                rationale="Independent verification; findings become new graph nodes",
            ),
            TaskSpec(
                id="T-050",
                title="Summarize the run for a human reviewer",
                agent="summary",
                depends_on=["T-040"],
                reads=["requirement"],
                writes=["run_summary"],
                rationale="Reports what was decided, what was verified and what is still open",
            ),
        ],
    )


def _greenfield_code() -> CodeBundle:
    """The implementation response.

    `app/api/routes.py` is synthesised from the approved contract rather than
    stored: change an endpoint and the emitted module changes with it. The
    modules behind it — storage, cache, the analytics writer — are library
    code, because an HTTP contract says what the surface is, not how a short
    code is allocated.

    The missing analytics route is a consequence of that split rather than a
    hand-written omission: the contract declares the endpoint, `OPERATIONS` has
    no implementation for it, so the generator leaves it out and the contract
    check reports it.
    """
    generated = codegen.render_routes(api_contract())
    files = [
        GeneratedFile(
            path=path,
            content=content,
            kind=(
                ArtifactKind.MIGRATION
                if path.endswith(".sql")
                else ArtifactKind.DOC
                if path.endswith(".txt")
                else ArtifactKind.CODE
            ),
            language="sql" if path.endswith(".sql") else "python",
            covers=_covers_for(path),
        )
        for path, content in (
            {**bp.FILES, "app/api/routes.py": generated}
        ).items()
        # openapi.yaml is derived from the contract by the api_design agent and
        # README.md belongs to documentation; neither is implementation output.
        if path not in {"openapi.yaml", "README.md"}
    ]
    return CodeBundle(
        summary=(
            "Implemented the service as a FastAPI application over sqlite3: base62 "
            "code generation with collision retry, a cache-aside read path, expiry and "
            "deactivation, and a background analytics writer fed by a bounded queue."
        ),
        files=files,
        notes=[
            "The HTTP layer is generated from the approved contract; the modules "
            "behind it are library code, since a contract defines the surface "
            "rather than the behaviour.",
            "The catch-all /{code} route is registered last because the generator "
            "orders by path specificity, not because the contract happened to "
            "list it last.",
            "Analytics recording is enqueued, never awaited, so a slow write cannot "
            "delay a redirect.",
        ],
    )


def _covers_for(path: str) -> list[str]:
    mapping = {
        "app/api/routes.py": ["FR-001", "FR-002", "FR-003", "FR-006", "FR-007"],
        "app/shortcode.py": ["FR-001", "FR-002"],
        "app/repository.py": ["FR-001", "FR-004", "FR-006"],
        "app/analytics.py": ["FR-005"],
        "app/cache.py": ["NFR-001"],
        "app/schemas.py": ["NFR-004"],
        "app/main.py": ["FR-007"],
    }
    return mapping.get(path, [])


def _tests(scenario: str) -> CodeBundle:
    if scenario == GREENFIELD:
        files = bp.TEST_FILES
        summary = (
            "Generated a suite from the contract: creation and conflict handling, "
            "redirect semantics, expiry, deactivation, scheme and alias validation, "
            "route precedence, and the analytics endpoint."
        )
    else:
        files = {
            "tests/conftest.py": bp.CONFTEST,
            "tests/test_analytics.py": bp.TEST_ANALYTICS,
        }
        summary = (
            "Added analytics coverage and extended the existing fixtures with a short "
            "flush interval so recording is observable within a test."
        )
    return CodeBundle(
        summary=summary,
        files=[
            GeneratedFile(
                path=path,
                content=content,
                kind=ArtifactKind.TEST,
                covers=["FR-005"] if "analytics" in path else [],
            )
            for path, content in files.items()
        ],
        notes=[
            "Tests assert against the contract, including the analytics endpoint, "
            "which is why they fail if the implementation omits it.",
            "The overload test asserts that the recorder sheds load instead of "
            "blocking, since that is the behaviour the architecture depends on.",
        ],
    )


def _docs(scenario: str) -> CodeBundle:
    readme = bp.README if scenario == GREENFIELD else _brownfield_readme()
    return CodeBundle(
        summary="Wrote the ADR for the analytics write path and the project README.",
        files=[
            GeneratedFile(
                path="docs/adr/001-analytics-off-the-redirect-path.md",
                content=ADR,
                kind=ArtifactKind.DOC,
                language="markdown",
            ),
            GeneratedFile(
                path="README.md",
                content=readme,
                kind=ArtifactKind.DOC,
                language="markdown",
                covers=["FR-007"] if scenario == GREENFIELD else [],
            ),
        ],
    )


def _brownfield_readme() -> str:
    return """# URL Shortener

Create short links, resolve them through a cache-aside read path, and report how
often each one is used.

## Analytics

Every resolved redirect is recorded as a click event. Recording happens off the
redirect path: the handler enqueues the event on a bounded queue and returns, and
a background worker flushes batches to storage. If the queue is full the event is
dropped and counted, because delaying a user is worse than losing a data point.

    GET /api/v1/analytics/{code}

returns the total click count, the time of the last click, daily counts for the
last 30 days, and the top referrers.

## Layout

    app/analytics.py   bounded queue and background flusher
    app/repository.py  UrlRepository and ClickRepository
    app/api/routes.py  HTTP surface, including the analytics endpoint
    migrations/        001 creates short_urls, 002 adds click_events

## Running

    pip install -r requirements.txt
    uvicorn app.main:app --reload
    pytest -q

## Known limits

Buffered clicks are lost if the process stops uncleanly, and aggregates are
computed at read time, which will need rollups once the event table is large.
"""


def _repair(request: GenerationRequest) -> RepairPlan:
    """Turn structured findings into edits.

    The mapping is driven by the finding, not by the scenario: an edit is only
    produced for a hint this provider knows how to satisfy, and anything else is
    returned as unrepairable so the engine escalates instead of looping.
    """
    findings = request.context.get("findings", [])
    edits: list[RepairEdit] = []
    unrepairable: list[str] = []
    handled_route = False

    for finding in findings:
        hint = (finding.get("repair_hint") or "") if isinstance(finding, dict) else ""
        message = (finding.get("message") or "") if isinstance(finding, dict) else ""

        if "/api/v1/analytics/{code}" in hint and not handled_route:
            handled_route = True
            edits.append(
                RepairEdit(
                    path="app/api/routes.py",
                    action="replace",
                    anchor=LEGACY_IMPORT,
                    content=REPAIRED_IMPORT,
                    addresses=message,
                )
            )
            edits.append(
                RepairEdit(
                    path="app/api/routes.py",
                    action="append",
                    content=bp.ANALYTICS_ROUTE,
                    addresses=message,
                )
            )
        elif hint:
            unrepairable.append(f"no known edit for hint: {hint}")
        else:
            unrepairable.append(message or "finding carried no repair hint")

    return RepairPlan(
        summary=(
            "Added the analytics route the contract declares, and imported the "
            "response model it returns."
            if handled_route
            else "No edit could be derived from the findings supplied."
        ),
        edits=edits,
        unrepairable=unrepairable,
    )




requirement = _greenfield_requirement
architecture = _greenfield_architecture
api_contract = _greenfield_contract
code = _greenfield_code
covers_for = _covers_for


def plan() -> WorkPlan:
    return _plan("url_shortener")


def tests() -> CodeBundle:
    return _tests("url_shortener")


def docs() -> CodeBundle:
    return _docs("url_shortener")


def repair(request) -> RepairPlan:
    return _repair(request)


__all__ = [
    "ADR",
    "CLICK_MIGRATION",
    "LEGACY_IMPORT",
    "REPAIRED_IMPORT",
    "api_contract",
    "architecture",
    "code",
    "covers_for",
    "docs",
    "plan",
    "repair",
    "requirement",
    "tests",
]
