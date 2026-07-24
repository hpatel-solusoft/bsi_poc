from fastapi import HTTPException
import logging
import time
from typing import Callable, Dict, Any, Optional, List, Tuple

from config.settings import CONVERSATION_HISTORY_MAX_TURNS, TOP_LEVEL_SECTIONS
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


def try_resolve_case_data(case_id: str) -> Optional[Dict[str, Any]]:
    """
    Non-raising lookup used by the reload_ai_summary skip check on every
    ON-DEMAND route (D.1 lookup order, warm CASE_STORE then the Postgres
    case_ai_summary_store fallback — no client-supplied ai_summary path,
    since this is only used to answer "has this already run for this
    case_id", not to resolve the working context for an agent call).

    Returns the flat case_data dict if something has already been
    persisted for case_id, or None on a clean miss (never run yet) or a
    Postgres outage. A route uses this to distinguish "already ran" from
    "first run" before deciding whether reload_ai_summary=False should
    skip re-running its agent/tool/pipeline step.
    """
    if case_id in CASE_STORE and CASE_STORE[case_id]:
        return CASE_STORE[case_id]

    cached_session = case_session_repository.get_case_session(case_id)
    if cached_session is not None:
        return _case_data_from_session(cached_session)

    return None


# -----------------------------------------------------------------------
# agent_summary caching
#
# Every ON-DEMAND route that calls the LLM (intake, similar_cases,
# risk_assessment, plan) persists the raw markdown text its agent turn
# produced under this key, nested inside ai_summary["investigation"] (so
# it round-trips through _case_data_from_session's flat case_data the
# same as any other investigation field — it is deliberately NOT added
# to config.settings.TOP_LEVEL_SECTIONS).
#
# reload_ai_summary=False (default): the route looks here FIRST and, on a
# hit, returns the cached markdown without calling the LLM at all.
# reload_ai_summary=True: the route skips this lookup and always calls
# the LLM, then overwrites this route's entry with the fresh result.
# -----------------------------------------------------------------------

AGENT_SUMMARY_CACHE_KEY = "agent_summary_cache"


def get_cached_route_summary(case_id: str, route: str) -> Optional[Tuple[Dict[str, Any], str]]:
    """
    Non-LLM cache lookup used by the reload_ai_summary skip check on
    /intake, /similar_cases, /risk_assessment, and /plan.

    Returns (case_data, cached_markdown) when `route` ("intake",
    "similar_cases", "risk_assessment", or "plan") already has a
    persisted agent_summary for case_id — warm CASE_STORE first, then
    the PostgreSQL case_ai_summary_store fallback (same lookup order as
    try_resolve_case_data).

    Returns None on a clean miss: case_id has never run at all, or it
    has run but this particular route's agent_summary was never cached
    (e.g. it predates this cache, or was never reached) — either way the
    caller falls through to running the agent normally.
    """
    case_data = try_resolve_case_data(case_id)
    if case_data is None:
        return None
    cached_markdown = (case_data.get(AGENT_SUMMARY_CACHE_KEY) or {}).get(route)
    if not cached_markdown:
        return None
    return case_data, cached_markdown


def merge_agent_summary_cache(case_data: Dict[str, Any], route: str, markdown_text: str) -> Dict[str, str]:
    """
    Fold this route's freshly generated agent_summary markdown into the
    case's agent_summary_cache dict, carrying forward every other
    route's already-cached entry from `case_data` (the pre-call
    resolution for this request) so persisting this route's result never
    erases what another route already cached for this case_id.
    """
    cache = dict(case_data.get(AGENT_SUMMARY_CACHE_KEY) or {})
    cache[route] = markdown_text
    return cache


def _case_data_from_session(cached_session: Dict[str, Any]) -> Dict[str, Any]:
    """Rebuild the flat CS-4 case_data shape from a persisted case_ai_summary_store row."""
    ai_summary = cached_session.get("ai_summary") or {}
    case_data = {**ai_summary.get("investigation", {})}
    for key in ["similar_cases", "risk_assessment", "investigation_plan"]:
        if key in ai_summary:
            case_data[key] = ai_summary[key]
    case_data["provenance_trail"] = cached_session.get("provenance_trail", [])
    return case_data


def get_cached_investigation_steps(case_id: str) -> Optional[List[Dict[str, Any]]]:
    """
    Read-only extraction of
    ai_summary.investigation_plan.investigation_steps from
    case_ai_summary_store for case_id (Data Persistence Spec v1.0,
    Section D.1).

    Returns None if there is no cached case at all for case_id — a
    genuine miss, or the store being unreachable; get_case_session does
    not distinguish the two, so neither does this. Returns an empty
    list (not None) if the case is cached but has no investigation_plan
    yet (e.g. /plan has never run for it) — the case exists, its plan
    sub-resource is just empty.

    Does NOT apply investigation_plan_overrides — this is a raw read of
    whatever /plan or /copilot last cached, not the override-applied
    view those two endpoints serve. See
    core.investigation_plan_override_repository.get_override for the
    current human-edited state.
    """
    cached_session = case_session_repository.get_case_session(case_id)
    if cached_session is None:
        return None
    ai_summary = cached_session.get("ai_summary") or {}
    investigation_plan = ai_summary.get("investigation_plan") or {}
    return investigation_plan.get("investigation_steps") or []


def get_case_ai_summary_cache_updated_at(case_id: str) -> Optional[Any]:
    """
    Read-only lookup of case_ai_summary_store.updated_at for case_id.

    Used solely for the investigation_plan_overrides staleness
    comparison (Data Persistence Spec v1.0, Section E.5) — callers must
    read this BEFORE this request's own persist_case_session call,
    since that call always rewrites updated_at to now() and would
    otherwise make every override look stale.

    Returns None on a miss or a Postgres outage; the caller
    (investigation_plan_override_repository.compute_plan_staleness)
    treats that as "no staleness signal", never as an error.
    """
    cached_session = case_session_repository.get_case_session(case_id)
    return cached_session.get("updated_at") if cached_session else None


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


def _slice_ai_summary_for_persist(case_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Rebuild the ai_summary contract object from a flat CS-4 case_data dict —
    the exact inverse of _case_data_from_session above, and the same
    investigation/top-level split api/message_utils.build_ai_summary uses.
    Duplicated here (rather than imported) because core/ must not depend
    on api/ (see resolve_case_data's docstring) — this is a few lines of
    dict slicing, not business logic, so the duplication is cheap.
    """
    investigation_data = {k: v for k, v in case_data.items() if k not in TOP_LEVEL_SECTIONS}
    ai_summary: Dict[str, Any] = {"investigation": investigation_data}
    for key in ("similar_cases", "risk_assessment", "investigation_plan"):
        existing = case_data.get(key)
        if existing is not None:
            ai_summary[key] = existing
    ai_summary["provenance_trail"] = case_data.get("provenance_trail", [])
    return ai_summary


_RULES_FIRED_CONFIDENCE_ORDER = {"Unresolved": 0, "Medium": 1, "High": 2}


def _recompute_rule_rollup(entry: Dict[str, Any]) -> None:
    """
    Recompute the rule-level roll-up fields on `entry` (rules_fired[i]) from
    its own `instances` list, mirroring reasoning_layer/rules_fired.py's
    _summarise so the cached snapshot's rule-level status/confidence/
    rejected_count never drifts out of sync with the instance-level edits
    update_rules_fired_instance_status just applied. Mutates `entry` in place.
    """
    instances = entry.get("instances") or []
    active = [i for i in instances if i.get("status") == "active"]
    rejected = [i for i in instances if i.get("status") == "rejected"]

    confidences = [i.get("confidence") for i in active if i.get("confidence")]
    entry["confidence"] = (
        max(confidences, key=lambda c: _RULES_FIRED_CONFIDENCE_ORDER.get(c, 0))
        if confidences else "Unresolved"
    )
    entry["fired"] = len(active) > 0
    entry["corroborated"] = any(i.get("corroborated") for i in active)
    entry["evidence_count"] = len(active)
    entry["matched"] = len(instances) > 0
    entry["rejected_count"] = len(rejected)
    entry["revertable"] = len(rejected) > 0
    if active and rejected:
        entry["status"] = "partially_rejected"
    elif rejected:
        entry["status"] = "rejected"
    elif active:
        entry["status"] = "active"
    else:
        entry["status"] = "not_fired"


def update_rules_fired_instance_status(
    case_id: str,
    rule_id: str,
    action: str,
    investigator_id: str,
    reason: str,
    timestamp: str,
    matches: Callable[[Dict[str, Any]], bool],
) -> bool:
    """
    Sync a POST /reject_inference or /revert_rejection decision into the
    cached rules_fired snapshot — CS-4's warm CASE_STORE and its PostgreSQL
    fallback (case_ai_summary_store), the same "investigation.rules_fired"
    JSON /intake and /generate_report persist. Neo4j remains the system of
    record for the underlying fact (reasoning_layer/rejection.py owns that
    write); this only keeps the already-cached snapshot from going stale
    until the next full pipeline re-run.

    Only ever UPDATES an instance already present under rule_id's
    "instances" list — matched via `matches`, a per-rule-family predicate
    built by the caller (reasoning_layer.rejection knows how each rule
    family's subject_id_a/subject_id_b map onto an instance's fields, this
    module only knows how to read/write the cached case snapshot). No
    instance is ever removed: its "status" flips, its "inference" line is
    replaced outright by the reason the investigator typed for THIS
    decision (the prior finding text or a prior reject/revert's reason is
    discarded, not appended to), and rejected_by/rejected_at/reason (or
    reverted_by/reverted_at/revert_reason) are stamped the same way the
    Neo4j write stamps them.

    action: "reject" or "revert".

    Best-effort, matching persist_case_session's failure policy: returns
    False (never raises) if there is no cached snapshot yet for case_id,
    rule_id isn't present in it, nothing matched, or the Postgres
    write-through fails. A False return is expected and harmless whenever
    this is the first reject/revert before /intake has ever cached
    anything — the next /generate_report or /intake run will assemble the
    correct state from Neo4j regardless.
    """
    if action not in ("reject", "revert"):
        raise ValueError(f"update_rules_fired_instance_status: unknown action {action!r}")

    try:
        case_data = try_resolve_case_data(case_id)
        if not case_data:
            logger.info(
                "update_rules_fired_instance_status: no cached snapshot yet for "
                "case_id=%s — nothing to sync (Neo4j write already applied)", case_id,
            )
            return False

        rules_fired = case_data.get("rules_fired")
        if not isinstance(rules_fired, list):
            logger.info(
                "update_rules_fired_instance_status: no rules_fired block cached "
                "for case_id=%s — nothing to sync", case_id,
            )
            return False

        entry = next((e for e in rules_fired if e.get("rule_id") == rule_id), None)
        if entry is None:
            logger.info(
                "update_rules_fired_instance_status: rule_id=%s not present in "
                "cached rules_fired for case_id=%s — nothing to sync",
                rule_id, case_id,
            )
            return False

        instances = entry.get("instances") or []
        new_status = "rejected" if action == "reject" else "active"
        updated = 0
        for instance in instances:
            if not matches(instance):
                continue
            instance["status"] = new_status
            instance["revertable"] = new_status == "rejected"

            audit = dict(instance.get("rejection") or {})
            if action == "reject":
                audit.pop("reverted_by", None)
                audit.pop("reverted_at", None)
                audit.pop("revert_reason", None)
                audit["rejected_by"] = investigator_id
                audit["rejected_at"] = timestamp
                audit["reason"] = reason
            else:
                audit.pop("rejected_by", None)
                audit.pop("rejected_at", None)
                audit.pop("reason", None)
                audit["reverted_by"] = investigator_id
                audit["reverted_at"] = timestamp
                audit["revert_reason"] = reason
            instance["rejection"] = audit

            # Replace "inference" entirely with the reason the investigator
            # entered — the old finding text (or a prior reject/revert's
            # reason) is removed, not appended to. This is a full
            # replacement each time, so there is nothing to strip or track
            # between cycles: whatever reason is given for THIS decision is
            # the whole of what "inference" holds afterward.
            instance["inference"] = reason

            updated += 1

        if not updated:
            logger.info(
                "update_rules_fired_instance_status: no matching instance found "
                "for case_id=%s rule_id=%s action=%s — nothing to sync",
                case_id, rule_id, action,
            )
            return False

        _recompute_rule_rollup(entry)
        case_data["rules_fired"] = rules_fired
        CASE_STORE[case_id] = case_data

        ai_summary = _slice_ai_summary_for_persist(case_data)
        persist_case_session(case_id, ai_summary)

        logger.info(
            "update_rules_fired_instance_status: synced case_id=%s rule_id=%s "
            "action=%s instances_updated=%d", case_id, rule_id, action, updated,
        )
        return True
    except Exception:
        logger.exception(
            "update_rules_fired_instance_status: FAILED (non-fatal) for "
            "case_id=%s rule_id=%s action=%s — cached snapshot may be stale "
            "until the next pipeline run", case_id, rule_id, action,
        )
        return False


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


def fetch_copilot_history(case_id: str) -> Tuple[List[Dict[str, str]], str]:
    """
    Read-only fetch of the server-owned Copilot transcript for case_id, in
    the same user/assistant message shape /copilot returns.

    Lookup order: in-memory COPILOT_HISTORY_STORE (warm) -> PostgreSQL
    conversation_history table (D.2, durable, rolling 20-turn window). A
    warm miss populates the in-memory store from Postgres, same as
    resolve_copilot_history.

    Differs from resolve_copilot_history in two ways that matter for a bare
    GET fetch: there is no client-supplied seed path (a GET carries no
    body), and a store outage is surfaced rather than masked.
    conversation_repository.get_recent_turns returns None on an outage and
    [] for a case with no turns yet; this function raises 503 on the former
    so a fetch caller can tell "no history yet" apart from "the transcript
    store is unreachable" instead of receiving an empty list for both.

    Returns (messages, source) where source is SOURCE_CS_MEMORY or
    SOURCE_POSTGRES_FALLBACK.
    """
    history_entry = COPILOT_HISTORY_STORE.get(case_id)
    if isinstance(history_entry, dict):
        if history_entry.get("case_id") != case_id:
            raise HTTPException(
                status_code=409,
                detail="Stored conversation history does not match the requested case_id.",
            )
        logger.info("conversation_history FETCHED case_id=%s source=%s", case_id, SOURCE_CS_MEMORY)
        return validate_conversation_history(history_entry.get("messages", [])), SOURCE_CS_MEMORY

    persisted_turns = conversation_repository.get_recent_turns(case_id)
    if persisted_turns is None:
        logger.error(
            "conversation_history FETCH failed case_id=%s — transcript store unreachable",
            case_id,
        )
        raise HTTPException(
            status_code=503,
            detail=(
                "Conversation history store is unreachable; cannot distinguish "
                "an empty transcript from an outage. Retry once the store is reachable."
            ),
        )

    messages = validate_conversation_history(persisted_turns)
    COPILOT_HISTORY_STORE[case_id] = {"case_id": case_id, "messages": messages}
    logger.info(
        "conversation_history FETCHED case_id=%s source=%s turns=%d",
        case_id, SOURCE_POSTGRES_FALLBACK, len(messages),
    )
    return messages, SOURCE_POSTGRES_FALLBACK


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