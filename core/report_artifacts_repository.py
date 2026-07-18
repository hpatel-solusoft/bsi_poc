"""
Owns: read/write access to the report_artifacts table (Data Persistence
and Synchronisation Specification, Section D.5).

report_artifacts is a working/draft copy only — the AppWorks-saved report
(via the native BSI UI save action) is always the authoritative version.
This table exists so a generated report can be regenerated, compared
across drafts, or recovered without re-running the full agent chain
(D.5). It holds no primary case fact and is never read back into CS-4 —
core/case_store.py's warm store and Postgres fallback chain is untouched
by this module.

Does not own: assembling report content (reasoning_layer/report_generation.py,
agent_service/prompt_builders.py) or the /generate_report route (api/server.py).
"""

import json
import logging
from typing import Any, Dict, List, Optional

import psycopg2

from core.db import DatabaseUnavailableError, get_cursor

logger = logging.getLogger(__name__)

_INSERT_SQL = """
    INSERT INTO report_artifacts (case_id, content, status)
    VALUES (%(case_id)s, %(content)s, %(status)s)
    RETURNING id, generated_at;
"""

_SELECT_LATEST_SQL = """
    SELECT id, case_id, generated_at, content, status
    FROM report_artifacts
    WHERE case_id = %(case_id)s
    ORDER BY generated_at DESC
    LIMIT 1;
"""

_SELECT_HISTORY_SQL = """
    SELECT id, case_id, generated_at, content, status
    FROM report_artifacts
    WHERE case_id = %(case_id)s
    ORDER BY generated_at DESC
    LIMIT %(limit)s;
"""


def save_report(
    case_id: str,
    content: Dict[str, Any],
    status: str = "draft",
) -> Optional[Dict[str, Any]]:
    """
    Persist one generated report as a new row (Section D.5 — every
    /generate_report call writes a fresh draft; it never overwrites a
    prior one, which is what makes drafts comparable and recoverable).

    Best-effort by design, matching every other Postgres write in this
    codebase (core/case_session_repository.py, core/pipeline_state_repository.py):
    a Postgres outage must never fail the investigator-facing
    /generate_report request, since Neo4j + CS-4 already produced the
    authoritative content for THIS response — persistence is for later
    regeneration/recovery, not for the current response to depend on.

    Returns {"id": ..., "generated_at": ...} on success, or None if the
    write failed (outage) or was skipped.
    """
    try:
        with get_cursor(dict_cursor=True) as cur:
            cur.execute(
                _INSERT_SQL,
                {
                    "case_id": case_id,
                    "content": json.dumps(content),
                    "status": status,
                },
            )
            row = cur.fetchone()
        logger.info(
            "report_artifacts insert OK for case_id=%s id=%s status=%s",
            case_id, row["id"] if row else None, status,
        )
        return dict(row) if row else None
    except (psycopg2.Error, DatabaseUnavailableError) as exc:
        logger.error(
            "report_artifacts insert FAILED for case_id=%s: %s", case_id, exc,
        )
        return None


def get_latest_report(case_id: str) -> Optional[Dict[str, Any]]:
    """
    Return the most recently generated report for case_id, or None on a
    miss or a database outage — a caller falls back to "no prior draft"
    exactly as core/case_session_repository.get_case_session does.
    """
    try:
        with get_cursor(dict_cursor=True) as cur:
            cur.execute(_SELECT_LATEST_SQL, {"case_id": case_id})
            row = cur.fetchone()
    except (psycopg2.Error, DatabaseUnavailableError) as exc:
        logger.error(
            "report_artifacts lookup FAILED (outage) for case_id=%s: %s",
            case_id, exc,
        )
        return None

    if row is None:
        return None
    return dict(row)


def list_reports(case_id: str, limit: int = 10) -> List[Dict[str, Any]]:
    """
    Return up to `limit` prior drafts for case_id, newest first — the
    "compared across drafts" use case D.5 names. Returns an empty list
    (never raises) on a miss or a database outage; this is a supporting
    history view, not a request the investigator-facing route depends on.
    """
    try:
        with get_cursor(dict_cursor=True) as cur:
            cur.execute(_SELECT_HISTORY_SQL, {"case_id": case_id, "limit": limit})
            rows = cur.fetchall()
    except (psycopg2.Error, DatabaseUnavailableError) as exc:
        logger.error(
            "report_artifacts history lookup FAILED (outage) for case_id=%s: %s",
            case_id, exc,
        )
        return []

    return [dict(r) for r in rows]
