"""
PostgreSQL connection pool for the Agent Operational Store.

Owns: pool lifecycle and a single cursor-scoped context manager.
Does not own: table schemas, SQL statements, or business logic — those
belong to the repository modules in core/ that use this connection.

Per BSI Phase 2 Data Persistence and Synchronisation Specification,
Section D: PostgreSQL holds agent-operational data and derived
artifacts only, never a primary case fact.
"""

import logging
import os
from contextlib import contextmanager
from typing import Iterator, Optional

import psycopg2
import psycopg2.extras
from psycopg2 import pool as pg_pool

from config.settings import DB_POOL_MAX_CONN, DB_POOL_MIN_CONN

logger = logging.getLogger(__name__)

_connection_pool: Optional[pg_pool.ThreadedConnectionPool] = None


class DatabaseUnavailableError(RuntimeError):
    """Raised when the PostgreSQL pool cannot be reached.

    Callers use this to distinguish 'no fallback data available' from
    'the fallback store itself is down', so they can decide whether to
    degrade gracefully or surface a 502/503 to the investigator.
    """


def _build_dsn() -> str:
    """Build a libpq DSN from discrete env vars, or use DATABASE_URL directly."""
    database_url = os.getenv("DATABASE_URL")
    if database_url:
        return database_url

    host = os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("POSTGRES_PORT", "5432")
    db = os.getenv("POSTGRES_DB", "bsi_agent_store")
    user = os.getenv("POSTGRES_USER", "bsi_agent")
    password = os.getenv("POSTGRES_PASSWORD", "")
    return f"host={host} port={port} dbname={db} user={user} password={password}"


def init_pool() -> None:
    """Initialize the module-level connection pool. Safe to call more than once."""
    global _connection_pool
    if _connection_pool is not None:
        return
    try:
        _connection_pool = pg_pool.ThreadedConnectionPool(
            DB_POOL_MIN_CONN,
            DB_POOL_MAX_CONN,
            dsn=_build_dsn(),
        )
        logger.info("PostgreSQL connection pool initialized (min=%s, max=%s)",
                    DB_POOL_MIN_CONN, DB_POOL_MAX_CONN)
    except psycopg2.OperationalError as exc:
        logger.error("Failed to initialize PostgreSQL connection pool: %s", exc)
        _connection_pool = None
        raise DatabaseUnavailableError(str(exc)) from exc


def close_pool() -> None:
    """Close all pooled connections. Called on application shutdown."""
    global _connection_pool
    if _connection_pool is not None:
        _connection_pool.closeall()
        _connection_pool = None
        logger.info("PostgreSQL connection pool closed")


@contextmanager
def get_cursor(dict_cursor: bool = True) -> Iterator["psycopg2.extensions.cursor"]:
    """
    Yield a cursor from the pool, committing on success and rolling back on
    error. The connection is always returned to the pool.
    """
    if _connection_pool is None:
        init_pool()
    assert _connection_pool is not None

    conn = _connection_pool.getconn()
    try:
        cursor_factory = psycopg2.extras.RealDictCursor if dict_cursor else None
        with conn.cursor(cursor_factory=cursor_factory) as cur:
            yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _connection_pool.putconn(conn)
