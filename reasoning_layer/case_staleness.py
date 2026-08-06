"""
Owns: reading AI-31's (:Case).last_inference_change_at back out of
Neo4j — the one read every consumer of that field needs, done once
here rather than re-implemented per caller. Two consumers today:

  * reasoning_layer/rule_audit.py's GET /rule_audit response, which
    wants the exact raw value (a string) to serialise into JSON as-is.
  * AI-32's core/narrative_staleness.py staleness check (via
    api/pipeline_execution.py's evaluate_cache_staleness), which wants
    it parsed into a timezone-aware datetime so it can be compared
    directly against case_ai_summary_store.updated_at /
    report_artifacts.generated_at (both native psycopg2 datetimes).

Does NOT own: writing the field (reasoning_layer/rejection.py's
_touch_case_last_inference_change), or deciding what a stale graph
means for a cached narrative (core/narrative_staleness.py).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from reasoning_layer.neo4j_client import get_session

logger = logging.getLogger(__name__)

_QUERY = """
MATCH (c:Case {case_id: $case_id})
RETURN c.last_inference_change_at AS last_inference_change_at
"""


def get_last_inference_change_at_raw(case_id: str) -> Optional[str]:
    """
    The exact string reasoning_layer/rejection.py wrote to Neo4j, or
    None when there is no (:Case {case_id}) node, or the node exists
    but has never had a reject/revert call against it.
    """
    with get_session() as session:
        record = session.run(_QUERY, case_id=case_id).single()
    return record["last_inference_change_at"] if record else None


def get_last_inference_change_at(case_id: str) -> Optional[datetime]:
    """
    Same value as get_last_inference_change_at_raw, parsed into a
    timezone-aware datetime for direct comparison against a Postgres
    timestamptz value already returned as a datetime by psycopg2.

    Returns None on every case that also makes the raw lookup return
    None, AND on a value that fails to parse as ISO-8601. The latter is
    a defensive, never-raise degrade: a single malformed timestamp
    (e.g. from a future write that changes format) must fall back to
    "no graph staleness signal available" for the callers that need a
    datetime, not take down every cached-narrative endpoint that calls
    this.
    """
    raw = get_last_inference_change_at_raw(case_id)
    if raw is None:
        return None
    try:
        return datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        logger.warning(
            "case_id=%s: (:Case).last_inference_change_at=%r is not a "
            "parseable ISO-8601 timestamp — treating as no graph "
            "staleness signal for this request",
            case_id,
            raw,
        )
        return None
