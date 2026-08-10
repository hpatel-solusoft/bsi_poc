"""
HTTP endpoints for the BSI Fraud Investigation Platform.
Responsibilities: endpoints, CASE_STORE (CS-4), response shaping,
provenance trail extraction and persistence.
Outside its scope: calling appworks_services directly, knowing tool names
or manifest structure directly, or knowing SQL/table schemas for the
PostgreSQL fallback (that lives in core/case_store.py and its repositories).
"""

import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import psycopg2
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from neo4j.exceptions import Neo4jError

from agent_service.agent_runner import BSIAgentRunner
from agent_service.prompt_builders import (
    build_copilot_prompt,
    build_intake_system_prompt,
    build_plan_prompt,
    build_report_generation_prompt,
    build_risk_assessment_prompt,
    build_similar_cases_prompt,
)
from api.message_utils import (
    build_ai_summary,
    extract_agent_summary,
    extract_tool_results,
    merge_direct_result,
    merge_provenance,
)
from api.models import (
    ConversationHistoryResponse,
    CopilotRequest,
    FraudNetworkResponse,
    GraphIngestRequest,
    InvestigationStepsResponse,
    ModifyInvestigationStepsRequest,
    ModifyInvestigationStepsResponse,
    PlanRequest,
    RejectInferenceRequest,
    RejectInferenceResponse,
    ReportGenerationRequest,
    RevertRejectionRequest,
    RevertRejectionResponse,
    RevertToAiPlanRequest,
    RevertToAiPlanResponse,
    RiskAssessmentRequest,
    RuleAuditResponse,
    SimilarCasesRequest,
    intakeRequest,
)
from api.pipeline_execution import (
    evaluate_cache_staleness,
    fetch_live_graph_findings,
    fetch_live_risk_signals,
    fetch_live_rule_aware_tasks,
    fetch_live_similar_cases,
    prepare_plan_context,
    resolve_prerequisite_case_data,
    run_intake_direct_pipeline,
    run_plan_pipeline,
    run_risk_assessment_pipeline,
    run_similar_cases_pipeline,
)
from api.response_builders import (
    apply_step_override_to_summary,
    build_confidence_summary,
    fired_rules_only,
    format_provenance_lines,
    render_markdown_html,
    render_markdown_html_with_sources,
    replace_markdown_section,
    resolve_plan_agent_summary,
    validate_ai_summary_contract,
)
from core import graph_ingest_repository
from core.agent_audit_repository import log_agent_call
from core.case_store import (
    AGENT_SUMMARY_CACHE_KEY,
    CASE_STORE,
    fetch_copilot_history,
    get_cached_investigation_steps,
    get_cached_route_summary,
    get_case_ai_summary_cache_updated_at,
    get_route_generated_at,
    get_route_generated_at_datetime,
    get_route_summary_text,
    merge_agent_summary_cache,
    persist_case_session,
    resolve_case_data,
    resolve_copilot_history,
    store_copilot_turn,
    try_resolve_case_data,
)
from core.db import DatabaseUnavailableError
from core.db import close_pool as close_db_pool
from core.db import init_pool as init_db_pool
from core.investigation_plan_override_repository import (
    compute_plan_staleness,
    delete_override,
    get_override,
    upsert_override,
)
from core.narrative_staleness import StalenessCheck
from core.report_artifacts_repository import get_latest_report, save_report
from etl.ingest_service import ingest as run_graph_ingest
from reasoning_layer.apply_schema import apply_schema
from reasoning_layer.decision_log import build_decision_log
from reasoning_layer.fraud_network import get_fraud_network
from reasoning_layer.investigation_tasks import build_rule_aware_tasks
from reasoning_layer.neo4j_client import (
    GraphUnavailableError,
)
from reasoning_layer.neo4j_client import close_driver as close_neo4j_driver
from reasoning_layer.neo4j_client import init_driver as init_neo4j_driver
from reasoning_layer.rejection import (
    InferenceNotFoundError,
    reject_inference,
    revert_rejection,
)
from reasoning_layer.report_generation import assemble_related_network
from reasoning_layer.report_llm_context import build_report_llm_context
from reasoning_layer.rule_audit import get_rule_audit
from reasoning_layer.rule_engine import verify_rule_files
from semantic_layer.entity_contracts import GeneratedReport as GeneratedReportContract
from utils.report_pdf_renderer import render_report_pdf, report_pdf_filename

_runner: Optional[BSIAgentRunner] = None

load_dotenv()
logger = logging.getLogger(__name__)

app = FastAPI(title="BSI Fraud Investigation Platform")

# CORS: AppWorks (and any other browser-side caller) hits this API
# cross-origin — different host/port than wherever this API is deployed —
# so the browser sends a preflight OPTIONS request first. With no CORS
# middleware, that preflight has no Access-Control-Allow-Origin header to
# check against and the browser blocks the real request before it ever
# reaches a route handler (visible client-side as HTTP status 0 /
# net::ERR_FAILED, not as a 4xx/5xx from this app).
#
# Defaults to allowing all origins ("*"), since the set of AppWorks
# hosts calling this API varies by environment and isn't known in
# advance. To lock this down later, set CORS_ALLOWED_ORIGINS to a
# comma-separated list of explicit origins, e.g.
# "http://processsuite-cm.localdomain.com:81,https://bsi.example.com" —
# no code change needed, just the env var.
_cors_allowed_origins_raw = os.getenv("CORS_ALLOWED_ORIGINS", "*").strip()
if _cors_allowed_origins_raw == "*":
    _cors_allowed_origins = ["*"]
    # allow_credentials must be False with a wildcard origin — the CORS
    # spec forbids "Access-Control-Allow-Origin: *" together with
    # "Access-Control-Allow-Credentials: true", and browsers reject the
    # response if a server sends both. This app doesn't rely on
    # cookie/session-based auth for these routes, so this is safe.
    _cors_allow_credentials = False
else:
    _cors_allowed_origins = [o.strip() for o in _cors_allowed_origins_raw.split(",") if o.strip()]
    _cors_allow_credentials = True

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_allowed_origins,
    allow_credentials=_cors_allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)
logger.info(
    "CORS enabled — allow_origins=%s allow_credentials=%s",
    _cors_allowed_origins,
    _cors_allow_credentials,
)


@app.on_event("startup")
def _init_agent_operational_store() -> None:
    """
    Warm the PostgreSQL connection pool on startup so the first request
    doesn't pay connection-setup latency, and print a clear, unmissable
    terminal banner reporting whether it succeeded. This is printed
    directly (not just logged) so it's visible on `uvicorn` startup
    regardless of log level or handler configuration elsewhere in the app.

    A failure here is not fatal — the app still serves in-memory CS-4
    traffic; only the Postgres fallback (case_ai_summary_store,
    conversation_history, agent_audit_log) is unavailable until
    connectivity is restored.
    """
    banner = "=" * 72
    try:
        init_db_pool()
        # Ensure the ETL bookkeeping table exists even when running under a
        # bare `uvicorn` (local dev), which does not go through the docker
        # entrypoint that applies migrations/*.sql. Idempotent and best-effort.
        graph_ingest_repository.ensure_table()
        print(banner)
        print("[BSI] PostgreSQL: CONNECTED — agent_operational_store fallback is live")
        print(banner)
    except DatabaseUnavailableError as exc:
        print(banner)
        print(f"[BSI] WARNING: PostgreSQL: NOT CONNECTED — {exc}")
        print("[BSI] Starting anyway. In-memory CS-4 will serve requests, but the ")
        print("[BSI] case_ai_summary_store / conversation_history / agent_audit_log ")
        print("[BSI] fallback is UNAVAILABLE until PostgreSQL is reachable.")
        print(banner)
        logger.error("PostgreSQL pool unavailable at startup — fallback reads will miss: %s", exc)


@app.on_event("startup")
def _init_reasoning_layer() -> None:
    """
    Warm the Neo4j driver on startup, same banner treatment as Postgres.
    A failure here is not fatal to the app itself — AppWorks-backed
    routes (/intake, /similar_cases, /risk_assessment, /plan, /copilot's
    AppWorks path) are unaffected — but reasoning_layer.pipeline.run_pipeline
    (invoked directly by Context Enrichment and by the ETL ingest service —
    never LLM-callable, never in manifest.yaml, per Section 9.1) and any
    future Neo4j-backed dispatcher tool will fail once called until
    connectivity is restored.
    """
    banner = "=" * 72
    try:
        init_neo4j_driver()

        # Constraints/indexes and the :InferenceRule registry. Every statement
        # is IF NOT EXISTS / MERGE, so this is a no-op on an already-provisioned
        # graph. It runs on startup because the alternative — a human
        # remembering to pipe schema.cypher into cypher-shell — means the rule
        # library eventually runs against an unconstrained graph, where every
        # MERGE is a label scan and two concurrent ingests can create duplicate
        # :Employer nodes that Rule 1 then silently fails to match across.
        # Set NEO4J_APPLY_SCHEMA_ON_STARTUP=false to opt out (e.g. if graph DDL
        # is owned by a DBA in your environment).
        if os.getenv("NEO4J_APPLY_SCHEMA_ON_STARTUP", "true").lower() != "false":
            apply_schema()

        # Fail fast if a rule .cypher file is missing: a rule that cannot be
        # loaded must break the boot, not quietly never fire in production.
        rule_ids = verify_rule_files()

        print(banner)
        print(f"[BSI] Neo4j: CONNECTED — reasoning layer live ({len(rule_ids)} rules loaded)")
        print(banner)
    except GraphUnavailableError as exc:
        print(banner)
        print(f"[BSI] WARNING: Neo4j: NOT CONNECTED — {exc}")
        print("[BSI] Starting anyway. AppWorks-backed routes are unaffected; ")
        print("[BSI] reasoning_layer.pipeline.run_pipeline will fail until Neo4j is reachable.")
        print(banner)
        logger.error("Neo4j driver unavailable at startup — reasoning pipeline calls will fail: %s", exc)


@app.on_event("shutdown")
def _close_agent_operational_store() -> None:
    """Release pooled PostgreSQL connections on shutdown."""
    close_db_pool()


@app.on_event("shutdown")
def _close_reasoning_layer() -> None:
    """Close the Neo4j driver on shutdown."""
    close_neo4j_driver()


# -----------------------------------------------------------------------
# CS-4: Case session context — in-memory for warm, same-process lookups.
# On a miss (server restart, or a request landing on a different worker),
# falls back to the PostgreSQL case_ai_summary_store table (Data Persistence
# and Synchronisation Specification v1.0, Section D.1) before finally
# accepting ai_summary in the request body as a legacy/explicit-override
# path. AppWorks now sends case_id only by default — see
# core.case_store.resolve_case_data for the full resolution order.
# -----------------------------------------------------------------------


def _get_runner() -> BSIAgentRunner:
    """
    Returns the shared BSIAgentRunner instance.
    Initialized once on first request — deferred to ensure
    environment variables are loaded before OpenAI client is created.
    """
    global _runner
    if _runner is None:
        _runner = BSIAgentRunner()
    return _runner


def _resolve_case_store(case_id: str, ai_summary: Optional[Dict[str, Any]]) -> tuple:
    """
    CS-4 lookup pattern used by all ON-DEMAND handlers.

    Resolution order (Data Persistence Spec v1.0, Section D.1):
      1. In-memory CASE_STORE (CS-4) — warm, same-process.
      2. PostgreSQL case_ai_summary_store — fallback used whenever AppWorks
         sends case_id only, which is now the default request shape.
      3. ai_summary in the request body — explicit-override / legacy path.
    Delegates to core.case_store.resolve_case_data so the fallback logic
    lives in one place (core/) rather than duplicated per endpoint.

    Returns (case_data, source) — source is one of
    core.case_store.SOURCE_CS_MEMORY / SOURCE_POSTGRES_FALLBACK /
    SOURCE_CLIENT_SUPPLIED, logged by the caller and useful for testing.
    """
    return resolve_case_data(case_id, ai_summary, validate_ai_summary_contract)


# -----------------------------------------------------------------------
# reload_ai_summary
#
# This flag now governs TWO things:
#
# 1. Agent-summary caching (core.case_store.AGENT_SUMMARY_CACHE_KEY).
#    /intake, /similar_cases, /risk_assessment, and /plan each check
#    core.case_store.get_cached_route_summary(case_id, route) FIRST:
#      False (default) — on a cache hit, the route returns the persisted
#                         agent_summary markdown from case_ai_summary_store
#                         / warm CASE_STORE WITHOUT calling the LLM again.
#                         On a miss (never run for this case_id, or an
#                         older cache with no entry for this route), the
#                         route runs its agent normally and caches the
#                         result for next time.
#      True             — skip the cache lookup unconditionally, always
#                          call the LLM, and overwrite this route's cache
#                          entry with the fresh result.
#
# 2. reasoning_layer/pipeline.py's run_pipeline (invoked via
#    reasoning_layer/context_enrichment.py's enrich_graph_context, called
#    from /intake and /copilot) — Principle 10 in pipeline.py.
#      False (default) — the pipeline keeps its own existing skip-if-
#                         already-run behavior; unchanged either way.
#      True             — force the pipeline to re-run even though it
#                          already completed (bypasses the Principle 10
#                          skip for this call only).
# -----------------------------------------------------------------------


# -----------------------------------------------------------------------
# Endpoints
# -----------------------------------------------------------------------


@app.get("/health")
def health():
    """Liveness check — returns ok plus the current server timestamp."""
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.post("/graph/ingest")
def graph_ingest(req: GraphIngestRequest):
    """
    AppWorks Lifecycle-event entry point: ingest one or more cases into
    Neo4j and run the full rule pipeline over them.

    AppWorks will call this on a case lifecycle event once that event is
    wired up. Until then the identical path is reachable from the CLI
    (python -m etl.run_sync), which calls the same service function — there
    is no second, manual-only implementation to drift.

    Deliberately NOT an agent route: no LLM, no prompt, no dispatcher. The
    dispatcher's three gates exist to validate tool calls an LLM *proposed*;
    there is no LLM here to propose anything, and routing a deterministic
    backend job through a gate designed for a non-deterministic caller adds
    a hop without adding a check. This is consistent with reasoning_layer/
    pipeline.py's run_pipeline itself, which is never registered in
    manifest.yaml and is never LLM-callable (Section 9.1) — Context
    Enrichment and this ETL path both invoke it as a direct Python call,
    not through the dispatcher. The prior round's PHASE2_STATUS.md flagged
    an assumption that this route went "LLM → dispatcher → pipeline" via a
    manifest-registered run_reasoning_pipeline tool; that assumption was
    wrong and has been corrected — the tool entry has been removed from
    manifest.yaml.

    Synchronous by design at POC scale (18 cases). At production volume this
    is the natural place to hand off to a task queue and return 202 with a
    job id — the service function underneath would not change.
    """
    if not req.case_ids:
        raise HTTPException(status_code=400, detail="case_ids must not be empty")

    try:
        report = run_graph_ingest(
            req.case_ids,
            run_reasoning=req.run_rules,
        )
    except GraphUnavailableError as exc:
        # No fallback graph exists — unlike a Postgres outage, this cannot
        # degrade gracefully, so it is a 503, not a silent partial success.
        raise HTTPException(status_code=503, detail=f"Neo4j unavailable: {exc}")
    except Exception as exc:  # noqa: BLE001 — never let an ingest failure masquerade as success
        # Anything the service did not handle itself is a real failure. A
        # 500 with the cause is far more useful than {"status":"ok","report":null},
        # which is what a swallowed error or a mis-edited service produces.
        logger.exception("graph_ingest FAILED for case_ids=%s", req.case_ids)
        raise HTTPException(status_code=500, detail=f"ingest failed: {type(exc).__name__}: {exc}")

    # A well-formed ingest always returns a report dict. If it somehow did
    # not, that is a bug in the service, not a success — surface it rather
    # than returning a null report under an "ok" status.
    if report is None:
        raise HTTPException(
            status_code=500,
            detail="ingest returned no report — this indicates a bug in etl.ingest_service.ingest()",
        )

    return {"status": "ok", "report": report}


@app.get("/graph/ingest/status")
def graph_ingest_status():
    """What is actually in the graph right now, and did the last sync of
    each case succeed. Reads graph_ingest_state (PostgreSQL) — no Neo4j
    call, no LLM. This is the endpoint that answers "why does this case
    show an empty network" without anyone reading server logs."""
    return {"cases": graph_ingest_repository.list_states()}


@app.post("/intake")
def intake(req: intakeRequest):
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
        # other (an investigator reject/revert). staleness.should_refresh
        # widens the cache-hit check below from reload_ai_summary alone
        # to "either trigger fired"; staleness.should_rerun_full_pipeline
        # narrows back down to core_data-only for the one thing actually
        # worth re-running from scratch for — see
        # core.narrative_staleness.StalenessCheck.should_rerun_full_pipeline's
        # docstring for why a graph-only change never warrants it.
        staleness = evaluate_cache_staleness(req.case_id, req.reload_ai_summary)

        # Agent-summary cache: if intake has already produced and
        # persisted an agent_summary for this case_id, answer from
        # case_ai_summary_store / warm CASE_STORE WITHOUT calling the LLM
        # again. Either staleness trigger bypasses this lookup and falls
        # through to a fresh agent run below (a graph-only trigger still
        # regenerates the narrative — see run_intake_direct_pipeline's
        # force argument further down for what it does NOT also do).
        if not staleness.should_refresh:
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
                            # AI-32: always None on this branch — it is
                            # only reached when staleness.should_refresh
                            # is False, i.e. neither trigger fired.
                            "stale": staleness.stale,
                            "stale_reason": staleness.stale_reason,
                            "generated_at": get_route_generated_at(cached_case_data, "intake"),
                        },
                    },
                }

        if not os.getenv("OPENAI_API_KEY"):
            raise HTTPException(status_code=500, detail="OPENAI_API_KEY not configured")

        runner = _get_runner()
        # Scope to intake + enrichment only; similar cases is a separate route.

        messages, provenance_trail, _ = runner.run_scoped(
            system_prompt=build_intake_system_prompt(),
            user_message=(f"intake case {req.case_id}."),
            scope="CASE_SUMMARY",  # ← this scope includes intake + enrichment tools only;
        )
        sections = extract_tool_results(messages, runner.dispatcher.tool_to_section)

        # Direct, non-LLM network-match + context-enrichment pipeline work
        # (Section 8.1 AI-12, Section 9.1 AI-13) — factored out to
        # api/pipeline_execution.py. subject_primary_id was injected into
        # complaint_intelligence by extract_tool_results above.
        #
        # AI-32: staleness.should_rerun_full_pipeline, NOT
        # req.reload_ai_summary directly, now controls the Wave 1/2
        # force-rerun inside run_intake_direct_pipeline (it gates
        # enrich_graph_context's own `force` argument) — True only for a
        # core_data-driven refresh. A graph-only refresh still reaches
        # this call (staleness.should_refresh already got it past the
        # cache-hit check above, which is what makes the LLM run again at
        # all and therefore what regenerates the narrative), but passes
        # False here, so Wave 1/2 is left exactly as an investigator's
        # reject/revert already left it in Neo4j — nothing about AppWorks
        # structural data changed, so nothing there needs recomputing.
        sections, provenance_trail = run_intake_direct_pipeline(
            req.case_id,
            staleness.should_rerun_full_pipeline,
            sections,
            provenance_trail,
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
        )
        intake_generated_at = get_route_generated_at(sections, "intake")

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
                    "stale_reason": staleness.stale_reason,
                    "generated_at": intake_generated_at,
                },
            },
        }
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
        )
        raise HTTPException(status_code=500, detail=f"Investigation failed: {exc}") from exc
    finally:
        logger.info("POST /intake completed for case_id=%s", req.case_id)


@app.post("/similar_cases")
def similar_cases(req: SimilarCasesRequest):
    """
    ON-DEMAND — Similar Cases Route (Step 2 in flow).
    Calls search_similar_cases to find historical cases with matching fraud patterns.
    Requires case_data from a prior /intake run (via CS-4 or ai_summary body).
    Explains historical case matches, pattern relevance, and archive findings.
    Flow: /intake → /similar_cases → /risk_assessment → /plan → /copilot |
    """
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
            run_prerequisite_route=lambda: intake(intakeRequest(case_id=req.case_id)),
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
        # core.narrative_staleness's module docstring. /similar_cases has
        # no separate "pipeline" stage of its own (find_structural_matches
        # is already a live, always-fresh Neo4j read regardless of cache
        # state — see fetch_live_similar_cases below), so unlike /intake
        # there is nothing to selectively skip: should_refresh alone is
        # exactly "regenerate the narrative", which is the entirety of
        # what a refresh means for this route either way.
        staleness = evaluate_cache_staleness(req.case_id, req.reload_ai_summary)

        # Agent-summary cache: if similar_cases has already produced and
        # persisted an agent_summary for this case_id, answer from it
        # WITHOUT calling the LLM again. Either staleness trigger bypasses
        # this lookup and falls through to a fresh agent run below.
        if not staleness.should_refresh:
            cached_summary = get_route_summary_text(case_data, "similar_cases")
            if cached_summary is not None:
                # similar_cases is ALWAYS a live Neo4j read, cache hit or
                # not — case_data never carries the structural matches
                # section at all (it is entirely graph-derived; see
                # core.persistence_filters.strip_graph_derived_fields), so
                # this tab reflects the graph as it stands right now.
                live_similar_cases = fetch_live_similar_cases(req.case_id)
                duration_seconds = round(time.time() - start, 1)
                logger.info(
                    "similar_cases CACHE HIT for case_id=%s — answering from "
                    "case_ai_summary_store, no LLM call made",
                    req.case_id,
                )
                log_agent_call(
                    case_id=req.case_id,
                    agent_name="similar_cases",
                    endpoint="/similar_cases",
                    latency_ms=int(duration_seconds * 1000),
                    status="success",
                )
                return {
                    "case_id": req.case_id,
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
                            "stale_reason": staleness.stale_reason,
                            "generated_at": get_route_generated_at(case_data, "similar_cases"),
                        },
                    },
                }

        runner = _get_runner()

        # Direct structural-matching + LLM-explain pipeline work
        # (Section 8.3 AI-14, Section 9.2) — factored out to
        # api/pipeline_execution.py.
        agent_summary, similar_cases_data, similar_section, merged_provenance = run_similar_cases_pipeline(
            req.case_id,
            case_data,
            runner,
            build_similar_cases_prompt,
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
        )
        similar_cases_generated_at = get_route_generated_at(ai_summary["investigation"], "similar_cases")
        CASE_STORE[req.case_id][AGENT_SUMMARY_CACHE_KEY] = ai_summary["investigation"][
            AGENT_SUMMARY_CACHE_KEY
        ]
        persist_case_session(req.case_id, ai_summary)
        log_agent_call(
            case_id=req.case_id,
            agent_name="similar_cases",
            endpoint="/similar_cases",
            latency_ms=int((time.time() - start) * 1000),
            status="success",
        )

        logger.info(f"SIMILAR CASES NARRATIVE TOTAL KEYs: {len(similar_cases_data)}")
        return {
            "case_id": req.case_id,
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
                    "stale_reason": staleness.stale_reason,
                    "generated_at": similar_cases_generated_at,
                },
            },
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Similar cases route failed for case_id=%s", req.case_id)
        log_agent_call(
            case_id=req.case_id,
            agent_name="similar_cases",
            endpoint="/similar_cases",
            latency_ms=int((time.time() - start) * 1000),
            status="error",
        )
        raise HTTPException(status_code=500, detail=f"Similar cases analysis failed: {exc}") from exc
    finally:
        logger.info("POST /similar_cases completed for case_id=%s", req.case_id)


@app.post("/risk_assessment")
def risk_assessment(req: RiskAssessmentRequest):
    """
    ON-DEMAND — Risk Assessment Route (Step 3 in flow).
    Calls get_risk_rules and calculate_risk_metrics.
    Requires case_data from a prior /intake + /similar_cases run
    (via CS-4 or ai_summary body).
    Explains case seriousness, triggered rules, and escalation thresholds.
    Flow: /intake → /similar_cases → /risk_assessment → /plan → /copilot |
    """
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
            run_prerequisite_route=lambda: similar_cases(SimilarCasesRequest(case_id=req.case_id)),
            resolve_case_store=_resolve_case_store,
            route_name="risk_assessment",
        )
        logger.info("case_id=%s data_source=%s", req.case_id, data_source)

        # AI-32: the two independent staleness triggers, combined — see
        # core.narrative_staleness's module docstring. Like
        # /similar_cases, /risk_assessment has no separate "pipeline"
        # stage of its own (neo4j_signals/risk_score/risk_tier are
        # already a live, always-fresh recompute regardless of cache
        # state — see fetch_live_risk_signals below), so should_refresh
        # alone is exactly "regenerate the narrative".
        staleness = evaluate_cache_staleness(req.case_id, req.reload_ai_summary)

        # Agent-summary cache: if risk_assessment has already produced
        # and persisted an agent_summary for this case_id, answer from it
        # WITHOUT calling the LLM again. Either staleness trigger bypasses
        # this lookup and falls through to a fresh agent run below.
        if not staleness.should_refresh:
            cached_summary = get_route_summary_text(case_data, "risk_assessment")
            cached_risk_assessment = case_data.get("risk_assessment")
            if (
                cached_summary is not None
                and isinstance(cached_risk_assessment, dict)
                and "risk_score" in cached_risk_assessment
            ):
                # neo4j_signals / risk_score / risk_tier are ALWAYS
                # recomputed live from Neo4j, cache hit or not.
                # cached_risk_assessment.risk_score/risk_tier are the
                # untouched AppWorks BASE values here — Postgres/CS-4
                # persist base_risk_score/base_risk_tier under those plain
                # keys precisely so there is a clean starting point to
                # re-augment from (see
                # core.persistence_filters.strip_graph_derived_fields).
                # base_risk_score/base_risk_tier are preferred when
                # present (a warm same-process CASE_STORE entry from the
                # LLM run that just populated it still carries the real
                # augmented dict, with both keys).
                subject_id = (case_data.get("complaint_intelligence") or {}).get("subject_primary_id")
                base_risk_score = cached_risk_assessment.get(
                    "base_risk_score", cached_risk_assessment.get("risk_score")
                )
                base_risk_tier = cached_risk_assessment.get(
                    "base_risk_tier", cached_risk_assessment.get("risk_tier")
                )
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
                log_agent_call(
                    case_id=req.case_id,
                    agent_name="risk_assessment",
                    endpoint="/risk_assessment",
                    latency_ms=int(duration_seconds * 1000),
                    status="success",
                )
                return {
                    "case_id": req.case_id,
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
                            "stale_reason": staleness.stale_reason,
                            "generated_at": get_route_generated_at(case_data, "risk_assessment"),
                        },
                    },
                }

        runner = _get_runner()

        # --- EXPLICIT DEPENDENCY INJECTION ---
        # We package the backend state into a generic execution_context
        execution_context = {"ai_summary": req.ai_summary}
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
        )
        risk_assessment_generated_at = get_route_generated_at(ai_summary["investigation"], "risk_assessment")
        CASE_STORE[req.case_id][AGENT_SUMMARY_CACHE_KEY] = ai_summary["investigation"][
            AGENT_SUMMARY_CACHE_KEY
        ]
        persist_case_session(req.case_id, ai_summary)
        log_agent_call(
            case_id=req.case_id,
            agent_name="risk_assessment",
            endpoint="/risk_assessment",
            latency_ms=int((time.time() - start) * 1000),
            status="success",
        )

        return {
            "case_id": req.case_id,
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
                    "stale_reason": staleness.stale_reason,
                    "generated_at": risk_assessment_generated_at,
                },
            },
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Risk assessment route failed for case_id=%s", req.case_id)
        log_agent_call(
            case_id=req.case_id,
            agent_name="risk_assessment",
            endpoint="/risk_assessment",
            latency_ms=int((time.time() - start) * 1000),
            status="error",
        )
        raise HTTPException(status_code=500, detail=f"Risk assessment failed: {exc}") from exc
    finally:
        logger.info("POST /risk_assessment completed for case_id=%s", req.case_id)


@app.post("/plan")
def plan(req: PlanRequest):
    """
    ON-DEMAND — Plan Route (Step 4 in flow).
    Calls get_investigation_plan only.
    Requires risk_tier from prior /risk_assessment run (via CS-4 or ai_summary body).
    ai_summary is REQUIRED per v6 spec — server decides which source to use.
    Flow: /intake → /similar_cases → /risk_assessment → /plan → /copilot |
    """
    start = time.time()
    try:

        # CS-4 pattern: warm lookup -> Postgres fallback -> ai_summary body.
        # AI-33: an investigator can open Plan without ever having opened
        # Risk Assessment. Auto-run /risk_assessment's own logic internally
        # the moment risk_assessment (that route's output) isn't already
        # resolvable, instead of the hard 400 this used to raise. Chains
        # backward one hop at a time: /risk_assessment's own entry check
        # (similar_cases) fires /similar_cases if that is missing too, and
        # /similar_cases' own entry check fires /intake if THAT is
        # missing — this route only needs to know about the one route
        # immediately before it.
        case_data, data_source = resolve_prerequisite_case_data(
            req.case_id,
            req.ai_summary,
            required_field="risk_assessment",
            run_prerequisite_route=lambda: risk_assessment(RiskAssessmentRequest(case_id=req.case_id)),
            resolve_case_store=_resolve_case_store,
            route_name="plan",
        )
        logger.info("case_id=%s data_source=%s", req.case_id, data_source)

        # AI-35: /plan's OWN per-tab save-time (AI-34), read from the
        # already-resolved case_data BEFORE this route's own
        # merge_agent_summary_cache/persist_case_session calls below
        # overwrite this exact "plan" cache entry — reading it late would
        # make every override look stale (Section E.5), and would make
        # the AI-32 graph check below compare against the run that is
        # about to happen instead of the one actually cached.
        #
        # This replaces the old case-wide case_ai_summary_store.updated_at
        # column (shared by every tab — /intake, /similar_cases,
        # /risk_assessment, /plan all touch it) for BOTH of /plan's
        # staleness checks: the AI-32 graph check immediately below, and
        # compute_plan_staleness's manual-edit check further down. Reading
        # the shared column here made /plan look "fresh" whenever ANY
        # other tab refreshed, even though nothing about /plan's own
        # narrative or override had changed — after this ticket, nothing
        # in /plan reads that shared column anymore.
        plan_generated_at_before_call = get_route_generated_at_datetime(case_data, "plan")

        # AI-32: the two independent staleness triggers, combined — see
        # core.narrative_staleness's module docstring. Reuses the SAME
        # plan_generated_at_before_call read just above (rather than a
        # second lookup) so plan_stale's own comparison and this one are
        # guaranteed to agree on which snapshot of /plan's own
        # generated_at they judged staleness against.
        # Like /similar_cases and /risk_assessment, /plan has no separate
        # "pipeline" stage of its own (rule_aware_tasks/rules_fired are
        # already a live, always-fresh Neo4j read regardless of cache
        # state — see fetch_live_rule_aware_tasks/prepare_plan_context),
        # so should_refresh alone is exactly "regenerate the narrative".
        staleness = evaluate_cache_staleness(
            req.case_id, req.reload_ai_summary, cache_generated_at=plan_generated_at_before_call
        )

        # Agent-summary cache: if /plan has already produced and persisted
        # an agent_summary for this case_id, answer from it WITHOUT calling
        # the LLM again.
        #
        # The investigation_plan_override (Section D.6) is checked FIRST,
        # regardless of staleness: once an investigator has modified the
        # plan, that modification is authoritative, so a cache hit is
        # served even when a refresh was otherwise indicated — a fresh LLM
        # run would only be discarded in favour of override["modified_steps"]
        # anyway (see run_plan_pipeline below), so skipping straight to it
        # here saves the wasted LLM call. A staleness trigger only bypasses
        # the cache when NO override exists; it still falls through to a
        # fresh agent run below in that case, same as before.
        override = get_override(req.case_id)
        if not staleness.should_refresh or override is not None:
            cached_summary = get_route_summary_text(case_data, "plan")
            cached_plan = case_data.get("investigation_plan")
            if cached_summary is not None and isinstance(cached_plan, dict):
                if override is not None:
                    cached_plan = {**cached_plan, "investigation_steps": override["modified_steps"]}
                    plan_source, modified_by, modified_on = (
                        "User Modified",
                        override["modified_by"],
                        override["modified_on"],
                    )
                    plan_stale = compute_plan_staleness(plan_generated_at_before_call, modified_on)
                    # The structured investigation_plan above now carries the
                    # override, but cached_summary is still the pre-override
                    # LLM markdown pulled straight from case_ai_summary_store —
                    # without this, the investigator reads AI-generated steps
                    # while graph_findings/meta already say User Modified.
                    cached_summary = apply_step_override_to_summary(
                        cached_summary,
                        override["modified_steps"],
                    )
                else:
                    plan_source, modified_by, modified_on, plan_stale = "AI Summerized", None, None, False

                duration_seconds = round(time.time() - start, 1)
                logger.info(
                    "plan CACHE HIT for case_id=%s — answering from "
                    "case_ai_summary_store, no LLM call made",
                    req.case_id,
                )
                log_agent_call(
                    case_id=req.case_id,
                    agent_name="investigation_plan",
                    endpoint="/plan",
                    latency_ms=int(duration_seconds * 1000),
                    status="success",
                )
                # rule_aware_tasks / rules_fired are ALWAYS a live Neo4j
                # read, cache hit or not — cached_plan never carries
                # rule_aware_tasks and case_data never carries rules_fired
                # at all (see core.persistence_filters.strip_graph_derived_fields),
                # so a rejected inference removes the task it justified
                # immediately instead of waiting for a full case reload.
                subject_id = (case_data.get("complaint_intelligence") or {}).get("subject_primary_id")
                live_findings = fetch_live_graph_findings(req.case_id, subject_id)
                live_rule_aware_tasks = build_rule_aware_tasks(
                    live_findings.get("rules_fired", []),
                    live_findings.get("graph_context") or {},
                )
                return {
                    "case_id": req.case_id,
                    "status": "completed",
                    "details": {
                        "agent_summary": cached_summary,
                        "provenance_trail": format_provenance_lines(case_data.get("provenance_trail", [])),
                        "graph_findings": {
                            "rule_aware_tasks": live_rule_aware_tasks,
                            "rules_fired": fired_rules_only(live_findings.get("rules_fired")),
                        },
                        "meta": {
                            "data_source": data_source,
                            "plan_source": plan_source,
                            "modified_by": modified_by,
                            "modified_on": modified_on.isoformat() if modified_on else None,
                            "plan_stale": plan_stale,
                            "agent_summary_source": "db_cache",
                            # AI-32: independent of plan_stale above —
                            # plan_stale is specifically about a saved
                            # HUMAN OVERRIDE lagging behind case data
                            # (Section E.5); stale_reason is about the
                            # underlying AI-GENERATED narrative itself.
                            # Both can be true/non-null at once and mean
                            # different things.
                            "stale": staleness.stale,
                            "stale_reason": staleness.stale_reason,
                            "generated_at": get_route_generated_at(case_data, "plan"),
                        },
                    },
                }

        execution_context = {"ai_summary": req.ai_summary}
        runner = _get_runner()
        # Scope to plan retrieval only (Step 4)

        # AI-16 (Section 8.5): build the rule-aware task recommendations from
        # the rules_fired already in context and hand them to the prompt, so
        # the agent selects investigation steps from both the rule-derived
        # tasks and the BSI catalogue tasks its scoped tool returns.
        case_data_for_prompt, rule_aware_tasks = prepare_plan_context(req.case_id, case_data)

        messages, new_provenance, _ = runner.run_scoped(
            system_prompt=build_plan_prompt(case_data_for_prompt),
            user_message=(
                f"Review the investigation context for case {req.case_id} and execute the "
                "appropriate on-demand tools to assemble the investigation plan."
            ),
            scope="INVESTIGATION_PLAN",  # ← this scope includes intake + enrichment tools only
            execution_context=execution_context,
        )

        sections = extract_tool_results(messages, runner.dispatcher.tool_to_section)

        # Parse/validate the LLM's plan output and apply any human override
        # (Section D.6) — factored out to api/pipeline_execution.py.
        (
            assistant_text,
            investigation_plan,
            plan_section,
            merged_provenance,
            plan_source,
            modified_by,
            modified_on,
            plan_stale,
        ) = run_plan_pipeline(
            req.case_id,
            case_data,
            sections,
            messages,
            new_provenance,
            plan_generated_at_before_call,
            rule_aware_tasks,
        )

        # Update CS-4 warm store but return only the route-specific section.
        # plan_section/investigation_plan here are the PURE, un-overridden
        # LLM output (see run_plan_pipeline) — this is intentional; the
        # warm store and Postgres must only ever hold the AI baseline so
        # POST /plan/revert_to_ai has something real to revert to.
        CASE_STORE[req.case_id].update(plan_section)
        CASE_STORE[req.case_id]["provenance_trail"] = merged_provenance

        # ai_summary: updated contract — investigation sections separate from plan.
        # Persisted server-side (Postgres case_ai_summary_store); /copilot falls
        # back to it via CS-4 resolution rather than receiving it directly.
        ai_summary = build_ai_summary(
            case_data,
            {"investigation_plan": investigation_plan},
            merged_provenance,
        )
        # Cache this run's RESOLVED agent_summary markdown — the LLM's own
        # markdown, PURE, with no override spliced in. This is what makes
        # POST /plan/revert_to_ai correct: the cached "AI" text must never
        # be the override's own text, or reverting has nothing genuine to
        # fall back to. The override is spliced into a SEPARATE
        # response-only copy below (response_agent_summary) — never into
        # what gets cached here. Carries forward any other route's
        # already-cached entry for this case_id.
        resolved_agent_summary = resolve_plan_agent_summary(
            assistant_text,
            investigation_plan,
            req.case_id,
            case_data,
            merged_provenance,
        )
        ai_summary["investigation"][AGENT_SUMMARY_CACHE_KEY] = merge_agent_summary_cache(
            case_data,
            "plan",
            resolved_agent_summary,
        )
        plan_generated_at = get_route_generated_at(ai_summary["investigation"], "plan")
        CASE_STORE[req.case_id][AGENT_SUMMARY_CACHE_KEY] = ai_summary["investigation"][
            AGENT_SUMMARY_CACHE_KEY
        ]
        persist_case_session(req.case_id, ai_summary)

        # Response-only override splice — mirrors the /plan CACHE-HIT
        # branch above exactly: the persisted/cached text stays the pure
        # AI baseline (just written above); only what THIS response
        # returns to the caller reflects the override, the same way a
        # cache hit never mutates cached_summary/cached_plan before
        # persisting anything.
        response_agent_summary = resolved_agent_summary
        if plan_source == "User Modified":
            response_agent_summary = apply_step_override_to_summary(
                resolved_agent_summary,
                override["modified_steps"] if override is not None else None,
            )
        log_agent_call(
            case_id=req.case_id,
            agent_name="investigation_plan",
            endpoint="/plan",
            latency_ms=int((time.time() - start) * 1000),
            status="success",
        )

        # rules_fired for the response is a live Neo4j read, same as the
        # rule_aware_tasks above — case_data (resolved before this
        # request) never carries rules_fired at all (see
        # core.persistence_filters.strip_graph_derived_fields).
        plan_subject_id = (case_data.get("complaint_intelligence") or {}).get("subject_primary_id")
        live_plan_rules_fired = fetch_live_graph_findings(req.case_id, plan_subject_id).get(
            "rules_fired", []
        )

        return {
            "case_id": req.case_id,
            "status": "completed",
            "details": {
                "agent_summary": response_agent_summary,
                "provenance_trail": format_provenance_lines(merged_provenance),
                # Graph-derived plan output (AI-16 —
                # reasoning_layer.investigation_tasks.build_rule_aware_tasks):
                # the rule-aware task recommendations and the rules_fired
                # block they were derived from. Section 8.5 requires these to
                # be displayed SEPARATELY from the generic steps, which the UI
                # can only do if it receives them as data — the rendered
                # agent_summary alone cannot be split reliably. catalog_tasks
                # is deliberately not here: it is the AppWorks task catalogue,
                # not a graph finding, and it already travels on the plan.
                "graph_findings": {
                    "rule_aware_tasks": investigation_plan.get("rule_aware_tasks"),
                    "rules_fired": fired_rules_only(live_plan_rules_fired),
                },
                "meta": {
                    "data_source": data_source,
                    "plan_source": plan_source,
                    "modified_by": modified_by,
                    "modified_on": modified_on.isoformat() if modified_on else None,
                    "plan_stale": plan_stale,
                    "agent_summary_source": "llm",
                    "stale": staleness.stale,
                    "stale_reason": staleness.stale_reason,
                    "generated_at": plan_generated_at,
                },
            },
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Plan route failed for case_id=%s", req.case_id)
        log_agent_call(
            case_id=req.case_id,
            agent_name="investigation_plan",
            endpoint="/plan",
            latency_ms=int((time.time() - start) * 1000),
            status="error",
        )
        raise HTTPException(status_code=500, detail=f"Plan generation failed: {exc}") from exc
    finally:
        logger.info("POST /plan completed for case_id=%s", req.case_id)


@app.post("/plan/modify_investigation_steps", response_model=ModifyInvestigationStepsResponse)
def modify_investigation_steps(req: ModifyInvestigationStepsRequest) -> ModifyInvestigationStepsResponse:
    """
    Investigator saves an edited investigation_steps list from the
    Investigation Plan "Modify" popup (Data Persistence Spec v1.0,
    Section D.6; Modify Investigation Steps flow).

    Persists the edit to investigation_plan_overrides — durable,
    attributable, one row per case_id, a new save overwriting the
    prior one. Every later /plan or /copilot call for this case_id
    looks this row up server-side and applies it, regardless of which
    client calls those endpoints or what they pass in their own
    request body.
    """
    start = time.time()
    try:
        modified_on = upsert_override(
            case_id=req.case_id,
            modified_steps=[step.model_dump(exclude_none=True) for step in req.steps],
            modified_by=req.investigator_id,
            comment=req.comment,
        )
        log_agent_call(
            case_id=req.case_id,
            agent_name="investigation_plan_override",
            endpoint="/plan/modify_investigation_steps",
            latency_ms=int((time.time() - start) * 1000),
            status="success",
        )
        return ModifyInvestigationStepsResponse(
            case_id=req.case_id,
            status="saved",
            plan_source="User Modified",
            modified_by=req.investigator_id,
            modified_on=modified_on,
        )
    except (psycopg2.Error, DatabaseUnavailableError) as exc:
        logger.exception(
            "modify_investigation_steps FAILED to save for case_id=%s",
            req.case_id,
        )
        log_agent_call(
            case_id=req.case_id,
            agent_name="investigation_plan_override",
            endpoint="/plan/modify_investigation_steps",
            latency_ms=int((time.time() - start) * 1000),
            status="error",
        )
        raise HTTPException(
            status_code=502,
            detail=f"Could not save the modified investigation steps: {exc}",
        ) from exc
    finally:
        logger.info(
            "POST /plan/modify_investigation_steps completed for case_id=%s",
            req.case_id,
        )


@app.post("/plan/revert_to_ai", response_model=RevertToAiPlanResponse)
def revert_to_ai_plan(req: RevertToAiPlanRequest) -> RevertToAiPlanResponse:
    """
    Investigator clicks "Revert to AI Plan" — deletes case_id's saved
    investigation_plan_overrides row. The next /plan or /copilot call
    for this case_id finds no override row and falls back to
    plan_source: AI Summerized.
    """
    start = time.time()
    try:
        existed = delete_override(req.case_id)
        log_agent_call(
            case_id=req.case_id,
            agent_name="investigation_plan_override",
            endpoint="/plan/revert_to_ai",
            latency_ms=int((time.time() - start) * 1000),
            status="success",
        )
        return RevertToAiPlanResponse(
            case_id=req.case_id,
            status="reverted" if existed else "no_override_existed",
            plan_source="AI Summerized",
        )
    except (psycopg2.Error, DatabaseUnavailableError) as exc:
        logger.exception(
            "revert_to_ai_plan FAILED for case_id=%s",
            req.case_id,
        )
        log_agent_call(
            case_id=req.case_id,
            agent_name="investigation_plan_override",
            endpoint="/plan/revert_to_ai",
            latency_ms=int((time.time() - start) * 1000),
            status="error",
        )
        raise HTTPException(
            status_code=502,
            detail=f"Could not revert case {req.case_id} to the AI-generated plan: {exc}",
        ) from exc
    finally:
        logger.info("POST /plan/revert_to_ai completed for case_id=%s", req.case_id)


@app.get(
    "/plan/modify_investigation_steps/{case_id}",
    response_model=InvestigationStepsResponse,
    response_model_exclude_none=True,
)
def get_investigation_steps(case_id: str) -> InvestigationStepsResponse:
    """
    ON-DEMAND — read-only fetch of the current investigation_steps for
    case_id.

    Same base path as POST /plan/modify_investigation_steps since these
    are matched as (method, path) pairs, not by path alone — the POST
    (exact) and this parameterized GET never collide, same as GET
    /copilot/{case_id} alongside POST /copilot.

    Single field, single source at a time — investigation_steps is
    never split across two parallel fields with one left null.
    is_modify_investigation_steps carries which table it came from:

    1. True  — investigation_plan_overrides. The investigator's saved
       edit, if one exists for case_id. Always checked first, and
       always the current, attributable fact when present.
    2. False — case_ai_summary_store.ai_summary.investigation_plan. The
       last AI-generated (or previously-overridden-and-cached) plan,
       used only when no override exists.

    Read-only: no LLM, no dispatcher, no CASE_STORE write — the same
    class of endpoint as GET /copilot/{case_id}.
    """
    override = get_override(case_id)
    if override is not None:
        investigation_steps = override["modified_steps"]
        logger.info(
            "GET /plan/modify_investigation_steps case_id=%s source=override steps=%d",
            case_id,
            len(investigation_steps),
        )
        return InvestigationStepsResponse(
            case_id=case_id,
            investigation_steps=investigation_steps,
            is_modify_investigation_steps=True,
        )

    try:
        investigation_steps = get_cached_investigation_steps(case_id)
    except Exception as exc:
        logger.exception("investigation_steps lookup FAILED for case_id=%s", case_id)
        raise HTTPException(
            status_code=500,
            detail=f"Investigation steps lookup failed: {exc}",
        ) from exc

    if investigation_steps is None:
        raise HTTPException(
            status_code=404,
            detail=f"No cached case data found for case_id={case_id}. Call /plan first.",
        )

    logger.info(
        "GET /plan/modify_investigation_steps case_id=%s source=case_ai_summary_store steps=%d",
        case_id,
        len(investigation_steps),
    )
    return InvestigationStepsResponse(
        case_id=case_id,
        investigation_steps=investigation_steps,
        is_modify_investigation_steps=False,
    )


@app.post("/generate_report")
def generate_report(req: ReportGenerationRequest):
    """
    ON-DEMAND — Report Generation Route (AI-18, Functional Spec Section
    8.7, Developer Spec Section 7.5). Built last — depends on /intake,
    /similar_cases, /risk_assessment, and /plan already having populated
    CS-4 for this case (AI-13, AI-17).

    Assembles the Related Network section deterministically from Neo4j
    (reasoning_layer.report_generation — every active High/Medium fact
    plus every rejected fact for the Primary Subject, rejected facts
    never silently omitted), combines it with the case narrative already
    on file in CS-4, and has the LLM write the narrative prose ONLY — it
    is never asked to decide which connections belong in the report
    (Section 8.7). The result is persisted to report_artifacts (D.5) as
    a new draft.

    reload_ai_summary=False (default): the Related Network is always
    re-read fresh from Neo4j and the plan override always re-read fresh
    from Postgres (both cheap, non-LLM reads) — if a report already
    exists for this case_id in report_artifacts AND that fresh read is
    identical to what was cached, answer from the latest persisted draft
    with only the Decision & Override Log re-derived, no LLM call. If
    the Related Network has changed since (a rejection, a revert, a
    newly-active connection), OR AI-31's (:Case).last_inference_change_at
    is newer than this draft's own generated_at (AI-32), the cache is
    treated as stale regardless of reload_ai_summary and a fresh report
    is generated. reload_ai_summary=True always skips SERVING from this
    cache and persists a fresh draft row — though the report_artifacts
    lookup itself still runs even then, purely so the response's
    stale_reason can report "both" accurately; see the AI-32 comment
    inline below for why that one extra read is worth its cost here.
    """
    start = time.time()
    try:
        # CS-4 pattern: warm lookup -> Postgres fallback -> ai_summary body.
        case_data, data_source = _resolve_case_store(req.case_id, req.ai_summary)
        logger.info(
            "case_id=%s data_source=%s key_count=%d",
            req.case_id,
            data_source,
            len(list(case_data.keys())),
        )

        runner = _get_runner()

        subject_id = (case_data.get("complaint_intelligence") or {}).get("subject_primary_id")
        if not subject_id:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Report generation requires a resolved primary subject for "
                    f"case_id={req.case_id}. Run /intake first."
                ),
            )

        # --- AI-18: deterministic Related Network assembly (Section 8.7) ---
        # Called DIRECTLY — not an LLM tool, not dispatcher-routed, not in
        # manifest.yaml (same governance as similar_cases/AI-14 and
        # check_network_match/AI-12: manifest holds a tool only if it is
        # LLM-called AND makes an AppWorks call; this is a Neo4j read).
        # The LLM's role is to EXPLAIN this section, never to decide its
        # contents. Non-blocking: a graph outage degrades to an empty,
        # clearly-unavailable section rather than failing the route.
        #
        # Run BEFORE the report cache check below (not just in the fresh-
        # generation path) so a cache hit can compare against the CURRENT
        # graph state — a Neo4j read is cheap relative to the LLM call the
        # cache exists to avoid, so there is no reason to trust a stale
        # snapshot here just because /generate_report has been called
        # before for this case_id.
        try:
            related_envelope = assemble_related_network(req.case_id, subject_id)
            related = related_envelope["result"]
        except (ValueError, GraphUnavailableError, Neo4jError) as exc:
            logger.warning(
                "related-network assembly unavailable for case_id=%s subject_id=%s — %s",
                req.case_id,
                subject_id,
                exc,
            )
            related = {
                "subject_id": subject_id,
                "related_network": [],
                "confidence_summary": {"high": 0, "medium": 0, "unresolved": 0},
                "rejected_count": 0,
                "unavailable_reason": str(exc),
            }
            related_envelope = {
                "result": related,
                "provenance": {
                    "sources": [],
                    "retrieved_at": "",
                    "computed_by": "reasoning_layer.report_generation.assemble_related_network",
                },
            }

        # investigation_plan_overrides (Section D.6) must be reflected
        # "regardless of which endpoint is queried" (see core/
        # investigation_plan_override_repository.py) — /plan and /copilot
        # already re-check it fresh on every call rather than trusting
        # whatever was true at generation time. Same reasoning as the
        # Related Network read above: fetch it fresh here, before the
        # cache check, so a cache hit can be judged against the current
        # override state instead of the one that was true when the
        # cached report was generated.
        try:
            plan_override = get_override(req.case_id)
        except Exception as exc:
            logger.warning(
                "investigation_plan_overrides lookup failed for case_id=%s "
                "during report generation — treating as no override: %s",
                req.case_id,
                exc,
            )
            plan_override = None

        # Report cache: if a report has already been generated and
        # persisted for this case_id AND neither staleness trigger below
        # fired, answer from the latest report_artifacts row WITHOUT
        # calling the LLM again.
        #
        # /generate_report's "graph changed" signal is the OR of TWO
        # independent detectors, deliberately kept BOTH rather than one
        # replacing the other:
        #   1. The existing, finer-grained content diff: is the live
        #      Related Network read above byte-identical to what was
        #      cached? This predates AI-32 and stays authoritative for
        #      the actual cache-serve decision — it catches the exact
        #      set of cases that matter for THIS route (a connection's
        #      status/membership actually differs) and is immune to
        #      false positives from a reject immediately followed by a
        #      revert (net content unchanged, but AI-31's timestamp
        #      would still have moved).
        #   2. AI-32's new (:Case).last_inference_change_at vs this
        #      report's own generated_at — the same coarse signal every
        #      other cached-narrative route now uses. Folded in with OR,
        #      never replacing #1: a defense-in-depth signal, not a
        #      downgrade of the existing precision. Practically this
        #      only ever fires ahead of #1 in the reject-then-revert case
        #      above, where reporting stale_reason="graph" even though
        #      the content is provably unchanged is the honest answer —
        #      an investigator DID touch the graph — while this route
        #      still correctly serves the (genuinely still-accurate)
        #      cached content either way.
        # Any actual difference means the cached narrative prose
        # (Reviewed and Excluded Connections, Network Connections) can no
        # longer be trusted — that text was written by the LLM once, at
        # generation time, and there is no safe way to splice a per-
        # connection narrative sentence the way the deterministic Decision
        # & Override Log block below can be — so a real change falls
        # through to the full regeneration path and gets a fresh LLM
        # narrative, exactly as if reload_ai_summary=True had been passed.
        #
        # core_data_changed (reload_ai_summary=True) always skips SERVING
        # from this cache — a forced reload must produce fresh prose. The
        # lookup itself still runs either way (a cheap indexed Postgres
        # read next to the LLM call this cache exists to avoid), purely so
        # stale_reason below can report "both" accurately when core data
        # AND the graph both changed, instead of silently losing the
        # graph signal whenever a caller also happens to pass
        # reload_ai_summary=True.
        core_data_changed = bool(req.reload_ai_summary)
        cached_report = get_latest_report(req.case_id)
        if cached_report is not None:
            cached_content = cached_report.get("content") or {}
            cached_related_network = cached_content.get("related_network", [])
            live_related_network = related.get("related_network", [])
            content_unchanged = live_related_network == cached_related_network

            # cached_report["generated_at"] is report_artifacts' own
            # native Postgres timestamp column for this exact draft
            # (distinct from cached_content["generated_at"], a string
            # baked into the JSON body) — the correct cache_generated_at
            # reference point for THIS report, never
            # case_ai_summary_store.updated_at, which tracks a different
            # cache entirely.
            report_staleness = evaluate_cache_staleness(
                req.case_id,
                reload_ai_summary_requested=req.reload_ai_summary,
                cache_generated_at=cached_report.get("generated_at"),
            )
            graph_changed = (not content_unchanged) or report_staleness.graph_changed
            # StalenessCheck is frozen and takes plain booleans, so
            # folding in content_unchanged alongside AI-31's own
            # timestamp signal is a direct construction rather than a
            # second call into evaluate_cache_staleness — see this
            # block's own comment above for why both detectors matter.
            staleness = StalenessCheck(core_data_changed=core_data_changed, graph_changed=graph_changed)

            if not core_data_changed and content_unchanged:
                cached_rejected_count = sum(
                    1 for entry in cached_related_network if entry.get("status") == "rejected"
                )

                # The Related Network itself is unchanged, but the plan
                # override could still have moved (a plan modification
                # or revert doesn't touch related_network at all) — so
                # the Decision & Override Log is still re-derived fresh
                # here and spliced into the cached narrative rather
                # than trusted from cached_content["decision_log"].
                cached_rejected_connections = [
                    entry for entry in cached_related_network if entry.get("status") == "rejected"
                ]
                decision_log_envelope = build_decision_log(cached_rejected_connections, plan_override)
                decision_log_result = decision_log_envelope["result"]

                # Splice the freshly-rendered section into the cached
                # narrative markdown without re-invoking the LLM — same
                # technique apply_step_override_to_summary already uses
                # to overlay a live override onto /plan's cached prose.
                cached_report_markdown = (cached_content.get("standard_sections") or {}).get(
                    "report_markdown", ""
                )
                resolved_report_markdown = replace_markdown_section(
                    cached_report_markdown,
                    "Decision & Override Log",
                    decision_log_result["decision_log_markdown"],
                )

                duration_seconds = round(time.time() - start, 1)
                logger.info(
                    "generate_report CACHE HIT for case_id=%s — Related Network "
                    "unchanged since last draft, answering from report_artifacts "
                    "with a freshly-derived Decision & Override Log, no LLM call made",
                    req.case_id,
                )
                log_agent_call(
                    case_id=req.case_id,
                    agent_name="report_generation",
                    endpoint="/generate_report",
                    latency_ms=int(duration_seconds * 1000),
                    status="success",
                )
                return {
                    "case_id": req.case_id,
                    "status": "completed",
                    "report_id": cached_content.get("report_id"),
                    "generated_at": cached_content.get("generated_at"),
                    "details": {
                        "agent_summary": resolved_report_markdown,
                        "provenance_trail": format_provenance_lines(cached_report.get("provenance_trail", [])),
                        "related_network": cached_related_network,
                        "confidence_summary": cached_content.get(
                            "confidence_summary", {"high": 0, "medium": 0, "unresolved": 0}
                        ),
                        "rejected_count": cached_rejected_count,
                        "decision_log": decision_log_result.get("decision_log", []),
                        "meta": {
                            "data_source": data_source,
                            "report_status": cached_content.get("status", "draft"),
                            "agent_summary_source": "db_cache",
                            "persisted_to_postgres": True,
                            "stale": staleness.stale,
                            "stale_reason": staleness.stale_reason,
                        },
                    },
                }

            logger.info(
                "generate_report CACHE STALE for case_id=%s stale_reason=%s — "
                "regenerating a fresh report instead of serving report_artifacts",
                req.case_id,
                staleness.stale_reason,
            )
        else:
            # No report has ever been generated for this case_id — a
            # genuine first run, not staleness. graph_changed is reported
            # False here for the same reason every other cached-narrative
            # route treats a missing cache as "nothing to compare, no
            # signal" rather than guessing (see
            # core.narrative_staleness.is_graph_newer's docstring).
            staleness = StalenessCheck(core_data_changed=core_data_changed, graph_changed=False)

        # --- Decision & Override Log assembly (Report Design ACTIONS #3) ---
        # Deterministic, non-LLM formatting over two inputs /generate_report
        # already has in hand: the case's investigation-plan override
        # (fetched fresh above, before the cache check) and the rejected
        # entries out of the Related Network read just above. No graph or
        # DB call of its own — see reasoning_layer/decision_log.py.
        rejected_connections = [
            entry for entry in related.get("related_network", []) if entry.get("status") == "rejected"
        ]
        decision_log_envelope = build_decision_log(rejected_connections, plan_override)
        decision_log_result = decision_log_envelope["result"]

        # rules_fired for the report narrative is a live Neo4j read, same
        # as /intake and /plan (see fetch_live_graph_findings) — case_data
        # (resolved above via CS-4 warm lookup -> Postgres fallback ->
        # ai_summary body) NEVER carries rules_fired at all
        # (core.persistence_filters.strip_graph_derived_fields strips it
        # before every write), so case_data.get("rules_fired") was always
        # None/[] here and "Rules Fired" narrated as empty regardless of
        # what had actually fired in the graph. Reading it fresh means an
        # investigator's reject/revert, or a rule that fired since the
        # last cached draft, is reflected in this report immediately.
        live_report_rules_fired = fetch_live_graph_findings(req.case_id, subject_id).get(
            "rules_fired", []
        )

        # Inject the computed network and decision log into the case
        # context the prompt serialises, so the LLM narrates THESE facts
        # (never adds, removes, or reorders them — REPORT_GENERATION scope
        # carries no tools, so the LLM cannot re-query the graph or
        # Postgres itself either).
        #
        # rules_fired is also trimmed here to fired-only entries — the same
        # response-boundary filter /intake and /plan already apply via
        # fired_rules_only() (see api/response_builders.py: CASE_STORE and
        # the merge keep the full fixed 14-entry block; every reader that
        # displays it to an investigator trims to fired:true first).
        case_data_for_prompt = {
            **case_data,
            "rules_fired": fired_rules_only(live_report_rules_fired),
            "related_network": related.get("related_network", []),
            "confidence_summary": related.get("confidence_summary", {}),
            "rejected_count": related.get("rejected_count", 0),
            "decision_log": decision_log_result.get("decision_log", []),
        }
        # case_data_for_prompt above is the FULL context (superset of what
        # the report needs — kept as-is; nothing downstream that reads
        # case_data_for_prompt itself changes). build_report_llm_context
        # derives a MINIMIZED copy of it for the LLM prompt ONLY: drops
        # agent_summary_cache / provenance_trail / network_match_flag,
        # keeps only the Primary Subject, strips rejection-audit
        # attribution (who/when) from rules_fired instances, trims
        # risk_assessment down to scores, similar_cases down to count +
        # similarity signal, investigation_plan down to steps, and
        # collapses prior-guilty case lists to a count. See
        # reasoning_layer/report_llm_context.py for the full rationale.
        llm_prompt_context = build_report_llm_context(case_data_for_prompt, case_id=req.case_id)

        messages, new_provenance, _ = runner.run_scoped(
            system_prompt=build_report_generation_prompt(llm_prompt_context),
            user_message=(
                f"Compose the investigation report narrative for case {req.case_id} "
                "from the case record already provided. The Related Network and "
                "Reviewed and Excluded Connections sections are already finalized "
                "in related_network — narrate every entry given, in full, without "
                "adding, removing, or reordering any of them."
            ),
            scope="REPORT_GENERATION",
        )

        # The authoritative related_network section is the DETERMINISTIC
        # graph result, not anything the LLM produced — the LLM narrates,
        # it does not decide inclusion (mirrors AI-14's similar_cases pattern).
        sections: dict = {}
        new_provenance = merge_direct_result(
            sections,
            new_provenance,
            "related_network",
            related_envelope,
        )
        # Same treatment for the Decision & Override Log — deterministic
        # Python output, never anything the LLM decided (mirrors the
        # related_network merge immediately above).
        new_provenance = merge_direct_result(
            sections,
            new_provenance,
            "decision_log",
            decision_log_envelope,
        )

        assistant_text = extract_agent_summary(messages)

        merged_provenance = merge_provenance(
            case_data.get("provenance_trail", []),
            new_provenance,
        )

        report_id = f"RPT-{req.case_id}-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        generated_at = datetime.now(timezone.utc).isoformat()
        confidence_summary = related.get("confidence_summary", {"high": 0, "medium": 0, "unresolved": 0})
        report_content = {
            "report_id": report_id,
            "case_id": req.case_id,
            "generated_at": generated_at,
            "status": "draft",
            "standard_sections": {"report_markdown": assistant_text},
            "related_network": related.get("related_network", []),
            "confidence_summary": confidence_summary,
            "decision_log": decision_log_result.get("decision_log", []),
        }

        try:
            validated_report = GeneratedReportContract(**report_content)
            report_content = validated_report.model_dump(exclude_none=True)
        except Exception as e:
            logger.warning(
                f"Generated report schema validation failed for case_id={req.case_id} "
                f"— storing unvalidated: {e}"
            )

        # D.5: report_artifacts is a working/draft copy only — never the
        # authoritative one (the AppWorks-saved report is). A write
        # failure here must not fail this investigator-facing response;
        # Neo4j + CS-4 already produced the authoritative content above.
        persisted = save_report(req.case_id, report_content, status="draft")

        log_agent_call(
            case_id=req.case_id,
            agent_name="report_generation",
            endpoint="/generate_report",
            latency_ms=int((time.time() - start) * 1000),
            status="success",
        )

        return {
            "case_id": req.case_id,
            "status": "completed",
            "report_id": report_id,
            "generated_at": generated_at,
            "details": {
                "agent_summary": assistant_text,
                "provenance_trail": format_provenance_lines(merged_provenance),
                "related_network": related.get("related_network", []),
                "confidence_summary": confidence_summary,
                "rejected_count": related.get("rejected_count", 0),
                "decision_log": decision_log_result.get("decision_log", []),
                "meta": {
                    "data_source": data_source,
                    "report_status": "draft",
                    "agent_summary_source": "llm",
                    "persisted_to_postgres": persisted is not None,
                    "stale": staleness.stale,
                    "stale_reason": staleness.stale_reason,
                },
            },
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Report generation route failed for case_id=%s", req.case_id)
        log_agent_call(
            case_id=req.case_id,
            agent_name="report_generation",
            endpoint="/generate_report",
            latency_ms=int((time.time() - start) * 1000),
            status="error",
        )
        raise HTTPException(status_code=500, detail=f"Report generation failed: {exc}") from exc
    finally:
        logger.info("POST /generate_report completed for case_id=%s", req.case_id)


@app.post("/generate_report/pdf")
def generate_report_pdf(req: ReportGenerationRequest):
    """
    ON-DEMAND — Report PDF Export Route (New_REPORT_Design_1.md,
    ACTIONS #6-#7 / TASKS #5-#6). Same request contract as POST
    /generate_report (ReportGenerationRequest: case_id, optional
    ai_summary, optional reload_ai_summary) and the exact same
    case-resolution, Related Network assembly, plan-override read,
    report cache, and Decision & Override Log pipeline — this route is
    a deliberate DUPLICATE of that pipeline, not a wrapper around it, so
    a future change to one route is never silently and implicitly
    inherited by the other. See POST /generate_report above for the
    full rationale behind every step below (cache semantics, the
    fresh-read-before-cache-check ordering, why the Decision & Override
    Log is re-derived even on a cache hit, etc.) — none of that changes
    here.

    The only difference is the last step: instead of returning the
    generated report as JSON, the finished report markdown (the same
    `report_markdown` body /generate_report returns inside
    details.agent_summary, pre-HTML-conversion) is rendered to a
    paginated PDF via utils/report_pdf_renderer.py — a NEW, static,
    non-interactive renderer (NOT utils/html_converter.py, whose
    <details>/<summary> collapsible sections are built for on-screen
    clicking and can end up hidden entirely in a headless print render;
    see New_REPORT_Design_1.md TASKS #5) — and streamed back as a
    downloadable application/pdf response.

    Every draft this route produces or reuses is persisted to
    report_artifacts (D.5) exactly as /generate_report persists it —
    the two routes read and write the same drafts, so calling one after
    the other never triggers a redundant LLM call.
    """
    start = time.time()
    try:
        # CS-4 pattern: warm lookup -> Postgres fallback -> ai_summary body.
        case_data, data_source = _resolve_case_store(req.case_id, req.ai_summary)
        logger.info(
            "case_id=%s data_source=%s key_count=%d",
            req.case_id,
            data_source,
            len(list(case_data.keys())),
        )

        runner = _get_runner()

        subject_id = (case_data.get("complaint_intelligence") or {}).get("subject_primary_id")
        if not subject_id:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Report generation requires a resolved primary subject for "
                    f"case_id={req.case_id}. Run /intake first."
                ),
            )

        # --- AI-18: deterministic Related Network assembly (Section 8.7) ---
        # Identical to /generate_report — see that route for the full
        # rationale on why this runs BEFORE the cache check below.
        try:
            related_envelope = assemble_related_network(req.case_id, subject_id)
            related = related_envelope["result"]
        except (ValueError, GraphUnavailableError, Neo4jError) as exc:
            logger.warning(
                "related-network assembly unavailable for case_id=%s subject_id=%s — %s",
                req.case_id,
                subject_id,
                exc,
            )
            related = {
                "subject_id": subject_id,
                "related_network": [],
                "confidence_summary": {"high": 0, "medium": 0, "unresolved": 0},
                "rejected_count": 0,
                "unavailable_reason": str(exc),
            }
            related_envelope = {
                "result": related,
                "provenance": {
                    "sources": [],
                    "retrieved_at": "",
                    "computed_by": "reasoning_layer.report_generation.assemble_related_network",
                },
            }

        # investigation_plan_overrides (Section D.6) — fetched fresh here,
        # before the cache check, same as /generate_report.
        try:
            plan_override = get_override(req.case_id)
        except Exception as exc:
            logger.warning(
                "investigation_plan_overrides lookup failed for case_id=%s "
                "during report generation — treating as no override: %s",
                req.case_id,
                exc,
            )
            plan_override = None

        # Report cache (reload_ai_summary=False, default) — identical
        # semantics to /generate_report's cache check. On a hit, splice a
        # freshly-derived Decision & Override Log into the cached
        # narrative markdown (no LLM call), then render THAT markdown to
        # PDF instead of returning it as JSON.
        if not req.reload_ai_summary:
            cached_report = get_latest_report(req.case_id)
            if cached_report is not None:
                cached_content = cached_report.get("content") or {}
                cached_related_network = cached_content.get("related_network", [])
                live_related_network = related.get("related_network", [])

                if live_related_network == cached_related_network:
                    cached_rejected_connections = [
                        entry for entry in cached_related_network if entry.get("status") == "rejected"
                    ]
                    decision_log_envelope = build_decision_log(cached_rejected_connections, plan_override)
                    decision_log_result = decision_log_envelope["result"]

                    cached_report_markdown = (cached_content.get("standard_sections") or {}).get(
                        "report_markdown", ""
                    )
                    resolved_report_markdown = replace_markdown_section(
                        cached_report_markdown,
                        "Decision & Override Log",
                        decision_log_result["decision_log_markdown"],
                    )

                    duration_seconds = round(time.time() - start, 1)
                    logger.info(
                        "generate_report/pdf CACHE HIT for case_id=%s — Related Network "
                        "unchanged since last draft, rendering PDF from report_artifacts "
                        "with a freshly-derived Decision & Override Log, no LLM call made",
                        req.case_id,
                    )
                    log_agent_call(
                        case_id=req.case_id,
                        agent_name="report_generation",
                        endpoint="/generate_report/pdf",
                        latency_ms=int(duration_seconds * 1000),
                        status="success",
                    )

                    cached_report_id = cached_content.get("report_id", "")
                    cached_generated_at = cached_content.get("generated_at", "")
                    pdf_bytes = render_report_pdf(
                        resolved_report_markdown,
                        case_id=req.case_id,
                        report_id=cached_report_id,
                        generated_at=cached_generated_at,
                    )
                    filename = report_pdf_filename(req.case_id, cached_report_id)
                    return Response(
                        content=pdf_bytes,
                        media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
                    )

                logger.info(
                    "generate_report/pdf CACHE STALE for case_id=%s — Related Network has "
                    "changed since the last draft (rejection, revert, or new connection); "
                    "regenerating a fresh report instead of serving report_artifacts",
                    req.case_id,
                )

        # --- Decision & Override Log assembly (Report Design ACTIONS #3) ---
        # Identical to /generate_report.
        rejected_connections = [
            entry for entry in related.get("related_network", []) if entry.get("status") == "rejected"
        ]
        decision_log_envelope = build_decision_log(rejected_connections, plan_override)
        decision_log_result = decision_log_envelope["result"]

        # rules_fired for the report narrative is a live Neo4j read,
        # identical to the fix in /generate_report above (and the same
        # pattern /intake and /plan already use via
        # fetch_live_graph_findings) — case_data never carries rules_fired
        # at all (core.persistence_filters.strip_graph_derived_fields), so
        # case_data.get("rules_fired") was always None/[] here too and the
        # rendered PDF's "Rules Fired" section always read "No inference
        # rules fired for this case."
        live_report_rules_fired = fetch_live_graph_findings(req.case_id, subject_id).get(
            "rules_fired", []
        )

        # Same context assembly and prompt as /generate_report — the LLM
        # narrates the Related Network, Reviewed and Excluded Connections,
        # and Decision & Override Log sections; it never decides their
        # contents.
        case_data_for_prompt = {
            **case_data,
            "rules_fired": fired_rules_only(live_report_rules_fired),
            "related_network": related.get("related_network", []),
            "confidence_summary": related.get("confidence_summary", {}),
            "rejected_count": related.get("rejected_count", 0),
            "decision_log": decision_log_result.get("decision_log", []),
        }
        llm_prompt_context = build_report_llm_context(case_data_for_prompt, case_id=req.case_id)

        messages, new_provenance, _ = runner.run_scoped(
            system_prompt=build_report_generation_prompt(llm_prompt_context),
            user_message=(
                f"Compose the investigation report narrative for case {req.case_id} "
                "from the case record already provided. The Related Network and "
                "Reviewed and Excluded Connections sections are already finalized "
                "in related_network — narrate every entry given, in full, without "
                "adding, removing, or reordering any of them."
            ),
            scope="REPORT_GENERATION",
        )

        # The authoritative related_network section is the DETERMINISTIC
        # graph result, not anything the LLM produced.
        sections: dict = {}
        new_provenance = merge_direct_result(
            sections,
            new_provenance,
            "related_network",
            related_envelope,
        )
        new_provenance = merge_direct_result(
            sections,
            new_provenance,
            "decision_log",
            decision_log_envelope,
        )

        assistant_text = extract_agent_summary(messages)

        report_id = f"RPT-{req.case_id}-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        generated_at = datetime.now(timezone.utc).isoformat()
        confidence_summary = related.get("confidence_summary", {"high": 0, "medium": 0, "unresolved": 0})
        report_content = {
            "report_id": report_id,
            "case_id": req.case_id,
            "generated_at": generated_at,
            "status": "draft",
            "standard_sections": {"report_markdown": assistant_text},
            "related_network": related.get("related_network", []),
            "confidence_summary": confidence_summary,
            "decision_log": decision_log_result.get("decision_log", []),
        }

        try:
            validated_report = GeneratedReportContract(**report_content)
            report_content = validated_report.model_dump(exclude_none=True)
        except Exception as e:
            logger.warning(
                f"Generated report schema validation failed for case_id={req.case_id} "
                f"— storing unvalidated: {e}"
            )

        # D.5: report_artifacts is a working/draft copy only — same
        # persistence as /generate_report, so both routes share the same
        # draft history for a given case_id. A write failure here must
        # not fail this investigator-facing PDF download. The PDF route
        # has no JSON body to surface persisted-state metadata in (unlike
        # /generate_report's "persisted_to_postgres"), so the return
        # value is intentionally not captured here.
        save_report(req.case_id, report_content, status="draft")

        log_agent_call(
            case_id=req.case_id,
            agent_name="report_generation",
            endpoint="/generate_report/pdf",
            latency_ms=int((time.time() - start) * 1000),
            status="success",
        )

        pdf_bytes = render_report_pdf(
            assistant_text,
            case_id=req.case_id,
            report_id=report_id,
            generated_at=generated_at,
        )
        filename = report_pdf_filename(req.case_id, report_id)
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Report PDF generation route failed for case_id=%s", req.case_id)
        log_agent_call(
            case_id=req.case_id,
            agent_name="report_generation",
            endpoint="/generate_report/pdf",
            latency_ms=int((time.time() - start) * 1000),
            status="error",
        )
        raise HTTPException(status_code=500, detail=f"Report PDF generation failed: {exc}") from exc
    finally:
        logger.info("POST /generate_report/pdf completed for case_id=%s", req.case_id)


@app.post("/reject_inference", response_model=RejectInferenceResponse)
def reject_inference_route(req: RejectInferenceRequest) -> RejectInferenceResponse:
    """
    D2 — Inference Rejection Handler. An investigator reviews a rule's
    findings on the Rule Audit panel (GET /rule_audit/{case_id}) or the
    Fraud Network screen (GET /fraud_network/{case_id}) and clicks
    "Reject" on ONE specific row/edge; the UI POSTs case_id, rule_id,
    reason, investigator_id, and that row's match_id (or its
    subject_id_a/subject_id_b) — every field is read straight off the
    clicked row, matching the v3 contract in reasoning_layer/rejection.py.

    This rejects exactly the ONE instance identified — never every
    currently-active fact rule_id produced within case_id's reasoning
    scope (that was the v2 bulk contract; see rejection.py's module
    docstring for why it changed, AI-28/AI-33).

    No LLM involvement (D2 Boundaries). Does not touch CASE_STORE or
    investigation_plan_overrides — this is a Neo4j write only, handled
    entirely by reasoning_layer.rejection.reject_inference.
    """
    start = time.time()
    try:
        envelope = reject_inference(
            case_id=req.case_id,
            rule_id=req.rule_id,
            reason=req.reason,
            investigator_id=req.investigator_id,
            match_id=req.match_id,
            subject_id_a=req.subject_id_a,
            subject_id_b=req.subject_id_b,
        )
        log_agent_call(
            case_id=req.case_id,
            agent_name="inference_rejection",
            endpoint="/reject_inference",
            latency_ms=int((time.time() - start) * 1000),
            status="success",
        )
        return RejectInferenceResponse(**envelope["result"])
    except InferenceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (GraphUnavailableError, Neo4jError) as exc:
        logger.exception("reject_inference FAILED for case_id=%s", req.case_id)
        log_agent_call(
            case_id=req.case_id,
            agent_name="inference_rejection",
            endpoint="/reject_inference",
            latency_ms=int((time.time() - start) * 1000),
            status="error",
        )
        raise HTTPException(status_code=502, detail=f"Could not reach the graph: {exc}") from exc
    finally:
        logger.info("POST /reject_inference completed for case_id=%s", req.case_id)


@app.post("/revert_rejection", response_model=RevertRejectionResponse)
def revert_rejection_route(req: RevertRejectionRequest) -> RevertRejectionResponse:
    """
    Undo the rejection of ONE specific instance. The Revert button is
    shown on a row already reporting status "rejected" (Case Summary /
    Rule Audit / Fraud Network view), so this endpoint is the exact
    inverse of POST /reject_inference and takes the same fields:
    case_id, rule_id, investigator_id, reason, plus that row's match_id
    (or its subject_id_a/subject_id_b) to identify which rejected
    instance to restore.

    Restores that one instance back to active, clears its rejection
    reason and audit fields, and deletes its :Rejection guard node so
    the rule can fire again for it on the next pipeline run — every
    OTHER instance this rule rejected for this case is untouched. No
    LLM, no AppWorks, no CASE_STORE write — a Neo4j write only, handled
    entirely by reasoning_layer.rejection.revert_rejection.
    """
    start = time.time()
    try:
        envelope = revert_rejection(
            case_id=req.case_id,
            rule_id=req.rule_id,
            investigator_id=req.investigator_id,
            reason=req.reason,
            match_id=req.match_id,
            subject_id_a=req.subject_id_a,
            subject_id_b=req.subject_id_b,
        )
        log_agent_call(
            case_id=req.case_id,
            agent_name="inference_rejection_revert",
            endpoint="/revert_rejection",
            latency_ms=int((time.time() - start) * 1000),
            status="success",
        )
        return RevertRejectionResponse(**envelope["result"])
    except InferenceNotFoundError as exc:
        # Nothing rejected matches — already reverted, or never rejected.
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (GraphUnavailableError, Neo4jError) as exc:
        logger.exception("revert_rejection FAILED for case_id=%s", req.case_id)
        log_agent_call(
            case_id=req.case_id,
            agent_name="inference_rejection_revert",
            endpoint="/revert_rejection",
            latency_ms=int((time.time() - start) * 1000),
            status="error",
        )
        raise HTTPException(status_code=502, detail=f"Could not reach the graph: {exc}") from exc
    finally:
        logger.info("POST /revert_rejection completed for case_id=%s", req.case_id)


@app.get("/fraud_network/{case_id}", response_model=FraudNetworkResponse)
def fraud_network_route(case_id: str) -> FraudNetworkResponse:
    """
    D3 — Fraud Network Graph API. Read-only, no LLM, no writes (Key
    Design Rules). Powers the frontend's D3.js/Cytoscape.js network
    visualisation and is the data source the UI's per-edge Reject
    button reads its POST /reject_inference parameters from.

    Returns TWO views of the same single graph read:
      * `graph`    — the full case subgraph: the Case, its Subjects,
                     Allegations, Employers, Addresses, Aliases,
                     Commentary, FraudNetworks, merged and prior cases,
                     and every relationship between them.
      * `networks` — the original FraudNetwork-only groupings, shape
                     unchanged, so existing consumers keep working.
    """
    try:
        envelope = get_fraud_network(case_id)
        return FraudNetworkResponse(**envelope["result"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (GraphUnavailableError, Neo4jError) as exc:
        logger.exception("fraud_network FAILED for case_id=%s", case_id)
        raise HTTPException(status_code=502, detail=f"Could not reach the graph: {exc}") from exc
    finally:
        logger.info("GET /fraud_network completed for case_id=%s", case_id)


@app.get("/rule_audit/{case_id}", response_model=RuleAuditResponse)
def rule_audit_route(case_id: str) -> RuleAuditResponse:
    """
    D4 — Rule Audit / Inference Explainability. Read-only, no LLM. The
    prerequisite view for D2: an investigator reviews everything a case
    inferred, with full provenance, before deciding what to reject.
    """
    try:
        envelope = get_rule_audit(case_id)
        return RuleAuditResponse(**envelope["result"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (GraphUnavailableError, Neo4jError) as exc:
        logger.exception("rule_audit FAILED for case_id=%s", case_id)
        raise HTTPException(status_code=502, detail=f"Could not reach the graph: {exc}") from exc
    finally:
        logger.info("GET /rule_audit completed for case_id=%s", case_id)


@app.post("/copilot")
def copilot(req: CopilotRequest):
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

        log_agent_call(
            case_id=req.case_id,
            agent_name="copilot",
            endpoint="/copilot",
            latency_ms=int((time.time() - start) * 1000),
            status="success",
        )

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
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Copilot route failed for case_id=%s", req.case_id)
        log_agent_call(
            case_id=req.case_id,
            agent_name="copilot",
            endpoint="/copilot",
            latency_ms=int((time.time() - start) * 1000),
            status="error",
        )
        raise HTTPException(status_code=500, detail=f"Copilot failed: {exc}") from exc
    finally:
        logger.info("POST /copilot completed for case_id=%s", req.case_id)


@app.get("/copilot/{case_id}", response_model=ConversationHistoryResponse)
def get_conversation_history(case_id: str):
    """
    ON-DEMAND — fetch the server-owned Copilot transcript for a case.

    GET /copilot/{case_id} — same base path as POST /copilot (ask a
    question) since these are matched as (method, path) pairs, not by
    path alone: POST /copilot (exact) and GET /copilot/{case_id}
    (parameterized) are two distinct routes and never collide.

    Returns conversation_history in the same user/assistant message shape
    /copilot returns, resolved from the CS-4 warm store first, then the
    PostgreSQL conversation_history table (D.2, rolling 20-turn window).

    Read-only: no LLM, no prompt, no dispatcher — the same class of
    endpoint as /graph/ingest/status. A transcript-store outage surfaces
    as 503 (see core.case_store.fetch_copilot_history) rather than an
    empty list, so a caller can tell "no history yet" from "store down".
    """
    try:
        conversation_history, history_source = fetch_copilot_history(case_id)
        logger.info(
            "GET /conversation_history case_id=%s source=%s turns=%d",
            case_id,
            history_source,
            len(conversation_history),
        )
        return {
            "case_id": case_id,
            "conversation_history": conversation_history,
            "conversation_history_source": history_source,
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Conversation history fetch failed for case_id=%s", case_id)
        raise HTTPException(
            status_code=500,
            detail=f"Conversation history fetch failed: {exc}",
        )