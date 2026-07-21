"""
Owns: read/write access to the graph_ingest_state table — one row per
case, recording what the ETL last did to it and when.

Why this belongs in Postgres and not Neo4j: Data Persistence Spec
Section C.2 is explicit that Neo4j "holds the reasoning graph only, never
operational application state", and names pipeline execution tracking as
an example of what must not be co-located there. An ETL run log is the
same kind of thing — pure machinery, fully regenerable by re-running the
ingest, never the sole copy of a case fact (Section A.1's test). It sits
alongside pipeline_execution_state, which tracks the stage after this one.

Why it exists at all: without it, "which cases are actually in the graph,
and did the last sync of case X succeed?" is unanswerable except by
grepping logs. Once AppWorks is driving ingest by lifecycle event, that
question is an operational necessity, not a nicety — a case that silently
failed to ingest is a case whose investigator sees an empty graph and no
explanation.

Does not own: the ETL itself (etl/), or the reasoning run state
(core/pipeline_state_repository.py).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import psycopg2

from core.db import DatabaseUnavailableError, get_cursor

logger = logging.getLogger(__name__)

# The migration that owns this table's DDL. In docker/production the
# entrypoint applies every migrations/*.sql via psql before the app starts,
# so the table already exists. In local dev, `uvicorn api.server:app`
# bypasses that entrypoint, so the table would be missing and every
# _write() below would fail (harmlessly, since they are best-effort — but
# noisily). ensure_table() closes that gap by executing the SAME migration
# file at startup: single source of DDL truth, no duplication, and the
# file's own `CREATE TABLE IF NOT EXISTS` makes the double-apply in docker a
# no-op.
_MIGRATION_FILE = Path(__file__).resolve().parent.parent / "migrations" / "006_graph_ingest_state.sql"


def ensure_table() -> None:
    """Create graph_ingest_state if it is missing, by running its migration.
    Idempotent and best-effort: a failure here must never block startup,
    because this table is operational bookkeeping, not a hard dependency of
    the ingest itself."""
    if not _MIGRATION_FILE.exists():
        logger.warning("graph_ingest_state: migration file not found at %s", _MIGRATION_FILE)
        return
    try:
        ddl = _MIGRATION_FILE.read_text(encoding="utf-8")
        with get_cursor(dict_cursor=False) as cur:
            cur.execute(ddl)
        logger.info("graph_ingest_state: table ensured (migration 006 applied if missing)")
    except (psycopg2.Error, DatabaseUnavailableError, OSError) as exc:
        logger.error("graph_ingest_state: ensure_table failed (non-fatal): %s", exc)

_MARK_STARTED = """
    INSERT INTO graph_ingest_state (case_id, status, started_at, attempts)
    VALUES (%(case_id)s, 'loading', now(), 1)
    ON CONFLICT (case_id) DO UPDATE SET
        status     = 'loading',
        started_at = now(),
        attempts   = graph_ingest_state.attempts + 1,
        last_error = NULL;
"""

_MARK_LOADED = """
    UPDATE graph_ingest_state
    SET status = 'loaded', loaded_at = now(), counts = %(counts)s, last_error = NULL
    WHERE case_id = %(case_id)s;
"""

_MARK_REASONED = """
    UPDATE graph_ingest_state
    SET status = 'reasoned', reasoned_at = now(), last_error = NULL
    WHERE case_id = %(case_id)s;
"""

_MARK_FAILED = """
    UPDATE graph_ingest_state
    SET status = 'failed', failed_at = now(), last_error = %(error)s
    WHERE case_id = %(case_id)s;
"""

_SELECT_ONE = """
    SELECT case_id, status, attempts, counts, started_at, loaded_at,
           reasoned_at, failed_at, last_error
    FROM graph_ingest_state WHERE case_id = %(case_id)s;
"""

_SELECT_ALL = """
    SELECT case_id, status, attempts, counts, started_at, loaded_at,
           reasoned_at, failed_at, last_error
    FROM graph_ingest_state ORDER BY coalesce(loaded_at, started_at) DESC;
"""


def _write(sql: str, params: Dict[str, Any], label: str) -> None:
    """Every write here is best-effort: an ingest that genuinely succeeded
    must not be reported as failed because its bookkeeping row could not
    be written. The graph is the outcome; this table is the receipt. A
    lost receipt is logged loudly and swallowed."""
    try:
        with get_cursor(dict_cursor=False) as cur:
            cur.execute(sql, params)
    except (psycopg2.Error, DatabaseUnavailableError) as exc:
        logger.error("graph_ingest_state %s FAILED case_id=%s: %s", label, params.get("case_id"), exc)


def mark_started(case_id: str) -> None:
    _write(_MARK_STARTED, {"case_id": case_id}, "mark_started")


def mark_loaded(case_id: str, counts: Dict[str, int]) -> None:
    _write(_MARK_LOADED, {"case_id": case_id, "counts": json.dumps(counts)}, "mark_loaded")


def mark_reasoned(case_id: str) -> None:
    _write(_MARK_REASONED, {"case_id": case_id}, "mark_reasoned")


def mark_failed(case_id: str, error: str) -> None:
    _write(_MARK_FAILED, {"case_id": case_id, "error": error[:2000]}, "mark_failed")


def get_state(case_id: str) -> Optional[Dict[str, Any]]:
    try:
        with get_cursor(dict_cursor=True) as cur:
            cur.execute(_SELECT_ONE, {"case_id": case_id})
            row = cur.fetchone()
    except (psycopg2.Error, DatabaseUnavailableError) as exc:
        logger.error("graph_ingest_state lookup FAILED case_id=%s: %s", case_id, exc)
        return None
    return dict(row) if row else None


def list_states() -> List[Dict[str, Any]]:
    """Every case the ETL has ever touched, newest first. This is what
    answers "what is actually in the graph right now" without a Cypher
    console."""
    try:
        with get_cursor(dict_cursor=True) as cur:
            cur.execute(_SELECT_ALL)
            rows = cur.fetchall()
    except (psycopg2.Error, DatabaseUnavailableError) as exc:
        logger.error("graph_ingest_state list FAILED: %s", exc)
        return []
    return [dict(row) for row in rows]