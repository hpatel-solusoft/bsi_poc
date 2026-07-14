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
    store_copilot_turn,
    resolve_copilot_history,
    resolve_case_data,
    persist_case_session,
)
from core.agent_audit_repository import log_agent_call
from core.db import init_pool as init_db_pool, close_pool as close_db_pool, DatabaseUnavailableError
from dotenv import load_dotenv
from semantic_layer.entity_contracts import InvestigationPlan as InvestigationPlanContract
from api.models import intakeRequest, RiskAssessmentRequest, SimilarCasesRequest, PlanRequest, CopilotRequest
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
    merge_provenance, )        

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


@app.on_event("shutdown")
def _close_agent_operational_store() -> None:
    """Release pooled PostgreSQL connections on shutdown."""
    close_db_pool()

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
# Endpoints
# -----------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.post("/intake")
def intake(req: intakeRequest):
    """
    AUTO flow — Section 3.1.
    Runs AUTO tools 1-2 (intake, enrichment) in dependency order
    (LLM decides sequence). Similar cases runs via /similar_cases.
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
        
        
        
        # --- EXPLICIT DEPENDENCY INJECTION ---
        # We package the backend state into a generic execution_context
        execution_context = {"ai_summary": req.ai_summary}
        # -----------------------------------

        messages, new_provenance, _ = runner.run_scoped(
            system_prompt=build_similar_cases_prompt(case_data),
            user_message=(
                f"Review the case data for case {req.case_id} and execute the "
                "appropriate tools to search for similar historical cases and explain "
                "the pattern matches found."
            ),
            scope="SIMILAR_CASES",  # ← this scope includes intake + enrichment tools only
            execution_context=execution_context
        )

        sections = extract_tool_results(messages, runner.dispatcher.tool_to_section)
        
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
            "conversation_history_source": history_source,
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