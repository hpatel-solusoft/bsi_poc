"""
HTTP endpoints for the BSI Fraud Investigation Platform.
Responsibilities: endpoints, CASE_STORE (CS-4), response shaping,
provenance trail extraction and persistence.
Outside its scope: calling appworks_services directly, knowing tool names
or manifest structure directly, or knowing SQL/table schemas for the
PostgreSQL fallback (that lives in core/case_store.py and its repositories).
"""

import logging

# THE ONE place this application configures logging. Every other module
# (appworks/appworks_auth.py included) only ever calls
# logging.getLogger(__name__) — never logging.basicConfig() — because
# basicConfig() configures the ROOT logger for the whole process, and
# only ever takes effect on its FIRST call; every later call is a silent
# no-op. A library/leaf module calling it (appworks_auth.py used to,
# unguarded, at import time) means whichever module happens to be
# imported first wins that race and silently decides the format for
# EVERY log line the entire application ever emits — which is exactly
# what was happening here: appworks_auth's bare
# logging.basicConfig(level=logging.INFO) (no format string) was
# executing before this call ever got a chance to, locking in Python's
# bare default format (just "LEVEL:logger.name:message", no timestamp)
# for the whole app. This call is placed here, as the very first thing
# this module — the actual application entry point — does, specifically
# so it always wins that race regardless of import order elsewhere.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, Generator, List, Optional

import psycopg2
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from neo4j.exceptions import Neo4jError

from agent_service.runner_provider import get_runner
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

# Not called directly by any route body left in this file (all moved to
# api/services/*.py) — kept importable on this module regardless because
# tests still do mock.patch.object(server, "<name>") for each of these.
from api.pipeline_execution import evaluate_cache_staleness  # noqa: F401
from api.pipeline_execution import run_risk_assessment_pipeline  # noqa: F401
from api.pipeline_execution import run_similar_cases_pipeline  # noqa: F401
from api.response_builders import validate_ai_summary_contract
from api.services import (
    copilot_service,
    intake_service,
    plan_service,
    report_service,
    risk_assessment_service,
    similar_cases_service,
)
from api.auth_headers import get_token, get_username
from core import graph_ingest_repository
from core.agent_audit_repository import log_agent_call
from core.case_store import (
    fetch_copilot_history,
    get_cached_investigation_steps,
    get_complaint_number,
    resolve_case_data,
    try_resolve_case_data,
)

# Not called directly by any route body left in this file (all moved to
# api/services/*.py) — kept importable on this module regardless because
# tests still do `server.CASE_STORE[...]` / `server.AGENT_SUMMARY_CACHE_KEY`
# and mock.patch.object(server, "<name>") for the rest.
from core.case_store import AGENT_SUMMARY_CACHE_KEY  # noqa: F401
from core.case_store import CASE_STORE  # noqa: F401
from core.case_store import get_case_ai_summary_cache_updated_at  # noqa: F401
from core.case_store import persist_case_session  # noqa: F401
from core.case_store import resolve_copilot_history  # noqa: F401
from core.case_store import store_copilot_turn  # noqa: F401
from core.db import DatabaseUnavailableError
from core.db import close_pool as close_db_pool
from core.db import init_pool as init_db_pool
from core.investigation_plan_override_repository import (
    delete_override,
    get_override,
    upsert_override,
)
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


def _envelope_complaint_number(case_id: str) -> Optional[str]:
    """
    Resolve the investigator-facing complaint number for a route whose
    response envelope comes straight from a reasoning_layer function
    (reject_inference, revert_rejection, fraud_network, rule_audit) —
    all four deal only in case_id, never case_data, so there is nothing
    to read complaint_number off of without a small lookup here.

    A cheap warm-store-first read (CASE_STORE, then a single Postgres
    SELECT — see core.case_store.try_resolve_case_data), purely for
    display; never touches Neo4j or AppWorks. Returns None on a genuine
    miss (case_id not found anywhere) — callers fall back to case_id in
    that case, same as every other route.
    """
    return get_complaint_number(try_resolve_case_data(case_id) or {})


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
def health(
    username: str = Depends(get_username),
    token: str = Depends(get_token),
):
    """Liveness check — returns ok plus the current server timestamp.

    username/token are accepted (and validated non-blank) for parity with
    every other route, but this endpoint performs no persistence and no
    AppWorks call, so neither value is used for anything beyond that.
    """
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.post("/graph/ingest")
def graph_ingest(
    req: GraphIngestRequest,
    username: str = Depends(get_username),
    token: str = Depends(get_token),
):
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
            username=username,
            token=token,
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
def graph_ingest_status(
    username: str = Depends(get_username),
    token: str = Depends(get_token),
):
    """What is actually in the graph right now, and did the last sync of
    each case succeed. Reads graph_ingest_state (PostgreSQL) — no Neo4j
    call, no LLM. This is the endpoint that answers "why does this case
    show an empty network" without anyone reading server logs.

    username/token are accepted for parity with every other route; this
    is a read-only endpoint, so neither is persisted here.
    """
    return {"cases": graph_ingest_repository.list_states()}


@app.post("/intake")
def intake(
    req: intakeRequest,
    username: str = Depends(get_username),
    token: str = Depends(get_token),
):
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
    return intake_service.run_intake(req, username, token)


@app.post("/similar_cases")
def similar_cases(
    req: SimilarCasesRequest,
    username: str = Depends(get_username),
    token: str = Depends(get_token),
):
    """
    ON-DEMAND — Similar Cases Route (Step 2 in flow).
    Thin HTTP adapter — all business logic lives in
    api.services.similar_cases_service.run_similar_cases (prerequisite
    resolution, staleness check, agent-summary cache lookup, structural-
    matching + LLM-explain pipeline, persistence, response shaping).
    Kept as a real route function (not just `app.post(...)(run_similar_cases)`)
    so /risk_assessment and other in-process callers can keep calling
    `similar_cases(...)` as a plain Python function, same as before this
    refactor.
    """
    return similar_cases_service.run_similar_cases(req, username, token)


@app.post("/risk_assessment")
def risk_assessment(
    req: RiskAssessmentRequest,
    username: str = Depends(get_username),
    token: str = Depends(get_token),
):
    """
    ON-DEMAND — Risk Assessment Route (Step 3 in flow).
    Thin HTTP adapter — all business logic lives in
    api.services.risk_assessment_service.run_risk_assessment
    (prerequisite resolution, staleness check, agent-summary cache
    lookup, LLM agent call + graph risk-signal pipeline, persistence,
    response shaping).
    Kept as a real route function (not just `app.post(...)(run_risk_assessment)`)
    so /plan, /reload_all, and other in-process callers can keep calling
    `risk_assessment(...)` as a plain Python function, same as before
    this refactor.
    """
    return risk_assessment_service.run_risk_assessment(req, username, token)


@app.post("/plan")
def plan(
    req: PlanRequest,
    username: str = Depends(get_username),
    token: str = Depends(get_token),
):
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
    return plan_service.run_plan(req, username, token)

@app.post("/reload_all", response_model=ReloadAllResponse)
def reload_all(
    req: ReloadAllRequest,
    username: str = Depends(get_username),
    token: str = Depends(get_token),
) -> ReloadAllResponse:
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
    steps, overall_status, overall_duration = _run_reload_all_to_completion(req, username, token)
    return ReloadAllResponse(
        complaint_number=_envelope_complaint_number(req.case_id) or req.case_id,
        status=overall_status,
        duration_seconds=overall_duration,
        steps=steps,
    )


# Present-continuous "in progress" copy paired with its past-tense "done"
# counterpart, per step — used only by /reload_all/stream's SSE framing
# below. /reload_all itself (JSON) has no use for human-readable prose;
# its ReloadStepResult.status ("success"/"failed"/"skipped") is already
# the machine-readable signal a caller renders its own copy from.
_RELOAD_STEP_MESSAGES: Dict[str, Dict[str, str]] = {
    "graph_ingest": {
        "running": "Syncing case data from AppWorks…",
        "success": "Case data synced.",
        "failed": "AppWorks sync failed.",
    },
    "intake": {
        "running": "Running intake agent…",
        "success": "Case intake complete.",
        "failed": "Intake failed.",
    },
    "similar_cases": {
        "running": "Searching for similar cases…",
        "success": "Similar cases identified.",
        "failed": "Similar case search failed.",
    },
    "risk_assessment": {
        "running": "Calculating risk assessment…",
        "success": "Risk assessment complete.",
        "failed": "Risk assessment failed.",
    },
    "plan": {
        "running": "Generating investigation plan…",
        "success": "Investigation plan ready.",
        "failed": "Plan generation failed.",
    },
}
_RELOAD_STEP_SKIPPED_MESSAGE = "Skipped — an earlier step failed."
_RELOAD_ALL_COMPLETE_MESSAGE = "Reload complete."


def _reload_all_step_calls(
    req: "ReloadAllRequest", username: str, token: str
) -> List[tuple]:
    """
    THE five-step call list /reload_all runs, in dependency order — the
    single source of truth both the JSON route (_run_reload_all_to_completion)
    and the SSE route (_run_reload_all_streaming) iterate over, so the two
    can never drift apart on which steps run, in what order, or with what
    parameters. See POST /reload_all's own docstring for why this exact
    order (graph_ingest -> intake -> similar_cases -> risk_assessment ->
    plan) and why a failure stops the rest rather than pushing on.

    Order matters: each callable is only invoked once its turn comes, so
    a request object for step N is never built (and never touches
    case_data) before step N-1 has actually completed.
    """
    return [
        (
            "graph_ingest",
            lambda: graph_ingest(
                GraphIngestRequest(case_ids=[req.case_id], run_rules=True),
                username=username,
                token=token,
            ),
        ),
        (
            "intake",
            lambda: intake(
                intakeRequest(case_id=req.case_id, reload_ai_summary=True),
                username=username,
                token=token,
            ),
        ),
        (
            "similar_cases",
            lambda: similar_cases(
                SimilarCasesRequest(case_id=req.case_id, reload_ai_summary=True),
                username=username,
                token=token,
            ),
        ),
        (
            "risk_assessment",
            lambda: risk_assessment(
                RiskAssessmentRequest(case_id=req.case_id, reload_ai_summary=True),
                username=username,
                token=token,
            ),
        ),
        (
            "plan",
            lambda: plan(
                PlanRequest(case_id=req.case_id, reload_ai_summary=True),
                username=username,
                token=token,
            ),
        ),
    ]


def _run_reload_all_steps(
    req: "ReloadAllRequest", username: str, token: str
) -> Generator[Dict[str, Any], None, None]:
    """
    THE step-execution engine behind both POST /reload_all (JSON,
    collects every event below into one response) and POST
    /reload_all/stream (SSE, formats and forwards each event to the
    client as it happens). This function contains the actual business
    logic — call order, the stop-on-first-failure rule, what counts as
    success/failure/skip, and every persistence side effect (which all
    happen inside the called route functions, not here) — exactly once,
    so the two routes cannot silently diverge in behaviour the way two
    independently-maintained copies of the same 40-line loop eventually
    would. See POST /reload_all's own docstring for the full rationale
    (why graph_ingest runs first, why a failure stops the rest, why this
    always returns 200-shaped data rather than raising).

    Yields one dict per step-lifecycle transition, in order:
      {"type": "running", "step": str, "step_index": int, "total_steps": int}
          — about to call this step. NEVER yielded for a step that gets
          skipped (skipped steps were never started).
      {"type": "done", "step": str, "step_index": int, "total_steps": int,
       "result": ReloadStepResult}
          — this step finished, one way or another; result.status is
          "success", "failed", or "skipped".
    ...then exactly one final event once every step has been accounted for:
      {"type": "summary", "steps": List[ReloadStepResult],
       "overall_status": str, "overall_duration": float}

    Every consumer MUST exhaust this generator (a plain `for` loop with
    no early `break`) — the audit log_agent_call row for the bulk action
    itself is written as a side effect of producing the "summary" event,
    so an early-abandoned iteration (e.g. a client disconnecting mid-SSE-
    stream) means that row silently never gets written. This mirrors the
    existing per-step log_agent_call calls already made by each step's
    own route function, which have the same property.
    """
    start = time.time()
    steps: List[ReloadStepResult] = []
    stopped = False

    step_calls = _reload_all_step_calls(req, username, token)
    total_steps = len(step_calls)

    for step_index, (step_name, call_step) in enumerate(step_calls, start=1):
        if stopped:
            result = ReloadStepResult(step=step_name, status="skipped", duration_seconds=0.0)
            steps.append(result)
            yield {"type": "done", "step": step_name, "step_index": step_index, "total_steps": total_steps, "result": result}
            continue

        yield {"type": "running", "step": step_name, "step_index": step_index, "total_steps": total_steps}

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
            result = ReloadStepResult(step=step_name, status="failed", duration_seconds=duration, error=str(exc.detail))
            steps.append(result)
            stopped = True
            yield {"type": "done", "step": step_name, "step_index": step_index, "total_steps": total_steps, "result": result}
            continue
        except Exception as exc:  # noqa: BLE001 — isolate one step's failure from the rest
            duration = round(time.time() - step_start, 1)
            logger.exception("reload_all STEP FAILED case_id=%s step=%s", req.case_id, step_name)
            result = ReloadStepResult(step=step_name, status="failed", duration_seconds=duration, error=str(exc))
            steps.append(result)
            stopped = True
            yield {"type": "done", "step": step_name, "step_index": step_index, "total_steps": total_steps, "result": result}
            continue

        duration = round(time.time() - step_start, 1)
        step_meta = (step_response.get("details") or {}).get("meta") or {}
        result = ReloadStepResult(
            step=step_name,
            status="success",
            duration_seconds=duration,
            agent_summary_source=step_meta.get("agent_summary_source"),
            stale=step_meta.get("stale"),
        )
        steps.append(result)
        yield {"type": "done", "step": step_name, "step_index": step_index, "total_steps": total_steps, "result": result}

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
        username=username,
    )
    yield {"type": "summary", "steps": steps, "overall_status": overall_status, "overall_duration": overall_duration}


def _run_reload_all_to_completion(
    req: "ReloadAllRequest", username: str, token: str
) -> tuple:
    """
    Drive _run_reload_all_steps to its "summary" event and return
    (steps, overall_status, overall_duration) — what POST /reload_all's
    JSON response is built from. The "running"/"done" events along the
    way carry nothing this caller needs (it already gets the accumulated
    `steps` list from "summary"); only /reload_all/stream's SSE framing
    cares about them individually.
    """
    for event in _run_reload_all_steps(req, username, token):
        if event["type"] == "summary":
            return event["steps"], event["overall_status"], event["overall_duration"]
    # Unreachable: _run_reload_all_steps always yields exactly one
    # "summary" event as its last item. Guarded anyway rather than
    # letting a caller silently receive None — a generator whose
    # contract was violated should fail loudly, not produce a
    # confusing downstream AttributeError.
    raise RuntimeError("_run_reload_all_steps ended without yielding a summary event — this is a bug.")


def _sse_event(event: str, data: Dict[str, Any]) -> str:
    """
    Format one Server-Sent Event: an `event:` line naming this event's
    type, a `data:` line carrying its JSON payload, and the blank line
    that terminates it per the SSE wire format. json.dumps with no
    `indent` is required here, not just tidier — SSE frames on newlines,
    so a multi-line payload would be parsed as multiple, broken events.
    """
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def _run_reload_all_streaming(req: "ReloadAllRequest", username: str, token: str) -> Generator[str, None, None]:
    """
    SSE framing layer for POST /reload_all/stream: consumes
    _run_reload_all_steps' event stream and formats each one as a
    Server-Sent Event, using _RELOAD_STEP_MESSAGES for the human-readable
    `message` field. Carries no business logic of its own — every
    call-order/failure/skip decision already happened in
    _run_reload_all_steps, exactly once, shared with POST /reload_all.

    Event types written to the client, one SSE `event:` per line below:
      progress — a step started, or a step finished (successfully or
                 skipped). data: {step, step_index, total_steps, status,
                 message[, duration_seconds]}
      error    — a step finished by failing. Same shape as progress,
                 plus `error` (the failure detail) — a distinct SSE event
                 name so a client can wire a single `.addEventListener`
                 for failures without inspecting `status` on every
                 progress event.
      complete — exactly one, always last: the identical JSON body POST
                 /reload_all itself would have returned, so a client
                 that only cares about the final result (not live
                 progress) can ignore every progress/error event and
                 just read this one.
    """
    for event in _run_reload_all_steps(req, username, token):
        if event["type"] == "running":
            step = event["step"]
            message = _RELOAD_STEP_MESSAGES.get(step, {}).get("running", f"Running {step}…")
            yield _sse_event(
                "progress",
                {
                    "step": step,
                    "step_index": event["step_index"],
                    "total_steps": event["total_steps"],
                    "status": "running",
                    "message": message,
                },
            )

        elif event["type"] == "done":
            step = event["step"]
            result: ReloadStepResult = event["result"]
            data = {
                "step": step,
                "step_index": event["step_index"],
                "total_steps": event["total_steps"],
                "status": result.status,
                "duration_seconds": result.duration_seconds,
            }
            if result.status == "skipped":
                data["message"] = _RELOAD_STEP_SKIPPED_MESSAGE
                yield _sse_event("progress", data)
            elif result.status == "failed":
                data["message"] = _RELOAD_STEP_MESSAGES.get(step, {}).get("failed", f"{step} failed.")
                data["error"] = result.error
                yield _sse_event("error", data)
            else:  # "success"
                data["message"] = _RELOAD_STEP_MESSAGES.get(step, {}).get("success", f"{step} complete.")
                yield _sse_event("progress", data)

        elif event["type"] == "summary":
            response = ReloadAllResponse(
                complaint_number=_envelope_complaint_number(req.case_id) or req.case_id,
                status=event["overall_status"],
                duration_seconds=event["overall_duration"],
                steps=event["steps"],
            )
            payload = json.loads(response.model_dump_json())
            payload["message"] = _RELOAD_ALL_COMPLETE_MESSAGE
            yield _sse_event("complete", payload)


@app.post("/reload_all/stream")
def reload_all_stream(
    req: ReloadAllRequest,
    username: str = Depends(get_username),
    token: str = Depends(get_token),
) -> StreamingResponse:
    """
    Streaming counterpart to POST /reload_all: identical request
    contract (same ReloadAllRequest body, same headers), and the exact
    same five-step engine (_run_reload_all_steps) — the only difference
    is that this route reports each step's progress live, via
    Server-Sent Events, instead of making the caller wait for one JSON
    response at the end. POST /reload_all itself is untouched and keeps
    working exactly as it did before this route existed; a frontend
    that doesn't need live progress has no reason to switch.

    Response is `text/event-stream` — see _run_reload_all_streaming's
    docstring for the three event types written (progress / error /
    complete) and their JSON shapes.

    Cache-Control: no-cache and X-Accel-Buffering: no are both here for
    the same reason: several layers between this process and the
    browser default to BUFFERING a response before forwarding it (some
    HTTP caches; nginx specifically, if this ever sits behind one, which
    is a common enterprise deployment shape) — which would silently
    defeat the entire point of streaming, delivering everything at once
    right before the connection closes instead of live. Both headers
    tell every such layer this response must be forwarded as it's
    written, not buffered.
    """
    return StreamingResponse(
        _run_reload_all_streaming(req, username, token),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.post("/plan/modify_investigation_steps", response_model=ModifyInvestigationStepsResponse)
def modify_investigation_steps(
    req: ModifyInvestigationStepsRequest,
    username: str = Depends(get_username),
    token: str = Depends(get_token),
) -> ModifyInvestigationStepsResponse:
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
            username=username,
        )
        log_agent_call(
            case_id=req.case_id,
            agent_name="investigation_plan_override",
            endpoint="/plan/modify_investigation_steps",
            latency_ms=int((time.time() - start) * 1000),
            status="success",
            username=username,
        )
        return ModifyInvestigationStepsResponse(
            complaint_number=_envelope_complaint_number(req.case_id) or req.case_id,
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
            username=username,
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
def revert_to_ai_plan(
    req: RevertToAiPlanRequest,
    username: str = Depends(get_username),
    token: str = Depends(get_token),
) -> RevertToAiPlanResponse:
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
            username=username,
        )
        return RevertToAiPlanResponse(
            complaint_number=_envelope_complaint_number(req.case_id) or req.case_id,
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
            username=username,
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
def get_investigation_steps(
    case_id: str,
    username: str = Depends(get_username),
    token: str = Depends(get_token),
) -> InvestigationStepsResponse:
    """
    ON-DEMAND — read-only fetch of the current investigation_steps for
    case_id.

    username/token are accepted for parity with every other route; this
    is a read-only endpoint, so neither is persisted here.

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
            complaint_number=_envelope_complaint_number(case_id) or case_id,
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
        complaint_number=_envelope_complaint_number(case_id) or case_id,
        investigation_steps=investigation_steps,
        is_modify_investigation_steps=False,
    )


@app.post("/generate_report")
def generate_report(
    req: ReportGenerationRequest,
    username: str = Depends(get_username),
    token: str = Depends(get_token),
):
    """
    ON-DEMAND — Report Generation Route (AI-18). Thin HTTP adapter —
    all business logic lives in
    api.services.report_service.run_generate_report (Related Network
    assembly, plan-override read, report cache, Decision & Override Log
    assembly, LLM agent call, persistence, response shaping).
    """
    return report_service.run_generate_report(req, username, token)

@app.post("/generate_report/pdf")
def generate_report_pdf(
    req: ReportGenerationRequest,
    username: str = Depends(get_username),
    token: str = Depends(get_token),
):
    """
    ON-DEMAND — Report PDF Export Route. Thin HTTP adapter — all
    business logic lives in
    api.services.report_service.run_generate_report_pdf, a deliberate
    duplicate of run_generate_report's pipeline (see that function's own
    docstring for why), ending in a rendered PDF Response instead of a
    JSON dict.
    """
    return report_service.run_generate_report_pdf(req, username, token)

@app.post("/reject_inference", response_model=RejectInferenceResponse)
def reject_inference_route(
    req: RejectInferenceRequest,
    username: str = Depends(get_username),
    token: str = Depends(get_token),
) -> RejectInferenceResponse:
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
            username=username,
        )
        result = envelope["result"]
        result["complaint_number"] = _envelope_complaint_number(req.case_id) or req.case_id
        result.pop("case_id", None)
        return RejectInferenceResponse(**result)
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
            username=username,
        )
        raise HTTPException(status_code=502, detail=f"Could not reach the graph: {exc}") from exc
    finally:
        logger.info("POST /reject_inference completed for case_id=%s", req.case_id)


@app.post("/revert_rejection", response_model=RevertRejectionResponse)
def revert_rejection_route(
    req: RevertRejectionRequest,
    username: str = Depends(get_username),
    token: str = Depends(get_token),
) -> RevertRejectionResponse:
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
            username=username,
        )
        result = envelope["result"]
        result["complaint_number"] = _envelope_complaint_number(req.case_id) or req.case_id
        result.pop("case_id", None)
        return RevertRejectionResponse(**result)
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
            username=username,
        )
        raise HTTPException(status_code=502, detail=f"Could not reach the graph: {exc}") from exc
    finally:
        logger.info("POST /revert_rejection completed for case_id=%s", req.case_id)


@app.get("/fraud_network/{case_id}", response_model=FraudNetworkResponse)
def fraud_network_route(
    case_id: str,
    username: str = Depends(get_username),
    token: str = Depends(get_token),
) -> FraudNetworkResponse:
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

    username/token are accepted for parity with every other route; this
    is a read-only endpoint, so neither is persisted here.
    """
    try:
        envelope = get_fraud_network(case_id)
        result = envelope["result"]
        result["complaint_number"] = _envelope_complaint_number(case_id) or case_id
        result.pop("case_id", None)
        return FraudNetworkResponse(**result)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (GraphUnavailableError, Neo4jError) as exc:
        logger.exception("fraud_network FAILED for case_id=%s", case_id)
        raise HTTPException(status_code=502, detail=f"Could not reach the graph: {exc}") from exc
    finally:
        logger.info("GET /fraud_network completed for case_id=%s", case_id)


@app.get("/rule_audit/{case_id}", response_model=RuleAuditResponse)
def rule_audit_route(
    case_id: str,
    username: str = Depends(get_username),
    token: str = Depends(get_token),
) -> RuleAuditResponse:
    """
    D4 — Rule Audit / Inference Explainability. Read-only, no LLM. The
    prerequisite view for D2: an investigator reviews everything a case
    inferred, with full provenance, before deciding what to reject.

    username/token are accepted for parity with every other route; this
    is a read-only endpoint, so neither is persisted here.
    """
    try:
        envelope = get_rule_audit(case_id)
        result = envelope["result"]
        result["complaint_number"] = _envelope_complaint_number(case_id) or case_id
        result.pop("case_id", None)
        return RuleAuditResponse(**result)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (GraphUnavailableError, Neo4jError) as exc:
        logger.exception("rule_audit FAILED for case_id=%s", case_id)
        raise HTTPException(status_code=502, detail=f"Could not reach the graph: {exc}") from exc
    finally:
        logger.info("GET /rule_audit completed for case_id=%s", case_id)


@app.post("/copilot")
def copilot(
    req: CopilotRequest,
    username: str = Depends(get_username),
    token: str = Depends(get_token),
):
    """
    ON-DEMAND — Copilot Route (Step 5 in flow).
    Thin HTTP adapter — all business logic lives in
    api.services.copilot_service.run_copilot (CS-4 resolution, override
    merge, conversation-history resolution, LLM agent call, provenance/
    citation assembly, persistence, response shaping).
    Kept as a real route function (not just `app.post(...)(run_copilot)`)
    for the same reason as every other extracted route: any in-process
    caller (and the existing tests) can keep calling `copilot(...)` as a
    plain Python function, same as before this refactor.
    """
    return copilot_service.run_copilot(req, username, token)


@app.get("/copilot/{case_id}", response_model=ConversationHistoryResponse)
def get_conversation_history(
    case_id: str,
    username: str = Depends(get_username),
    token: str = Depends(get_token),
):
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

    username/token are accepted for parity with every other route; this
    is a read-only endpoint, so neither is persisted here.
    """
    try:
        conversation_history, history_source = fetch_copilot_history(case_id)
        logger.info(
            "GET /conversation_history case_id=%s source=%s turns=%d",
            case_id,
            history_source,
            len(conversation_history),
        )
        # Cheap warm-store-first lookup (CASE_STORE, then a single
        # Postgres SELECT) purely to resolve the investigator-facing
        # identifier — see core.case_store.get_complaint_number. No
        # AppWorks/LLM call here; this route stays read-only.
        case_data_for_display = try_resolve_case_data(case_id) or {}
        return {
            "complaint_number": get_complaint_number(case_data_for_display) or case_id,
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