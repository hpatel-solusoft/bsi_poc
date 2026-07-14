"""
Owns: writes to the append-only agent_audit_log table (D.4). Records
which agent handled which case, on which endpoint, and how it went —
the case_id + agent_name pairing used for operational diagnostics.

Does not own: any investigative content. This is telemetry only.
"""

import logging
from typing import Optional

import psycopg2

from core.db import DatabaseUnavailableError, get_cursor

logger = logging.getLogger(__name__)

_INSERT_LOG_SQL = """
    INSERT INTO agent_audit_log (case_id, agent_name, endpoint, latency_ms, tokens_used, status)
    VALUES (%(case_id)s, %(agent_name)s, %(endpoint)s, %(latency_ms)s, %(tokens_used)s, %(status)s);
"""


def log_agent_call(
    case_id: str,
    agent_name: str,
    endpoint: str,
    latency_ms: int,
    status: str,
    tokens_used: Optional[int] = None,
) -> None:
    """
    Record one agent invocation. Best-effort and non-blocking: telemetry
    must never fail the investigator-facing request, so all errors are
    logged and swallowed here.
    """
    try:
        with get_cursor(dict_cursor=False) as cur:
            cur.execute(
                _INSERT_LOG_SQL,
                {
                    "case_id": case_id,
                    "agent_name": agent_name,
                    "endpoint": endpoint,
                    "latency_ms": latency_ms,
                    "tokens_used": tokens_used,
                    "status": status,
                },
            )
    except (psycopg2.Error, DatabaseUnavailableError) as exc:
        logger.error(
            "agent_audit_log write failed for case_id=%s agent_name=%s: %s",
            case_id, agent_name, exc,
        )
