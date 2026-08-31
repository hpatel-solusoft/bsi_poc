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
        logger.info(
            "PostgreSQL connection pool initialized (min=%s, max=%s)", DB_POOL_MIN_CONN, DB_POOL_MAX_CONN
        )
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
    if _connection_pool is None:
        # init_pool() always either sets the pool or raises
        # DatabaseUnavailableError — this should be unreachable, but an
        # `assert` here would be silently stripped under `python -O`,
        # so guard explicitly instead.
        raise DatabaseUnavailableError("PostgreSQL connection pool failed to initialize")

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


@contextmanager
def advisory_lock(key1: str, key2: str) -> Iterator[None]:
    """
    Hold a PostgreSQL session-level advisory lock keyed on (key1, key2)
    for the lifetime of this context manager, serializing any other
    caller — in this process, another thread, or another connected
    process entirely — trying to acquire the SAME (key1, key2) pair at
    the same time. Blocks until acquired; always released on exit,
    success or error.

    THE RACE THIS CLOSES: reasoning_layer/rule_engine.py's rule writes
    are idempotent MERGE, but Neo4j MERGE's find-or-create check is only
    atomic WITHIN one transaction — two concurrent callers running the
    reasoning pipeline for the same (case_id, subject_id) (a
    double-clicked "Reload", two app workers both picking up the same
    case, etc.) can each see "no existing relationship" before either
    commits, and each create one — producing a duplicate physical
    relationship for the same logical fact, regardless of whether the
    write query's MERGE pattern is directed or undirected.
    reasoning_layer.rules_fired._dedupe_rows() hides any such duplicate
    from the UI as a safety net, but this lock is what stops the
    duplicate from being written in the first place — see
    core.pipeline_state_repository.pipeline_run_lock, the caller that
    actually uses this around a pipeline run's full critical section.

    SESSION-scoped, not transaction-scoped (pg_advisory_lock, not
    pg_advisory_xact_lock) — deliberately: the critical section this is
    built to guard spans several independent get_cursor() calls, each
    its own committed transaction, so a transaction-scoped lock would
    release the moment the FIRST of those committed, defeating the
    point. A session-scoped lock instead needs a connection held OUTSIDE
    get_cursor()'s per-call auto-commit-and-return pattern for as long
    as the lock is held — which is exactly what this function does:
    checks a connection out of the pool directly
    (_connection_pool.getconn()) rather than via get_cursor(), and only
    returns it to the pool once the lock has been released, in a
    finally block, so a crash or exception can never leak either the
    lock or the connection.

    hashtext(key1), hashtext(key2) are PostgreSQL's own string-to-int32
    hash — an occasional collision only means two DIFFERENT key pairs
    briefly contend for the same lock (a spurious wait, never a
    correctness problem), vanishingly unlikely at the cardinality this
    guards (case_id x subject_id pairs).

    Raises DatabaseUnavailableError if the pool cannot be reached —
    callers that consider the lock a nice-to-have rather than a hard
    requirement (see pipeline_run_lock) should catch this and proceed
    without it rather than blocking case investigation on a Postgres
    outage.
    """
    if _connection_pool is None:
        init_pool()
    if _connection_pool is None:
        raise DatabaseUnavailableError("PostgreSQL connection pool failed to initialize")

    conn = _connection_pool.getconn()
    lock_acquired = False
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(hashtext(%s), hashtext(%s));", (key1, key2))
        conn.commit()
        lock_acquired = True
        yield
    finally:
        try:
            if lock_acquired:
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(hashtext(%s), hashtext(%s));", (key1, key2))
                conn.commit()
        finally:
            _connection_pool.putconn(conn)
