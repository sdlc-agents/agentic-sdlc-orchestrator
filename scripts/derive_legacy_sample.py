"""Regenerate `sample_codebase/url_shortener_legacy` from the blueprint.

The brownfield scenario needs a service that genuinely predates the feature
being added, and it has to stay consistent with the blueprint the greenfield
scenario builds — otherwise the two runs quietly describe different systems.
So the legacy tree is derived by removing analytics from the blueprint rather
than maintained by hand.

Every removal asserts that it matched. If the blueprint changes shape this
fails loudly instead of emitting a legacy tree that no longer corresponds to it.

    python scripts/derive_legacy_sample.py

The output is committed; this only needs re-running when the blueprint changes.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from asep.providers.blueprints import url_shortener as bp  # noqa: E402

OUT = pathlib.Path(__file__).resolve().parents[1] / "sample_codebase" / "url_shortener_legacy"


def cut(text: str, start: str, end: str | None, label: str) -> str:
    i = text.find(start)
    assert i != -1, f"{label}: start marker not found"
    if end is None:
        return text[:i]
    j = text.find(end, i + len(start))
    assert j != -1, f"{label}: end marker not found"
    return text[:i] + text[j:]


def sub(text: str, old: str, new: str, label: str) -> str:
    assert old in text, f"{label}: pattern not found"
    return text.replace(old, new, 1)


def drop_lines(text: str, needle: str, label: str) -> str:
    kept = [ln for ln in text.splitlines(keepends=True) if needle not in ln]
    assert len(kept) < len(text.splitlines(keepends=True)), f"{label}: no lines dropped"
    return "".join(kept)


# --- app/config.py: no analytics tuning knobs exist yet -----------------------
config = drop_lines(bp.CONFIG, "analytics_", "config analytics settings")

# --- app/models.py: no ClickEvent entity -------------------------------------
models = cut(bp.MODELS, "\n\n@dataclass(frozen=True)\nclass ClickEvent:", None, "models ClickEvent")
models = models.rstrip("\n") + "\n"

# --- app/db.py: schema has no click_events table -----------------------------
db = cut(
    bp.DB,
    "\nCREATE TABLE IF NOT EXISTS click_events (",
    '\n"""',
    "db click_events table",
)
db = sub(
    db,
    """    sqlite3 connections are not safe to share across threads, and the analytics
    worker writes from its own thread, so each thread gets its own handle and WAL
    keeps the redirect reads from blocking behind those writes.""",
    """    sqlite3 connections are not safe to share across threads, so each thread
    gets its own handle, and WAL keeps readers from blocking behind writers.""",
    "db thread-safety docstring",
)

# --- app/repository.py: no ClickRepository -----------------------------------
repository = cut(bp.REPOSITORY, "\n\nclass ClickRepository:", None, "repo ClickRepository")
repository = repository.rstrip("\n") + "\n"
repository = sub(repository, "from collections import Counter\n", "", "repo Counter import")
repository = sub(
    repository,
    "from .models import ClickEvent, ShortUrl",
    "from .models import ShortUrl",
    "repo models import",
)

# --- app/schemas.py: no analytics transport models ---------------------------
schemas = cut(
    bp.SCHEMAS,
    "class ReferrerCount(BaseModel):",
    "class HealthResponse(BaseModel):",
    "schemas analytics models",
)

# --- app/api/routes.py: redirect does not record a click ---------------------
routes = sub(bp.ROUTES, "from ..models import ClickEvent\n", "", "routes ClickEvent import")
routes = sub(
    routes,
    """    request.app.state.analytics.record(
        ClickEvent(
            code=code,
            occurred_at=time.time(),
            referrer=request.headers.get("referer"),
            user_agent=request.headers.get("user-agent"),
        )
    )
""",
    "",
    "routes click recording",
)
routes = sub(
    routes,
    """    # 302, not 301: a permanent redirect is cached by the browser and the click
    # is never reported again, which silently destroys the analytics product.""",
    """    # 302, not 301: a permanent redirect is cached by the browser, so the
    # service never sees the request again and keeps no ability to observe it.""",
    "routes 302 comment",
)

# --- app/main.py: nothing constructs an analytics writer ---------------------
main = sub(bp.MAIN, "from .analytics import AnalyticsRecorder\n", "", "main analytics import")
main = sub(
    main,
    "from .repository import ClickRepository, UrlRepository",
    "from .repository import UrlRepository",
    "main repository import",
)
main = sub(
    main,
    """    clicks = ClickRepository(database)
    recorder = AnalyticsRecorder(
        clicks,
        batch_size=settings.analytics_batch,
        interval_s=settings.analytics_interval_s,
        queue_size=settings.analytics_queue_size,
    )
""",
    "",
    "main recorder construction",
)
main = sub(
    main,
    """    async def lifespan(app: FastAPI):
        recorder.start()
        try:
            yield
        finally:
            recorder.stop()
            database.close()
""",
    """    async def lifespan(app: FastAPI):
        try:
            yield
        finally:
            database.close()
""",
    "main lifespan",
)
main = drop_lines(main, "app.state.clicks", "main clicks state")
main = drop_lines(main, "app.state.analytics", "main analytics state")

# --- migrations: no click_events ---------------------------------------------
migration = cut(
    bp.MIGRATION,
    "\nCREATE TABLE IF NOT EXISTS click_events (",
    "\n-- Rollback",
    "migration click_events",
)
migration = sub(
    migration,
    """-- Reverse order: index idx_click_events_code_time, then table click_events,
-- then indexes idx_short_urls_expires and idx_short_urls_long_url, then table
-- short_urls.""",
    """-- Reverse order: indexes idx_short_urls_expires and idx_short_urls_long_url,
-- then table short_urls.""",
    "migration rollback prose",
)

# --- openapi.yaml: analytics path and schemas are not published --------------
openapi = cut(
    bp.OPENAPI,
    "  /api/v1/analytics/{code}:",
    "  /healthz:",
    "openapi analytics path",
)
openapi = cut(openapi, "    AnalyticsResponse:", None, "openapi AnalyticsResponse schema")
openapi = openapi.rstrip("\n") + "\n"
openapi = sub(
    openapi,
    "    click analytics collected asynchronously off the redirect path.\n",
    "",
    "openapi analytics description",
)

# --- tests: the legacy suite knows nothing about clicks ----------------------
conftest = sub(
    bp.CONFTEST,
    """        base_url="http://testserver",
        analytics_interval_s=0.05,
        analytics_batch=1,
    )""",
    """        base_url="http://testserver",
    )""",
    "conftest analytics settings",
)

requirements = bp.REQUIREMENTS

readme = """# URL Shortener (legacy)

The service as it exists today: create short links, resolve them through a
cache-aside read path, expire them, and deactivate them.

## What it does not do

Redirects are served and then forgotten. Nothing records who followed a link,
when, or from where, so there is no way to answer "how is this campaign doing?"
Adding that is the change the platform is asked to make against this tree.

## Layout

    app/config.py      runtime settings, all environment-overridable
    app/models.py      domain entities
    app/db.py          sqlite3 wrapper with per-thread connections
    app/shortcode.py   base62 code generation and validation
    app/cache.py       LRU + TTL cache in front of the read path
    app/repository.py  persistence for short URLs
    app/schemas.py     request/response transport models
    app/api/routes.py  HTTP surface
    app/main.py        composition root

## Running

    pip install -r requirements.txt
    uvicorn app.main:app --reload
    pytest -q
"""


FILES = {
    "app/__init__.py": '"""URL shortener service."""\n',
    "app/config.py": config,
    "app/models.py": models,
    "app/db.py": db,
    "app/shortcode.py": bp.SHORTCODE,
    "app/cache.py": bp.CACHE,
    "app/repository.py": repository,
    "app/schemas.py": schemas,
    "app/api/__init__.py": 'from .routes import router\n\n__all__ = ["router"]\n',
    "app/api/routes.py": routes,
    "app/main.py": main,
    "migrations/001_init.sql": migration,
    "openapi.yaml": openapi,
    "requirements.txt": requirements,
    "README.md": readme,
    "tests/conftest.py": conftest,
    "tests/test_shortcode.py": bp.TEST_SHORTCODE,
    "tests/test_api.py": bp.TEST_API,
}

if OUT.exists():
    import shutil

    shutil.rmtree(OUT)

for rel, content in FILES.items():
    path = OUT / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")

print(f"wrote {len(FILES)} files to {OUT}")
for rel in sorted(FILES):
    print("  ", rel)
