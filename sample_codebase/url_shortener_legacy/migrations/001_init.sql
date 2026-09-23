-- 001_init.sql
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

-- Rollback, described rather than executable on purpose: the platform's policy
-- layer forbids generated artifacts from containing destructive DDL, so undoing
-- this migration is a deliberate human step, not a statement an agent can run.
-- Reverse order: indexes idx_short_urls_expires and idx_short_urls_long_url,
-- then table short_urls.
