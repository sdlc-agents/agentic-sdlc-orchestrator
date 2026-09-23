"""Brownfield refactor: extract a service layer, change no behaviour.

A refactor is the one kind of change whose success criterion is that nothing
observable happened. That makes the existing tests the specification, and it is
why this scenario turns on `behaviour_preserved`: the suite must pass *unedited*.
A refactor that adjusts the tests proving its behaviour has not preserved that
behaviour, it has redefined it, and no amount of green ticks says otherwise.
"""

from __future__ import annotations

from ...agents.schemas import CodeBundle, GeneratedFile, TaskSpec, WorkPlan
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

SERVICE = '''from __future__ import annotations

import time

from .cache import Cache
from .models import ShortUrl
from .repository import CodeUnavailable, UrlRepository
from .shortcode import is_valid


class LinkNotFound(Exception):
    """No link with that code, or it is no longer resolvable."""


class AliasRejected(Exception):
    """The requested alias is reserved or malformed."""


class AliasTaken(Exception):
    """The requested alias is already in use."""


class UrlService:
    """Link lifecycle, independent of HTTP.

    Extracted from the route handlers so the rules can be read, tested and
    changed without a web framework in the way. The handlers keep exactly one
    job: turning these exceptions into status codes.
    """

    def __init__(self, urls: UrlRepository, cache: Cache):
        self._urls = urls
        self._cache = cache

    def create(
        self,
        long_url: str,
        custom_code: str | None = None,
        ttl_seconds: int | None = None,
        owner: str | None = None,
    ) -> ShortUrl:
        if custom_code is not None and not is_valid(custom_code):
            raise AliasRejected(custom_code)
        expires_at = time.time() + ttl_seconds if ttl_seconds else None
        try:
            return self._urls.create(
                long_url=long_url,
                code=custom_code,
                expires_at=expires_at,
                owner=owner,
            )
        except CodeUnavailable as exc:
            raise AliasTaken(custom_code) from exc

    def get(self, code: str) -> ShortUrl:
        url = self._urls.get(code)
        if url is None:
            raise LinkNotFound(code)
        return url

    def deactivate(self, code: str) -> None:
        if not self._urls.deactivate(code):
            raise LinkNotFound(code)
        # Invalidating here rather than in the handler is the point of the
        # extraction: forgetting it is a correctness bug, and it should not be
        # possible to forget it by calling the service from somewhere new.
        self._cache.invalidate(code)

    def resolve(self, code: str) -> str:
        """The read path. Cache-aside, and deliberately unchanged by this refactor."""
        target = self._cache.get(code)
        if target is not None:
            return target

        url = self._urls.get(code)
        if url is None or not url.resolvable:
            raise LinkNotFound(code)
        self._cache.set(code, url.long_url)
        return url.long_url
'''

ROUTES = '''from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import RedirectResponse

from ..schemas import CreateUrlRequest, HealthResponse, ShortUrlResponse
from ..service import AliasRejected, AliasTaken, LinkNotFound

router = APIRouter()


def _to_response(request: Request, url) -> ShortUrlResponse:
    base = request.app.state.settings.base_url.rstrip("/")
    return ShortUrlResponse(
        code=url.code,
        short_url=base + "/" + url.code,
        long_url=url.long_url,
        created_at=url.created_at,
        expires_at=url.expires_at,
        active=url.active,
    )


@router.post("/api/v1/urls", response_model=ShortUrlResponse, status_code=201)
def create_url(payload: CreateUrlRequest, request: Request) -> ShortUrlResponse:
    try:
        url = request.app.state.service.create(
            long_url=str(payload.long_url),
            custom_code=payload.custom_code,
            ttl_seconds=payload.ttl_seconds,
            owner=payload.owner,
        )
    except AliasRejected:
        raise HTTPException(
            status_code=422, detail="custom_code is reserved or malformed"
        ) from None
    except AliasTaken:
        raise HTTPException(status_code=409, detail="custom_code already in use") from None
    return _to_response(request, url)


@router.get("/api/v1/urls/{code}", response_model=ShortUrlResponse)
def get_url(code: str, request: Request) -> ShortUrlResponse:
    try:
        url = request.app.state.service.get(code)
    except LinkNotFound:
        raise HTTPException(status_code=404, detail="unknown code") from None
    return _to_response(request, url)


@router.delete("/api/v1/urls/{code}", status_code=204)
def delete_url(code: str, request: Request) -> Response:
    try:
        request.app.state.service.deactivate(code)
    except LinkNotFound:
        raise HTTPException(status_code=404, detail="unknown code") from None
    return Response(status_code=204)


@router.get("/healthz", response_model=HealthResponse)
def healthz() -> HealthResponse:
    return HealthResponse(status="ok")


@router.get("/readyz", response_model=HealthResponse)
def readyz(request: Request) -> HealthResponse:
    try:
        request.app.state.db.connect().execute("SELECT 1").fetchone()
    except Exception as exc:  # noqa: BLE001 - readiness must report, not raise
        raise HTTPException(status_code=503, detail=f"database unavailable: {exc}") from exc
    return HealthResponse(status="ready", checks={"database": "ok"})


# Registered last on purpose: FastAPI resolves routes in declaration order, so a
# single-segment catch-all must not be able to shadow /healthz or /api/*.
@router.get("/{code}")
def redirect(code: str, request: Request) -> RedirectResponse:
    try:
        target = request.app.state.service.resolve(code)
    except LinkNotFound:
        raise HTTPException(status_code=404, detail="unknown or expired code") from None

    # 302, not 301: a permanent redirect is cached by the browser, so the
    # service never sees the request again and keeps no ability to observe it.
    return RedirectResponse(url=target, status_code=302)
'''

MAIN_ANCHOR = "    app.state.cache = LruTtlCache(settings.cache_capacity, settings.cache_ttl_s)"
MAIN_WIRING = (
    "    app.state.cache = LruTtlCache(settings.cache_capacity, settings.cache_ttl_s)\n"
    "    app.state.service = UrlService(urls, app.state.cache)"
)

IMPORT_ANCHOR = "from .repository import UrlRepository"
IMPORT_WIRING = "from .repository import UrlRepository\nfrom .service import UrlService"


def requirement(raw: str) -> NormalizedRequirement:
    return NormalizedRequirement(
        raw=raw,
        intent=(
            "Move link lifecycle rules out of the HTTP handlers into a service "
            "layer, so the rules can be read and tested without a web framework, "
            "with no change to what any endpoint returns."
        ),
        kind=RequirementKind.BROWNFIELD,
        clarity=Clarity.CLEAR,
        functional=[
            FunctionalRequirement(
                id="FR-001",
                statement="Link creation, lookup, deactivation and resolution move to a service",
            ),
            FunctionalRequirement(
                id="FR-002",
                statement=(
                    "Handlers translate domain errors into status codes and do nothing else"
                ),
            ),
            FunctionalRequirement(
                id="FR-003",
                statement="Every existing endpoint keeps its path, status codes and body",
            ),
        ],
        non_functional=[
            NonFunctionalRequirement(
                id="NFR-001",
                category="maintainability",
                statement=(
                    "The existing test suite must pass unedited; it is the "
                    "specification this change is measured against"
                ),
                target="zero modifications to existing test files",
            ),
            NonFunctionalRequirement(
                id="NFR-002",
                category="performance",
                statement="The cache-aside read path keeps its current shape and cost",
            ),
        ],
        ambiguities=[
            Ambiguity(
                id="AMB-001",
                question="Should the service be injected per request or built once at startup?",
                why_it_matters=(
                    "Per-request construction would rebuild the cache reference on "
                    "every call; it is cheap here but the habit does not scale."
                ),
                blocking=False,
                default_assumption=(
                    "Constructed once in the composition root and reached through "
                    "app.state, matching how every other dependency is already wired"
                ),
            )
        ],
        out_of_scope=[
            "Changing any endpoint's behaviour, including the known expiry defect",
            "Introducing a dependency-injection framework",
            "Splitting the repository or the cache",
        ],
    )


def architecture() -> Architecture:
    return Architecture(
        style="Layered: transport, service, persistence",
        summary=(
            "One seam added between the HTTP surface and the repository. Handlers "
            "parse and serialize; the service owns the rules; the repository owns "
            "storage. The interesting constraint is negative — no observable "
            "behaviour may change — which makes the existing suite the arbiter and "
            "leaves the refactor with nowhere to hide."
        ),
        components=[
            Component(
                name="UrlService",
                responsibility=(
                    "Link lifecycle rules: alias validation, expiry arithmetic, "
                    "cache invalidation on deactivation, cache-aside resolution"
                ),
                technology="Plain Python, no framework imports",
            ),
            Component(
                name="Route handlers",
                responsibility="Translate requests to service calls and domain errors to status codes",
                technology="FastAPI",
            ),
            Component(
                name="Domain exceptions",
                responsibility=(
                    "LinkNotFound, AliasRejected, AliasTaken — the vocabulary the "
                    "service raises in instead of HTTP status codes"
                ),
                technology="Python exceptions",
            ),
        ],
        data_flows=[
            DataFlow(
                source="Route handler",
                target="UrlService",
                description="Calls one method and catches a domain exception",
            ),
            DataFlow(
                source="UrlService",
                target="UrlRepository",
                description="Reads and writes links; unchanged by this refactor",
            ),
            DataFlow(
                source="UrlService",
                target="Read cache",
                description="Cache-aside resolution and invalidation on deactivation",
            ),
        ],
        trade_offs=[
            TradeOff(
                decision="Where cache invalidation lives",
                chosen="Inside the service, beside the deactivation it belongs to",
                alternatives=["Leave it in the delete handler as it is today"],
                rationale=(
                    "In the handler it is a step a future caller can forget, and "
                    "forgetting it means deleted links keep resolving. Next to the "
                    "write it guards, it cannot be skipped by calling the service "
                    "from somewhere new."
                ),
                accepted_cost=(
                    "The service now knows the cache exists, so it is no longer a "
                    "pure rules object"
                ),
            ),
            TradeOff(
                decision="How handlers learn what went wrong",
                chosen="Domain exceptions the handler maps to status codes",
                alternatives=["Return result objects", "Let the service raise HTTPException"],
                rationale=(
                    "A service that raises HTTPException is not extracted from the "
                    "web layer, it has dragged the web layer with it, and it cannot "
                    "be called from a worker or a CLI."
                ),
                accepted_cost="Three exception types and a try block per handler",
            ),
        ],
        risks=[
            Risk(
                id="RISK-001",
                description="A behavioural change slips in under cover of the restructuring",
                likelihood="medium",
                impact="high",
                mitigation=(
                    "The existing suite must pass unedited; the behaviour_preserved "
                    "check fails the run if any pre-existing test file was touched"
                ),
            ),
            Risk(
                id="RISK-002",
                description="The known expiry defect is silently fixed or silently worsened",
                likelihood="low",
                impact="medium",
                mitigation=(
                    "The read path is moved verbatim. Fixing it here would be a "
                    "behaviour change smuggled into a refactor; it belongs in its "
                    "own change with its own regression test"
                ),
            ),
        ],
        diagram=(
            "graph LR\n"
            "    Client --> Handlers[Route handlers]\n"
            "    Handlers -->|domain calls| Service[UrlService NEW]\n"
            "    Service --> Repo[UrlRepository]\n"
            "    Service --> Cache[LRU + TTL cache]\n"
            "    Repo --> DB[(sqlite)]"
        ),
    )


def api_contract() -> ApiContract:
    """Unchanged by definition — restated so the checks can prove it.

    Listing the whole existing surface is the point: if the refactor drops or
    renames an endpoint, the contract check catches it without anyone having to
    remember that it used to be there.
    """
    return ApiContract(
        title="URL Shortener — surface preserved across refactor",
        version="1.0.0",
        endpoints=[
            Endpoint(
                method="POST",
                path="/api/v1/urls",
                summary="Create a short link",
                covers=["FR-003"],
                request_model="CreateUrlRequest",
                response_model="ShortUrlResponse",
                status_codes=[201, 409, 422],
            ),
            Endpoint(
                method="GET",
                path="/api/v1/urls/{code}",
                summary="Read link metadata",
                covers=["FR-003"],
                response_model="ShortUrlResponse",
                status_codes=[200, 404],
            ),
            Endpoint(
                method="DELETE",
                path="/api/v1/urls/{code}",
                summary="Deactivate a link",
                covers=["FR-003"],
                status_codes=[204, 404],
            ),
            Endpoint(
                method="GET",
                path="/healthz",
                summary="Liveness",
                covers=["FR-003"],
                response_model="HealthResponse",
            ),
            Endpoint(
                method="GET",
                path="/readyz",
                summary="Readiness",
                covers=["FR-003"],
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


def plan() -> WorkPlan:
    return WorkPlan(
        strategy=(
            "One restructuring task, then verification. The work is not split "
            "because a half-extracted service is a broken application, and a task "
            "that cannot be validated on its own is not a task. No new tests are "
            "written: the existing suite is the specification, and adding tests "
            "here would quietly change what 'unchanged' means."
        ),
        parallelism_note="Nothing to parallelise; one change, then the suite that judges it.",
        tasks=[
            TaskSpec(
                id="T-010",
                title="Extract UrlService and reduce the handlers to translation",
                agent="implementation",
                depends_on=[],
                covers=["FR-001", "FR-002", "FR-003"],
                reads=["api_contract", "architecture"],
                writes=["artifacts_index", "implementation_summary"],
                risk=RiskLevel.MEDIUM,
                rationale="Rewrites existing source, so it is gated for human approval",
            ),
            TaskSpec(
                id="T-020",
                title="Verify behaviour is unchanged and the tests were not edited",
                agent="validation",
                depends_on=["T-010"],
                reads=["artifacts_index"],
                writes=["validation_report"],
                rationale=(
                    "Runs the untouched suite and fails the run if any pre-existing "
                    "test file changed"
                ),
            ),
            TaskSpec(
                id="T-030",
                title="Summarize the refactor for a reviewer",
                agent="summary",
                depends_on=["T-020"],
                reads=["requirement"],
                writes=["run_summary"],
            ),
        ],
    )


def code() -> CodeBundle:
    return CodeBundle(
        summary=(
            "Added app/service.py holding the link lifecycle rules, reduced the "
            "handlers to request translation and error mapping, and wired the "
            "service into the composition root. No endpoint changed."
        ),
        files=[
            GeneratedFile(
                path="app/service.py",
                content=SERVICE,
                kind=ArtifactKind.CODE,
                covers=["FR-001"],
            ),
            GeneratedFile(
                path="app/api/routes.py",
                content=ROUTES,
                kind=ArtifactKind.CODE,
                covers=["FR-002", "FR-003"],
            ),
            GeneratedFile(
                path="app/main.py",
                content=IMPORT_WIRING,
                replaces=IMPORT_ANCHOR,
                kind=ArtifactKind.CODE,
                covers=["FR-001"],
            ),
            GeneratedFile(
                path="app/main.py",
                content=MAIN_WIRING,
                replaces=MAIN_ANCHOR,
                kind=ArtifactKind.CODE,
                covers=["FR-001"],
            ),
        ],
        notes=[
            "The read path was moved verbatim, including the known expiry defect. "
            "Fixing it here would be a behaviour change hidden inside a refactor.",
            "Cache invalidation moved next to deactivation, where it cannot be "
            "forgotten by a future caller.",
            "The composition root is edited by two anchored edits rather than "
            "rewritten, so the diff stays reviewable.",
        ],
    )


__all__ = ["api_contract", "architecture", "code", "plan", "requirement"]
