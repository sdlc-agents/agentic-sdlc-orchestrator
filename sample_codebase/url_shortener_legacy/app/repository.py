from __future__ import annotations

import sqlite3
import time

from .db import Database
from .models import ShortUrl
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
