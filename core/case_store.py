from fastapi import HTTPException
import time
from typing import Dict, Any, Optional, List


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
) -> List[Dict[str, str]]:
    """
    Return server-owned Copilot history for case_id.

    Client history is optional and is only used to seed the backend after a
    server restart or first request for the case.
    """
    supplied_history = validate_conversation_history(conversation_history)
    history_entry = COPILOT_HISTORY_STORE.get(case_id)
    if isinstance(history_entry, dict):
        if history_entry.get("case_id") != case_id:
            raise HTTPException(
                status_code=409,
                detail="Stored conversation history does not match the requested case_id.",
            )
        stored_messages = validate_conversation_history(history_entry.get("messages", []))
        return stored_messages

    COPILOT_HISTORY_STORE[case_id] = {
        "case_id": case_id,
        "messages": supplied_history,
    }
    return supplied_history


def store_copilot_turn(case_id: str, question: str, answer: str) -> List[Dict[str, str]]:
    """Append the latest Copilot exchange to the server-owned case history."""
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
    COPILOT_HISTORY_STORE[case_id] = {
        "case_id": case_id,
        "messages": messages,
    }
    return messages
