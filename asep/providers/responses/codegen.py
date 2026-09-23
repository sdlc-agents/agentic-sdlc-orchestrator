"""Synthesise the HTTP layer from an approved API contract.

The contract determines the API surface, so the API surface is generated from
it rather than stored. Change an endpoint's path, status codes or response
model and the emitted module changes to match; remove an endpoint and its
handler disappears.

What is *not* derivable from an HTTP contract is the behaviour behind each
endpoint — a contract says "POST /api/v1/urls returns 201 or 409", not how to
allocate a short code or when to retry a collision. Those bodies come from
`OPERATIONS`, the way a scaffolding tool pairs generated wiring with a library
of implementations.

An endpoint the contract declares but `OPERATIONS` has no entry for is **left
out**, not stubbed. That is deliberate: the contract check then reports a
declared endpoint with no route, which is a true statement about the codebase
and something the repair loop can act on. A stub that raises would satisfy the
structural check while lying about being implemented.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ...models import ApiContract, Endpoint

HEADER = '''from __future__ import annotations

{imports}

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
'''

CATCH_ALL_NOTE = (
    "# Registered last on purpose: FastAPI resolves routes in declaration order, so a\n"
    "# single-segment catch-all must not be able to shadow /healthz or /api/*."
)


@dataclass(frozen=True)
class Operation:
    """One endpoint's implementation, independent of how it is routed."""

    handler: str
    signature: str
    returns: str
    body: str
    imports: frozenset[str] = field(default_factory=frozenset)


OPERATIONS: dict[tuple[str, str], Operation] = {
    ("POST", "/api/v1/urls"): Operation(
        handler="create_url",
        signature="payload: CreateUrlRequest, request: Request",
        returns="ShortUrlResponse",
        imports=frozenset({"time", "CreateUrlRequest", "CodeUnavailable", "is_valid"}),
        body='''    repo = request.app.state.urls
    if payload.custom_code is not None and not is_valid(payload.custom_code):
        raise HTTPException(status_code=422, detail="custom_code is reserved or malformed")
    expires_at = time.time() + payload.ttl_seconds if payload.ttl_seconds else None
    try:
        url = repo.create(
            long_url=str(payload.long_url),
            code=payload.custom_code,
            expires_at=expires_at,
            owner=payload.owner,
        )
    except CodeUnavailable:
        raise HTTPException(status_code=409, detail="custom_code already in use") from None
    return _to_response(request, url)''',
    ),
    ("GET", "/api/v1/urls/{code}"): Operation(
        handler="get_url",
        signature="code: str, request: Request",
        returns="ShortUrlResponse",
        body='''    url = request.app.state.urls.get(code)
    if url is None:
        raise HTTPException(status_code=404, detail="unknown code")
    return _to_response(request, url)''',
    ),
    ("DELETE", "/api/v1/urls/{code}"): Operation(
        handler="delete_url",
        signature="code: str, request: Request",
        returns="Response",
        imports=frozenset({"Response"}),
        body='''    if not request.app.state.urls.deactivate(code):
        raise HTTPException(status_code=404, detail="unknown code")
    request.app.state.cache.invalidate(code)
    return Response(status_code=204)''',
    ),
    ("GET", "/healthz"): Operation(
        handler="healthz",
        signature="",
        returns="HealthResponse",
        imports=frozenset({"HealthResponse"}),
        body='    return HealthResponse(status="ok")',
    ),
    ("GET", "/readyz"): Operation(
        handler="readyz",
        signature="request: Request",
        returns="HealthResponse",
        imports=frozenset({"HealthResponse"}),
        body='''    try:
        request.app.state.db.connect().execute("SELECT 1").fetchone()
    except Exception as exc:  # noqa: BLE001 - readiness must report, not raise
        raise HTTPException(status_code=503, detail=f"database unavailable: {exc}") from exc
    return HealthResponse(status="ready", checks={"database": "ok"})''',
    ),
    ("GET", "/{code}"): Operation(
        handler="redirect",
        signature="code: str, request: Request",
        returns="RedirectResponse",
        imports=frozenset({"RedirectResponse", "ClickEvent", "time"}),
        body='''    cache = request.app.state.cache
    target = cache.get(code)

    if target is None:
        url = request.app.state.urls.get(code)
        if url is None or not url.resolvable:
            raise HTTPException(status_code=404, detail="unknown or expired code")
        target = url.long_url
        cache.set(code, target)

    request.app.state.analytics.record(
        ClickEvent(
            code=code,
            occurred_at=time.time(),
            referrer=request.headers.get("referer"),
            user_agent=request.headers.get("user-agent"),
        )
    )
    # 302, not 301: a permanent redirect is cached by the browser and the click
    # is never reported again, which silently destroys the analytics product.
    return RedirectResponse(url=target, status_code=302)''',
    ),
}

# Where each importable name lives. Kept beside the operations so adding one
# does not mean remembering to update an import list somewhere else.
IMPORT_SOURCES = {
    "time": "import time",
    "Response": "from fastapi import Response",
    "RedirectResponse": "from fastapi.responses import RedirectResponse",
    "ClickEvent": "from ..models import ClickEvent",
    "CodeUnavailable": "from ..repository import CodeUnavailable",
    "is_valid": "from ..shortcode import is_valid",
}
SCHEMA_NAMES = {
    "CreateUrlRequest",
    "ShortUrlResponse",
    "HealthResponse",
    "AnalyticsResponse",
}


def specificity(endpoint: Endpoint) -> tuple[int, int]:
    """Sort key placing catch-all routes last.

    FastAPI matches in declaration order, so `GET /{code}` registered before
    `/healthz` swallows it. The generator works this out rather than trusting
    whoever writes the contract to list endpoints in a safe order.
    """
    segments = [s for s in endpoint.path.strip("/").split("/") if s]
    parameters = sum(1 for s in segments if s.startswith("{"))
    literal_first = 0 if segments and not segments[0].startswith("{") else 1
    return (literal_first, -(len(segments) - parameters))


def success_code(endpoint: Endpoint) -> int | None:
    """The 2xx the contract promises, if it is not FastAPI's default.

    3xx is excluded: a redirect carries its own status on the response object,
    and pinning it in the decorator as well is redundant.
    """
    for code in sorted(endpoint.status_codes):
        if 200 <= code < 300:
            return None if code == 200 else code
    return None


def _shape(path: str) -> str:
    """Path with parameter names flattened, so `/urls/{id}` matches `/urls/{code}`.

    What a parameter is called is a contract-authoring choice, not a different
    operation. Moving an endpoint to a genuinely different path *is* a
    different operation, and is reported as unimplemented rather than guessed.
    """
    parts = [
        "{}" if s.startswith("{") and s.endswith("}") else s
        for s in path.strip("/").split("/")
    ]
    return "/" + "/".join(parts)


BY_SHAPE = {(method, _shape(path)): op for (method, path), op in OPERATIONS.items()}


def lookup(endpoint: Endpoint) -> Operation | None:
    return BY_SHAPE.get((endpoint.method.upper(), _shape(endpoint.path)))


def implemented(contract: ApiContract) -> list[Endpoint]:
    return [e for e in contract.endpoints if lookup(e) is not None]


def unimplemented(contract: ApiContract) -> list[Endpoint]:
    return [e for e in contract.endpoints if lookup(e) is None]


def render_routes(contract: ApiContract) -> str:
    """Emit the routing module for every endpoint the contract declares."""
    endpoints = sorted(implemented(contract), key=specificity)

    needed: set[str] = set()
    for endpoint in endpoints:
        operation = lookup(endpoint)
        needed |= operation.imports
        if endpoint.response_model:
            needed.add(endpoint.response_model)

    fastapi_names = {"APIRouter", "HTTPException", "Request"}
    if "Response" in needed:
        fastapi_names.add("Response")
    schemas = sorted(n for n in needed if n in SCHEMA_NAMES)

    imports = []
    if "time" in needed:
        imports += ["import time", ""]
    imports.append(f"from fastapi import {', '.join(sorted(fastapi_names))}")
    if "RedirectResponse" in needed:
        imports.append(IMPORT_SOURCES["RedirectResponse"])
    imports.append("")
    for name in ("ClickEvent", "CodeUnavailable"):
        if name in needed:
            imports.append(IMPORT_SOURCES[name])
    if schemas:
        imports.append(f"from ..schemas import {', '.join(schemas)}")
    if "is_valid" in needed:
        imports.append(IMPORT_SOURCES["is_valid"])

    parts = [HEADER.format(imports="\n".join(imports).strip())]

    for endpoint in endpoints:
        operation = lookup(endpoint)
        decorator = [f'"{endpoint.path}"']
        if endpoint.response_model:
            decorator.append(f"response_model={endpoint.response_model}")
        code = success_code(endpoint)
        if code is not None:
            decorator.append(f"status_code={code}")

        block = []
        if specificity(endpoint)[0] == 1:
            block.append(CATCH_ALL_NOTE)
        block.append(
            f"@router.{endpoint.method.lower()}({', '.join(decorator)})\n"
            f"def {operation.handler}({operation.signature}) -> {operation.returns}:\n"
            f"{operation.body}"
        )
        parts.append("\n".join(block))

    # Two blank lines between top-level definitions, as PEP 8 asks. Generated
    # code that a linter would complain about undermines the claim that it is
    # production-shaped.
    return "\n\n\n".join(part.rstrip() for part in parts).rstrip() + "\n"


__all__ = [
    "OPERATIONS",
    "Operation",
    "implemented",
    "lookup",
    "render_routes",
    "specificity",
    "unimplemented",
]
