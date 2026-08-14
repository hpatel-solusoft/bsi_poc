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
from typing import Any, Dict, List, Optional

import psycopg2
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from neo4j.exceptions import Neo4jError

from agent_service.runner_provider import get_runner
from agent_service.prompt_builders import (
    build_copilot_prompt,
    build_risk_assessment_prompt,
    build_similar_cases_prompt,
)
from api.message_utils import (
    build_ai_summary,
    extract_agent_summary,
    extract_tool_results,
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
    ReloadAllRequest,
    ReloadAllResponse,
    ReloadStepResult,
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
    fetch_live_risk_signals,
    fetch_live_rule_aware_tasks,
    fetch_live_similar_cases,
    resolve_prerequisite_case_data,
    run_risk_assessment_pipeline,
    run_similar_cases_pipeline,
)
from api.response_builders import (
    apply_step_override_to_summary,
    build_confidence_summary,
    format_provenance_lines,
    render_markdown_html,
    render_markdown_html_with_sources,
    validate_ai_summary_contract,
)
from api.services import intake_service, plan_service, report_service
from core import graph_ingest_repository
from core.agent_audit_repository import log_agent_call
from core.case_store import (
    AGENT_SUMMARY_CACHE_KEY,
    CASE_STORE,
    fetch_copilot_history,
    get_cached_investigation_steps,
    get_case_ai_summary_cache_updated_at,
    get_route_generated_at,
    get_route_summary_text,
    merge_agent_summary_cache,
    persist_case_session,
    resolve_case_data,
    resolve_copilot_history,
    store_copilot_turn,
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
from etl.ingest_service import ingest as run_graph_ingest
from reasoning_layer.apply_schema import apply_schema
from reasoning_layer.fraud_network import get_fraud_network
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
from reasoning_layer.rule_audit import get_rule_audit
from reasoning_layer.rule_engine import verify_rule_files

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


# Local alias — the exact same singleton api/services/*.py modules call
# via get_runner() directly; kept here for routes not yet migrated to
# api/services/*.py (similar_cases, risk_assessment, plan, copilot).
_get_runner = get_runner


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
    AUTO flow — Section 3.1. Thin HTTP adapter — all business logic
    lives in api.services.intake_service.run_intake (staleness check,
    agent-summary cache lookup, LLM agent call, direct network-match/
    context-enrichment pipeline step, persistence, response shaping).
    Kept as a real route function (not just `app.post(...)(run_intake)`)
    so /reload_all and other in-process callers can keep calling
    `intake(...)` as a plain Python function, same as before this
    refactor.
    """
    return intake_service.run_intake(req)


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
        # state — see fetch_live_similar_cases below). staleness.stale
        # reports the graph trigger only to the caller (see
        # StalenessCheck.stale's docstring for why core_data is
        # excluded); should_auto_refresh (the gate below) only fires on
        # an explicit reload_ai_summary=True — a graph-only change no
        # longer forces this route to regenerate its narrative on its
        # own (see StalenessCheck.should_auto_refresh's docstring).
        staleness = evaluate_cache_staleness(req.case_id, req.reload_ai_summary)

        # Agent-summary cache: if similar_cases has already produced and
        # persisted an agent_summary for this case_id, answer from it
        # WITHOUT calling the LLM again. Only an explicit
        # reload_ai_summary=True bypasses this lookup and falls through
        # to a fresh agent run below.
        if not staleness.should_auto_refresh:
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
        # state — see fetch_live_risk_signals below). staleness.stale
        # reports the graph trigger only to the caller (see
        # StalenessCheck.stale's docstring for why core_data is
        # excluded); should_auto_refresh (the gate below) only fires on
        # an explicit reload_ai_summary=True — a graph-only change no
        # longer forces this route to regenerate its narrative on its
        # own (see StalenessCheck.should_auto_refresh's docstring).
        staleness = evaluate_cache_staleness(req.case_id, req.reload_ai_summary)

        # Agent-summary cache: if risk_assessment has already produced
        # and persisted an agent_summary for this case_id, answer from it
        # WITHOUT calling the LLM again. Only an explicit
        # reload_ai_summary=True bypasses this lookup and falls through
        # to a fresh agent run below.
        if not staleness.should_auto_refresh:
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
    ON-DEMAND — Plan Route (Step 4 in flow). Thin HTTP adapter — all
    business logic lives in api.services.plan_service.run_plan
    (prerequisite auto-resolve, staleness/override checks, LLM agent
    call, plan pipeline, persistence, response shaping). Kept as a real
    route function (not just `app.post(...)(run_plan)`) so /reload_all,
    /plan/modify_investigation_steps, and other in-process callers can
    keep calling `plan(...)` as a plain Python function, same as before
    this refactor.
    """
    return plan_service.run_plan(req)

@app.post("/reload_all", response_model=ReloadAllResponse)
def reload_all(req: ReloadAllRequest) -> ReloadAllResponse:
    """
    ON-DEMAND — force-refresh every tab for case_id in one call.

    Runs /graph/ingest -> /intake -> /similar_cases -> /risk_assessment
    -> /plan, in that exact dependency order — the same order an
    investigator would click through the tabs in, and the same order
    each route's own prerequisite-auto-resolve chain already assumes
    (see api.pipeline_execution.resolve_prerequisite_case_data).

    The /graph/ingest step is what makes this a genuine "refresh
    everything from source" action rather than "re-run the LLM against
    whatever happens to already be in Neo4j". Every other step here
    reads its case data through the reload_ai_summary=True cache-bypass
    (core_data_changed, see core.narrative_staleness), but that bypass
    only forces AppWorks to be re-fetched for the AUTO/ON-DEMAND
    sections themselves — it was never what kept Neo4j's structural
    data (subjects, addresses, employers, wages, allegations,
    commentary) current. Until AppWorks' own lifecycle event is wired
    up (see /graph/ingest's docstring), the only thing that pushes a
    fresh AppWorks read into Neo4j at all is a POST /graph/ingest call,
    and reload_all previously never made one — an investigator could
    hit "reload" all day and still have /intake's Wave 1/2 rules
    reasoning over yesterday's graph.

    Run with run_rules=True (the /graph/ingest default) rather than
    False: reload_all is the one caller that should always want a
    graph that is both freshly loaded AND freshly reasoned before
    anything downstream reads it, not staged as a separate step — see
    GraphIngestRequest's own docstring on why run_rules only ever
    defaults to True, "a loaded-but-unreasoned graph looks complete
    and is not". This does mean Wave 1/2 runs twice in the same
    request: once here inside /graph/ingest, and again a few lines
    down inside /intake, because /intake's own reload_ai_summary=True
    unconditionally clears pipeline_execution_state and reruns
    (staleness.should_rerun_full_pipeline in api.pipeline_execution)
    regardless of what /graph/ingest just did — it has no way to know
    a reasoning pass already happened in this same call. That
    duplication is redundant work, not a correctness risk: every rule
    write is MERGE/SET (Principle 15), so a second pass over unchanged
    data reasserts the same facts rather than producing different or
    duplicate ones. The added latency is the deliberate trade for
    never serving a stale-graph reasoning pass under this endpoint.

    This is a thin orchestrator over the existing routes, not a
    parallel implementation: each step below is a plain Python call to
    that route's own function — the exact same pattern this codebase
    already uses for auto-resolving a missing prerequisite (e.g. /plan
    calling `lambda: risk_assessment(RiskAssessmentRequest(...))` when
    risk_assessment is missing). Every persistence side effect —
    the Neo4j structural sync (and its own stale-edge reconciliation,
    see etl/graph_sync.py's RECONCILE section), Postgres
    case_ai_summary_store, the Neo4j reasoning pipeline, the warm
    CASE_STORE, agent_summary_cache's per-tab generated_at — still
    happens exactly the way it always has, inside those route
    functions. reload_all adds no new write path of its own, so there
    is no risk of it drifting out of sync with what a single-tab
    reload_ai_summary=True call (or a direct POST /graph/ingest call)
    already does.

    Stops at the first failing step rather than pushing on: every step
    here depends on the one before it having just produced fresh data
    (e.g. /plan reads /risk_assessment's freshly persisted result, and
    /intake's Wave 1/2 rules read whatever /graph/ingest just wrote), so
    continuing past a failure would silently run later steps against a
    now-stale upstream instead of the fresh one this call promised.
    Every step after the failure is reported "skipped", never attempted.
    A /graph/ingest failure therefore stops the whole run rather than
    falling back to reasoning over a possibly-stale graph — a partial
    reload that silently skipped the one step that actually refreshes
    Neo4j would be worse than an obvious failure here.

    Always returns 200 — this is a bulk status report, not a single
    pass/fail action. The response body's per-step `status` values and
    top-level `status` (success / partial / failed) carry the actual
    outcome, so a caller can render "3 of 5 tabs refreshed, Plan
    failed: <reason>" instead of a single opaque error.
    """
    start = time.time()
    steps: List[ReloadStepResult] = []
    stopped = False

    # Order matters: each callable is only invoked once its turn comes,
    # so a request object for step N is never built (and never touches
    # case_data) before step N-1 has actually completed.
    step_calls = [
        (
            "graph_ingest",
            lambda: graph_ingest(GraphIngestRequest(case_ids=[req.case_id], run_rules=True)),
        ),
        ("intake", lambda: intake(intakeRequest(case_id=req.case_id, reload_ai_summary=True))),
        (
            "similar_cases",
            lambda: similar_cases(SimilarCasesRequest(case_id=req.case_id, reload_ai_summary=True)),
        ),
        (
            "risk_assessment",
            lambda: risk_assessment(RiskAssessmentRequest(case_id=req.case_id, reload_ai_summary=True)),
        ),
        ("plan", lambda: plan(PlanRequest(case_id=req.case_id, reload_ai_summary=True))),
    ]

    for step_name, call_step in step_calls:
        if stopped:
            steps.append(ReloadStepResult(step=step_name, status="skipped", duration_seconds=0.0))
            continue

        step_start = time.time()
        try:
            step_response = call_step()
        except HTTPException as exc:
            duration = round(time.time() - step_start, 1)
            logger.error(
                "reload_all STEP FAILED case_id=%s step=%s status_code=%s detail=%s",
                req.case_id,
                step_name,
                exc.status_code,
                exc.detail,
            )
            steps.append(
                ReloadStepResult(
                    step=step_name,
                    status="failed",
                    duration_seconds=duration,
                    error=str(exc.detail),
                )
            )
            stopped = True
            continue
        except Exception as exc:  # noqa: BLE001 — isolate one step's failure from the rest
            duration = round(time.time() - step_start, 1)
            logger.exception("reload_all STEP FAILED case_id=%s step=%s", req.case_id, step_name)
            steps.append(
                ReloadStepResult(
                    step=step_name,
                    status="failed",
                    duration_seconds=duration,
                    error=str(exc),
                )
            )
            stopped = True
            continue

        duration = round(time.time() - step_start, 1)
        step_meta = (step_response.get("details") or {}).get("meta") or {}
        steps.append(
            ReloadStepResult(
                step=step_name,
                status="success",
                duration_seconds=duration,
                agent_summary_source=step_meta.get("agent_summary_source"),
                stale=step_meta.get("stale"),
            )
        )

    overall_duration = round(time.time() - start, 1)
    succeeded_count = sum(1 for s in steps if s.status == "success")
    failed_count = sum(1 for s in steps if s.status == "failed")
    if failed_count == 0:
        overall_status = "success"
    elif succeeded_count == 0:
        overall_status = "failed"
    else:
        overall_status = "partial"

    logger.info(
        "reload_all COMPLETED case_id=%s status=%s duration_seconds=%.1f steps=%s",
        req.case_id,
        overall_status,
        overall_duration,
        ", ".join(f"{s.step}:{s.status}" for s in steps),
    )
    # Best-effort audit row for the bulk action itself, alongside the
    # four per-step rows each underlying route already writes on its
    # own via log_agent_call.
    log_agent_call(
        case_id=req.case_id,
        agent_name="reload_all",
        endpoint="/reload_all",
        latency_ms=int(overall_duration * 1000),
        status=overall_status,
    )

    return ReloadAllResponse(
        case_id=req.case_id,
        status=overall_status,
        duration_seconds=overall_duration,
        steps=steps,
    )


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
    ON-DEMAND — Report Generation Route (AI-18). Thin HTTP adapter —
    all business logic lives in
    api.services.report_service.run_generate_report (Related Network
    assembly, plan-override read, report cache, Decision & Override Log
    assembly, LLM agent call, persistence, response shaping).
    """
    return report_service.run_generate_report(req)

@app.post("/generate_report/pdf")
def generate_report_pdf(req: ReportGenerationRequest):
    """
    ON-DEMAND — Report PDF Export Route. Thin HTTP adapter — all
    business logic lives in
    api.services.report_service.run_generate_report_pdf, a deliberate
    duplicate of run_generate_report's pipeline (see that function's own
    docstring for why), ending in a rendered PDF Response instead of a
    JSON dict.
    """
    return report_service.run_generate_report_pdf(req)

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