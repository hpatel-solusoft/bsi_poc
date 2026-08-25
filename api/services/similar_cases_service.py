"""
Service layer for POST /similar_cases (ON-DEMAND — Similar Cases Route,
Step 2 in flow).

Owns: the entire /similar_cases business logic — prerequisite resolution
(auto-running /intake when needed), staleness/cache-hit check, the
structural-matching + LLM-explain pipeline, persistence (CASE_STORE +
case_ai_summary_store), and response shaping.

Does NOT own: HTTP routing itself (api/server.py just calls
run_similar_cases(req, username, token) and returns/raises whatever it
does), or the underlying pipeline/staleness primitives
(api/pipeline_execution.py, core/narrative_staleness.py) — this module is
the orchestration layer between the two.

`_resolve_case_store` is deliberately imported LATE (inside
run_similar_cases, not at module level) rather than at the top of this
file: it still lives in api/server.py (a route-agnostic CS-4 helper that
hasn't been extracted to a service module of its own), and api/server.py
imports THIS module at its own top level to wire up the /similar_cases
route. A top-level `from api.server import _resolve_case_store` here
would therefore be a circular import at module load time. Deferring the
import into the function body sidesteps the cycle and, as a bonus, means
existing tests that patch `api.server._resolve_case_store` before calling
`server.similar_cases(...)` keep working unmodified — the deferred import
re-resolves the name from api.server's current state at call time, after
any patch has already been applied.

The /intake prerequisite is invoked via `api.services.intake_service.
run_intake` directly rather than `api.server.intake`: both are
equivalent (intake is now itself a thin wrapper over run_intake, same
pattern as this module), but importing the sibling service module avoids
the circular-import problem entirely instead of needing a second late
import for it.

Extracted verbatim from api/server.py's `/similar_cases` route body
during the service-layer refactor — same behavior, same log lines, same
response shape; only the module boundary changed. Tests that used to
patch api.server.<name> for this route's OWN internals (as opposed to
_resolve_case_store above) now patch
api.services.similar_cases_service.<name> instead, since that's where
the call sites actually live now.

Internal structure (post-refactor, behavior unchanged): run_similar_cases
is a thin orchestrator over _similar_cases_cache_hit (Optional[Dict] —
None means "no usable cache, fall through") and
_similar_cases_fresh_run (always returns a response dict), sharing
_log_call so the audit-log call site can't drift between the two paths.
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

from agent_service.prompt_builders import build_similar_cases_prompt
from agent_service.runner_provider import get_runner
from api.message_utils import build_ai_summary
from api.models import SimilarCasesRequest, intakeRequest
from api.pipeline_execution import (
    evaluate_cache_staleness,
    fetch_live_similar_cases,
    resolve_prerequisite_case_data,
    run_similar_cases_pipeline,
)
from api.response_builders import format_provenance_lines
from api.services import intake_service
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
    """Single call site for the /similar_cases audit-log write, so every
    branch (cache hit, fresh run, auth_error, error) logs the same five
    fields and only ever varies status/latency."""
    log_agent_call(
        case_id=case_id,
        agent_name="similar_cases",
        endpoint="/similar_cases",
        latency_ms=latency_ms,
        status=status,
        username=username,
    )


def _similar_cases_cache_hit(
    req: SimilarCasesRequest,
    username: str,
    case_data: Dict[str, Any],
    data_source: str,
    staleness: Any,
    start: float,
) -> Optional[Dict[str, Any]]:
    """Answer from case_ai_summary_store / warm CASE_STORE WITHOUT calling
    the LLM again, if there's a usable cached agent_summary for this
    case_id. Only an explicit reload_ai_summary=True
    (staleness.should_auto_refresh) bypasses this lookup and forces a
    fresh run below.

    similar_cases is ALWAYS a live Neo4j read, cache hit or not —
    case_data never carries the structural matches section at all (it is
    entirely graph-derived; see core.persistence_filters.
    strip_graph_derived_fields), so this tab reflects the graph as it
    stands right now even on a cache hit.

    Returns None (never raises) if there's no usable cache — callers
    should fall through to _similar_cases_fresh_run in that case.
    """
    if staleness.should_auto_refresh:
        return None

    cached_summary = get_route_summary_text(case_data, "similar_cases")
    if cached_summary is None:
        return None

    live_similar_cases = fetch_live_similar_cases(req.case_id)
    duration_seconds = round(time.time() - start, 1)
    logger.info(
        "similar_cases CACHE HIT for case_id=%s — answering from "
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
                "similar_cases": live_similar_cases,
            },
            "meta": {
                "data_source": data_source,
                "agent_summary_source": "db_cache",
                "stale": staleness.stale,
                "generated_at": get_route_generated_at(case_data, "similar_cases"),
                "username": get_route_username(case_data, "similar_cases"),
            },
        },
    }


def _similar_cases_fresh_run(
    req: SimilarCasesRequest,
    username: str,
    token: str,
    case_data: Dict[str, Any],
    data_source: str,
    staleness: Any,
    start: float,
) -> Dict[str, Any]:
    """Run the direct structural-matching + LLM-explain pipeline
    (Section 8.3 AI-14, Section 9.2 — factored out to
    api/pipeline_execution.py) and persist the result. Always returns a
    response dict — never returns None."""
    runner = get_runner()

    agent_summary, similar_cases_data, similar_section, merged_provenance = run_similar_cases_pipeline(
        req.case_id,
        case_data,
        runner,
        build_similar_cases_prompt,
        token=token,
    )

    # Update CS-4 warm store but return only the route-specific section.
    CASE_STORE[req.case_id].update(similar_section)
    CASE_STORE[req.case_id]["provenance_trail"] = merged_provenance

    # ai_summary: updated contract — investigation sections with similar cases.
    # Persisted server-side (Postgres case_ai_summary_store) for the next
    # route to fall back on; no longer returned to the caller.
    ai_summary = build_ai_summary(
        case_data,
        {"similar_cases": similar_cases_data},
        merged_provenance,
    )
    # Cache this run's agent_summary markdown (carrying forward any
    # other route's already-cached entry for this case_id) inside
    # ai_summary["investigation"], the same place every other
    # investigation field lives, so it round-trips on the next fetch.
    ai_summary["investigation"][AGENT_SUMMARY_CACHE_KEY] = merge_agent_summary_cache(
        case_data,
        "similar_cases",
        agent_summary,
        username=username,
    )
    similar_cases_generated_at = get_route_generated_at(ai_summary["investigation"], "similar_cases")
    similar_cases_username = get_route_username(ai_summary["investigation"], "similar_cases")
    CASE_STORE[req.case_id][AGENT_SUMMARY_CACHE_KEY] = ai_summary["investigation"][
        AGENT_SUMMARY_CACHE_KEY
    ]
    persist_case_session(req.case_id, ai_summary)
    _log_call(req.case_id, username, "success", int((time.time() - start) * 1000))

    logger.info("SIMILAR CASES NARRATIVE TOTAL KEYs: %d", len(similar_cases_data))
    return {
        "complaint_number": get_complaint_number(case_data) or req.case_id,
        "status": "completed",
        "details": {
            "agent_summary": agent_summary,
            "provenance_trail": format_provenance_lines(merged_provenance),
            # Raw Neo4j structural match result (AI-14 —
            # reasoning_layer.similar_cases.find_structural_matches):
            # matches, match_reasons, score, source, total_candidates_scored.
            # Previously computed into `sections`/ai_summary but never
            # returned to the caller — only the LLM's narrative explanation
            # of it was. Surfaced here the same way graph_findings is on
            # /intake, so the graph JSON itself reaches the UI.
            "graph_findings": {
                "similar_cases": similar_cases_data,
            },
            "meta": {
                "data_source": data_source,
                "agent_summary_source": "llm",
                "stale": staleness.stale,
                "generated_at": similar_cases_generated_at,
                "username": similar_cases_username,
            },
        },
    }


def run_similar_cases(req: SimilarCasesRequest, username: str, token: str) -> Dict[str, Any]:
    """
    ON-DEMAND — Similar Cases Route (Step 2 in flow).
    Calls search_similar_cases to find historical cases with matching fraud patterns.
    Requires case_data from a prior /intake run (via CS-4 or ai_summary body).
    Explains historical case matches, pattern relevance, and archive findings.
    Flow: /intake → /similar_cases → /risk_assessment → /plan → /copilot |
    """
    from api.server import _resolve_case_store

    start = time.time()
    try:
        # CS-4 pattern: warm lookup -> Postgres fallback -> ai_summary body.
        # AI-33: an investigator can open Similar Cases without ever
        # having opened Case Summary. Rather than assuming the frontend
        # guarantees click order, auto-run /intake's own logic internally
        # the moment complaint_intelligence (Case Summary's output) isn't
        # already resolvable — instead of the hard 400 this used to raise.
        case_data, data_source = resolve_prerequisite_case_data(
            req.case_id,
            req.ai_summary,
            required_field="complaint_intelligence",
            run_prerequisite_route=lambda: intake_service.run_intake(
                intakeRequest(case_id=req.case_id), username, token
            ),
            resolve_case_store=_resolve_case_store,
            route_name="similar_cases",
        )
        logger.info(
            "case_id=%s data_source=%s key_count=%d",
            req.case_id,
            data_source,
            len(list(case_data.keys())),
        )

        # AI-32: the two independent staleness triggers, combined — see
        # core.narrative_staleness's module docstring. See
        # _similar_cases_cache_hit's docstring for why this route has no
        # separate "pipeline" stage of its own.
        staleness = evaluate_cache_staleness(req.case_id, req.reload_ai_summary)

        cache_hit = _similar_cases_cache_hit(req, username, case_data, data_source, staleness, start)
        if cache_hit is not None:
            return cache_hit

        return _similar_cases_fresh_run(req, username, token, case_data, data_source, staleness, start)

    except AppworksSessionExpiredError as exc:
        logger.warning("similar_cases route: AppWorks session expired for case_id=%s", req.case_id)
        _log_call(req.case_id, username, "auth_error", int((time.time() - start) * 1000))
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Similar cases route failed for case_id=%s", req.case_id)
        _log_call(req.case_id, username, "error", int((time.time() - start) * 1000))
        raise HTTPException(status_code=500, detail=f"Similar cases analysis failed: {exc}") from exc
    finally:
        logger.info("POST /similar_cases completed for case_id=%s", req.case_id)
