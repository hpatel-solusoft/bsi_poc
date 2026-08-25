"""
Service layer for POST /copilot (ON-DEMAND — Copilot Route, Step 5 in
flow).

Owns: the entire /copilot business logic — CS-4 case-data resolution,
the modify-investigation-steps override merge, conversation-history
resolution, the LLM agent call, provenance/source-citation assembly,
durable transcript persistence, and response shaping.

Does NOT own: HTTP routing itself (api/server.py just calls
run_copilot(req, username, token) and returns/raises whatever it
does), GET /copilot/{case_id} (a separate, still-inline route in
api/server.py), or the underlying primitives (core/case_store.py,
core/investigation_plan_override_repository.py).

Unlike /intake, /similar_cases, /risk_assessment, and /plan, this
route has no cache-hit/fresh-run split to preserve — Copilot always
answers the question against whatever context it has (Section 6.3:
"Copilot never re-triggers the Reasoning Pipeline under any
condition"), so there's nothing analogous to those routes'
"answer from cache without calling the LLM" branch. run_copilot is
therefore a single linear flow, not an orchestrator over two path
functions like its siblings.

`_resolve_case_store` and `_get_runner` are deliberately imported LATE
(inside run_copilot, not at module level) rather than at the top of
this file: both still live in api/server.py, and api/server.py imports
THIS module at its own top level to wire up the /copilot route. A
top-level `from api.server import ...` here would therefore be a
circular import at module load time. Deferring the import into the
function body sidesteps the cycle and, as a bonus, means existing
tests that patch `api.server._resolve_case_store` / `api.server.
_get_runner` before calling `server.copilot(...)` keep working
unmodified.

Extracted verbatim from api/server.py's `/copilot` route body during
the service-layer refactor — same behavior, same log lines, same
response shape; only the module boundary changed. Tests that used to
patch api.server.<name> for this route's OWN internals (as opposed to
_resolve_case_store / _get_runner above) now patch
api.services.copilot_service.<name> instead, since that's where the
call sites actually live now.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict

from fastapi import HTTPException

# See api/server.py's import of the same symbol for why this is a
# deliberate, narrow exception to "no file in api/ imports directly from
# appworks/" — an exception TYPE only, for HTTP error-boundary translation.
from appworks.appworks_auth import AppworksSessionExpiredError

from agent_service.prompt_builders import build_copilot_prompt
from api.message_utils import (
    build_ai_summary,
    extract_agent_summary,
    extract_tool_results,
    merge_provenance,
)
from api.models import CopilotRequest
from api.response_builders import format_provenance_lines
from core.agent_audit_repository import log_agent_call
from core.case_store import (
    CASE_STORE,
    get_case_ai_summary_cache_updated_at,
    persist_case_session,
    resolve_copilot_history,
    store_copilot_turn,
)
from core.investigation_plan_override_repository import compute_plan_staleness, get_override

logger = logging.getLogger(__name__)


def _log_call(case_id: str, username: str, status: str, latency_ms: int) -> None:
    """Single call site for the /copilot audit-log write, so every
    branch (success, auth_error, error) logs the same five fields and
    only ever varies status/latency."""
    log_agent_call(
        case_id=case_id,
        agent_name="copilot",
        endpoint="/copilot",
        latency_ms=latency_ms,
        status=status,
        username=username,
    )


def run_copilot(req: CopilotRequest, username: str, token: str) -> Dict[str, Any]:
    """
    ON-DEMAND — Copilot Route (Step 5 in flow).
    Answers investigator questions grounded in case context (CS-5).
    Answers from CS-4 context first; falls back to PostgreSQL
    case_ai_summary_store, then to ai_summary in the body if supplied.
    conversation_history is server-owned in PostgreSQL (D.2, rolling
    20-turn window) — the response returns only the new answer, never
    the full transcript, since AppWorks/the client no longer needs to
    round-trip it.
    Flow: /intake → /similar_cases → /risk_assessment → /plan → /copilot
    """
    from api.server import _get_runner, _resolve_case_store

    start = time.time()
    try:
        # CS-4 pattern: warm lookup -> Postgres fallback -> ai_summary body.
        case_data, case_data_source = _resolve_case_store(req.case_id, req.ai_summary)

        # AI-33: soft-check only. Unlike /similar_cases, /risk_assessment,
        # and /plan, Copilot never auto-runs another route to backfill a
        # missing prerequisite — doing so would itself block the chat
        # response on that route's LLM call, which is exactly what this
        # check exists to avoid. If Case Summary's output isn't there yet,
        # Copilot degrades gracefully and answers from whatever context it
        # does have (build_copilot_prompt already handles a sparse
        # case_data); this is purely an observability signal for why an
        # answer might be thinner than usual, never a gate on the chat.
        if not case_data.get("complaint_intelligence"):
            logger.info(
                "copilot SOFT-CHECK case_id=%s — complaint_intelligence not yet "
                "resolved (Case Summary likely never opened for this case); "
                "answering from available context without blocking",
                req.case_id,
            )

        # Captured BEFORE this route's own persist_case_session call below
        # (Section E.5) — see the identical comment in /plan.
        cache_updated_at_before_call = get_case_ai_summary_cache_updated_at(req.case_id)

        # reload_ai_summary=False (default): Copilot always answers the
        # question below — there is nothing to "skip" for a Q&A route —
        # but it does NOT force any extra work: it answers against
        # whatever graph_context is already cached, unchanged from today.
        # reload_ai_summary=True: force the reasoning pipeline to re-run
        # for this case's primary subject before answering (even if it
        # already completed), refreshing graph_context/graph_signals/
        # rules_fired in both PostgreSQL (pipeline_execution_state) and
        # Neo4j, then merge the refreshed context into case_data so the
        # answer below is grounded in it.
        # AI-17 / Section 6.3: "Copilot never re-triggers the Reasoning
        # Pipeline under any condition." This block previously called
        # enrich_graph_context(force=True) on reload_ai_summary, which did
        # exactly that. Copilot only ever READS the already-reasoned graph
        # (Principle 10) — the pipeline is owned by /intake and the ETL, and
        # a Q&A turn re-running inference would let a question mutate the
        # case an investigator is reading, and change answers mid-conversation.
        #
        # reload_ai_summary is therefore honoured as a CACHE instruction only:
        # _resolve_case_store above has already re-read the freshest stored
        # context, and any graph refresh must be requested from /intake.
        if req.reload_ai_summary:
            logger.info(
                "copilot reload_ai_summary=True for case_id=%s — answering from the "
                "freshest stored context; the reasoning pipeline is never re-triggered "
                "from Copilot (Section 6.3)",
                req.case_id,
            )

        # Modify Investigation Steps flow (Section D.6): looked up
        # server-side, from any client, any session — never relying on
        # the caller to relay it — so Copilot always sees the
        # human-modified steps and can answer questions about the
        # modification itself. Takes precedence over the legacy
        # frontend-relayed modified_ai_investigation_plan field, which
        # is kept only for callers that have not migrated yet.
        override = get_override(req.case_id)
        if override is not None:
            case_data["modified_ai_investigation_plan"] = {
                "source": "human_approved",
                "steps": override["modified_steps"],
                "modified_by": override["modified_by"],
                "modified_on": override["modified_on"].isoformat(),
                "comment": override.get("comment") or "",
            }
        elif req.modified_ai_investigation_plan:
            case_data["modified_ai_investigation_plan"] = req.modified_ai_investigation_plan

        plan_stale = (
            compute_plan_staleness(cache_updated_at_before_call, override["modified_on"])
            if override is not None
            else False
        )

        conversation_history, history_source = resolve_copilot_history(
            req.case_id,
            req.conversation_history,
        )
        logger.info(
            "case_id=%s case_data_source=%s conversation_history_source=%s",
            req.case_id,
            case_data_source,
            history_source,
        )

        runner = _get_runner()

        messages, new_provenance_trail, tool_call_log = runner.run_scoped(
            system_prompt=build_copilot_prompt(req.case_id, case_data),
            user_message=req.question,
            conversation_history=conversation_history,
            # token: caller's AppWorks SAMLart, consumed by
            # semantic_layer/dispatcher.py — see the risk_assessment
            # service's own explanation. Copilot runs with scope="ALL"
            # (default), so any AppWorks-touching tool is reachable here too.
            execution_context={"token": token},
        )

        answer = extract_agent_summary(messages)

        # sources_cited: include the stored provenance trail from CS-4 (so context-
        # grounded answers cite the original AppWorks sources) plus any new tool
        # calls made during this copilot turn.
        # This aligns with Section 3.4 where the response shows sources from the
        # original investigation even when tool_calls_made = 0.
        stored_provenance = case_data.get("provenance_trail", [])
        combined_provenance = merge_provenance(stored_provenance, new_provenance_trail)

        sources_cited = [f"retrieved {p.get('retrieved_at', '')}" for p in combined_provenance]
        sources_cited_details = [
            {
                "computed_by": p.get("computed_by", ""),
                "retrieved_at": p.get("retrieved_at", ""),
                "sources": p.get("sources", []),
            }
            for p in combined_provenance
        ]

        # Durable transcript write: PostgreSQL conversation_history (D.2) is
        # authoritative; the in-memory store is updated for this process's
        # fast path. The full transcript is not returned to the caller.
        # sources_cited_details is persisted alongside the assistant's turn
        # so a later /copilot call resolving history from Postgres (or
        # anyone reading conversation_history directly) still has the
        # citations this answer was grounded in — previously this argument
        # was never passed, so every row's sources_cited column was "[]".
        store_copilot_turn(
            req.case_id,
            req.question,
            answer,
            sources_cited=sources_cited_details,
            username=username,
        )

        # CS-4: Update the warm store only if the case entry still exists (it may
        # have been evicted if TTL expires between _resolve_case_store and here),
        # and write through to Postgres case_ai_summary_store so the next fallback
        # read for this case sees whatever new tool output Copilot produced.
        if new_provenance_trail and req.case_id in CASE_STORE:
            new_sections = extract_tool_results(messages, runner.dispatcher.tool_to_section)
            CASE_STORE[req.case_id].update(new_sections)
            CASE_STORE[req.case_id]["provenance_trail"] = combined_provenance

            ai_summary = build_ai_summary(case_data, new_sections, combined_provenance)
            persist_case_session(req.case_id, ai_summary)

        _log_call(req.case_id, username, "success", int((time.time() - start) * 1000))

        return {
            "answer": answer,
            "provenance_trail": format_provenance_lines(combined_provenance),
            "sources_cited": sources_cited,
            "sources_cited_details": sources_cited_details,
            "case_data_source": case_data_source,
            "conversation_history": conversation_history,
            "conversation_history_source": history_source,
            "reload_ai_summary": req.reload_ai_summary,
            "plan_source": "User Modified" if override is not None else "AI Summerized",
            "plan_stale": plan_stale,
        }
    except AppworksSessionExpiredError as exc:
        logger.warning("copilot route: AppWorks session expired for case_id=%s", req.case_id)
        _log_call(req.case_id, username, "auth_error", int((time.time() - start) * 1000))
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Copilot route failed for case_id=%s", req.case_id)
        _log_call(req.case_id, username, "error", int((time.time() - start) * 1000))
        raise HTTPException(status_code=500, detail=f"Copilot failed: {exc}") from exc
    finally:
        logger.info("POST /copilot completed for case_id=%s", req.case_id)
