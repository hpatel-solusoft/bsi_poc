"""
Owns: read/write access to the investigation_plan_overrides table (D.6
of the Data Persistence and Synchronisation Specification), and the
pure staleness comparison used by /plan and /copilot to decide whether
a saved override may be describing a case that has since moved on.

Does not own: how /plan or /copilot merge an override into a response
or an LLM prompt — that lives in api/server.py and
agent_service/prompt_builders.py respectively. Does not own the
Investigation Plan "Modify" popup HTTP contract either — that lives in
api/models.py.

Unlike case_ai_summary_store (a derived artifact — see
core/case_session_repository.py), a row in this table is a primary
fact: an investigator's explicit, attributable edit. A write failure
here must therefore never be silently swallowed the way a cache-write
failure is — the caller has to know the edit did not actually save.
"""

import json
import logging
from datetime import datetime
from typing import Any, Dict, Optional

import psycopg2

from core.db import DatabaseUnavailableError, get_cursor

logger = logging.getLogger(__name__)

_UPSERT_SQL = """
    INSERT INTO investigation_plan_overrides
        (case_id, modified_steps, modified_by, comment, modified_on)
    VALUES
        (%(case_id)s, %(modified_steps)s, %(modified_by)s, %(comment)s, now())
    ON CONFLICT (case_id) DO UPDATE SET
        modified_steps = EXCLUDED.modified_steps,
        modified_by    = EXCLUDED.modified_by,
        comment        = EXCLUDED.comment,
        modified_on    = now()
    RETURNING modified_on;
"""

_SELECT_SQL = """
    SELECT case_id, modified_steps, modified_by, comment, modified_on
    FROM investigation_plan_overrides
    WHERE case_id = %(case_id)s;
"""

_DELETE_SQL = """
    DELETE FROM investigation_plan_overrides
    WHERE case_id = %(case_id)s;
"""


def upsert_override(
    case_id: str,
    modified_steps: list,
    modified_by: str,
    comment: Optional[str],
) -> datetime:
    """
    Save (or replace) the investigator's current investigation_steps
    override for case_id. One row per case — a new save overwrites the
    prior one, per the "current state only, no version history"
    retention rule in Section D.6.

    Returns the modified_on timestamp the database assigned, so the
    caller can echo it back in the response for the AppWorks badge.

    Raises on any database error rather than degrading quietly: this
    is the write path for a primary fact (an investigator's edit), not
    a cache refresh, so a failure here must surface as a failed save,
    not a silent no-op.
    """
    try:
        with get_cursor(dict_cursor=True) as cur:
            cur.execute(
                _UPSERT_SQL,
                {
                    "case_id": case_id,
                    "modified_steps": json.dumps(modified_steps),
                    "modified_by": modified_by,
                    "comment": comment,
                },
            )
            row = cur.fetchone()
        modified_on = row["modified_on"]
        logger.info(
            "investigation_plan_overrides SAVED case_id=%s modified_by=%s steps=%d",
            case_id, modified_by, len(modified_steps),
        )
        return modified_on
    except (psycopg2.Error, DatabaseUnavailableError) as exc:
        logger.error(
            "investigation_plan_overrides SAVE FAILED case_id=%s modified_by=%s: %s",
            case_id, modified_by, exc,
        )
        raise


def get_override(case_id: str) -> Optional[Dict[str, Any]]:
    """
    Return the current investigation_steps override for case_id, or
    None if no override has ever been saved (or one was saved and then
    reverted) or the database is unreachable.

    A database outage degrades to None (no override applied) rather
    than raising: /plan and /copilot must still be able to serve the
    AI-generated plan when the override store is temporarily down,
    consistent with how every other Postgres-backed read in this
    platform degrades (case_session_repository.get_case_session,
    pipeline_state_repository.get_run_state).
    """
    try:
        with get_cursor(dict_cursor=True) as cur:
            cur.execute(_SELECT_SQL, {"case_id": case_id})
            row = cur.fetchone()
    except (psycopg2.Error, DatabaseUnavailableError) as exc:
        logger.error(
            "investigation_plan_overrides lookup FAILED (outage) case_id=%s: %s",
            case_id, exc,
        )
        return None

    if row is None:
        logger.info("investigation_plan_overrides MISS case_id=%s", case_id)
        return None

    logger.info(
        "investigation_plan_overrides HIT case_id=%s modified_by=%s modified_on=%s",
        case_id, row["modified_by"], row["modified_on"],
    )
    return dict(row)


def delete_override(case_id: str) -> bool:
    """
    Revert case_id to the AI-generated plan by deleting its override
    row ("Revert to AI Plan" action). Returns True if a row was
    deleted, False if there was no override to revert.

    Raises on a database error rather than degrading quietly, for the
    same reason as upsert_override: a caller must know whether the
    revert actually took effect, not silently keep serving the
    (undeleted) override on the next /plan or /copilot call.
    """
    try:
        with get_cursor(dict_cursor=False) as cur:
            cur.execute(_DELETE_SQL, {"case_id": case_id})
            deleted = cur.rowcount > 0
        logger.info(
            "investigation_plan_overrides REVERTED case_id=%s (existed=%s)",
            case_id, deleted,
        )
        return deleted
    except (psycopg2.Error, DatabaseUnavailableError) as exc:
        logger.error("investigation_plan_overrides revert FAILED case_id=%s: %s", case_id, exc)
        raise


def compute_plan_staleness(
    cache_updated_at: Optional[datetime],
    override_modified_on: Optional[datetime],
) -> bool:
    """
    Section E.5 staleness comparison: has case_ai_summary_store been
    refreshed from AppWorks more recently than the investigator's
    saved override?

    cache_updated_at is the case_ai_summary_store.updated_at value from
    BEFORE the current request's own write (the caller must capture it
    early — every /plan and /copilot call re-persists the cache at the
    end of its own run, so reading it late would make plan_stale always
    true). If no cache row exists yet, there is nothing to compare
    against and this returns False rather than guessing.

    Returns True when the cache is newer than the override (case data
    has moved since the edit was saved — surfaced to the investigator
    as a non-blocking notice, never auto-resolved, per Section E.5).
    """
    if cache_updated_at is None or override_modified_on is None:
        return False
    return cache_updated_at > override_modified_on