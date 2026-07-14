"""
Owns: read/write access to the case_ai_summary_store table (D.1 of the Data
Persistence and Synchronisation Specification).

Does not own: when to use the in-memory CS-4 store vs. this fallback,
or how case_data is shaped for the LLM — that orchestration lives in
core/case_store.py.
"""

import json
import logging
from typing import Any, Dict, List, Optional

import psycopg2

from core.db import DatabaseUnavailableError, get_cursor

logger = logging.getLogger(__name__)

_UPSERT_SQL = """
    INSERT INTO case_ai_summary_store (case_id, ai_summary, provenance_trail, source, updated_at)
    VALUES (%(case_id)s, %(ai_summary)s, %(provenance_trail)s, %(source)s, now())
    ON CONFLICT (case_id) DO UPDATE SET
        ai_summary       = EXCLUDED.ai_summary,
        provenance_trail = EXCLUDED.provenance_trail,
        source           = EXCLUDED.source,
        updated_at       = now();
"""

_SELECT_SQL = """
    SELECT case_id, ai_summary, provenance_trail, source, updated_at
    FROM case_ai_summary_store
    WHERE case_id = %(case_id)s;
"""


def upsert_case_session(
    case_id: str,
    ai_summary: Dict[str, Any],
    provenance_trail: List[dict],
    source: str = "appworks_fetch",
) -> None:
    """
    Persist the current merged case snapshot for case_id.

    Best-effort by design: a Postgres write failure here must never fail
    the investigator-facing request, since case_ai_summary_store is a
    derived artifact (Section A.1) and the in-memory CS-4 store already
    holds the authoritative copy for this process's lifetime.
    """
    try:
        with get_cursor(dict_cursor=False) as cur:
            cur.execute(
                _UPSERT_SQL,
                {
                    "case_id": case_id,
                    "ai_summary": json.dumps(ai_summary),
                    "provenance_trail": json.dumps(provenance_trail or []),
                    "source": source,
                },
            )
        logger.info(
            "case_ai_summary_store upsert OK for case_id=%s (source=%s)",
            case_id, source,
        )
    except (psycopg2.Error, DatabaseUnavailableError) as exc:
        logger.error(
            "case_ai_summary_store upsert FAILED for case_id=%s (source=%s): %s",
            case_id, source, exc,
        )


def get_case_session(case_id: str) -> Optional[Dict[str, Any]]:
    """
    Return the persisted case snapshot for case_id, or None on a cache
    miss or a database outage.

    Returning None on outage (rather than raising) means a Postgres
    failure degrades to "no fallback data" — the caller falls through to
    its existing 400 "call /intake first" behaviour instead of a 500.
    """
    try:
        with get_cursor(dict_cursor=True) as cur:
            cur.execute(_SELECT_SQL, {"case_id": case_id})
            row = cur.fetchone()
    except (psycopg2.Error, DatabaseUnavailableError) as exc:
        logger.error("case_ai_summary_store lookup FAILED (outage) for case_id=%s: %s", case_id, exc)
        return None

    if row is None:
        logger.info("case_ai_summary_store MISS for case_id=%s", case_id)
        return None

    logger.info(
        "case_ai_summary_store HIT for case_id=%s (source=%s, updated_at=%s)",
        case_id, row["source"], row["updated_at"],
    )
    return {
        "case_id": row["case_id"],
        "ai_summary": row["ai_summary"],
        "provenance_trail": row["provenance_trail"],
        "source": row["source"],
        "updated_at": row["updated_at"],
    }
