"""
HTTP endpoints for the BSI Fraud Investigation Platform.
Responsibilities: endpoints, CASE_STORE (CS-4), response shaping,
provenance trail extraction and persistence.
Outside its scope: calling appworks_services directly, knowing tool names
or manifest structure directly.
"""

import logging
import os
import re
import time
from agent_service.agent_runner import BSIAgentRunner
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any
from fastapi import FastAPI, HTTPException
from core.case_store import CASE_STORE, store_copilot_turn, resolve_copilot_history
from dotenv import load_dotenv
from semantic_layer.entity_contracts import InvestigationPlan as InvestigationPlanContract
from api.models import InvestigateRequest, RiskAssessmentRequest, SimilarCasesRequest, PlanRequest, CopilotRequest
from agent_service.prompt_builders import (
    build_investigate_system_prompt,
    build_risk_assessment_prompt,
    build_plan_prompt,
    build_similar_cases_prompt,
    build_copilot_prompt,
)
from api.response_builders import (
    validate_ai_summary_contract,
    render_markdown_html_with_sources,
    parse_bsi_section, 
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

# -----------------------------------------------------------------------
# CS-4: Case session context — in-memory for POC with no TTL.
# Entries live for the lifetime of the server process.
# Falls back to ai_summary sent in the request body only if the server has restarted.
# ai_summary is a REQUIRED field on all ON-DEMAND requests (v6 spec).
# -----------------------------------------------------------------------

# POC requirement (MD v6): in-memory CS-4 has no TTL.


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

def _resolve_case_store(case_id: str, ai_summary: Optional[Dict[str, Any]]) -> dict:
    """
    CS-4 lookup pattern used by all ON-DEMAND handlers.
    Prioritizes ai_summary from request body as the absolute source of truth (v6).
    Updates CASE_STORE for persistence but always returns the fresh data from the request.
    """
    if ai_summary:
        validate_ai_summary_contract(ai_summary)
        
        # Build fresh case_data from input
        case_data = {**ai_summary.get("investigation", {})}
        
        # Pull top-level on-demand sections
        for key in ["similar_cases", "risk_assessment", "investigation_plan"]:
            if key in ai_summary:
                case_data[key] = ai_summary[key]
        
        case_data["provenance_trail"] = ai_summary.get("provenance_trail", [])
        
        # Update persistence store
        CASE_STORE[case_id] = case_data
        return case_data

    # Fallback to store only if request body is empty
    if case_id in CASE_STORE and CASE_STORE[case_id]:
        return CASE_STORE[case_id]

    raise HTTPException(
        status_code=400,
        detail=f"Case {case_id} session data not found. Provide ai_summary in request body."
    )

# -----------------------------------------------------------------------
# Endpoints
# -----------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.post("/investigate")
def investigate(req: InvestigateRequest):
    """
    AUTO flow — Section 3.1.
    Runs AUTO tools 1-2 (intake, enrichment) in dependency order
    (LLM decides sequence). Similar cases runs via /similar_cases.
    Populates CS-4 CASE_STORE for all subsequent on-demand calls.
    Flow: /investigate → /similar_cases → /risk_assessment → /plan → /copilot | 
    """
    start = time.time()
    try:
        if not os.getenv("OPENAI_API_KEY"):
            raise HTTPException(status_code=500, detail="OPENAI_API_KEY not configured")
        

        runner = _get_runner()
        # Scope to intake + enrichment only; similar cases is a separate route.
        
        messages, provenance_trail, _ = runner.run_scoped(
            system_prompt=build_investigate_system_prompt(),
            user_message=(
                f"Investigate case {req.case_id}."
            ),
            scope="CASE_SUMMARY",  # ← this scope includes intake + enrichment tools only; 
        )
        sections = extract_tool_results(messages, runner.dispatcher.tool_to_section)

        # CS-4: populate store with all sections + provenance.
        CASE_STORE[req.case_id] = {**sections, "provenance_trail": provenance_trail}

        # ── Response split (v6 spec) ────────────────────────────────────────
        # ai_summary: the contract object passed to the next route in the flow.
        # Flow: /investigate → /similar_cases → /risk_assessment → /plan → /copilot | 
        # details: human-readable narrative + meta — NOT required by downstream.
        # ──────────────────────────────────────────────────────────────────────
        ai_summary = {
            "investigation":    sections,
            "provenance_trail": provenance_trail,
        }

        # BSI requirement: swap internal case_id for business complaint_no in narrative
    
        return {
            "case_id":    req.case_id,
            "status":     "completed",
            "ai_summary": ai_summary,          # ← pass this object to /similar_cases
            "details": {
                "agent_summary": render_markdown_html_with_sources(extract_agent_summary(messages), provenance_trail),
                "meta": {
                    "tool_calls_made":  len(provenance_trail),
                    "duration_seconds": round(time.time() - start, 1),
                },
            },
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Investigate route failed for case_id=%s", req.case_id)
        raise HTTPException(status_code=500, detail=f"Investigation failed: {exc}") from exc
    finally:
        logger.info("POST /investigate completed for case_id=%s", req.case_id)



@app.post("/similar_cases")
def similar_cases(req: SimilarCasesRequest):
    """
    ON-DEMAND — Similar Cases Route (Step 2 in flow).
    Calls search_similar_cases to find historical cases with matching fraud patterns.
    Requires case_data from a prior /investigate run (via CS-4 or ai_summary body).
    Explains historical case matches, pattern relevance, and archive findings.
    Flow: /investigate → /similar_cases → /risk_assessment → /plan → /copilot | 
    """
    

    try:
        
        # CS-4 pattern (v6): warm lookup or re-hydrate from ai_summary.
        
        case_data = _resolve_case_store(req.case_id, req.ai_summary)
        logger.info(f"Case data resolved for case_id={req.case_id}: Key length: {len(list(case_data.keys()))}")
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

        # Update CS-4 but return only the route-specific section.
        CASE_STORE[req.case_id].update(similar_section)
        CASE_STORE[req.case_id]["provenance_trail"] = merged_provenance

        # ai_summary: updated contract — investigation sections with similar cases.
        # Pass this object to /risk_assessment.
        ai_summary = build_ai_summary(
            case_data,
            {"similar_cases": similar_cases_data},
            merged_provenance,
        )
        
        
        logger.info(f"SIMILAR CASES NARRATIVE TOTAL KEYs: {len(similar_cases_data)}") 
        return {
            "case_id":    req.case_id,
            "status":     "completed",
            "ai_summary": ai_summary,          # ← pass this object to /risk_assessment
            "details": {
                "agent_summary": render_markdown_html_with_sources(agent_summary,merged_provenance),
            },
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Similar cases route failed for case_id=%s", req.case_id)
        raise HTTPException(status_code=500, detail=f"Similar cases analysis failed: {exc}") from exc
    finally:
        logger.info("POST /similar_cases completed for case_id=%s", req.case_id)


@app.post("/risk_assessment")
def risk_assessment(req: RiskAssessmentRequest):
    """
    ON-DEMAND — Risk Assessment Route (Step 3 in flow).
    Calls get_risk_rules and calculate_risk_metrics.
    Requires case_data from a prior /investigate + /similar_cases run
    (via CS-4 or ai_summary body).
    Explains case seriousness, triggered rules, and escalation thresholds.
    Flow: /investigate → /similar_cases → /risk_assessment → /plan → /copilot | 
    """
    
    try:
        
        # CS-4 pattern (v6): warm lookup or re-hydrate from ai_summary.
        case_data = _resolve_case_store(req.case_id, req.ai_summary)
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

        # Update CS-4 but return only the route-specific section.
        CASE_STORE[req.case_id].update(risk_section)
        CASE_STORE[req.case_id]["provenance_trail"] = merged_provenance

        ai_summary = build_ai_summary(
            case_data,
            {"risk_assessment": risk_assessment},
            merged_provenance,
        )

        return {
            "case_id":    req.case_id,
            "status":     "completed",
            "ai_summary": ai_summary,          # ← pass this object to /plan
            "details": {
                "agent_summary": render_markdown_html_with_sources(extract_agent_summary(messages), merged_provenance),
            },
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Risk assessment route failed for case_id=%s", req.case_id)
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
    Flow: /investigate → /similar_cases → /risk_assessment → /plan → /copilot | 
    """
    try:
        
        # CS-4 pattern (v6): warm lookup or re-hydrate from ai_summary.
        case_data = _resolve_case_store(req.case_id, req.ai_summary)
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

        # Update CS-4 but return only the route-specific section.
        CASE_STORE[req.case_id].update(plan_section)
        CASE_STORE[req.case_id]["provenance_trail"] = merged_provenance

        # ai_summary: updated contract — investigation sections separate from plan.
        # Pass this object to  and /copilot.
        ai_summary = build_ai_summary(
            case_data,
            {"investigation_plan": investigation_plan},
            merged_provenance,
        )

        return {
            "case_id":    req.case_id,
            "status":     "completed",
            "ai_summary": ai_summary,          # ← pass this object to , /copilot
            "details": {
                "agent_summary": render_markdown_html_with_sources(
                    assistant_text,
                    merged_provenance,
                ),
            },
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Plan route failed for case_id=%s", req.case_id)
        raise HTTPException(status_code=500, detail=f"Plan generation failed: {exc}") from exc
    finally:
        logger.info("POST /plan completed for case_id=%s", req.case_id)

@app.post("/copilot")
def copilot(req: CopilotRequest):
    """
    ON-DEMAND — Copilot Route (Step 5 in flow).
    Answers investigator questions grounded in case context (CS-5).
    Answers from CS-4 context first; falls back to tools only if needed.
    ai_summary is REQUIRED per v6 spec — server decides which source to use.
    If provenance_trail is absent from ai_summary, source citations degrade
    gracefully — no crash.
    Flow: /investigate → /similar_cases → /risk_assessment → /plan → /copilot 
    """
    try:
        
     
        # CS-4 pattern (v6): warm lookup or re-hydrate from ai_summary
        cs4_warm = req.case_id in CASE_STORE
        case_data = _resolve_case_store(req.case_id, req.ai_summary)

        # If the frontend has supplied a human-approved investigation plan, merge it
        # into case_data so the copilot prompt's precedence rule can act on it.
        if req.modified_ai_investigation_plan:
            case_data["modified_ai_investigation_plan"] = req.modified_ai_investigation_plan

        conversation_history = resolve_copilot_history(
            req.case_id,
            req.conversation_history,
        )

        runner = _get_runner()

        messages, new_provenance_trail, tool_call_log = runner.run_scoped(
            system_prompt=build_copilot_prompt(req.case_id, case_data),
            user_message=req.question,
            conversation_history=conversation_history,
        )
        
        answer = extract_agent_summary(messages)
        updated_conversation_history = store_copilot_turn(
            req.case_id,
            req.question,
            answer,
        )

        # sources_cited: include the stored provenance trail from CS-4 (so context-
        # grounded answers cite the original AppWorks sources) plus any new tool
        # calls made during this copilot turn.
        # This aligns with Section 3.4 where the response shows sources from the
        # original investigation even when tool_calls_made = 0.
        stored_provenance = case_data.get("provenance_trail", [])
        combined_provenance = merge_provenance(stored_provenance, new_provenance_trail)

        sources_cited = [
            # f"{p['tool']} — {p.get('computed_by', '')} — "
            f"retrieved {p.get('retrieved_at', '')}"
            for p in combined_provenance
        ]
        sources_cited_details = [
            {
                # "tool": p.get("tool", ""),
                "computed_by": p.get("computed_by", ""),
                "retrieved_at": p.get("retrieved_at", ""),
                "sources": p.get("sources", []),
            }
            for p in combined_provenance
        ]

        # CS-4: Update store only if the case entry still exists (it may have
        # been evicted if TTL expires between _resolve_case_store and here).
        if new_provenance_trail and req.case_id in CASE_STORE:
            new_sections = extract_tool_results(messages,runner.dispatcher.tool_to_section)
            CASE_STORE[req.case_id].update(new_sections)
            CASE_STORE[req.case_id]["provenance_trail"] = combined_provenance

        return {
            "answer":               render_markdown_html(answer),
            "sources_cited":        sources_cited,
            "sources_cited_details": sources_cited_details,
            "provenance_trail":     combined_provenance,
            "conversation_history":  updated_conversation_history,
            # "tool_calls_made":      len(new_provenance_trail),
            "cs4_source":           "warm" if cs4_warm else "rehydrated",
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Copilot route failed for case_id=%s", req.case_id)
        raise HTTPException(status_code=500, detail=f"Copilot failed: {exc}") from exc
    finally:
        logger.info("POST /copilot completed for case_id=%s", req.case_id)
