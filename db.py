"""
Database connection pool and helper utilities.

On first import, call ensure_tables() to create any application tables that
don't yet exist. This is idempotent (all statements use CREATE TABLE IF NOT
EXISTS) so it is safe to run on every startup.
"""
import logging
from contextlib import contextmanager
from typing import Optional

import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

import config

logger = logging.getLogger(__name__)

_pool: Optional[ThreadedConnectionPool] = None


def get_pool() -> ThreadedConnectionPool:
    global _pool
    if _pool is None:
        _pool = ThreadedConnectionPool(minconn=1, maxconn=5, dsn=config.DATABASE_URL)
        logger.debug("Database connection pool created")
    return _pool


@contextmanager
def get_conn():
    pool = get_pool()
    conn = pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


def execute(sql: str, params=None) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)


def fetchall(sql: str, params=None) -> list[dict]:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]


def fetchone(sql: str, params=None) -> Optional[dict]:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            return dict(row) if row else None


def notice_already_seen(notice_id: str) -> bool:
    row = fetchone(
        "SELECT 1 FROM raw_notices WHERE notice_id = %s", (notice_id,)
    )
    return row is not None


# ── Schema bootstrap ──────────────────────────────────────────────────────────
# All statements use CREATE TABLE IF NOT EXISTS — safe to run on every startup.

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS leads (
    id              SERIAL PRIMARY KEY,
    name            TEXT,
    organisation    TEXT,
    role            TEXT,
    email           TEXT,
    phone           TEXT,
    sectors         TEXT,
    plan            TEXT        NOT NULL DEFAULT 'watch',
    source          TEXT        NOT NULL DEFAULT 'signup_form',
    status          TEXT        NOT NULL DEFAULT 'enquiry',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    notes           TEXT,
    portal_username TEXT
);
CREATE INDEX IF NOT EXISTS ix_leads_status     ON leads (status);
CREATE INDEX IF NOT EXISTS ix_leads_created_at ON leads (created_at DESC);
CREATE INDEX IF NOT EXISTS ix_leads_email      ON leads (email);

CREATE TABLE IF NOT EXISTS pursuit_requests (
    id            SERIAL PRIMARY KEY,
    client_id     TEXT        NOT NULL,
    notice_id     TEXT        NOT NULL,
    request_type  TEXT        NOT NULL DEFAULT 'pursuit',
    details       TEXT,
    priority      TEXT        NOT NULL DEFAULT 'normal',
    status        TEXT        NOT NULL DEFAULT 'pending',
    requested_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at    TIMESTAMPTZ,
    completed_at  TIMESTAMPTZ,
    output_path   TEXT,
    error_message TEXT
);
CREATE INDEX IF NOT EXISTS ix_pursuit_requests_client ON pursuit_requests (client_id);
CREATE INDEX IF NOT EXISTS ix_pursuit_requests_status ON pursuit_requests (status);

CREATE TABLE IF NOT EXISTS competitor_requests (
    id           SERIAL PRIMARY KEY,
    user_id      TEXT,
    firm_name    TEXT,
    context      TEXT,
    status       TEXT        NOT NULL DEFAULT 'pending',
    requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    artefact_path TEXT
);

CREATE TABLE IF NOT EXISTS brief_sends (
    id         SERIAL PRIMARY KEY,
    client_id  TEXT        NOT NULL,
    sent_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    sectors    TEXT[],
    status     TEXT        NOT NULL DEFAULT 'sent',
    error_msg  TEXT
);
CREATE INDEX IF NOT EXISTS ix_brief_sends_client   ON brief_sends (client_id);
CREATE INDEX IF NOT EXISTS ix_brief_sends_sent_at  ON brief_sends (sent_at DESC);

CREATE TABLE IF NOT EXISTS pipeline_runs (
    id           SERIAL PRIMARY KEY,
    stage        TEXT        NOT NULL,
    triggered_by TEXT        NOT NULL DEFAULT 'scheduler',
    started_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at  TIMESTAMPTZ,
    status       TEXT        NOT NULL DEFAULT 'running',
    summary      TEXT
);
CREATE INDEX IF NOT EXISTS ix_pipeline_runs_started ON pipeline_runs (started_at DESC);

CREATE TABLE IF NOT EXISTS user_preferences (
    user_id          TEXT PRIMARY KEY,
    preferred_sectors TEXT[],
    agency_focus      TEXT[],
    min_value_nzd     INTEGER,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


def ensure_tables() -> None:
    """
    Create all application tables that don't yet exist.
    Called once from portal.py at startup. Safe to re-run — all statements
    are idempotent (CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS).
    """
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(_SCHEMA_SQL)
        logger.info("db.ensure_tables(): schema bootstrap complete")
    except Exception as exc:
        # Log but never crash — the app can still serve pages that don't need
        # these tables, and Railway logs will surface the error.
        logger.error("db.ensure_tables() failed: %s", exc)
