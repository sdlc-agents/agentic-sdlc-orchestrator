from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from .api.routes import router
from .cache import LruTtlCache
from .config import Settings
from .db import Database
from .repository import UrlRepository


def create_app(settings: Settings | None = None) -> FastAPI:
    """Composition root. Every dependency is constructed here and reached through
    `app.state`, which is what lets the tests swap in a temporary database
    without patching module globals."""

    settings = settings or Settings()
    database = Database(settings.db_path)
    database.init_schema()

    urls = UrlRepository(database, settings.code_length, settings.max_collision_retries)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            yield
        finally:
            database.close()

    app = FastAPI(title="URL Shortener", version="1.0.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.db = database
    app.state.urls = urls
    app.state.cache = LruTtlCache(settings.cache_capacity, settings.cache_ttl_s)
    app.include_router(router)
    return app


app = create_app()
