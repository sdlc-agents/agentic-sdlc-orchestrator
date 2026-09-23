"""Brownfield enhancement: add click analytics to a service that already works.

The change is additive by construction — a new table, a new module, one
non-blocking call added to the existing redirect path — because the constraint
that shapes it is that the current suite must keep passing untouched.
"""

from __future__ import annotations

from ...agents.schemas import CodeBundle, GeneratedFile, WorkPlan
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
    TradeOff,
)
from ..blueprints import url_shortener as bp


def _brownfield_requirement(raw: str) -> NormalizedRequirement:
    return NormalizedRequirement(
        raw=raw,
        intent=(
            "Add click analytics to a running URL shortener without changing how "
            "redirects behave or how fast they are."
        ),
        kind=RequirementKind.BROWNFIELD,
        clarity=Clarity.NEEDS_CLARIFICATION,
        functional=[
            FunctionalRequirement(
                id="FR-001", statement="Record every resolved redirect as a click event"
            ),
            FunctionalRequirement(
                id="FR-002", statement="Attribute clicks to a referrer where the browser sends one"
            ),
            FunctionalRequirement(
                id="FR-003",
                statement="Expose per-code totals, last-click time, daily counts and top referrers",
            ),
            FunctionalRequirement(
                id="FR-004",
                statement="Return 404 for analytics on a code that does not exist",
                priority="should",
            ),
        ],
        non_functional=[
            NonFunctionalRequirement(
                id="NFR-001",
                category="performance",
                statement="Redirect latency must not regress once recording is added",
                target="no measurable change at p99",
            ),
            NonFunctionalRequirement(
                id="NFR-002",
                category="availability",
                statement=(
                    "A failure or backlog in analytics must degrade reporting only, "
                    "never redirects"
                ),
            ),
            NonFunctionalRequirement(
                id="NFR-003",
                category="maintainability",
                statement=(
                    "The existing suite must keep passing unchanged; the change is "
                    "additive to the schema and the HTTP surface"
                ),
            ),
        ],
        ambiguities=[
            Ambiguity(
                id="AMB-001",
                question="Is it acceptable to lose buffered clicks when the process restarts?",
                why_it_matters=(
                    "It is the difference between an in-process queue and an "
                    "operational dependency on a durable broker."
                ),
                blocking=True,
                default_assumption=(
                    "Yes at this stage: analytics are directional, and bounded loss "
                    "under overload is preferable to slowing the redirect path"
                ),
            ),
            Ambiguity(
                id="AMB-002",
                question="Should analytics be readable by anyone holding the short code?",
                why_it_matters="Click counts leak traffic information about the link owner.",
                blocking=False,
                default_assumption=(
                    "Yes for now, matching the existing unauthenticated surface; "
                    "revisit with authentication"
                ),
            ),
        ],
        out_of_scope=[
            "Geographic or device breakdown of clicks",
            "Backfilling analytics for redirects served before this change",
            "A reporting UI or scheduled exports",
        ],
    )


def _brownfield_architecture() -> Architecture:
    return Architecture(
        style="Additive extension of the existing modular monolith",
        summary=(
            "The existing redirect path is not restructured. One non-blocking call is "
            "added to it, and everything else the feature needs lives behind that call: "
            "a bounded queue, a background flusher, a new table and a new read endpoint. "
            "The constraint that shapes the design is that the current suite must keep "
            "passing untouched, so nothing already on disk changes behaviour."
        ),
        components=[
            Component(
                name="Analytics recorder",
                responsibility=(
                    "Accept click events without blocking, batch them, and flush from a "
                    "background thread"
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
                name="Analytics endpoint",
                responsibility="Serve per-code totals, daily counts and top referrers",
                technology="FastAPI",
            ),
        ],
        data_flows=[
            DataFlow(
                source="Redirect handler",
                target="Analytics recorder",
                description="Enqueue a click event after the target is resolved, then return",
                synchronous=False,
            ),
            DataFlow(
                source="Analytics recorder",
                target="Click repository",
                description="Flush batches on an interval or when the batch size is reached",
                synchronous=False,
            ),
            DataFlow(
                source="Analytics endpoint",
                target="Click repository",
                description="Aggregate click rows for one code on demand",
            ),
        ],
        trade_offs=[
            TradeOff(
                decision="How the redirect path records a click",
                chosen="Enqueue onto a bounded in-process queue and return",
                alternatives=["Insert the click row inline", "Publish to an external broker"],
                rationale=(
                    "The existing redirect latency is the thing most likely to regress, "
                    "and it is what NFR-001 protects. An inline insert couples it to write "
                    "throughput."
                ),
                accepted_cost="Bounded click loss on restart and under sustained overload",
            ),
            TradeOff(
                decision="Schema change strategy",
                chosen="Additive migration introducing a new table only",
                alternatives=["Add click counters to the existing short_urls row"],
                rationale=(
                    "Counters on the link row turn every redirect into an update on a hot "
                    "row and discard the per-event detail the reports need."
                ),
                accepted_cost=(
                    "Aggregates are computed at read time and will need rollups as the "
                    "event table grows"
                ),
            ),
        ],
        risks=[
            Risk(
                id="RISK-001",
                description="The new background thread keeps a database handle open across a restart",
                likelihood="low",
                impact="medium",
                mitigation="The recorder is started and stopped by the application lifespan",
            ),
            Risk(
                id="RISK-002",
                description="Click volume outgrows read-time aggregation",
                likelihood="medium",
                impact="medium",
                mitigation=(
                    "Index on (code, occurred_at) now; precomputed daily rollups when the "
                    "event table gets large"
                ),
            ),
            Risk(
                id="RISK-003",
                description="Adding analytics regresses redirect latency",
                likelihood="low",
                impact="high",
                mitigation=(
                    "The only work added to the redirect path is a non-blocking enqueue "
                    "that sheds load rather than waiting"
                ),
            ),
        ],
        diagram=(
            "graph LR\n"
            "    Redirect[GET /{code} existing] -.enqueue, non-blocking.-> Recorder[Analytics recorder NEW]\n"
            "    Recorder -.batched flush.-> Clicks[Click repository NEW]\n"
            "    Clicks --> DB[(sqlite: click_events NEW)]\n"
            "    Report[GET /api/v1/analytics/{code} NEW] --> Clicks"
        ),
    )


def _brownfield_contract() -> ApiContract:
    return ApiContract(
        title="URL Shortener — analytics",
        version="1.1.0",
        endpoints=[
            Endpoint(
                method="GET",
                path="/api/v1/analytics/{code}",
                summary="Click analytics for a short link",
                covers=["FR-003", "FR-004"],
                response_model="AnalyticsResponse",
                status_codes=[200, 404],
            )
        ],
    )




from .build import (  # noqa: E402
    CLICK_MIGRATION,
    _covers_for,
    _docs,
    _plan,
    _tests,
)


def _brownfield_code() -> CodeBundle:
    changed = {
        "app/config.py": bp.CONFIG,
        "app/models.py": bp.MODELS,
        "app/db.py": bp.DB,
        "app/repository.py": bp.REPOSITORY,
        "app/schemas.py": bp.SCHEMAS,
        "app/analytics.py": bp.ANALYTICS,
        "app/api/routes.py": bp.ROUTES,
        "app/main.py": bp.MAIN,
        "migrations/002_add_click_events.sql": CLICK_MIGRATION,
    }
    files = [
        GeneratedFile(
            path=path,
            content=content,
            kind=ArtifactKind.MIGRATION if path.endswith(".sql") else ArtifactKind.CODE,
            language="sql" if path.endswith(".sql") else "python",
            covers=_covers_for(path),
        )
        for path, content in changed.items()
    ]
    return CodeBundle(
        summary=(
            "Added a click_events table, a ClickRepository, a queue-backed "
            "AnalyticsRecorder started by the application lifespan, and a single "
            "non-blocking enqueue on the existing redirect path."
        ),
        files=files,
        notes=[
            "The redirect handler gains one non-blocking call and no new failure mode.",
            "The migration is additive, so the existing suite runs unchanged against "
            "the new schema.",
        ],
    )




requirement = _brownfield_requirement
architecture = _brownfield_architecture
api_contract = _brownfield_contract
code = _brownfield_code


def plan() -> WorkPlan:
    return _plan("analytics_upgrade")


def tests() -> CodeBundle:
    return _tests("analytics_upgrade")


def docs() -> CodeBundle:
    return _docs("analytics_upgrade")


__all__ = [
    "api_contract",
    "architecture",
    "code",
    "docs",
    "plan",
    "requirement",
    "tests",
]
