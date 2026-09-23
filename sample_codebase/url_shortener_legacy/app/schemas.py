from __future__ import annotations

from pydantic import AnyHttpUrl, BaseModel, Field


class CreateUrlRequest(BaseModel):
    """AnyHttpUrl is the security control: it rejects javascript:, data: and
    file: targets, which would otherwise turn every short link into a redirect
    gadget."""

    long_url: AnyHttpUrl
    custom_code: str | None = Field(default=None, min_length=4, max_length=16)
    ttl_seconds: int | None = Field(default=None, ge=60, le=60 * 60 * 24 * 365)
    owner: str | None = None


class ShortUrlResponse(BaseModel):
    code: str
    short_url: str
    long_url: str
    created_at: float
    expires_at: float | None = None
    active: bool = True


class HealthResponse(BaseModel):
    status: str
    checks: dict[str, str] = {}
