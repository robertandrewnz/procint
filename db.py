"""
Database connection pool and helper utilities.
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


# ── Pipeline output store ─────────────────────────────────────────────────────

def save_output(
    output_type: str,
    run_date,
    filename: str,
    content: Optional[str] = None,
    content_bytes: Optional[bytes] = None,
    client_slug: Optional[str] = None,
    notice_id: Optional[str] = None,
    storage_path: Optional[str] = None,
) -> None:
    """Upsert a generated artefact into pipeline_outputs."""
    execute(
        """
        INSERT INTO pipeline_outputs
               (output_type, run_date, filename, content, content_bytes,
                client_slug, notice_id, storage_path)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (output_type, run_date, filename) DO UPDATE
          SET content       = EXCLUDED.content,
              content_bytes = EXCLUDED.content_bytes,
              storage_path  = EXCLUDED.storage_path,
              created_at    = NOW()
        """,
        (output_type, run_date, filename, content, content_bytes,
         client_slug, notice_id, storage_path),
    )


def load_output(output_type: str, run_date, filename: str) -> Optional[dict]:
    """Load a single artefact row (content + content_bytes) from pipeline_outputs."""
    return fetchone(
        """
        SELECT content, content_bytes, filename
          FROM pipeline_outputs
         WHERE output_type = %s AND run_date = %s AND filename = %s
        """,
        (output_type, run_date, filename),
    )


def load_latest_output(output_type: str, filename: str) -> Optional[dict]:
    """Load the most recent row matching output_type and filename (any run_date)."""
    return fetchone(
        """
        SELECT content, content_bytes, filename
          FROM pipeline_outputs
         WHERE output_type = %s AND filename = %s
         ORDER BY created_at DESC
         LIMIT 1
        """,
        (output_type, filename),
    )


def list_outputs(
    output_type: str,
    client_slug: Optional[str] = None,
    limit: int = 20,
) -> list[dict]:
    """List artefact metadata rows, newest first."""
    if client_slug is not None:
        return fetchall(
            """
            SELECT id, output_type, run_date, filename, client_slug, notice_id, created_at
              FROM pipeline_outputs
             WHERE output_type = %s AND client_slug = %s
             ORDER BY created_at DESC
             LIMIT %s
            """,
            (output_type, client_slug, limit),
        )
    return fetchall(
        """
        SELECT id, output_type, run_date, filename, client_slug, notice_id, created_at
          FROM pipeline_outputs
         WHERE output_type = %s
         ORDER BY created_at DESC
         LIMIT %s
        """,
        (output_type, limit),
    )
