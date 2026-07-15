"""
Owns: read/write access to the pipeline_execution_state table (D.3 of the
Data Persistence Specification). This is what makes Principle 10 ("the
reasoning pipeline runs once per subject per case, not on every read")
and Principle 15 ("failure is all-or-nothing, no resume") enforceable
rather than aspirational.

Does not own: rule execution itself, or what a "wave" means in terms of
which rules belong to it — that lives in reasoning_layer/pipeline.py.
"""

import logging
from typing import Any, Dict, Optional

import psycopg2

from core.db import DatabaseUnavailableError, get_cursor

logger = logging.getLogger(__name__)

_SELECT_SQL = """
    SELECT case_id, subject_id, status, wave1_status, wave1_completed_at,
           extraction_status, extraction_completed_at,
           wave2_status, wave2_completed_at, started_at, completed_at,
           failed_at, cleared_at, cleared_reason
    FROM pipeline_execution_state
    WHERE case_id = %(case_id)s AND subject_id = %(subject_id)s;
"""

_START_RUN_SQL = """
    INSERT INTO pipeline_execution_state (case_id, subject_id, status, started_at)
    VALUES (%(case_id)s, %(subject_id)s, 'running', now())
    ON CONFLICT (case_id, subject_id) DO UPDATE SET
        status = 'running',
        started_at = now(),
        wave1_status = 'pending',
        wave1_completed_at = NULL,
        extraction_status = 'pending',
        extraction_completed_at = NULL,
        wave2_status = 'pending',
        wave2_completed_at = NULL,
        completed_at = NULL,
        failed_at = NULL,
        cleared_at = NULL,
        cleared_reason = NULL;
"""

_MARK_WAVE1_COMPLETE_SQL = """
    UPDATE pipeline_execution_state
    SET wave1_status = 'complete', wave1_completed_at = now()
    WHERE case_id = %(case_id)s AND subject_id = %(subject_id)s;
"""

_MARK_EXTRACTION_COMPLETE_SQL = """
    UPDATE pipeline_execution_state
    SET extraction_status = 'complete', extraction_completed_at = now()
    WHERE case_id = %(case_id)s AND subject_id = %(subject_id)s;
"""

_MARK_WAVE2_COMPLETE_SQL = """
    UPDATE pipeline_execution_state
    SET wave2_status = 'complete', wave2_completed_at = now(),
        status = 'completed', completed_at = now()
    WHERE case_id = %(case_id)s AND subject_id = %(subject_id)s;
"""

_MARK_FAILED_SQL = """
    UPDATE pipeline_execution_state
    SET status = 'failed', failed_at = now()
    WHERE case_id = %(case_id)s AND subject_id = %(subject_id)s;
"""

_CLEAR_SQL = """
    UPDATE pipeline_execution_state
    SET cleared_at = now(), cleared_reason = %(reason)s
    WHERE case_id = %(case_id)s AND subject_id = %(subject_id)s;
"""


def get_run_state(case_id: str, subject_id: str) -> Optional[Dict[str, Any]]:
    """
    Return the current run record for this case+subject, or None if the
    pipeline has never run for this pair, or has been cleared and not
    yet re-run.

    A database outage also returns None here — the caller (pipeline.py)
    treats that identically to "never run" and proceeds to run fresh,
    since Principle 15 already requires every rule write to be
    idempotent; a spurious re-run from a transient outage is safe,
    merely wasteful, and preferable to blocking case investigation.
    """
    try:
        with get_cursor(dict_cursor=True) as cur:
            cur.execute(_SELECT_SQL, {"case_id": case_id, "subject_id": subject_id})
            row = cur.fetchone()
    except (psycopg2.Error, DatabaseUnavailableError) as exc:
        logger.error(
            "pipeline_execution_state lookup FAILED (outage) case_id=%s subject_id=%s: %s",
            case_id, subject_id, exc,
        )
        return None

    if row is None:
        logger.info("pipeline_execution_state MISS case_id=%s subject_id=%s (never run)", case_id, subject_id)
        return None

    # A cleared run is treated as "never run" by the caller (Section 9.5) —
    # surface cleared_at so pipeline.py can log why it's re-running.
    logger.info(
        "pipeline_execution_state HIT case_id=%s subject_id=%s status=%s "
        "wave1=%s wave2=%s cleared_at=%s",
        case_id, subject_id, row["status"], row["wave1_status"],
        row["wave2_status"], row["cleared_at"],
    )
    return dict(row)


def start_run(case_id: str, subject_id: str) -> None:
    """
    Mark a fresh run as started, resetting any prior wave/failure state.
    Called at the top of every pipeline execution — Principle 15 means
    there is no partial-resume path, so a (re-)run always starts clean.
    """
    try:
        with get_cursor(dict_cursor=False) as cur:
            cur.execute(_START_RUN_SQL, {"case_id": case_id, "subject_id": subject_id})
        logger.info("pipeline run STARTED case_id=%s subject_id=%s", case_id, subject_id)
    except (psycopg2.Error, DatabaseUnavailableError) as exc:
        logger.error("pipeline start_run FAILED case_id=%s subject_id=%s: %s", case_id, subject_id, exc)
        raise


def mark_wave1_complete(case_id: str, subject_id: str) -> None:
    try:
        with get_cursor(dict_cursor=False) as cur:
            cur.execute(_MARK_WAVE1_COMPLETE_SQL, {"case_id": case_id, "subject_id": subject_id})
        logger.info("pipeline WAVE1 complete case_id=%s subject_id=%s", case_id, subject_id)
    except (psycopg2.Error, DatabaseUnavailableError) as exc:
        logger.error("pipeline mark_wave1_complete FAILED case_id=%s subject_id=%s: %s", case_id, subject_id, exc)
        raise


def mark_extraction_complete(case_id: str, subject_id: str) -> None:
    """
    Marks Steps 3-4 (Narrative Extraction + Graph Load) complete for
    this case+subject. Does NOT set status='completed' on the overall
    run — only mark_wave2_complete does that — because Wave 2 rule
    execution (Step 5) still has to run after this before the pipeline
    as a whole is done (Phase 6, not yet built).
    """
    try:
        with get_cursor(dict_cursor=False) as cur:
            cur.execute(_MARK_EXTRACTION_COMPLETE_SQL, {"case_id": case_id, "subject_id": subject_id})
        logger.info("pipeline EXTRACTION complete case_id=%s subject_id=%s", case_id, subject_id)
    except (psycopg2.Error, DatabaseUnavailableError) as exc:
        logger.error("pipeline mark_extraction_complete FAILED case_id=%s subject_id=%s: %s", case_id, subject_id, exc)
        raise


def mark_wave2_complete(case_id: str, subject_id: str) -> None:
    try:
        with get_cursor(dict_cursor=False) as cur:
            cur.execute(_MARK_WAVE2_COMPLETE_SQL, {"case_id": case_id, "subject_id": subject_id})
        logger.info("pipeline WAVE2 complete case_id=%s subject_id=%s (run completed)", case_id, subject_id)
    except (psycopg2.Error, DatabaseUnavailableError) as exc:
        logger.error("pipeline mark_wave2_complete FAILED case_id=%s subject_id=%s: %s", case_id, subject_id, exc)
        raise


def mark_failed(case_id: str, subject_id: str) -> None:
    """
    Mark the run failed (Principle 15). Deliberately swallows its own
    DB errors rather than raising — this is called from an exception
    handler in pipeline.py, and an error here must never mask or replace
    the original exception that triggered the failure.
    """
    try:
        with get_cursor(dict_cursor=False) as cur:
            cur.execute(_MARK_FAILED_SQL, {"case_id": case_id, "subject_id": subject_id})
        logger.warning("pipeline run FAILED case_id=%s subject_id=%s", case_id, subject_id)
    except (psycopg2.Error, DatabaseUnavailableError) as exc:
        logger.error("pipeline mark_failed ITSELF failed case_id=%s subject_id=%s: %s", case_id, subject_id, exc)


def clear_run(case_id: str, subject_id: str, reason: str) -> None:
    """
    Explicit reload path (Section 9.5): invalidates the current run
    record so the next pipeline trigger treats this case+subject as cold
    and performs a full re-run, rather than skipping on the strength of
    a stale completed/failed record.
    """
    try:
        with get_cursor(dict_cursor=False) as cur:
            cur.execute(_CLEAR_SQL, {"case_id": case_id, "subject_id": subject_id, "reason": reason})
        logger.info("pipeline run CLEARED case_id=%s subject_id=%s reason=%s", case_id, subject_id, reason)
    except (psycopg2.Error, DatabaseUnavailableError) as exc:
        logger.error("pipeline clear_run FAILED case_id=%s subject_id=%s: %s", case_id, subject_id, exc)
        raise
