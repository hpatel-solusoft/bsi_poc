"""
Service layer for POST /fraud_patterns (ON-DEMAND — Fraud Patterns tab).

Owns: the entire /fraud_patterns business logic — prerequisite resolution
(auto-running /intake when needed), the cache-hit check, the graph-read +
LLM-explain pipeline, persistence of the written summary, and response
shaping. Same flow as api/services/similar_cases_service.py.

Does NOT own: HTTP routing (api/server.py calls run_fraud_patterns and
returns whatever it does) or the pipeline and staleness primitives
(api/pipeline_execution.py, core/narrative_staleness.py).

THE GRAPH RESULT IS NEVER SAVED. Only the written summary text is cached
(under ROUTE_NAME in agent_summary_cache); the Fraud Patterns result is
read live from Neo4j on every request, cache hit or not, and is never
handed to CASE_STORE or persist_case_session. That is why this module,
unlike the Similar Cases one, does not copy a graph section into the warm
store or into ai_summary.

`_resolve_case_store` is imported late (inside run_fraud_patterns) for the
same circular-import reason documented in similar_cases_service.py.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

from fastapi import HTTPException

from agent_service.prompt_builders import build_fraud_patterns_prompt
from agent_service.runner_provider import get_runner
from api.message_utils import build_ai_summary
from api.models import FraudPatternsRequest, intakeRequest
from api.pipeline_execution import (
    evaluate_cache_staleness,
    fetch_live_fraud_patterns,
    resolve_prerequisite_case_data,
    run_fraud_patterns_pipeline,
)
from api.response_builders import format_provenance_lines
from api.services import intake_service

# Exception TYPE only, for HTTP error-boundary translation — the same narrow
# exception to "api/ never imports from appworks/" that api/server.py makes.
from appworks.appworks_auth import AppworksSessionExpiredError
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

ROUTE_NAME = "fraud_patterns"
ENDPOINT = "/fraud_patterns"
SOURCE_CACHE = "db_cache"
SOURCE_LLM = "llm"


def _log_call(case_id: str, username: str, status: str, started: float) -> None:
    """Single call site for the audit-log write, so every branch logs the same fields."""
    log_agent_call(
        case_id=case_id,
        agent_name=ROUTE_NAME,
        endpoint=ENDPOINT,
        latency_ms=int((time.time() - started) * 1000),
        status=status,
        username=username,
    )


def _build_response(
    req: FraudPatternsRequest,
    case_data: Dict[str, Any],
    agent_summary: str,
    provenance: Any,
    graph_result: Dict[str, Any],
    meta: Dict[str, Any],
) -> Dict[str, Any]:
    """The response envelope shared by the cache-hit and fresh-run paths."""
    return {
        "complaint_number": get_complaint_number(case_data) or req.case_id,
        "status": "completed",
        "details": {
            "agent_summary": agent_summary,
            "provenance_trail": format_provenance_lines(provenance),
            "graph_findings": {ROUTE_NAME: graph_result},
            "meta": meta,
        },
    }


def _fraud_patterns_cache_hit(
    req: FraudPatternsRequest,
    username: str,
    case_data: Dict[str, Any],
    data_source: str,
    staleness: Any,
    started: float,
) -> Optional[Dict[str, Any]]:
    """Answer from the cached summary WITHOUT calling the LLM, if one exists.

    The graph result is still read live. Returns None (never raises) when
    there is no usable cache or a refresh was requested, so the caller falls
    through to _fraud_patterns_fresh_run.
    """
    if staleness.should_auto_refresh:
        return None
    cached_summary = get_route_summary_text(case_data, ROUTE_NAME)
    if cached_summary is None:
        return None

    graph_result = fetch_live_fraud_patterns(req.case_id)
    logger.info("fraud_patterns CACHE HIT for case_id=%s — no LLM call made", req.case_id)
    _log_call(req.case_id, username, "success", started)
    meta = {
        "data_source": data_source,
        "agent_summary_source": SOURCE_CACHE,
        "stale": staleness.stale,
        "generated_at": get_route_generated_at(case_data, ROUTE_NAME),
        "username": get_route_username(case_data, ROUTE_NAME),
    }
    return _build_response(
        req, case_data, cached_summary, case_data.get("provenance_trail", []), graph_result, meta
    )


def _cache_summary(
    req: FraudPatternsRequest, username: str, case_data: Dict[str, Any], agent_summary: str, provenance: Any
) -> Dict[str, Any]:
    """Persist ONLY the written summary (plus carried-forward case data) and return the ai_summary.

    No graph section is passed to build_ai_summary, and none is put in
    CASE_STORE, so the Fraud Patterns result cannot reach Postgres.
    """
    ai_summary = build_ai_summary(case_data, {}, provenance)
    cache = merge_agent_summary_cache(case_data, ROUTE_NAME, agent_summary, username=username)
    ai_summary["investigation"][AGENT_SUMMARY_CACHE_KEY] = cache
    CASE_STORE[req.case_id][AGENT_SUMMARY_CACHE_KEY] = cache
    CASE_STORE[req.case_id]["provenance_trail"] = provenance
    persist_case_session(req.case_id, ai_summary)
    return ai_summary


def _fraud_patterns_fresh_run(
    req: FraudPatternsRequest,
    username: str,
    token: str,
    case_data: Dict[str, Any],
    data_source: str,
    staleness: Any,
    started: float,
) -> Dict[str, Any]:
    """Run the graph read + LLM-explain pipeline, cache the summary, and respond."""
    agent_summary, graph_result, provenance = run_fraud_patterns_pipeline(
        req.case_id, case_data, get_runner(), build_fraud_patterns_prompt, token=token
    )
    ai_summary = _cache_summary(req, username, case_data, agent_summary, provenance)
    _log_call(req.case_id, username, "success", started)
    investigation = ai_summary["investigation"]
    meta = {
        "data_source": data_source,
        "agent_summary_source": SOURCE_LLM,
        "stale": staleness.stale,
        "generated_at": get_route_generated_at(investigation, ROUTE_NAME),
        "username": get_route_username(investigation, ROUTE_NAME),
    }
    return _build_response(req, case_data, agent_summary, provenance, graph_result, meta)


def run_fraud_patterns(req: FraudPatternsRequest, username: str, token: str) -> Dict[str, Any]:
    """
    ON-DEMAND — Fraud Patterns Route.
    Explains how this case's allegation category compares with the archive:
    related cases, financial exposure, closed-case outcomes and yearly trend.
    Flow: /intake -> /fraud_patterns (auto-runs /intake if Case Summary has
    not been opened yet).
    """
    from api.server import _resolve_case_store

    started = time.time()
    try:
        case_data, data_source = resolve_prerequisite_case_data(
            req.case_id,
            req.ai_summary,
            required_field="complaint_intelligence",
            run_prerequisite_route=lambda: intake_service.run_intake(
                intakeRequest(case_id=req.case_id), username, token
            ),
            resolve_case_store=_resolve_case_store,
            route_name=ROUTE_NAME,
        )
        staleness = evaluate_cache_staleness(req.case_id, req.reload_ai_summary)
        cached = _fraud_patterns_cache_hit(req, username, case_data, data_source, staleness, started)
        if cached is not None:
            return cached
        return _fraud_patterns_fresh_run(req, username, token, case_data, data_source, staleness, started)

    except AppworksSessionExpiredError as exc:
        logger.warning("fraud_patterns route: AppWorks session expired for case_id=%s", req.case_id)
        _log_call(req.case_id, username, "auth_error", started)
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Fraud patterns route failed for case_id=%s", req.case_id)
        _log_call(req.case_id, username, "error", started)
        raise HTTPException(status_code=500, detail=f"Fraud patterns analysis failed: {exc}") from exc
    finally:
        logger.info("POST /fraud_patterns completed for case_id=%s", req.case_id)
