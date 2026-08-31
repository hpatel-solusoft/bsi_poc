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
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional

import psycopg2

from core.db import DatabaseUnavailableError, advisory_lock, get_cursor

logger = logging.getLogger(__name__)

_SELECT_SQL = """
    SELECT case_id, subject_id, status, wave1_status, wave1_completed_at,
           extraction_status, extraction_completed_at,
           wave2_status, wave2_completed_at, started_at, completed_at,
           failed_at, cleared_at, cleared_reason, username
    FROM pipeline_execution_state
    WHERE case_id = %(case_id)s AND subject_id = %(subject_id)s;
"""

_START_RUN_SQL = """
    INSERT INTO pipeline_execution_state (case_id, subject_id, status, started_at, username)
    VALUES (%(case_id)s, %(subject_id)s, 'running', now(), %(username)s)
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
        cleared_reason = NULL,
        username = EXCLUDED.username;
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


@contextmanager
def pipeline_run_lock(case_id: str, subject_id: str) -> Iterator[None]:
    """
    Serialize reasoning pipeline runs for the SAME (case_id, subject_id)
    pair — see core.db.advisory_lock's docstring for the concurrent-MERGE
    duplicate-relationship race this closes. reasoning_layer/pipeline.py's
    run_pipeline() wraps its entire check-then-act critical section
    (get_run_state -> maybe start_run -> Wave 1/Extraction/Wave 2 -> mark
    complete) in this, so two concurrent callers for the same pair can
    never both decide "nothing running yet, I'll run it" and both
    proceed to write to Neo4j at once. A second caller for the SAME pair
    simply waits for the first to finish, then (per Principle 10) sees
    it already completed and returns the cached result instead of
    running again — the correct outcome, since both callers wanted the
    same thing done, not two independent runs of it. Different pairs
    never contend with each other.

    Degrades OPEN on a PostgreSQL outage, same policy as every other
    function in this module (see get_run_state's docstring): every rule
    write underneath is already idempotent MERGE, so a spurious
    concurrent run during a Postgres outage is wasteful, not unsafe —
    losing the extra protection this lock adds must never itself block
    case investigation.

    CORRECTNESS NOTE, worth being explicit about: only a failure to
    ACQUIRE the lock is caught and degraded-around here. Once the lock
    is held, the caller's wrapped code (the `yield`) runs completely
    outside any except block — an exception raised by the pipeline body
    itself (a Neo4j failure, for instance) is deliberately left to
    propagate untouched, past this function, to run_pipeline()'s own
    try/except. Catching broadly around the whole `with` statement here
    would risk mistaking a genuine pipeline failure for a lock-acquire
    failure and silently re-running the entire pipeline body a second
    time, unlocked — exactly the race this function exists to close.
    """
    lock_cm = advisory_lock(f"pipeline_run:{case_id}", subject_id)
    have_lock = False
    try:
        lock_cm.__enter__()
        have_lock = True
    except (DatabaseUnavailableError, psycopg2.Error) as exc:
        logger.warning(
            "pipeline_run_lock: PostgreSQL unavailable — proceeding WITHOUT the "
            "concurrency lock for case_id=%s subject_id=%s (%s). Every rule write "
            "remains individually idempotent; a concurrent duplicate run is "
            "possible but not unsafe.",
            case_id,
            subject_id,
            exc,
        )

    try:
        yield
    finally:
        if have_lock:
            lock_cm.__exit__(None, None, None)


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
            case_id,
            subject_id,
            exc,
        )
        return None

    if row is None:
        # DEBUG, not INFO — same reasoning as reasoning_layer/pipeline.py's
        # matching downgrade just above this table's actual caller: fires
        # once per subject per call, and is a raw cache-lookup result, not
        # a business-level event. reasoning_layer.pipeline's case-level
        # "run_pipeline_for_case: ..." summary is the right INFO signal.
        logger.debug("pipeline_execution_state MISS case_id=%s subject_id=%s (never run)", case_id, subject_id)
        return None

    # A cleared run is treated as "never run" by the caller (Section 9.5) —
    # surface cleared_at so pipeline.py can log why it's re-running.
    logger.debug(
        "pipeline_execution_state HIT case_id=%s subject_id=%s status=%s " "wave1=%s wave2=%s cleared_at=%s",
        case_id,
        subject_id,
        row["status"],
        row["wave1_status"],
        row["wave2_status"],
        row["cleared_at"],
    )
    return dict(row)


def start_run(case_id: str, subject_id: str, username: Optional[str] = None) -> None:
    """
    Mark a fresh run as started, resetting any prior wave/failure state.
    Called at the top of every pipeline execution — Principle 15 means
    there is no partial-resume path, so a (re-)run always starts clean.

    username is the investigator/caller whose request triggered this run
    (threaded down from the API layer through
    reasoning_layer.pipeline.run_pipeline — see api/models.py's
    AuthFieldsMixin), stored purely for attribution. None for a run
    triggered with no request-scoped caller (e.g. the CLI ETL path).
    """
    try:
        with get_cursor(dict_cursor=False) as cur:
            cur.execute(
                _START_RUN_SQL,
                {"case_id": case_id, "subject_id": subject_id, "username": username},
            )
        logger.info(
            "pipeline run STARTED case_id=%s subject_id=%s username=%s", case_id, subject_id, username
        )
    except (psycopg2.Error, DatabaseUnavailableError) as exc:
        logger.error("pipeline start_run FAILED case_id=%s subject_id=%s: %s", case_id, subject_id, exc)
        raise


def mark_wave1_complete(case_id: str, subject_id: str) -> None:
    """Record Wave 1 rule execution (Step 2) as complete for this case+subject."""
    try:
        with get_cursor(dict_cursor=False) as cur:
            cur.execute(_MARK_WAVE1_COMPLETE_SQL, {"case_id": case_id, "subject_id": subject_id})
        logger.info("pipeline WAVE1 complete case_id=%s subject_id=%s", case_id, subject_id)
    except (psycopg2.Error, DatabaseUnavailableError) as exc:
        logger.error(
            "pipeline mark_wave1_complete FAILED case_id=%s subject_id=%s: %s", case_id, subject_id, exc
        )
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
        logger.error(
            "pipeline mark_extraction_complete FAILED case_id=%s subject_id=%s: %s", case_id, subject_id, exc
        )
        raise


def mark_wave2_complete(case_id: str, subject_id: str) -> None:
    """Record Wave 2 rule execution as complete, marking the overall pipeline run 'completed'."""
    try:
        with get_cursor(dict_cursor=False) as cur:
            cur.execute(_MARK_WAVE2_COMPLETE_SQL, {"case_id": case_id, "subject_id": subject_id})
        logger.info("pipeline WAVE2 complete case_id=%s subject_id=%s (run completed)", case_id, subject_id)
    except (psycopg2.Error, DatabaseUnavailableError) as exc:
        logger.error(
            "pipeline mark_wave2_complete FAILED case_id=%s subject_id=%s: %s", case_id, subject_id, exc
        )
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
        logger.error(
            "pipeline mark_failed ITSELF failed case_id=%s subject_id=%s: %s", case_id, subject_id, exc
        )


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
