from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    """Runtime configuration. Every value is overridable by environment."""

    db_path: str = os.getenv("URLS_DB_PATH", "urls.db")
    base_url: str = os.getenv("URLS_BASE_URL", "http://localhost:8000")
    code_length: int = int(os.getenv("URLS_CODE_LENGTH", "7"))
    cache_capacity: int = int(os.getenv("URLS_CACHE_CAPACITY", "10000"))
    cache_ttl_s: float = float(os.getenv("URLS_CACHE_TTL_S", "300"))
    max_collision_retries: int = 5
