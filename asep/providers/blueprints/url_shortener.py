"""Deterministic source blueprint for the URL shortener.

The mock provider serves these files as the implementation agent's output so the
platform can be evaluated end to end without an API key. They are ordinary
FastAPI + stdlib-sqlite3 sources: the generated tests really execute during the
validation stage rather than being reported as skipped.

`ROUTES` intentionally omits the analytics endpoint that `OPENAPI` and the API
contract declare. Nothing special-cases that gap: the contract check and the
generated test suite both discover it, and the repair agent closes it with
`ANALYTICS_ROUTE`. It is the fixture that makes the repair loop demonstrable on
a real defect.
"""

from __future__ import annotations

CONFIG = '''from __future__ import annotations

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
    analytics_batch: int = int(os.getenv("URLS_ANALYTICS_BATCH", "50"))
    analytics_interval_s: float = float(os.getenv("URLS_ANALYTICS_INTERVAL_S", "1.0"))
    analytics_queue_size: int = int(os.getenv("URLS_ANALYTICS_QUEUE", "10000"))
    max_collision_retries: int = 5
'''

MODELS = '''from __future__ import annotations

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


@dataclass(frozen=True)
class ClickEvent:
    code: str
    occurred_at: float
    referrer: str | None = None
    user_agent: str | None = None
'''

DB = '''from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS short_urls (
    code       TEXT PRIMARY KEY,
    long_url   TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL,
    owner      TEXT,
    active     INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_short_urls_long_url ON short_urls(long_url);
CREATE INDEX IF NOT EXISTS idx_short_urls_expires  ON short_urls(expires_at);

CREATE TABLE IF NOT EXISTS click_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT NOT NULL,
    occurred_at REAL NOT NULL,
    referrer    TEXT,
    user_agent  TEXT
);
CREATE INDEX IF NOT EXISTS idx_click_events_code_time
    ON click_events(code, occurred_at);
"""


class Database:
    """Thin sqlite3 wrapper with per-thread connections.

    sqlite3 connections are not safe to share across threads, and the analytics
    worker writes from its own thread, so each thread gets its own handle and WAL
    keeps the redirect reads from blocking behind those writes.
    """

    def __init__(self, path: str):
        self.path = path
        self._local = threading.local()
        self._all: list[sqlite3.Connection] = []
        self._lock = threading.Lock()

    def connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=5.0, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
            with self._lock:
                self._all.append(conn)
        return conn

    def init_schema(self) -> None:
        conn = self.connect()
        conn.executescript(SCHEMA)
        conn.commit()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def close(self) -> None:
        with self._lock:
            for conn in self._all:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            self._all.clear()
'''

SHORTCODE = '''from __future__ import annotations

import re
import secrets

ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
BASE = len(ALPHABET)
_VALID = re.compile(r"^[0-9A-Za-z]{4,16}$")

# Paths the service owns. A user-supplied alias may never shadow one of them.
RESERVED = frozenset(
    {"api", "health", "healthz", "readyz", "docs", "redoc", "openapi", "metrics", "static"}
)


def generate(length: int = 7) -> str:
    """Random code from a CSPRNG.

    Random rather than a counter: sequential ids let anyone enumerate every link
    in the system. The cost is collisions, which the repository retries against
    the primary-key constraint. At 62^7 the birthday bound is far beyond the
    volume this service is sized for.
    """
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def encode(number: int) -> str:
    if number < 0:
        raise ValueError("number must be non-negative")
    if number == 0:
        return ALPHABET[0]
    out: list[str] = []
    while number:
        number, rem = divmod(number, BASE)
        out.append(ALPHABET[rem])
    return "".join(reversed(out))


def decode(code: str) -> int:
    total = 0
    for char in code:
        total = total * BASE + ALPHABET.index(char)
    return total


def is_valid(code: str) -> bool:
    return bool(_VALID.match(code)) and code.lower() not in RESERVED
'''

CACHE = '''from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any, Protocol


class Cache(Protocol):
    def get(self, key: str) -> Any | None: ...
    def set(self, key: str, value: Any) -> None: ...
    def invalidate(self, key: str) -> None: ...


class LruTtlCache:
    """In-process cache-aside store for the redirect hot path.

    The interface is the seam: swapping this for Redis in production is a
    constructor change in `main.create_app`, because nothing else imports it.
    """

    def __init__(self, capacity: int = 10_000, ttl_s: float = 300.0):
        self._capacity = capacity
        self._ttl = ttl_s
        self._data: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> Any | None:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                self.misses += 1
                return None
            expires_at, value = entry
            if expires_at <= time.time():
                del self._data[key]
                self.misses += 1
                return None
            self._data.move_to_end(key)
            self.hits += 1
            return value

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = (time.time() + self._ttl, value)
            self._data.move_to_end(key)
            while len(self._data) > self._capacity:
                self._data.popitem(last=False)

    def invalidate(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)

    def stats(self) -> dict[str, int | float]:
        total = self.hits + self.misses
        return {
            "hits": self.hits,
            "misses": self.misses,
            "size": len(self._data),
            "hit_ratio": round(self.hits / total, 4) if total else 0.0,
        }
'''

REPOSITORY = '''from __future__ import annotations

import sqlite3
import time
from collections import Counter

from .db import Database
from .models import ClickEvent, ShortUrl
from .shortcode import generate


class CodeUnavailable(Exception):
    """The requested alias is already taken."""


class UrlRepository:
    def __init__(self, db: Database, code_length: int = 7, max_retries: int = 5):
        self._db = db
        self._code_length = code_length
        self._max_retries = max_retries

    @staticmethod
    def _row_to_url(row: sqlite3.Row) -> ShortUrl:
        return ShortUrl(
            code=row["code"],
            long_url=row["long_url"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            owner=row["owner"],
            active=bool(row["active"]),
        )

    def create(
        self,
        long_url: str,
        code: str | None = None,
        expires_at: float | None = None,
        owner: str | None = None,
    ) -> ShortUrl:
        """Insert a mapping, retrying generated-code collisions.

        The uniqueness decision belongs to the database, not to a read-then-write
        check in the application: two concurrent writers would both pass a check
        and one would still lose. The PRIMARY KEY is the arbiter.
        """
        attempts = 1 if code else self._max_retries
        for attempt in range(attempts):
            candidate = code or generate(self._code_length)
            try:
                with self._db.tx() as conn:
                    conn.execute(
                        "INSERT INTO short_urls (code, long_url, created_at, expires_at,"
                        " owner, active) VALUES (?, ?, ?, ?, ?, 1)",
                        (candidate, long_url, time.time(), expires_at, owner),
                    )
                return self.get(candidate)  # type: ignore[return-value]
            except sqlite3.IntegrityError:
                if code is not None:
                    raise CodeUnavailable(candidate) from None
                if attempt == attempts - 1:
                    raise
        raise CodeUnavailable("exhausted code generation retries")

    def get(self, code: str) -> ShortUrl | None:
        row = (
            self._db.connect()
            .execute("SELECT * FROM short_urls WHERE code = ?", (code,))
            .fetchone()
        )
        return self._row_to_url(row) if row else None

    def deactivate(self, code: str) -> bool:
        with self._db.tx() as conn:
            cur = conn.execute(
                "UPDATE short_urls SET active = 0 WHERE code = ? AND active = 1", (code,)
            )
        return cur.rowcount > 0

    def purge_expired(self, now: float | None = None) -> int:
        now = now if now is not None else time.time()
        with self._db.tx() as conn:
            cur = conn.execute(
                "UPDATE short_urls SET active = 0"
                " WHERE active = 1 AND expires_at IS NOT NULL AND expires_at <= ?",
                (now,),
            )
        return cur.rowcount


class ClickRepository:
    def __init__(self, db: Database):
        self._db = db

    def record_many(self, events: list[ClickEvent]) -> int:
        if not events:
            return 0
        with self._db.tx() as conn:
            conn.executemany(
                "INSERT INTO click_events (code, occurred_at, referrer, user_agent)"
                " VALUES (?, ?, ?, ?)",
                [(e.code, e.occurred_at, e.referrer, e.user_agent) for e in events],
            )
        return len(events)

    def summary(self, code: str) -> dict:
        conn = self._db.connect()
        totals = conn.execute(
            "SELECT COUNT(*) AS total, MAX(occurred_at) AS last_click"
            " FROM click_events WHERE code = ?",
            (code,),
        ).fetchone()
        by_day = conn.execute(
            "SELECT date(occurred_at, 'unixepoch') AS day, COUNT(*) AS clicks"
            " FROM click_events WHERE code = ? GROUP BY day ORDER BY day DESC LIMIT 30",
            (code,),
        ).fetchall()
        referrers = conn.execute(
            "SELECT referrer FROM click_events WHERE code = ? AND referrer IS NOT NULL",
            (code,),
        ).fetchall()
        top = Counter(r["referrer"] for r in referrers).most_common(5)
        return {
            "total_clicks": totals["total"] or 0,
            "last_click_at": totals["last_click"],
            "clicks_by_day": {r["day"]: r["clicks"] for r in by_day},
            "top_referrers": [{"referrer": k, "clicks": v} for k, v in top],
        }
'''

ANALYTICS = '''from __future__ import annotations

import queue
import threading
import time

from .models import ClickEvent
from .repository import ClickRepository


class AnalyticsRecorder:
    """Buffers click events off the redirect path and writes them in batches.

    Recording is explicitly lossy under overload: if the queue is full the event
    is dropped and counted rather than blocking a redirect. Analytics are a
    reporting concern, and a 300ms redirect to protect a click count is a worse
    outcome than an approximate click count. `dropped` is exported so the loss is
    visible instead of silent.
    """

    def __init__(
        self,
        repository: ClickRepository,
        batch_size: int = 50,
        interval_s: float = 1.0,
        queue_size: int = 10_000,
    ):
        self._repo = repository
        self._batch = batch_size
        self._interval = interval_s
        self._queue: queue.Queue[ClickEvent] = queue.Queue(maxsize=queue_size)
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self.dropped = 0
        self.written = 0

    def start(self) -> None:
        if self._worker is not None:
            return
        self._stop.clear()
        self._worker = threading.Thread(
            target=self._loop, name="analytics-writer", daemon=True
        )
        self._worker.start()

    def stop(self) -> None:
        self._stop.set()
        if self._worker is not None:
            self._worker.join(timeout=5.0)
            self._worker = None
        self.flush()

    def record(self, event: ClickEvent) -> bool:
        try:
            self._queue.put_nowait(event)
            return True
        except queue.Full:
            self.dropped += 1
            return False

    def _write(self, events: list[ClickEvent]) -> int:
        if not events:
            return 0
        written = self._repo.record_many(events)
        self.written += written
        for _ in events:
            self._queue.task_done()
        return written

    def flush(self) -> int:
        """Drain and persist everything currently accepted.

        Draining the queue is not sufficient on its own: the worker may already
        hold an event it has not written yet, which is invisible to a caller
        polling the queue. `task_done`/`join` is the handoff that closes that
        window, so flush is a real barrier rather than a best effort.
        """
        drained: list[ClickEvent] = []
        while True:
            try:
                drained.append(self._queue.get_nowait())
            except queue.Empty:
                break
        written = self._write(drained)
        self._queue.join()
        return written

    def _loop(self) -> None:
        pending: list[ClickEvent] = []
        last_write = time.monotonic()
        while not self._stop.is_set():
            try:
                pending.append(self._queue.get(timeout=self._interval))
            except queue.Empty:
                pass
            due = time.monotonic() - last_write >= self._interval
            if pending and (len(pending) >= self._batch or due):
                self._write(pending)
                pending = []
                last_write = time.monotonic()
        self._write(pending)

    def stats(self) -> dict[str, int]:
        return {"written": self.written, "dropped": self.dropped, "queued": self._queue.qsize()}
'''

SCHEMAS = '''from __future__ import annotations

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


class ReferrerCount(BaseModel):
    referrer: str
    clicks: int


class AnalyticsResponse(BaseModel):
    code: str
    total_clicks: int
    last_click_at: float | None = None
    clicks_by_day: dict[str, int] = {}
    top_referrers: list[ReferrerCount] = []


class HealthResponse(BaseModel):
    status: str
    checks: dict[str, str] = {}
'''

ROUTES = '''from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import RedirectResponse

from ..models import ClickEvent
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
    return RedirectResponse(url=target, status_code=302)
'''

ANALYTICS_ROUTE = '''

@router.get("/api/v1/analytics/{code}", response_model=AnalyticsResponse)
def get_analytics(code: str, request: Request) -> AnalyticsResponse:
    if request.app.state.urls.get(code) is None:
        raise HTTPException(status_code=404, detail="unknown code")
    summary = request.app.state.clicks.summary(code)
    return AnalyticsResponse(code=code, **summary)
'''

MAIN = '''from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from .analytics import AnalyticsRecorder
from .api.routes import router
from .cache import LruTtlCache
from .config import Settings
from .db import Database
from .repository import ClickRepository, UrlRepository


def create_app(settings: Settings | None = None) -> FastAPI:
    """Composition root. Every dependency is constructed here and reached through
    `app.state`, which is what lets the tests swap in a temporary database
    without patching module globals."""

    settings = settings or Settings()
    database = Database(settings.db_path)
    database.init_schema()

    urls = UrlRepository(database, settings.code_length, settings.max_collision_retries)
    clicks = ClickRepository(database)
    recorder = AnalyticsRecorder(
        clicks,
        batch_size=settings.analytics_batch,
        interval_s=settings.analytics_interval_s,
        queue_size=settings.analytics_queue_size,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        recorder.start()
        try:
            yield
        finally:
            recorder.stop()
            database.close()

    app = FastAPI(title="URL Shortener", version="1.0.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.db = database
    app.state.urls = urls
    app.state.clicks = clicks
    app.state.cache = LruTtlCache(settings.cache_capacity, settings.cache_ttl_s)
    app.state.analytics = recorder
    app.include_router(router)
    return app


app = create_app()
'''

CONFTEST = '''from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        db_path=str(tmp_path / "test.db"),
        base_url="http://testserver",
        analytics_interval_s=0.05,
        analytics_batch=1,
    )


@pytest.fixture
def client(settings) -> TestClient:
    with TestClient(create_app(settings)) as test_client:
        yield test_client
'''

TEST_SHORTCODE = '''from __future__ import annotations

import pytest

from app.shortcode import RESERVED, decode, encode, generate, is_valid


def test_generate_respects_length():
    assert len(generate(7)) == 7
    assert len(generate(12)) == 12


def test_generated_codes_are_valid_and_unique_enough():
    codes = {generate(7) for _ in range(2000)}
    assert len(codes) == 2000
    assert all(is_valid(code) for code in codes)


@pytest.mark.parametrize("number", [0, 1, 61, 62, 3843, 987654321])
def test_base62_roundtrip(number):
    assert decode(encode(number)) == number


def test_encode_rejects_negative():
    with pytest.raises(ValueError):
        encode(-1)


@pytest.mark.parametrize("code", sorted(RESERVED))
def test_reserved_paths_are_not_valid_codes(code):
    assert is_valid(code) is False


@pytest.mark.parametrize("code", ["abc", "a" * 17, "has space", "sym!bol"])
def test_malformed_codes_rejected(code):
    assert is_valid(code) is False
'''

TEST_API = '''from __future__ import annotations

TARGET = "https://example.com/some/deep/path?q=1"


def test_create_returns_201_with_short_url(client):
    response = client.post("/api/v1/urls", json={"long_url": TARGET})
    assert response.status_code == 201
    body = response.json()
    assert body["long_url"] == TARGET
    assert body["short_url"].endswith(body["code"])
    assert body["active"] is True


def test_redirect_is_302_to_target(client):
    code = client.post("/api/v1/urls", json={"long_url": TARGET}).json()["code"]
    response = client.get("/" + code, follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == TARGET


def test_unknown_code_is_404(client):
    assert client.get("/nosuchcode", follow_redirects=False).status_code == 404


def test_non_http_scheme_is_rejected(client):
    response = client.post("/api/v1/urls", json={"long_url": "javascript:alert(1)"})
    assert response.status_code == 422


def test_custom_alias_is_honoured_then_conflicts(client):
    first = client.post(
        "/api/v1/urls", json={"long_url": TARGET, "custom_code": "schwab1"}
    )
    assert first.status_code == 201
    assert first.json()["code"] == "schwab1"

    second = client.post(
        "/api/v1/urls", json={"long_url": TARGET, "custom_code": "schwab1"}
    )
    assert second.status_code == 409


def test_reserved_alias_is_rejected(client):
    response = client.post(
        "/api/v1/urls", json={"long_url": TARGET, "custom_code": "healthz"}
    )
    assert response.status_code == 422


def test_expired_link_stops_resolving(client):
    code = client.post(
        "/api/v1/urls", json={"long_url": TARGET, "ttl_seconds": 60}
    ).json()["code"]
    client.app.state.urls.purge_expired(now=9_999_999_999)
    client.app.state.cache.invalidate(code)
    assert client.get("/" + code, follow_redirects=False).status_code == 404


def test_delete_removes_link_from_cache_and_resolution(client):
    code = client.post("/api/v1/urls", json={"long_url": TARGET}).json()["code"]
    assert client.get("/" + code, follow_redirects=False).status_code == 302
    assert client.delete("/api/v1/urls/" + code).status_code == 204
    assert client.get("/" + code, follow_redirects=False).status_code == 404


def test_metadata_endpoint(client):
    code = client.post("/api/v1/urls", json={"long_url": TARGET}).json()["code"]
    body = client.get("/api/v1/urls/" + code).json()
    assert body["code"] == code
    assert client.get("/api/v1/urls/missing").status_code == 404


def test_health_endpoints_are_not_shadowed_by_the_catch_all(client):
    assert client.get("/healthz").json()["status"] == "ok"
    assert client.get("/readyz").json()["status"] == "ready"
'''

TEST_ANALYTICS = '''from __future__ import annotations

import time

from app.analytics import AnalyticsRecorder
from app.db import Database
from app.models import ClickEvent
from app.repository import ClickRepository

TARGET = "https://example.com/analytics-target"


def _create(client) -> str:
    return client.post("/api/v1/urls", json={"long_url": TARGET}).json()["code"]


def test_clicks_are_counted(client):
    code = _create(client)
    for _ in range(3):
        client.get("/" + code, follow_redirects=False)
    client.app.state.analytics.flush()

    body = client.get("/api/v1/analytics/" + code).json()
    assert body["code"] == code
    assert body["total_clicks"] == 3
    assert body["last_click_at"] is not None


def test_referrers_are_attributed(client):
    code = _create(client)
    client.get("/" + code, headers={"referer": "https://news.example"}, follow_redirects=False)
    client.get("/" + code, headers={"referer": "https://news.example"}, follow_redirects=False)
    client.get("/" + code, headers={"referer": "https://blog.example"}, follow_redirects=False)
    client.app.state.analytics.flush()

    top = client.get("/api/v1/analytics/" + code).json()["top_referrers"]
    assert top[0] == {"referrer": "https://news.example", "clicks": 2}


def test_analytics_for_unknown_code_is_404(client):
    assert client.get("/api/v1/analytics/nosuchcode").status_code == 404


def test_recorder_drops_instead_of_blocking(settings):
    """Overload must shed load, not slow the redirect path down."""
    db = Database(settings.db_path)
    db.init_schema()
    recorder = AnalyticsRecorder(ClickRepository(db), queue_size=2)
    try:
        accepted = [
            recorder.record(ClickEvent(code="abcd", occurred_at=time.time()))
            for _ in range(5)
        ]
        assert accepted.count(True) == 2
        assert recorder.dropped == 3
        assert recorder.flush() == 2
    finally:
        db.close()
'''

OPENAPI = '''openapi: 3.0.3
info:
  title: URL Shortener
  version: 1.0.0
  description: >
    Create short links, resolve them with a cache-aside read path, and report
    click analytics collected asynchronously off the redirect path.
paths:
  /api/v1/urls:
    post:
      summary: Create a short URL
      operationId: createUrl
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/CreateUrlRequest'
      responses:
        '201':
          description: created
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ShortUrlResponse'
        '409': { description: custom_code already in use }
        '422': { description: invalid target url or alias }
  /api/v1/urls/{code}:
    get:
      summary: Fetch short URL metadata
      operationId: getUrl
      parameters:
        - { name: code, in: path, required: true, schema: { type: string } }
      responses:
        '200':
          description: ok
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ShortUrlResponse'
        '404': { description: unknown code }
    delete:
      summary: Deactivate a short URL
      operationId: deleteUrl
      parameters:
        - { name: code, in: path, required: true, schema: { type: string } }
      responses:
        '204': { description: deactivated }
        '404': { description: unknown code }
  /api/v1/analytics/{code}:
    get:
      summary: Click analytics for a short URL
      operationId: getAnalytics
      parameters:
        - { name: code, in: path, required: true, schema: { type: string } }
      responses:
        '200':
          description: ok
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/AnalyticsResponse'
        '404': { description: unknown code }
  /healthz:
    get:
      summary: Liveness probe
      operationId: healthz
      responses:
        '200': { description: ok }
  /readyz:
    get:
      summary: Readiness probe, including database reachability
      operationId: readyz
      responses:
        '200': { description: ready }
        '503': { description: dependency unavailable }
  /{code}:
    get:
      summary: Resolve a short code and redirect
      operationId: redirect
      parameters:
        - { name: code, in: path, required: true, schema: { type: string } }
      responses:
        '302': { description: redirect to the target url }
        '404': { description: unknown or expired code }
components:
  schemas:
    CreateUrlRequest:
      type: object
      required: [long_url]
      properties:
        long_url: { type: string, format: uri }
        custom_code: { type: string, minLength: 4, maxLength: 16 }
        ttl_seconds: { type: integer, minimum: 60 }
        owner: { type: string }
    ShortUrlResponse:
      type: object
      required: [code, short_url, long_url, created_at]
      properties:
        code: { type: string }
        short_url: { type: string }
        long_url: { type: string }
        created_at: { type: number }
        expires_at: { type: number, nullable: true }
        active: { type: boolean }
    AnalyticsResponse:
      type: object
      required: [code, total_clicks]
      properties:
        code: { type: string }
        total_clicks: { type: integer }
        last_click_at: { type: number, nullable: true }
        clicks_by_day:
          type: object
          additionalProperties: { type: integer }
        top_referrers:
          type: array
          items:
            type: object
            properties:
              referrer: { type: string }
              clicks: { type: integer }
'''

MIGRATION = '''-- 001_init.sql
-- Forward migration for the URL shortener.
-- Both statements are idempotent so a partially applied migration can be re-run.

CREATE TABLE IF NOT EXISTS short_urls (
    code       TEXT PRIMARY KEY,
    long_url   TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL,
    owner      TEXT,
    active     INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_short_urls_long_url ON short_urls(long_url);
CREATE INDEX IF NOT EXISTS idx_short_urls_expires  ON short_urls(expires_at);

CREATE TABLE IF NOT EXISTS click_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT NOT NULL,
    occurred_at REAL NOT NULL,
    referrer    TEXT,
    user_agent  TEXT
);

CREATE INDEX IF NOT EXISTS idx_click_events_code_time
    ON click_events(code, occurred_at);

-- Rollback, described rather than executable on purpose: the platform's policy
-- layer forbids generated artifacts from containing destructive DDL, so undoing
-- this migration is a deliberate human step, not a statement an agent can run.
-- Reverse order: index idx_click_events_code_time, then table click_events,
-- then indexes idx_short_urls_expires and idx_short_urls_long_url, then table
-- short_urls.
'''

REQUIREMENTS = '''fastapi>=0.110
uvicorn[standard]>=0.29
pydantic>=2.6
httpx>=0.27
pytest>=7.4
'''

README = '''# URL Shortener

Generated by the Agentic Software Engineering Platform from the requirement:

> Build a scalable URL shortener service with APIs, persistence, and analytics.

## Run

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

```bash
curl -X POST localhost:8000/api/v1/urls \\
  -H 'content-type: application/json' \\
  -d '{"long_url": "https://example.com/a/very/long/path"}'

curl -i localhost:8000/<code>
curl localhost:8000/api/v1/analytics/<code>
```

## Test

```bash
pytest -q
```

## Design

| Concern | Decision | Why |
| --- | --- | --- |
| Code generation | 7-char CSPRNG base62 | sequential ids let anyone enumerate every link |
| Collisions | database PRIMARY KEY + retry | a read-then-write check loses under concurrency |
| Read path | cache-aside, LRU + TTL | redirects are the hot path and are read-dominated |
| Redirect status | 302, not 301 | 301 is cached by browsers and analytics stop arriving |
| Analytics write | queued, batched, lossy under overload | a redirect must not wait on a reporting write |
| Persistence | sqlite3 behind a repository | the port is the seam; Postgres is an adapter swap |

## Production gaps

This is a prototype. Before production it needs: Postgres and Redis adapters in
place of the sqlite3 and in-process implementations, rate limiting on link
creation, a malware/phishing check on submitted targets, authentication on
create and delete, and the analytics worker moved out of process onto a durable
queue so events survive a restart.
'''

FILES: dict[str, str] = {
    "app/__init__.py": '"""URL shortener service."""\n',
    "app/config.py": CONFIG,
    "app/models.py": MODELS,
    "app/db.py": DB,
    "app/shortcode.py": SHORTCODE,
    "app/cache.py": CACHE,
    "app/repository.py": REPOSITORY,
    "app/analytics.py": ANALYTICS,
    "app/schemas.py": SCHEMAS,
    "app/api/__init__.py": "from .routes import router\n\n__all__ = [\"router\"]\n",
    "app/api/routes.py": ROUTES,
    "app/main.py": MAIN,
    "migrations/001_init.sql": MIGRATION,
    "openapi.yaml": OPENAPI,
    "requirements.txt": REQUIREMENTS,
    "README.md": README,
}

TEST_FILES: dict[str, str] = {
    "tests/conftest.py": CONFTEST,
    "tests/test_shortcode.py": TEST_SHORTCODE,
    "tests/test_api.py": TEST_API,
    "tests/test_analytics.py": TEST_ANALYTICS,
}
