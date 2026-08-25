"""
Service layer for POST /risk_assessment (ON-DEMAND — Risk Assessment
Route, Step 3 in flow).

Owns: the entire /risk_assessment business logic — prerequisite
resolution (auto-running /similar_cases when needed), staleness/
cache-hit check, the LLM agent call + graph risk-signal pipeline,
persistence (CASE_STORE + case_ai_summary_store), and response shaping.

Does NOT own: HTTP routing itself (api/server.py just calls
run_risk_assessment(req, username, token) and returns/raises whatever
it does), or the underlying pipeline/staleness primitives
(api/pipeline_execution.py, core/narrative_staleness.py) — this module
is the orchestration layer between the two.

`_resolve_case_store` is deliberately imported LATE (inside
run_risk_assessment, not at module level) rather than at the top of
this file: it still lives in api/server.py (a route-agnostic CS-4
helper that hasn't been extracted to a service module of its own), and
api/server.py imports THIS module at its own top level to wire up the
/risk_assessment route. A top-level `from api.server import
_resolve_case_store` here would therefore be a circular import at
module load time. Deferring the import into the function body
sidesteps the cycle and, as a bonus, means existing tests that patch
`api.server._resolve_case_store` before calling
`server.risk_assessment(...)` keep working unmodified.

The /similar_cases prerequisite is invoked via
`api.services.similar_cases_service.run_similar_cases` directly rather
than `api.server.similar_cases`: both are equivalent (similar_cases is
now itself a thin wrapper over run_similar_cases, same pattern as this
module), but importing the sibling service module avoids the
circular-import problem entirely instead of needing a second late
import for it.

Extracted verbatim from api/server.py's `/risk_assessment` route body
during the service-layer refactor — same behavior, same log lines,
same response shape; only the module boundary changed. Tests that used
to patch api.server.<name> for this route's OWN internals (as opposed
to _resolve_case_store above) now patch
api.services.risk_assessment_service.<name> instead, since that's
where the call sites actually live now.

Internal structure (post-refactor, behavior unchanged):
run_risk_assessment is a thin orchestrator over
_risk_assessment_cache_hit (Optional[Dict] — None means "no usable
cache, fall through") and _risk_assessment_fresh_run (always returns a
response dict), sharing _log_call so the audit-log call site can't
drift between the two paths.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

from fastapi import HTTPException

# See api/server.py's import of the same symbol for why this is a
# deliberate, narrow exception to "no file in api/ imports directly from
# appworks/" — an exception TYPE only, for HTTP error-boundary translation.
from appworks.appworks_auth import AppworksSessionExpiredError

from agent_service.prompt_builders import build_risk_assessment_prompt
from agent_service.runner_provider import get_runner
from api.message_utils import build_ai_summary, extract_agent_summary, extract_tool_results
from api.models import RiskAssessmentRequest, SimilarCasesRequest
from api.pipeline_execution import (
    evaluate_cache_staleness,
    fetch_live_risk_signals,
    resolve_prerequisite_case_data,
    run_risk_assessment_pipeline,
)
from api.response_builders import format_provenance_lines
from api.services import similar_cases_service
from core.agent_audit_repository import log_agent_call
from core.case_store import (
    AGENT_SUMMARY_CACHE_KEY,
    CASE_STORE,
    get_complaint_number,
    get_route_generated_at,
    get_route_summary_text,
    get_route_username,
    merge_agent_summary_cache,
    persist_case_session,
)

logger = logging.getLogger(__name__)


def _log_call(case_id: str, username: str, status: str, latency_ms: int) -> None:
    """Single call site for the /risk_assessment audit-log write, so
    every branch (cache hit, fresh run, auth_error, error) logs the
    same five fields and only ever varies status/latency."""
    log_agent_call(
        case_id=case_id,
        agent_name="risk_assessment",
        endpoint="/risk_assessment",
        latency_ms=latency_ms,
        status=status,
        username=username,
    )


def _risk_assessment_cache_hit(
    req: RiskAssessmentRequest,
    username: str,
    case_data: Dict[str, Any],
    data_source: str,
    staleness: Any,
    start: float,
) -> Optional[Dict[str, Any]]:
    """Answer from case_ai_summary_store / warm CASE_STORE WITHOUT calling
    the LLM again, if there's a usable cached agent_summary AND a
    persisted risk_score for this case_id. Only an explicit
    reload_ai_summary=True (staleness.should_auto_refresh) bypasses this
    lookup and forces a fresh run below.

    neo4j_signals/risk_score/risk_tier are ALWAYS recomputed live from
    Neo4j, cache hit or not — see fetch_live_risk_signals below.

    Returns None (never raises) if there's no usable cache — callers
    should fall through to _risk_assessment_fresh_run in that case.
    """
    if staleness.should_auto_refresh:
        return None

    cached_summary = get_route_summary_text(case_data, "risk_assessment")
    cached_risk_assessment = case_data.get("risk_assessment")
    if not (
        cached_summary is not None
        and isinstance(cached_risk_assessment, dict)
        and "risk_score" in cached_risk_assessment
    ):
        return None

    # neo4j_signals / risk_score / risk_tier are ALWAYS recomputed live
    # from Neo4j, cache hit or not. cached_risk_assessment.risk_score/
    # risk_tier are the untouched AppWorks BASE values here —
    # Postgres/CS-4 persist base_risk_score/base_risk_tier under those
    # plain keys precisely so there is a clean starting point to
    # re-augment from (see core.persistence_filters.
    # strip_graph_derived_fields). base_risk_score/base_risk_tier are
    # preferred when present (a warm same-process CASE_STORE entry from
    # the LLM run that just populated it still carries the real
    # augmented dict, with both keys).
    subject_id = (case_data.get("complaint_intelligence") or {}).get("subject_primary_id")
    base_risk_score = cached_risk_assessment.get("base_risk_score", cached_risk_assessment.get("risk_score"))
    base_risk_tier = cached_risk_assessment.get("base_risk_tier", cached_risk_assessment.get("risk_tier"))
    live_risk = fetch_live_risk_signals(
        req.case_id,
        subject_id,
        base_risk_score,
        base_risk_tier,
    )
    duration_seconds = round(time.time() - start, 1)
    logger.info(
        "risk_assessment CACHE HIT for case_id=%s — answering from "
        "case_ai_summary_store, no LLM call made",
        req.case_id,
    )
    _log_call(req.case_id, username, "success", int(duration_seconds * 1000))

    return {
        "complaint_number": get_complaint_number(case_data) or req.case_id,
        "status": "completed",
        "details": {
            "agent_summary": cached_summary,
            "provenance_trail": format_provenance_lines(case_data.get("provenance_trail", [])),
            "graph_findings": {
                "neo4j_signals": live_risk.get("neo4j_signals"),
                "base_risk_score": live_risk.get("base_risk_score", base_risk_score),
                "base_risk_tier": live_risk.get("base_risk_tier", base_risk_tier),
                "risk_score": live_risk.get("risk_score"),
                "risk_tier": live_risk.get("risk_tier"),
            },
            "meta": {
                "data_source": data_source,
                "agent_summary_source": "db_cache",
                "stale": staleness.stale,
                "generated_at": get_route_generated_at(case_data, "risk_assessment"),
                "username": get_route_username(case_data, "risk_assessment"),
            },
        },
    }


def _risk_assessment_fresh_run(
    req: RiskAssessmentRequest,
    username: str,
    token: str,
    case_data: Dict[str, Any],
    data_source: str,
    staleness: Any,
    start: float,
) -> Dict[str, Any]:
    """Run the LLM agent + graph risk-signal pipeline (Section 8.4
    AI-15 — factored out to api/pipeline_execution.py) and persist the
    result. Always returns a response dict — never returns None."""
    runner = get_runner()

    # --- EXPLICIT DEPENDENCY INJECTION ---
    # We package the backend state into a generic execution_context.
    # token is the caller's AppWorks SAMLart (AuthFieldsMixin.token) —
    # semantic_layer/dispatcher.py consumes and removes it before any
    # tool function sees **context_kwargs; it is never forwarded to a
    # tool as a parameter. See appworks/appworks_auth.py's
    # set_request_token for how it reaches the actual AppWorks call.
    execution_context = {"ai_summary": req.ai_summary, "token": token}
    # -------------------------------------

    messages, new_provenance, tool_call_log = runner.run_scoped(
        system_prompt=build_risk_assessment_prompt(case_data),
        user_message=(
            f"Review the case data for case {req.case_id} and execute the "
            "appropriate tools to calculate the risk assessment and explain why "
            "this case received its risk score."
        ),
        scope="RISK_ASSESSMENT",  # ← this scope includes intake + enrichment tools only
        execution_context=execution_context,
    )

    sections = extract_tool_results(messages, runner.dispatcher.tool_to_section)

    # Direct graph risk-signal pipeline work (Section 8.4 AI-15) plus
    # recommendation-text normalization — factored out to
    # api/pipeline_execution.py.
    risk_assessment, risk_section, merged_provenance = run_risk_assessment_pipeline(
        req.case_id,
        case_data,
        sections,
        tool_call_log,
        new_provenance,
        messages,
    )

    # Update CS-4 warm store but return only the route-specific section.
    CASE_STORE[req.case_id].update(risk_section)
    CASE_STORE[req.case_id]["provenance_trail"] = merged_provenance

    ai_summary = build_ai_summary(
        case_data,
        {"risk_assessment": risk_assessment},
        merged_provenance,
    )
    # Cache this run's agent_summary markdown (carrying forward any
    # other route's already-cached entry for this case_id) inside
    # ai_summary["investigation"], the same place every other
    # investigation field lives, so it round-trips on the next fetch.
    assistant_text = extract_agent_summary(messages)
    ai_summary["investigation"][AGENT_SUMMARY_CACHE_KEY] = merge_agent_summary_cache(
        case_data,
        "risk_assessment",
        assistant_text,
        username=username,
    )
    risk_assessment_generated_at = get_route_generated_at(ai_summary["investigation"], "risk_assessment")
    risk_assessment_username = get_route_username(ai_summary["investigation"], "risk_assessment")
    CASE_STORE[req.case_id][AGENT_SUMMARY_CACHE_KEY] = ai_summary["investigation"][
        AGENT_SUMMARY_CACHE_KEY
    ]
    persist_case_session(req.case_id, ai_summary)
    _log_call(req.case_id, username, "success", int((time.time() - start) * 1000))

    return {
        "complaint_number": get_complaint_number(case_data) or req.case_id,
        "status": "completed",
        "details": {
            "agent_summary": assistant_text,
            "provenance_trail": format_provenance_lines(merged_provenance),
            # Neo4j graph risk signals (AI-15 —
            # reasoning_layer.risk_signals.apply_graph_risk_signals):
            # the four Section 8.4 signals plus the AppWorks base score
            # they were layered on. Returned the same way graph_findings
            # is on /intake and /similar_cases, so the investigator can
            # see WHICH graph signal moved the score rather than only the
            # LLM's prose about the final number. base_* are carried
            # alongside so the graph contribution stays auditable.
            "graph_findings": {
                "neo4j_signals": risk_assessment.get("neo4j_signals"),
                "base_risk_score": risk_assessment.get("base_risk_score"),
                "base_risk_tier": risk_assessment.get("base_risk_tier"),
                "risk_score": risk_assessment.get("risk_score"),
                "risk_tier": risk_assessment.get("risk_tier"),
            },
            "meta": {
                "data_source": data_source,
                "agent_summary_source": "llm",
                "stale": staleness.stale,
                "generated_at": risk_assessment_generated_at,
                "username": risk_assessment_username,
            },
        },
    }


def run_risk_assessment(req: RiskAssessmentRequest, username: str, token: str) -> Dict[str, Any]:
    """
    ON-DEMAND — Risk Assessment Route (Step 3 in flow).
    Calls get_risk_rules and calculate_risk_metrics.
    Requires case_data from a prior /intake + /similar_cases run
    (via CS-4 or ai_summary body).
    Explains case seriousness, triggered rules, and escalation thresholds.
    Flow: /intake → /similar_cases → /risk_assessment → /plan → /copilot |
    """
    from api.server import _resolve_case_store

    start = time.time()
    try:
        # CS-4 pattern: warm lookup -> Postgres fallback -> ai_summary body.
        # AI-33: an investigator can open Risk Assessment without ever
        # having opened Similar Cases. Auto-run /similar_cases' own logic
        # internally the moment similar_cases (that route's output) isn't
        # already resolvable, instead of the hard 400 this used to raise.
        # /similar_cases' own entry check (complaint_intelligence) fires
        # the same way if IT turns out to be missing too — this route
        # only needs to know about the one route immediately before it.
        case_data, data_source = resolve_prerequisite_case_data(
            req.case_id,
            req.ai_summary,
            required_field="similar_cases",
            run_prerequisite_route=lambda: similar_cases_service.run_similar_cases(
                SimilarCasesRequest(case_id=req.case_id), username, token
            ),
            resolve_case_store=_resolve_case_store,
            route_name="risk_assessment",
        )
        logger.info("case_id=%s data_source=%s", req.case_id, data_source)

        # AI-32: the two independent staleness triggers, combined — see
        # core.narrative_staleness's module docstring. See
        # _risk_assessment_cache_hit's docstring for why this route has
        # no separate "pipeline" stage of its own.
        staleness = evaluate_cache_staleness(req.case_id, req.reload_ai_summary)

        cache_hit = _risk_assessment_cache_hit(req, username, case_data, data_source, staleness, start)
        if cache_hit is not None:
            return cache_hit

        return _risk_assessment_fresh_run(req, username, token, case_data, data_source, staleness, start)

    except AppworksSessionExpiredError as exc:
        logger.warning("risk_assessment route: AppWorks session expired for case_id=%s", req.case_id)
        _log_call(req.case_id, username, "auth_error", int((time.time() - start) * 1000))
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Risk assessment route failed for case_id=%s", req.case_id)
        _log_call(req.case_id, username, "error", int((time.time() - start) * 1000))
        raise HTTPException(status_code=500, detail=f"Risk assessment failed: {exc}") from exc
    finally:
        logger.info("POST /risk_assessment completed for case_id=%s", req.case_id)
