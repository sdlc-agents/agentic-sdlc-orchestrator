from __future__ import annotations

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

"""


class Database:
    """Thin sqlite3 wrapper with per-thread connections.

    sqlite3 connections are not safe to share across threads, so each thread
    gets its own handle, and WAL keeps readers from blocking behind writers.
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
