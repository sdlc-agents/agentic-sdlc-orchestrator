from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass(frozen=True)
class ShortUrl:
    """Domain entity. Deliberately separate from the transport schemas so the
    wire format can change without touching persistence."""

    code: str
    long_url: str
    created_at: float
    expires_at: float | None = None
    owner: str | None = None
    active: bool = True

    @property
    def expired(self) -> bool:
        return self.expires_at is not None and self.expires_at <= time.time()

    @property
    def resolvable(self) -> bool:
        return self.active and not self.expired
