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
import re
import time
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
)
from core.agent_audit_repository import log_agent_call
from core.db import init_pool as init_db_pool, close_pool as close_db_pool, DatabaseUnavailableError
from reasoning_layer.neo4j_client import (
    init_driver as init_neo4j_driver,
    close_driver as close_neo4j_driver,
    GraphUnavailableError,
)
from reasoning_layer.graph_queries import check_network_match
from reasoning_layer.context_enrichment import enrich_graph_context
from reasoning_layer.similar_cases import find_structural_matches
from neo4j.exceptions import Neo4jError
from reasoning_layer.apply_schema import apply_schema
from reasoning_layer.rule_engine import verify_rule_files
from etl.ingest_service import ingest as run_graph_ingest
from core import graph_ingest_repository
from dotenv import load_dotenv
from semantic_layer.entity_contracts import InvestigationPlan as InvestigationPlanContract
from api.models import (
    ConversationHistoryResponse, intakeRequest, RiskAssessmentRequest, SimilarCasesRequest, PlanRequest,
    CopilotRequest, GraphIngestRequest,
)
from agent_service.prompt_builders import (
    build_intake_system_prompt,
    build_risk_assessment_prompt,
    build_plan_prompt,
    build_similar_cases_prompt,
    build_copilot_prompt,
)
from api.response_builders import (
    validate_ai_summary_contract,
    render_markdown_html_with_sources,
    parse_bsi_section, render_markdown_html
)
from api.message_utils import (
    build_ai_summary,
    extract_agent_summary,
    extract_tool_results,
    merge_provenance,
    merge_direct_result, )    

from reasoning_layer.similar_cases import find_structural_matches
from reasoning_layer.risk_signals import apply_graph_risk_signals    

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

        # Direct, non-LLM network-match check (Section 8.1). subject_primary_id
        # was injected into complaint_intelligence by extract_tool_results.
        subject_id = (sections.get("complaint_intelligence") or {}).get("subject_primary_id")
        if subject_id:
            # --- AI-12: proactive network match flag (Section 8.1) ---
            # Preliminary "is this subject already in a known network" check.
            # Section 9.1 keeps this as its own section (network_match_flag),
            # distinct from the full graph_context that Context Enrichment
            # assembles below.
            try:
                envelope = check_network_match(subject_id)
                provenance_trail = merge_direct_result(
                    sections, provenance_trail, "network_match_flag", envelope
                )
            except (ValueError, GraphUnavailableError, Neo4jError) as exc:
                # Non-blocking by design: a graph outage or bad subject_id
                # must not fail complaint intake. Degrade to an empty,
                # clearly-unavailable flag instead.
                logger.warning(
                    "check_network_match unavailable for case_id=%s subject_id=%s — %s",
                    req.case_id, subject_id, exc,
                )
                sections["network_match_flag"] = {
                    "subject_id": subject_id, "in_network": None,
                    "network_count": None, "networks": [],
                    "rejected_membership_count": None,
                    "unavailable_reason": str(exc),
                }

            # --- AI-13: Context Enrichment gateway (Section 9.1) ---
            # Context Enrichment's own processing, once fetch_subject_history
            # has returned: run the reasoning pipeline directly (never an LLM
            # tool, not dispatcher-routed, not in manifest.yaml — the same
            # direct-call pattern as the network match above), then assemble
            # the full graph_context, graph_signals, and rules_fired.
            # Non-blocking: a graph or pipeline failure degrades to an empty,
            # clearly-unavailable graph_context rather than failing intake.
            try:
                # force=req.reload_ai_summary: when True this bypasses
                # Principle 10 and makes the reasoning pipeline re-run for
                # this (case, subject) even though it may already have
                # completed, updating pipeline_execution_state (PostgreSQL)
                # and the Neo4j graph rather than returning the cached
                # rules_fired.
                enrichment = enrich_graph_context(
                    req.case_id, subject_id, force=req.reload_ai_summary,
                )["result"]
                provenance_trail = merge_direct_result(
                    sections, provenance_trail, "graph_context",
                    {
                        "result": enrichment["graph_context"],
                        "provenance": {
                            "sources": ["reasoning pipeline", "Neo4j graph query"],
                            "retrieved_at": "",
                            "computed_by": "reasoning_layer.context_enrichment.enrich_graph_context",
                        },
                    },
                )
                sections["graph_signals"] = enrichment["graph_signals"]
                sections["rules_fired"] = enrichment["rules_fired"]
            except (ValueError, GraphUnavailableError, Neo4jError) as exc:
                logger.warning(
                    "context enrichment unavailable for case_id=%s subject_id=%s — %s",
                    req.case_id, subject_id, exc,
                )
                sections["graph_context"] = {
                    "subject_id": subject_id,
                    "is_cross_case_hub": None, "hub_case_ids": [],
                    "fraud_networks": [], "prior_guilty_cases": [],
                    "shared_connections": [],
                    "unavailable_reason": str(exc),
                }
                sections["graph_signals"] = {"unavailable_reason": str(exc)}
                sections["rules_fired"] = []
        else:
            logger.warning(
                "context enrichment + network match skipped for case_id=%s — "
                "no subject_primary_id resolved", req.case_id,
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

        # --- AI-14: deterministic structural matching (Section 8.3, 9.2) ---
        # Replaces the Phase 1 two-step LLM type-selection. The matches are
        # computed by a single Cypher query (reasoning_layer.similar_cases),
        # called DIRECTLY — not an LLM tool, not dispatcher-routed, not in
        # manifest.yaml (governance: manifest holds a tool only if it is
        # LLM-called AND makes an AppWorks call; this is a Neo4j read). The
        # LLM's role is now to EXPLAIN what the graph found, never to select
        # it (Section 8.3). Non-blocking: a graph outage degrades to an
        # empty, clearly-unavailable section rather than failing the route.
        try:
            structural = find_structural_matches(req.case_id)["result"]
        except (ValueError, GraphUnavailableError, Neo4jError) as exc:
            logger.warning(
                "structural similar-case matching unavailable for case_id=%s — %s",
                req.case_id, exc,
            )
            structural = {
                "matches": [], "source": "structural_graph",
                "total_candidates_scored": 0,
                "unavailable_reason": str(exc),
            }

        # Inject the computed matches into the case context the prompt
        # serialises, so the LLM explains THESE matches (Turn 2 in Section
        # 9.2) rather than being asked to find any. SIMILAR_CASES scope no
        # longer carries a matching tool, so the LLM only explains.
        case_data_for_prompt = {**case_data, "similar_cases": structural}

        messages, new_provenance, _ = runner.run_scoped(
            system_prompt=build_similar_cases_prompt(case_data_for_prompt),
            user_message=(
                f"Explain the structurally similar historical cases already "
                f"identified for case {req.case_id}: why each one matched "
                f"(see match_reasons) and how relevant its pattern is. Do not "
                f"add or remove cases; the graph has already decided the matches."
            ),
            scope="SIMILAR_CASES",
        )

        # The authoritative similar_cases section is the DETERMINISTIC
        # structural result, not anything the LLM produced — the LLM
        # explains, it does not decide inclusion.
        sections: dict = {}
        new_provenance = merge_direct_result(
            sections, new_provenance, "similar_cases",
            {"result": structural,
             "provenance": {"sources": ["Neo4j graph query"], "retrieved_at": "",
                            "computed_by": "reasoning_layer.similar_cases"}},
        )

        agent_summary = extract_agent_summary(messages)

        similar_cases_data = sections.get("similar_cases", {})
        similar_section = {
            "similar_cases": similar_cases_data
        }

        merged_provenance = merge_provenance(
            case_data.get("provenance_trail", []),
            new_provenance,
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
        
        risk_assessment = sections.get("risk_assessment", {})
        if not isinstance(risk_assessment, dict) or "risk_score" not in risk_assessment:
            called_tools = [
                entry.get("tool")
                for entry in tool_call_log
                if isinstance(entry, dict) and entry.get("status") == "ok"
            ]
            raise RuntimeError(
                "Risk assessment did not complete because calculate_risk_metrics "
                f"did not return a score. Successful tools: {called_tools}"
            )

        # --- AI-15: Neo4j graph risk signals (Section 8.4) ---
        # The AppWorks base score above is UNCHANGED. Four graph-sourced
        # signals are layered on top by a DIRECT call (Neo4j read, not an
        # AppWorks call, so not a manifest tool — same pattern as the other
        # reasoning-layer direct calls). The subject and rules_fired come
        # from CS-4 (populated at intake / Context Enrichment). Non-blocking:
        # a graph outage leaves the base result untouched rather than
        # failing the route.
        subject_id = (case_data.get("complaint_intelligence") or {}).get("subject_primary_id")
        if subject_id:
            try:
                graph_env = apply_graph_risk_signals(
                    req.case_id,
                    subject_id,
                    risk_assessment,
                    case_data.get("rules_fired", []),
                )
                risk_assessment = graph_env["result"]
                # Section 8.4 provenance requirement: keep the AppWorks base
                # scorer's computed_by AND add the Neo4j graph-signal
                # computed_by as a distinct, independently-attributable entry.
                new_provenance = merge_provenance(new_provenance, [graph_env["provenance"]])
            except (ValueError, GraphUnavailableError, Neo4jError) as exc:
                logger.warning(
                    "graph risk signals unavailable for case_id=%s subject_id=%s — %s; "
                    "returning AppWorks base score only",
                    req.case_id, subject_id, exc,
                )
                risk_assessment["neo4j_signals"] = {"unavailable_reason": str(exc)}
        else:
            logger.warning(
                "graph risk signals skipped for case_id=%s — no subject_primary_id resolved",
                req.case_id,
            )
        # Normalize recommendation text: rename singular "recommendation" to plural "recommendations"
        assistant_text = extract_agent_summary(messages)
        rec_text = None
        try:
            if isinstance(risk_assessment, dict):
                # Extract from either singular or plural field
                rec_text = risk_assessment.get("recommendation") or risk_assessment.get("recommendations")
                # Remove the singular field to avoid duplication
                risk_assessment.pop("recommendation", None)
        except Exception:
            rec_text = None

        if not rec_text and isinstance(assistant_text, str):
            # attempt to parse a recommendation section from assistant markdown
            m = re.search(
                r"(?:^|\n)#{1,6}\s*(?:Recommended Action|Recommendation|Recommendations)\s*\n(.*?)(?=\n#{1,6}\s|\Z)",
                assistant_text,
                re.DOTALL | re.IGNORECASE,
            )
            if m:
                rec_text = m.group(1).strip()

        if rec_text and isinstance(risk_assessment, dict):
            risk_assessment["recommendations"] = rec_text

        if isinstance(risk_assessment, dict):
            if "recommendations" not in risk_assessment:
                risk_assessment["recommendations"] = ""
        else:
            risk_assessment = {"recommendations": ""}
        risk_section = {
            "risk_assessment": risk_assessment
        }

        merged_provenance = merge_provenance(
            case_data.get("provenance_trail", []),
            new_provenance,
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

        execution_context = {"ai_summary": req.ai_summary}
        runner = _get_runner()
        # Scope to plan retrieval only (Step 4)
        
        messages, new_provenance, _ = runner.run_scoped(
            system_prompt=build_plan_prompt(case_data),
            user_message=(
                f"Review the investigation context for case {req.case_id} and execute the "
                "appropriate on-demand tool to retrieve the investigation plan."
            ),
            scope="INVESTIGATION_PLAN",  # ← this scope includes intake + enrichment tools only
            execution_context=execution_context
        )

        sections = extract_tool_results(messages,runner.dispatcher.tool_to_section)
        investigation_plan = sections.get("investigation_plan", {})

        assistant_text = extract_agent_summary(messages)

        # Parse markdown prose into structured fields (same source used for agent_summary)
        steps = parse_bsi_section(assistant_text, "Investigation Steps")
        checklist = parse_bsi_section(assistant_text, "Evidence Checklist")
        criteria = parse_bsi_section(assistant_text, "Escalation Criteria")

        # Convert parsed strings to typed dicts.
        # 'owner' and 'deadline_days' are intentionally absent —
        # they are populated during the human analyst review step.
        steps_dicts     = [{"step": i + 1, "action": s} for i, s in enumerate(steps)]     if steps     else None
        checklist_dicts = [{"item": s}                  for s in checklist]                 if checklist else None
        # Build structured plan from parsed prose
        # Start with metadata from tool result if available
        plan_result = sections.get("investigation_plan", {})
        
        
        id_match = re.search(r"Case\s*(?:ID|#)?\s*[:\s]*(\d+)", assistant_text, re.I)
        cid = id_match.group(1) if id_match else req.case_id
        plan_id = plan_result.get("plan_id") or f"PLAN-{cid}-{datetime.now().strftime('%Y%m%d')}"

        investigation_plan = {
            "plan_id":             plan_id,
            "fraud_types":         plan_result.get("fraud_types", []),
            "risk_tier":           plan_result.get("risk_tier", "UNSPECIFIED"),
            "investigation_steps": steps_dicts,
            "evidence_checklist":  checklist_dicts,
            "escalation_criteria": criteria or None,
            "escalation_required": plan_result.get("escalation_required", False)
        }

        try:
           
            validated_plan = InvestigationPlanContract(**investigation_plan)
            investigation_plan = validated_plan.model_dump(exclude_none=True)
        except Exception as e:
            logger.warning(
                f"Investigation plan schema validation failed — storing unvalidated: {e}"
            )


        plan_section = {
            "investigation_plan": investigation_plan
        }

        merged_provenance = merge_provenance(
            case_data.get("provenance_trail", []),
            new_provenance,
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
                    assistant_text,
                    merged_provenance,
                ),
                "meta": {
                    "data_source": data_source,
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
        if req.reload_ai_summary:
            subject_id = (case_data.get("complaint_intelligence") or {}).get("subject_primary_id")
            if subject_id:
                try:
                    enrichment = enrich_graph_context(req.case_id, subject_id, force=True)["result"]
                    case_data["graph_context"] = enrichment["graph_context"]
                    case_data["graph_signals"] = enrichment["graph_signals"]
                    case_data["rules_fired"] = enrichment["rules_fired"]
                    CASE_STORE[req.case_id] = case_data
                    persist_case_session(
                        req.case_id,
                        build_ai_summary(case_data, {}, case_data.get("provenance_trail", [])),
                    )
                    logger.info(
                        "copilot FORCED graph refresh case_id=%s subject_id=%s",
                        req.case_id, subject_id,
                    )
                except (ValueError, GraphUnavailableError, Neo4jError) as exc:
                    logger.warning(
                        "copilot forced graph refresh unavailable for case_id=%s subject_id=%s — %s",
                        req.case_id, subject_id, exc,
                    )
            else:
                logger.warning(
                    "copilot reload_ai_summary=True but no subject_primary_id resolved "
                    "for case_id=%s — nothing to refresh", req.case_id,
                )

        # If the frontend has supplied a human-approved investigation plan, merge it
        # into case_data so the copilot prompt's precedence rule can act on it.
        if req.modified_ai_investigation_plan:
            case_data["modified_ai_investigation_plan"] = req.modified_ai_investigation_plan

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