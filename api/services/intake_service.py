"""
Service layer for POST /intake (Section 3.1's AUTO flow).

Owns: the entire /intake business logic — staleness check, agent-summary
cache lookup, the LLM agent call (AUTO tools 1-2: intake + enrichment),
the direct non-LLM network-match/context-enrichment pipeline step,
persistence (CASE_STORE + case_ai_summary_store), and response shaping.

Does NOT own: HTTP routing itself (that's api/server.py, which just
calls run_intake(req) and returns whatever it returns/raises) or the
underlying pipeline/staleness primitives (api/pipeline_execution.py,
core/narrative_staleness.py) — this module is the orchestration layer
between the two.

Extracted verbatim from api/server.py's `/intake` route body during the
service-layer refactor — same behavior, same log lines, same response
shape; only the module boundary changed. Tests that used to patch
api.server.<name> for this route's internals now patch
api.services.intake_service.<name> instead, since that's where the
call sites actually live now.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict

from fastapi import HTTPException

# See api/server.py's import of the same symbol for why this is a
# deliberate, narrow exception to "no file in api/ imports directly from
# appworks/" — an exception TYPE only, for HTTP error-boundary translation.
from appworks.appworks_auth import AppworksSessionExpiredError

from agent_service.prompt_builders import build_intake_system_prompt
from agent_service.runner_provider import get_runner
from api.message_utils import extract_agent_summary, extract_tool_results
from api.models import intakeRequest
from api.pipeline_execution import evaluate_cache_staleness, fetch_live_graph_findings, run_intake_direct_pipeline
from api.response_builders import build_confidence_summary, fired_rules_only, format_provenance_lines
from core.agent_audit_repository import log_agent_call
from core.case_store import (
    AGENT_SUMMARY_CACHE_KEY,
    CASE_STORE,
    get_cached_route_summary,
    get_route_generated_at,
    get_route_username,
    merge_agent_summary_cache,
    persist_case_session,
    try_resolve_case_data,
)

logger = logging.getLogger(__name__)


def run_intake(req: intakeRequest, username: str, token: str) -> Dict[str, Any]:
    """
    AUTO flow — Section 3.1.
    Runs AUTO tools 1-2 (intake, enrichment) in dependency order
    (LLM decides sequence). Similar cases runs via /similar_cases.
    Immediately after, this route makes one direct, unconditional Python
    call to check_network_match(subject_primary_id) — not an LLM-decided
    tool call, not dispatcher-routed, not in manifest.yaml (Section 8.1:
    non-blocking, never gates complaint acceptance; Section 9.1's
    "invoked directly, never LLM-callable" pattern, same as run_pipeline).
    A Neo4j outage or missing subject degrades this to an empty
    graph_context rather than failing the whole route.
    Populates CS-4 CASE_STORE for all subsequent on-demand calls.
    Flow: /intake → /similar_cases → /risk_assessment → /plan → /copilot |
    """
    start = time.time()
    try:
        # AI-32: the existing reload_ai_summary flag (core data changed,
        # via AppWorks — see core.narrative_staleness's module docstring)
        # is now ONE of two independent triggers that can make this
        # cache stale; AI-31's (:Case).last_inference_change_at is the
        # other (an investigator reject/revert). staleness.stale reports
        # ONLY the graph trigger to the caller (core_data_changed is
        # AppWorks's own flag, so echoing it back would be circular —
        # see StalenessCheck.stale's docstring). staleness.should_auto_refresh
        # — the gate below — is narrower still: a graph-only change no
        # longer auto-runs the agent on its own (see
        # StalenessCheck.should_auto_refresh's docstring for why); only
        # an explicit reload_ai_summary=True does. Compare
        # staleness.should_rerun_full_pipeline, which narrows further
        # still, to core_data-only, for the one thing actually worth
        # re-running from scratch for — see
        # core.narrative_staleness.StalenessCheck.should_rerun_full_pipeline's
        # docstring for why a graph-only change never warrants it.
        staleness = evaluate_cache_staleness(req.case_id, req.reload_ai_summary)

        # Agent-summary cache: if intake has already produced and
        # persisted an agent_summary for this case_id, answer from
        # case_ai_summary_store / warm CASE_STORE WITHOUT calling the LLM
        # again. Only an explicit reload_ai_summary=True bypasses this
        # lookup and falls through to a fresh agent run below — a
        # graph-only trigger still reports stale=True in the cache-hit
        # response's meta below, but does not by itself force a fresh
        # run (see run_intake_direct_pipeline's force argument further
        # down for what a fresh run does NOT also do).
        if not staleness.should_auto_refresh:
            cached = get_cached_route_summary(req.case_id, "intake")
            if cached is not None:
                cached_case_data, cached_summary = cached
                CASE_STORE[req.case_id] = cached_case_data
                # graph_findings is ALWAYS a live Neo4j read, cache hit or
                # not — cached_case_data never carries network_match_flag/
                # graph_context/graph_signals/rules_fired (see
                # core.persistence_filters.strip_graph_derived_fields), so
                # an investigator who just rejected an inference sees it
                # reflected immediately instead of reading whatever
                # snapshot existed when the tab was first opened.
                subject_id = (cached_case_data.get("complaint_intelligence") or {}).get(
                    "subject_primary_id"
                )
                live_findings = fetch_live_graph_findings(req.case_id, subject_id)
                duration_seconds = round(time.time() - start, 1)
                logger.info(
                    "intake CACHE HIT for case_id=%s — answering from "
                    "case_ai_summary_store, no LLM call made",
                    req.case_id,
                )
                log_agent_call(
                    case_id=req.case_id,
                    agent_name="intake",
                    endpoint="/intake",
                    latency_ms=int(duration_seconds * 1000),
                    status="success",
                    username=username,
                )
                return {
                    "case_id": req.case_id,
                    "status": "completed",
                    "details": {
                        "agent_summary": cached_summary,
                        "provenance_trail": format_provenance_lines(cached_case_data.get("provenance_trail", [])),
                        "graph_findings": {
                            "network_match_flag": live_findings.get("network_match_flag"),
                            "graph_context": live_findings.get("graph_context"),
                            "graph_signals": live_findings.get("graph_signals"),
                            "rules_fired": fired_rules_only(live_findings.get("rules_fired")),
                            "confidence_summary": build_confidence_summary(
                                live_findings.get("rules_fired")
                            ),
                        },
                        "meta": {
                            "tool_calls_made": 0,
                            "duration_seconds": duration_seconds,
                            "pipeline_status": "cached",
                            "reload_ai_summary": req.reload_ai_summary,
                            "agent_summary_source": "db_cache",
                            # AI-32/graph-auto-refresh: this branch can be
                            # reached even when the graph changed
                            # (should_auto_refresh only cares about an
                            # explicit reload_ai_summary=True), so `stale`
                            # correctly reports True on a cache hit when
                            # that's genuinely the case. The caller, not
                            # this route, decides whether to act on that
                            # by requesting an explicit reload.
                            "stale": staleness.stale,
                            "generated_at": get_route_generated_at(cached_case_data, "intake"),
                            "username": get_route_username(cached_case_data, "intake"),
                        },
                    },
                }

        if not os.getenv("OPENAI_API_KEY"):
            raise HTTPException(status_code=500, detail="OPENAI_API_KEY not configured")

        runner = get_runner()
        # Scope to intake + enrichment only; similar cases is a separate route.

        messages, provenance_trail, _ = runner.run_scoped(
            system_prompt=build_intake_system_prompt(),
            user_message=(f"intake case {req.case_id}."),
            scope="CASE_SUMMARY",  # ← this scope includes intake + enrichment tools only;
            # token: caller's AppWorks SAMLart, consumed by
            # semantic_layer/dispatcher.py before any tool function sees
            # it — see appworks/appworks_auth.py's set_request_token.
            # CASE_SUMMARY is where the case's AppWorks fetch actually
            # happens, so this is the route that needs it most.
            execution_context={"token": token},
        )
        sections = extract_tool_results(messages, runner.dispatcher.tool_to_section)

        # Direct, non-LLM network-match + context-enrichment pipeline work
        # (Section 8.1 AI-12, Section 9.1 AI-13) — factored out to
        # api/pipeline_execution.py. subject_primary_id was injected into
        # complaint_intelligence by extract_tool_results above.
        #
        # AI-32: staleness.should_rerun_full_pipeline, NOT
        # req.reload_ai_summary directly, controls the Wave 1/2
        # force-rerun inside run_intake_direct_pipeline (it gates
        # enrich_graph_context's own `force` argument) — True only for a
        # core_data-driven refresh. This call is only reached at all when
        # should_auto_refresh was True (an explicit reload_ai_summary=True)
        # or the cache was empty — a graph-only signal with no explicit
        # reload now serves straight from cache above and never reaches
        # here (see the cache-hit branch's own AI-32 comment). Either way,
        # Wave 1/2 is left exactly as an investigator's
        # reject/revert already left it in Neo4j — nothing about AppWorks
        # structural data changed, so nothing there needs recomputing.
        sections, provenance_trail = run_intake_direct_pipeline(
            req.case_id,
            staleness.should_rerun_full_pipeline,
            sections,
            provenance_trail,
            username=username,
        )

        # Cache this run's agent_summary markdown (carrying forward any
        # other route's already-cached entry for this case_id) so the next
        # /intake call with reload_ai_summary=False can skip the LLM.
        assistant_text = extract_agent_summary(messages)
        existing_case_data = try_resolve_case_data(req.case_id) or {}
        sections[AGENT_SUMMARY_CACHE_KEY] = merge_agent_summary_cache(
            existing_case_data,
            "intake",
            assistant_text,
            username=username,
        )
        intake_generated_at = get_route_generated_at(sections, "intake")
        intake_username = get_route_username(sections, "intake")

        # CS-4: populate warm in-memory store with all sections + provenance.
        CASE_STORE[req.case_id] = {**sections, "provenance_trail": provenance_trail}

        # ai_summary is the internal contract object handed between routes.
        # It is no longer returned to the caller (Data Persistence Spec v1.0,
        # Section B.2/D.1): AppWorks now sends case_id only on every
        # subsequent call, so the full JSON is persisted server-side in
        # PostgreSQL case_ai_summary_store and rehydrated there on the next
        # request instead of round-tripping through the client.
        ai_summary = {
            "investigation": sections,
            "provenance_trail": provenance_trail,
        }
        persist_case_session(req.case_id, ai_summary)

        duration_seconds = round(time.time() - start, 1)
        log_agent_call(
            case_id=req.case_id,
            agent_name="intake",
            endpoint="/intake",
            latency_ms=int(duration_seconds * 1000),
            status="success",
            username=username,
        )

        return {
            "case_id": req.case_id,
            "status": "completed",
            "details": {
                "agent_summary": assistant_text,
                "provenance_trail": format_provenance_lines(provenance_trail),
                # graph_context/graph_signals/rules_fired) previously only
                # reached ai_summary.investigation — computed after the LLM's
                # agent_summary text was already finalised, so it never
                # surfaced in the response the UI actually renders. Surfaced
                # explicitly here so the pipeline's output stops being
                # silently dropped before it reaches the screen.
                "graph_findings": {
                    "network_match_flag": sections.get("network_match_flag"),
                    "graph_context": sections.get("graph_context"),
                    "graph_signals": sections.get("graph_signals"),
                    # Fired rules only. build_confidence_summary still
                    # receives the FULL block: it counts by confidence and
                    # already skips non-fired entries itself.
                    "rules_fired": fired_rules_only(sections.get("rules_fired")),
                    "confidence_summary": build_confidence_summary(sections.get("rules_fired")),
                },
                "meta": {
                    "tool_calls_made": len(provenance_trail),
                    "duration_seconds": duration_seconds,
                    # AI-32: "reloaded" now specifically means the full
                    # pipeline (Wave 1/2) re-ran — core_data was part of
                    # the reason. A graph-only refresh still called the
                    # LLM fresh (that's why this branch was reached at
                    # all) but skipped that heavier step, so it gets its
                    # own status rather than being folded into "ran" (a
                    # cache hit) or "reloaded" (implies the full pipeline).
                    "pipeline_status": (
                        "reloaded"
                        if staleness.should_rerun_full_pipeline
                        else ("narrative_regenerated" if staleness.graph_changed else "ran")
                    ),
                    "reload_ai_summary": req.reload_ai_summary,
                    "agent_summary_source": "llm",
                    "stale": staleness.stale,
                    "generated_at": intake_generated_at,
                    "username": intake_username,
                },
            },
        }
    except AppworksSessionExpiredError as exc:
        logger.warning("intake route: AppWorks session expired for case_id=%s", req.case_id)
        log_agent_call(
            case_id=req.case_id,
            agent_name="intake",
            endpoint="/intake",
            latency_ms=int((time.time() - start) * 1000),
            status="auth_error",
            username=username,
        )
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("intake route failed for case_id=%s", req.case_id)
        log_agent_call(
            case_id=req.case_id,
            agent_name="intake",
            endpoint="/intake",
            latency_ms=int((time.time() - start) * 1000),
            status="error",
            username=username,
        )
        raise HTTPException(status_code=500, detail=f"Investigation failed: {exc}") from exc
    finally:
        logger.info("POST /intake completed for case_id=%s", req.case_id)