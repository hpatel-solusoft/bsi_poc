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
import psycopg2
from agent_service.agent_runner import BSIAgentRunner
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from core.case_store import (
    CASE_STORE,
    fetch_copilot_history,
    store_copilot_turn,
    resolve_copilot_history,
    resolve_case_data,
    persist_case_session,
    get_case_ai_summary_cache_updated_at,
    get_cached_investigation_steps,
)
from core.agent_audit_repository import log_agent_call
from core.investigation_plan_override_repository import (
    upsert_override,
    get_override,
    delete_override,
    compute_plan_staleness,
)
from core.db import init_pool as init_db_pool, close_pool as close_db_pool, DatabaseUnavailableError
from reasoning_layer.neo4j_client import (
    init_driver as init_neo4j_driver,
    close_driver as close_neo4j_driver,
    GraphUnavailableError,
)
from reasoning_layer.context_enrichment import enrich_graph_context
from reasoning_layer.similar_cases import find_structural_matches
from reasoning_layer.report_generation import assemble_related_network
from reasoning_layer.rejection import (
    reject_inference,
    InferenceNotFoundError,
    RelationshipTypeMismatchError,
)
from reasoning_layer.fraud_network import get_fraud_network
from reasoning_layer.rule_audit import get_rule_audit
from core.report_artifacts_repository import save_report
from neo4j.exceptions import Neo4jError
from reasoning_layer.apply_schema import apply_schema
from reasoning_layer.rule_engine import verify_rule_files
from etl.ingest_service import ingest as run_graph_ingest
from core import graph_ingest_repository
from dotenv import load_dotenv
from semantic_layer.entity_contracts import InvestigationPlan as InvestigationPlanContract
from semantic_layer.entity_contracts import GeneratedReport as GeneratedReportContract
from api.models import (
    ConversationHistoryResponse, intakeRequest, RiskAssessmentRequest, SimilarCasesRequest, PlanRequest,
    CopilotRequest, GraphIngestRequest,
    ModifyInvestigationStepsRequest, ModifyInvestigationStepsResponse,
    RevertToAiPlanRequest, RevertToAiPlanResponse,
    InvestigationStepsResponse,
    ReportGenerationRequest,
    RejectInferenceRequest, RejectInferenceResponse,
    FraudNetworkResponse,
    RuleAuditResponse,
)
from agent_service.prompt_builders import (
    build_intake_system_prompt,
    build_risk_assessment_prompt,
    build_plan_prompt,
    build_similar_cases_prompt,
    build_copilot_prompt,
    build_report_generation_prompt,
)
from api.response_builders import (
    validate_ai_summary_contract,
    render_markdown_html_with_sources,
    render_markdown_html,
    resolve_plan_agent_summary,
    build_confidence_summary,
)
from api.message_utils import (
    build_ai_summary,
    extract_agent_summary,
    extract_tool_results,
    merge_direct_result,
    merge_provenance,
)
from api.pipeline_execution import (
    run_intake_direct_pipeline,
    run_similar_cases_pipeline,
    run_risk_assessment_pipeline,
    run_plan_pipeline,
    prepare_plan_context,
) 
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
    _cors_allowed_origins, _cors_allow_credentials,
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
# This flag governs ONE thing only: whether reasoning_layer/pipeline.py's
# run_pipeline (invoked via reasoning_layer/context_enrichment.py's
# enrich_graph_context, called from /intake and /copilot) is allowed to
# skip re-running when it has already completed for a (case_id,
# subject_id) — Principle 10 in pipeline.py.
#   False (default) — the pipeline keeps its own existing skip-if-already-
#                      run behavior; unchanged either way.
#   True             — force the pipeline to re-run even though it already
#                       completed (bypasses the Principle 10 skip for this
#                       call only).
#
# It does NOT gate whether a route's agent/tools run. Every ON-DEMAND
# route (/intake, /similar_cases, /risk_assessment, /plan) always runs
# its agent/tools and returns a fresh result when called — that behavior
# is unchanged from before this flag existed, regardless of whether
# reload_ai_summary is true or false, and regardless of whether the
# section already ran.
# -----------------------------------------------------------------------


# -----------------------------------------------------------------------
# Endpoints
# -----------------------------------------------------------------------

@app.get("/health")
def health():
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
        if not os.getenv("OPENAI_API_KEY"):
            raise HTTPException(status_code=500, detail="OPENAI_API_KEY not configured")

        runner = _get_runner()
        # Scope to intake + enrichment only; similar cases is a separate route.
        
        messages, provenance_trail, _ = runner.run_scoped(
            system_prompt=build_intake_system_prompt(),
            user_message=(
                f"intake case {req.case_id}."
            ),
            scope="CASE_SUMMARY",  # ← this scope includes intake + enrichment tools only; 
        )
        sections = extract_tool_results(messages, runner.dispatcher.tool_to_section)

        # Direct, non-LLM network-match + context-enrichment pipeline work
        # (Section 8.1 AI-12, Section 9.1 AI-13) — factored out to
        # api/pipeline_execution.py. subject_primary_id was injected into
        # complaint_intelligence by extract_tool_results above.
        sections, provenance_trail = run_intake_direct_pipeline(
            req.case_id, req.reload_ai_summary, sections, provenance_trail,
        )

        # CS-4: populate warm in-memory store with all sections + provenance.
        CASE_STORE[req.case_id] = {**sections, "provenance_trail": provenance_trail}

        # ai_summary is the internal contract object handed between routes.
        # It is no longer returned to the caller (Data Persistence Spec v1.0,
        # Section B.2/D.1): AppWorks now sends case_id only on every
        # subsequent call, so the full JSON is persisted server-side in
        # PostgreSQL case_ai_summary_store and rehydrated there on the next
        # request instead of round-tripping through the client.
        ai_summary = {
            "investigation":    sections,
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
                "agent_summary": render_markdown_html_with_sources(extract_agent_summary(messages), provenance_trail),
                # Graph reasoning results (AI-12 network_match_flag, AI-13
                # graph_context/graph_signals/rules_fired) previously only
                # reached ai_summary.investigation — computed after the LLM's
                # agent_summary text was already finalised, so it never
                # surfaced in the response the UI actually renders. Surfaced
                # explicitly here so the pipeline's output stops being
                # silently dropped before it reaches the screen.
                "graph_findings": {
                    "network_match_flag": sections.get("network_match_flag"),
                    "graph_context":      sections.get("graph_context"),
                    "graph_signals":      sections.get("graph_signals"),
                    "rules_fired":        sections.get("rules_fired"),
                    "confidence_summary": build_confidence_summary(sections.get("rules_fired")),
                },
                "meta": {
                    "tool_calls_made":  len(provenance_trail),
                    "duration_seconds": duration_seconds,
                    "pipeline_status": "reloaded" if req.reload_ai_summary else "ran",
                    "reload_ai_summary": req.reload_ai_summary,
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
        case_data, data_source = _resolve_case_store(req.case_id, req.ai_summary)
        logger.info(
            "case_id=%s data_source=%s key_count=%d",
            req.case_id, data_source, len(list(case_data.keys())),
        )
        runner = _get_runner()

        # Direct structural-matching + LLM-explain pipeline work
        # (Section 8.3 AI-14, Section 9.2) — factored out to
        # api/pipeline_execution.py.
        agent_summary, similar_cases_data, similar_section, merged_provenance = run_similar_cases_pipeline(
            req.case_id, case_data, runner, build_similar_cases_prompt,
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
                "agent_summary": render_markdown_html_with_sources(agent_summary, merged_provenance),
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
        case_data, data_source = _resolve_case_store(req.case_id, req.ai_summary)
        logger.info("case_id=%s data_source=%s", req.case_id, data_source)

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
            execution_context=execution_context
        )

        sections = extract_tool_results(messages, runner.dispatcher.tool_to_section)

        # Direct graph risk-signal pipeline work (Section 8.4 AI-15) plus
        # recommendation-text normalization — factored out to
        # api/pipeline_execution.py.
        risk_assessment, risk_section, merged_provenance = run_risk_assessment_pipeline(
            req.case_id, case_data, sections, tool_call_log, new_provenance, messages,
        )

        # Update CS-4 warm store but return only the route-specific section.
        CASE_STORE[req.case_id].update(risk_section)
        CASE_STORE[req.case_id]["provenance_trail"] = merged_provenance

        ai_summary = build_ai_summary(
            case_data,
            {"risk_assessment": risk_assessment},
            merged_provenance,
        )
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
                "agent_summary": render_markdown_html_with_sources(extract_agent_summary(messages), merged_provenance),
                # Neo4j graph risk signals (AI-15 —
                # reasoning_layer.risk_signals.apply_graph_risk_signals):
                # the four Section 8.4 signals plus the AppWorks base score
                # they were layered on. Returned the same way graph_findings
                # is on /intake and /similar_cases, so the investigator can
                # see WHICH graph signal moved the score rather than only the
                # LLM's prose about the final number. base_* are carried
                # alongside so the graph contribution stays auditable.
                "graph_findings": {
                    "neo4j_signals":   risk_assessment.get("neo4j_signals"),
                    "base_risk_score": risk_assessment.get("base_risk_score"),
                    "base_risk_tier":  risk_assessment.get("base_risk_tier"),
                    "risk_score":      risk_assessment.get("risk_score"),
                    "risk_tier":       risk_assessment.get("risk_tier"),
                },
                "meta": {
                    "data_source": data_source,
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
        case_data, data_source = _resolve_case_store(req.case_id, req.ai_summary)
        logger.info("case_id=%s data_source=%s", req.case_id, data_source)

        # Captured BEFORE this route's own persist_case_session call below,
        # which always rewrites updated_at to now() — reading it late would
        # make every override look stale (Section E.5).
        cache_updated_at_before_call = get_case_ai_summary_cache_updated_at(req.case_id)

        execution_context = {"ai_summary": req.ai_summary}
        runner = _get_runner()
        # Scope to plan retrieval only (Step 4)
        
        # AI-16 (Section 8.5): build the rule-aware task recommendations from
        # the rules_fired already in context and hand them to the prompt, so
        # the agent selects investigation steps from both the rule-derived
        # tasks and the BSI catalogue tasks its scoped tool returns.
        case_data_for_prompt, rule_aware_tasks = prepare_plan_context(case_data)

        messages, new_provenance, _ = runner.run_scoped(
            system_prompt=build_plan_prompt(case_data_for_prompt),
            user_message=(
                f"Review the investigation context for case {req.case_id} and execute the "
                "appropriate on-demand tools to assemble the investigation plan."
            ),
            scope="INVESTIGATION_PLAN",  # ← this scope includes intake + enrichment tools only
            execution_context=execution_context
        )

        sections = extract_tool_results(messages,runner.dispatcher.tool_to_section)

        # Parse/validate the LLM's plan output and apply any human override
        # (Section D.6) — factored out to api/pipeline_execution.py.
        (
            assistant_text, investigation_plan, plan_section, merged_provenance,
            plan_source, modified_by, modified_on, plan_stale,
        ) = run_plan_pipeline(
            req.case_id, case_data, sections, messages, new_provenance,
            cache_updated_at_before_call, rule_aware_tasks,
        )

        # Update CS-4 warm store but return only the route-specific section.
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
        persist_case_session(req.case_id, ai_summary)
        log_agent_call(
            case_id=req.case_id,
            agent_name="investigation_plan",
            endpoint="/plan",
            latency_ms=int((time.time() - start) * 1000),
            status="success",
        )

        return {
            "case_id": req.case_id,
            "status": "completed",
            "details": {
                "agent_summary": render_markdown_html_with_sources(
                    resolve_plan_agent_summary(
                        assistant_text, investigation_plan, req.case_id,
                        case_data, merged_provenance,
                    ),
                    merged_provenance,
                ),
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
                    "rules_fired":      case_data.get("rules_fired"),
                },
                "meta": {
                    "data_source": data_source,
                    "plan_source": plan_source,
                    "modified_by": modified_by,
                    "modified_on": modified_on.isoformat() if modified_on else None,
                    "plan_stale": plan_stale,
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
            plan_source="human_modified",
            modified_by=req.investigator_id,
            modified_on=modified_on,
        )
    except (psycopg2.Error, DatabaseUnavailableError) as exc:
        logger.exception(
            "modify_investigation_steps FAILED to save for case_id=%s", req.case_id,
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
            "POST /plan/modify_investigation_steps completed for case_id=%s", req.case_id,
        )


@app.post("/plan/revert_to_ai", response_model=RevertToAiPlanResponse)
def revert_to_ai_plan(req: RevertToAiPlanRequest) -> RevertToAiPlanResponse:
    """
    Investigator clicks "Revert to AI Plan" — deletes case_id's saved
    investigation_plan_overrides row. The next /plan or /copilot call
    for this case_id finds no override row and falls back to
    plan_source: ai_generated.
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
            plan_source="ai_generated",
        )
    except (psycopg2.Error, DatabaseUnavailableError) as exc:
        logger.exception(
            "revert_to_ai_plan FAILED for case_id=%s", req.case_id,
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
            case_id, len(investigation_steps),
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
        case_id, len(investigation_steps),
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
    """
    start = time.time()
    try:
        # CS-4 pattern: warm lookup -> Postgres fallback -> ai_summary body.
        case_data, data_source = _resolve_case_store(req.case_id, req.ai_summary)
        logger.info(
            "case_id=%s data_source=%s key_count=%d",
            req.case_id, data_source, len(list(case_data.keys())),
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
        try:
            related_envelope = assemble_related_network(req.case_id, subject_id)
            related = related_envelope["result"]
        except (ValueError, GraphUnavailableError, Neo4jError) as exc:
            logger.warning(
                "related-network assembly unavailable for case_id=%s subject_id=%s — %s",
                req.case_id, subject_id, exc,
            )
            related = {
                "subject_id": subject_id, "related_network": [],
                "confidence_summary": {"high": 0, "medium": 0, "unresolved": 0},
                "rejected_count": 0, "unavailable_reason": str(exc),
            }
            related_envelope = {
                "result": related,
                "provenance": {"sources": [], "retrieved_at": "",
                               "computed_by": "reasoning_layer.report_generation.assemble_related_network"},
            }

        # Inject the computed network into the case context the prompt
        # serialises, so the LLM narrates THESE facts (never adds,
        # removes, or reorders them — REPORT_GENERATION scope carries no
        # tools, so the LLM cannot re-query the graph itself either).
        case_data_for_prompt = {
            **case_data,
            "related_network": related.get("related_network", []),
            "confidence_summary": related.get("confidence_summary", {}),
            "rejected_count": related.get("rejected_count", 0),
        }

        messages, new_provenance, _ = runner.run_scoped(
            system_prompt=build_report_generation_prompt(case_data_for_prompt),
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
            sections, new_provenance, "related_network", related_envelope,
        )

        assistant_text = extract_agent_summary(messages)

        merged_provenance = merge_provenance(
            case_data.get("provenance_trail", []),
            new_provenance,
        )

        report_id = f"RPT-{req.case_id}-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        generated_at = datetime.now(timezone.utc).isoformat()
        confidence_summary = related.get(
            "confidence_summary", {"high": 0, "medium": 0, "unresolved": 0}
        )
        report_content = {
            "report_id": report_id,
            "case_id": req.case_id,
            "generated_at": generated_at,
            "status": "draft",
            "standard_sections": {"report_markdown": assistant_text},
            "related_network": related.get("related_network", []),
            "confidence_summary": confidence_summary,
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
                "agent_summary": render_markdown_html_with_sources(
                    assistant_text, merged_provenance,
                ),
                "related_network": related.get("related_network", []),
                "confidence_summary": confidence_summary,
                "rejected_count": related.get("rejected_count", 0),
                "meta": {
                    "data_source": data_source,
                    "report_status": "draft",
                    "persisted_to_postgres": persisted is not None,
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


@app.post("/reject_inference", response_model=RejectInferenceResponse)
def reject_inference_route(req: RejectInferenceRequest) -> RejectInferenceResponse:
    """
    D2 — Inference Rejection Handler. An investigator clicks "Reject" on
    an inferred fact shown in the Context Enrichment panel, the Fraud
    Network screen (GET /fraud_network/{case_id}), or the Rule Audit
    panel (GET /rule_audit/{case_id}), and the UI POSTs here with the
    fields that entry already carries.

    No LLM involvement (D2 Boundaries). Does not touch CASE_STORE or
    investigation_plan_overrides — this is a Neo4j write only, handled
    entirely by reasoning_layer.rejection.reject_inference.
    """
    start = time.time()
    try:
        envelope = reject_inference(
            case_id=req.case_id,
            subject_id_a=req.subject_id_a,
            subject_id_b=req.subject_id_b,
            rule_id=req.rule_id,
            relationship_type=req.relationship_type,
            investigator_id=req.investigator_id,
            reason=req.reason,
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
    except (ValueError, RelationshipTypeMismatchError) as exc:
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


@app.get("/fraud_network/{case_id}", response_model=FraudNetworkResponse)
def fraud_network_route(case_id: str) -> FraudNetworkResponse:
    """
    D3 — Fraud Network Graph API. Read-only, no LLM, no writes (Key
    Design Rules). Powers the frontend's D3.js/Cytoscape.js network
    visualisation and is the data source the UI's per-edge Reject
    button reads its POST /reject_inference parameters from.
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
                "from Copilot (Section 6.3)", req.case_id,
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
            req.case_id, case_data_source, history_source,
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

        sources_cited = [
            f"retrieved {p.get('retrieved_at', '')}"
            for p in combined_provenance
        ]
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
            "answer": render_markdown_html(answer),
            "sources_cited": sources_cited,
            "sources_cited_details": sources_cited_details,
            "case_data_source": case_data_source,
            "conversation_history": conversation_history,
            "conversation_history_source": history_source,
            "reload_ai_summary": req.reload_ai_summary,
            "plan_source": "human_modified" if override is not None else "ai_generated",
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
            case_id, history_source, len(conversation_history),
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