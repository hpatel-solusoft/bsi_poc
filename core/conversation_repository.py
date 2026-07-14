"""
Owns: read/write access to the conversation_history table (D.2), including
enforcement of the rolling 20-turn-per-case retention window.

Does not own: Copilot prompt construction or answer generation — this
module only persists and retrieves transcript turns.
"""

import json
import logging
from typing import List, Optional

import psycopg2

from config.settings import CONVERSATION_HISTORY_MAX_TURNS
from core.db import DatabaseUnavailableError, get_cursor

logger = logging.getLogger(__name__)

_SELECT_RECENT_SQL = """
    SELECT role, content, sources_cited, turn_index
    FROM conversation_history
    WHERE case_id = %(case_id)s
    ORDER BY turn_index ASC;
"""

_MAX_TURN_INDEX_SQL = """
    SELECT COALESCE(MAX(turn_index), -1) AS max_turn_index
    FROM conversation_history
    WHERE case_id = %(case_id)s;
"""

_INSERT_TURN_SQL = """
    INSERT INTO conversation_history (case_id, turn_index, role, content, sources_cited)
    VALUES (%(case_id)s, %(turn_index)s, %(role)s, %(content)s, %(sources_cited)s);
"""

_TRIM_OLD_TURNS_SQL = """
    DELETE FROM conversation_history
    WHERE case_id = %(case_id)s
      AND turn_index <= (
          SELECT MAX(turn_index) - %(max_turns)s
          FROM conversation_history
          WHERE case_id = %(case_id)s
      );
"""


def get_recent_turns(case_id: str) -> Optional[List[dict]]:
    """
    Return the persisted transcript for case_id, oldest first, or None on
    a lookup failure so callers can distinguish "no history yet" ([]) from
    "the store is unreachable" (None).
    """
    try:
        with get_cursor(dict_cursor=True) as cur:
            cur.execute(_SELECT_RECENT_SQL, {"case_id": case_id})
            rows = cur.fetchall()
    except (psycopg2.Error, DatabaseUnavailableError) as exc:
        logger.error("conversation_history lookup FAILED (outage) for case_id=%s: %s", case_id, exc)
        return None

    if not rows:
        logger.info("conversation_history MISS for case_id=%s", case_id)
    else:
        logger.info("conversation_history HIT for case_id=%s turns=%d", case_id, len(rows))

    return [
        {"role": row["role"], "content": row["content"]}
        for row in rows
    ]


def append_turn(
    case_id: str,
    role: str,
    content: str,
    sources_cited: Optional[List[dict]] = None,
) -> None:
    """
    Append one turn to case_id's transcript and trim anything older than
    the rolling window (CONVERSATION_HISTORY_MAX_TURNS).

    Best-effort: a write failure here is logged, not raised, since
    conversation_history is operational data (D.2) and losing one turn of
    durability does not corrupt the in-memory fast path the caller also
    maintains for the current process.
    """
    try:
        with get_cursor(dict_cursor=True) as cur:
            cur.execute(_MAX_TURN_INDEX_SQL, {"case_id": case_id})
            next_turn_index = cur.fetchone()["max_turn_index"] + 1

            cur.execute(
                _INSERT_TURN_SQL,
                {
                    "case_id": case_id,
                    "turn_index": next_turn_index,
                    "role": role,
                    "content": content,
                    "sources_cited": json.dumps(sources_cited or []),
                },
            )

            cur.execute(
                _TRIM_OLD_TURNS_SQL,
                {"case_id": case_id, "max_turns": CONVERSATION_HISTORY_MAX_TURNS},
            )
        logger.info(
            "conversation_history append OK case_id=%s role=%s turn_index=%d",
            case_id, role, next_turn_index,
        )
    except (psycopg2.Error, DatabaseUnavailableError) as exc:
        logger.error("conversation_history append FAILED for case_id=%s: %s", case_id, exc)
