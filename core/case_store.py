from fastapi import HTTPException
import logging
import time
from typing import Dict, Any, Optional, List, Tuple

from config.settings import CONVERSATION_HISTORY_MAX_TURNS
from core import case_session_repository, conversation_repository

logger = logging.getLogger(__name__)


class TTLStore:
    """
    Drop-in replacement for a plain dict that expires entries after a
    configurable TTL (CS-4 Case Session Context).

    Supports:   key in store  →  TTL-aware __contains__
                store[key]    →  __getitem__ (raises KeyError on expiry)
                store[key]=v  →  __setitem__ (resets TTL)
                store.get()   →  None on miss/expiry
    The stored dict is returned by reference — in-place .update() calls
    mutate it correctly without requiring a separate setter.
    """

    def __init__(self, ttl_seconds: Optional[int]):
        self._data: Dict[str, Dict] = {}
        self._ts:   Dict[str, float] = {}
        self._ttl = ttl_seconds

    # -- TTL helpers --------------------------------------------------

    def alive(self, key: str) -> bool:
        if key not in self._data:
            return False
        if self._ttl is None:
            return True
        return (time.monotonic() - self._ts.get(key, 0.0)) < self._ttl

    def evict(self, key: str) -> None:
        self._data.pop(key, None)
        self._ts.pop(key, None)

    def ttl_remaining(self, key: str) -> Optional[float]:
        """Seconds remaining before key expires, or None if not present / no TTL."""
        if not self.alive(key):
            return None
        if self._ttl is None:
            return None
        return max(0.0, self._ttl - (time.monotonic() - self._ts[key]))

    # -- Mapping interface --------------------------------------------

    def __contains__(self, key: str) -> bool:
        if not self.alive(key):
            self.evict(key)
            return False
        return True

    def __getitem__(self, key: str) -> Dict:
        if key not in self:          # triggers TTL check + eviction
            raise KeyError(key)
        return self._data[key]

    def __setitem__(self, key: str, value: Dict) -> None:
        self._data[key] = value
        self._ts[key]   = time.monotonic()

    def get(self, key: str, default=None):
        if key not in self:
            return default
        return self._data[key]

CASE_STORE_TTL_SECONDS = None
CASE_STORE: TTLStore = TTLStore(CASE_STORE_TTL_SECONDS)
COPILOT_HISTORY_STORE: TTLStore = TTLStore(CASE_STORE_TTL_SECONDS)


# Where a resolved case_data / conversation_history came from. Returned
# alongside the data (not embedded in it — these must never leak into an
# LLM prompt) so routes can log and, where useful, surface it in the
# response for testing and support.
SOURCE_CS_MEMORY = "cs_memory"
SOURCE_POSTGRES_FALLBACK = "postgres_fallback"
SOURCE_CLIENT_SUPPLIED = "client_supplied"


def resolve_case_data(
    case_id: str,
    ai_summary: Optional[Dict[str, Any]],
    validate_ai_summary_contract,
) -> Tuple[Dict[str, Any], str]:
    """
    Resolve the working case_data dict for case_id.

    Lookup order: in-memory CASE_STORE (CS-4, warm) -> PostgreSQL
    case_ai_summary_store fallback (D.1, survives a restart or a request
    landing on a different worker) -> client-supplied ai_summary body
    (legacy/explicit-override path, since AppWorks now sends case_id
    only by default). A miss on all three raises 400.

    Returns (case_data, source) where source is one of SOURCE_CS_MEMORY,
    SOURCE_POSTGRES_FALLBACK, or SOURCE_CLIENT_SUPPLIED, so the caller can
    log and/or surface where the data actually came from.

    validate_ai_summary_contract is injected by the caller so this
    module does not import from api/ (core/ must not depend on api/).
    """
    if case_id in CASE_STORE and CASE_STORE[case_id]:
        logger.info("case_data RESOLVED case_id=%s source=%s", case_id, SOURCE_CS_MEMORY)
        return CASE_STORE[case_id], SOURCE_CS_MEMORY

    cached_session = case_session_repository.get_case_session(case_id)
    if cached_session is not None:
        case_data = _case_data_from_session(cached_session)
        CASE_STORE[case_id] = case_data
        logger.info(
            "case_data RESOLVED case_id=%s source=%s (cache_source=%s, updated_at=%s)",
            case_id, SOURCE_POSTGRES_FALLBACK,
            cached_session.get("source"), cached_session.get("updated_at"),
        )
        return case_data, SOURCE_POSTGRES_FALLBACK

    if ai_summary:
        validate_ai_summary_contract(ai_summary)
        case_data = {**ai_summary.get("investigation", {})}
        for key in ["similar_cases", "risk_assessment", "investigation_plan"]:
            if key in ai_summary:
                case_data[key] = ai_summary[key]
        case_data["provenance_trail"] = ai_summary.get("provenance_trail", [])
        CASE_STORE[case_id] = case_data
        logger.info("case_data RESOLVED case_id=%s source=%s", case_id, SOURCE_CLIENT_SUPPLIED)
        return case_data, SOURCE_CLIENT_SUPPLIED

    logger.warning(
        "case_data NOT FOUND case_id=%s — no CS-4 entry, no case_ai_summary_store row, "
        "no ai_summary in request body",
        case_id,
    )
    raise HTTPException(
        status_code=400,
        detail=(
            f"Case {case_id} session data not found in memory or in the "
            "PostgreSQL fallback. Call /intake first."
        ),
    )


def _case_data_from_session(cached_session: Dict[str, Any]) -> Dict[str, Any]:
    """Rebuild the flat CS-4 case_data shape from a persisted case_ai_summary_store row."""
    ai_summary = cached_session.get("ai_summary") or {}
    case_data = {**ai_summary.get("investigation", {})}
    for key in ["similar_cases", "risk_assessment", "investigation_plan"]:
        if key in ai_summary:
            case_data[key] = ai_summary[key]
    case_data["provenance_trail"] = cached_session.get("provenance_trail", [])
    return case_data


def persist_case_session(case_id: str, ai_summary: Dict[str, Any]) -> None:
    """
    Write-through the current merged ai_summary to the PostgreSQL fallback
    store. Best-effort — see case_session_repository.upsert_case_session
    for the failure policy (a write failure here never fails the request).
    """
    case_session_repository.upsert_case_session(
        case_id=case_id,
        ai_summary=ai_summary,
        provenance_trail=ai_summary.get("provenance_trail", []),
        source="appworks_fetch",
    )


def validate_conversation_history(
    conversation_history: Optional[List[Dict[str, Any]]],
) -> List[Dict[str, str]]:
    """
    Validate client-supplied Copilot history before it can seed server state.

    Only user/assistant turns are accepted. System, tool, or arbitrary fields are
    intentionally rejected because the backend owns case context and tool state.
    """
    if conversation_history is None:
        return []
    if not isinstance(conversation_history, list):
        raise HTTPException(
            status_code=400,
            detail="conversation_history must be an array when provided.",
        )

    validated: List[Dict[str, str]] = []
    expected_roles = ("user", "assistant")
    for idx, message in enumerate(conversation_history):
        if not isinstance(message, dict):
            raise HTTPException(
                status_code=400,
                detail=f"conversation_history[{idx}] must be an object.",
            )
        role = message.get("role")
        content = message.get("content")
        if role not in expected_roles:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"conversation_history[{idx}].role must be 'user' or "
                    "'assistant'."
                ),
            )
        if not isinstance(content, str) or not content.strip():
            raise HTTPException(
                status_code=400,
                detail=f"conversation_history[{idx}].content must be a non-empty string.",
            )
        validated.append({"role": role, "content": content})
    return validated

def resolve_copilot_history(
    case_id: str,
    conversation_history: Optional[List[Dict[str, Any]]],
) -> Tuple[List[Dict[str, str]], str]:
    """
    Return server-owned Copilot history for case_id.

    Lookup order: in-memory COPILOT_HISTORY_STORE (warm) -> PostgreSQL
    conversation_history table (D.2, durable, rolling 20-turn window) ->
    client-supplied conversation_history (legacy seed path, since
    AppWorks now sends case_id only by default).

    Returns (messages, source) — see SOURCE_CS_MEMORY / SOURCE_POSTGRES_FALLBACK
    / SOURCE_CLIENT_SUPPLIED in this module — so the caller can log and/or
    surface where the transcript actually came from.
    """
    history_entry = COPILOT_HISTORY_STORE.get(case_id)
    if isinstance(history_entry, dict):
        if history_entry.get("case_id") != case_id:
            raise HTTPException(
                status_code=409,
                detail="Stored conversation history does not match the requested case_id.",
            )
        logger.info("conversation_history RESOLVED case_id=%s source=%s", case_id, SOURCE_CS_MEMORY)
        return validate_conversation_history(history_entry.get("messages", [])), SOURCE_CS_MEMORY

    persisted_turns = conversation_repository.get_recent_turns(case_id)
    if persisted_turns is not None and persisted_turns:
        stored_messages = validate_conversation_history(persisted_turns)
        COPILOT_HISTORY_STORE[case_id] = {"case_id": case_id, "messages": stored_messages}
        logger.info(
            "conversation_history RESOLVED case_id=%s source=%s turns=%d",
            case_id, SOURCE_POSTGRES_FALLBACK, len(stored_messages),
        )
        return stored_messages, SOURCE_POSTGRES_FALLBACK

    supplied_history = validate_conversation_history(conversation_history)
    COPILOT_HISTORY_STORE[case_id] = {
        "case_id": case_id,
        "messages": supplied_history,
    }
    logger.info(
        "conversation_history RESOLVED case_id=%s source=%s turns=%d",
        case_id, SOURCE_CLIENT_SUPPLIED, len(supplied_history),
    )
    return supplied_history, SOURCE_CLIENT_SUPPLIED


def store_copilot_turn(
    case_id: str,
    question: str,
    answer: str,
    sources_cited: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, str]]:
    """
    Append the latest Copilot exchange to the server-owned case history:
    durably to PostgreSQL conversation_history (D.2, authoritative,
    rolling window enforced there) and to the in-memory store (fast path
    for the rest of this process's requests).

    sources_cited is the caller's already-computed citation list for
    `answer` (e.g. sources_cited_details from api/server.py) and is
    written only against the assistant turn — the user's question never
    has citations of its own.
    """
    conversation_repository.append_turn(case_id, "user", question)
    conversation_repository.append_turn(case_id, "assistant", answer, sources_cited=sources_cited)

    history_entry = COPILOT_HISTORY_STORE.get(case_id) or {"case_id": case_id, "messages": []}
    if history_entry.get("case_id") != case_id:
        raise HTTPException(
            status_code=409,
            detail="Stored conversation history does not match the requested case_id.",
        )

    messages = validate_conversation_history(history_entry.get("messages", []))
    messages.extend([
        {"role": "user", "content": question},
        {"role": "assistant", "content": answer},
    ])
    # Mirror the same rolling window enforced in Postgres so the in-memory
    # fast path cannot grow unbounded within a long-lived process.
    messages = messages[-CONVERSATION_HISTORY_MAX_TURNS:]

    COPILOT_HISTORY_STORE[case_id] = {
        "case_id": case_id,
        "messages": messages,
    }
    return messages
