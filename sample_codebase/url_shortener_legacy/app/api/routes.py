from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import RedirectResponse

from ..repository import CodeUnavailable
from ..schemas import CreateUrlRequest, HealthResponse, ShortUrlResponse
from ..shortcode import is_valid

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
    repo = request.app.state.urls
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
    return _to_response(request, url)


@router.get("/api/v1/urls/{code}", response_model=ShortUrlResponse)
def get_url(code: str, request: Request) -> ShortUrlResponse:
    url = request.app.state.urls.get(code)
    if url is None:
        raise HTTPException(status_code=404, detail="unknown code")
    return _to_response(request, url)


@router.delete("/api/v1/urls/{code}", status_code=204)
def delete_url(code: str, request: Request) -> Response:
    if not request.app.state.urls.deactivate(code):
        raise HTTPException(status_code=404, detail="unknown code")
    request.app.state.cache.invalidate(code)
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
    cache = request.app.state.cache
    target = cache.get(code)

    if target is None:
        url = request.app.state.urls.get(code)
        if url is None or not url.resolvable:
            raise HTTPException(status_code=404, detail="unknown or expired code")
        target = url.long_url
        cache.set(code, target)

    # 302, not 301: a permanent redirect is cached by the browser, so the
    # service never sees the request again and keeps no ability to observe it.
    return RedirectResponse(url=target, status_code=302)
