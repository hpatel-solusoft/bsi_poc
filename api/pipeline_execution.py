"""
Route-level pipeline execution, factored out of api/server.py.

Each function here owns exactly the block of route logic that used to sit
between `sections = extract_tool_results(...)` (or, for /similar_cases,
the equivalent point where the route's own case context is ready) and the
`CASE_STORE[req.case_id] = ...` / `CASE_STORE[req.case_id].update(...)`
line in that route. That block is the route's direct, non-LLM pipeline
work: network-match / context-enrichment / structural-matching / graph
risk-signal calls into reasoning_layer, plus the section/provenance
assembly that depends on their results.

server.py keeps: request validation, runner.run_scoped (the LLM call),
CASE_STORE / ai_summary persistence, logging, and response shaping.
This module owns: what happens to the tool/LLM output in between.
"""

import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from neo4j.exceptions import Neo4jError

from reasoning_layer.neo4j_client import GraphUnavailableError
from reasoning_layer.graph_queries import check_network_match
from reasoning_layer.context_enrichment import enrich_graph_context
from reasoning_layer.similar_cases import find_structural_matches
from reasoning_layer.risk_signals import apply_graph_risk_signals
from reasoning_layer.investigation_tasks import build_rule_aware_tasks, tag_step_sources

from semantic_layer.entity_contracts import InvestigationPlan as InvestigationPlanContract

from core.investigation_plan_override_repository import get_override, compute_plan_staleness

from api.message_utils import extract_agent_summary, merge_provenance, merge_direct_result
from api.response_builders import parse_bsi_section

logger = logging.getLogger(__name__)


def run_intake_direct_pipeline(
    case_id: str,
    reload_ai_summary: bool,
    sections: Dict[str, Any],
    provenance_trail: List[dict],
) -> Tuple[Dict[str, Any], List[dict]]:
    """
    /intake direct pipeline work (Section 8.1 AI-12, Section 9.1 AI-13).
    subject_primary_id was injected into complaint_intelligence by
    extract_tool_results before this is called.
    Mutates and returns (sections, provenance_trail).
    """
    subject_id = (sections.get("complaint_intelligence") or {}).get("subject_primary_id")
    if not subject_id:
        logger.warning(
            "context enrichment + network match skipped for case_id=%s — "
            "no subject_primary_id resolved", case_id,
        )
        return sections, provenance_trail

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
            case_id, subject_id, exc,
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
        # force=reload_ai_summary: when True this bypasses Principle 10
        # and makes the reasoning pipeline re-run for this (case, subject)
        # even though it may already have completed, updating
        # pipeline_execution_state (PostgreSQL) and the Neo4j graph rather
        # than returning the cached rules_fired.
        enrichment = enrich_graph_context(
            case_id, subject_id, force=reload_ai_summary,
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
            case_id, subject_id, exc,
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

    return sections, provenance_trail


def run_similar_cases_pipeline(
    case_id: str,
    case_data: Dict[str, Any],
    runner,
    build_similar_cases_prompt,
) -> Tuple[str, Dict[str, Any], Dict[str, Any], List[dict]]:
    """
    /similar_cases direct + LLM-explain pipeline work (Section 8.3 AI-14,
    Section 9.2). Returns (agent_summary, similar_cases_data, similar_section,
    merged_provenance).
    """
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
        structural = find_structural_matches(case_id)["result"]
    except (ValueError, GraphUnavailableError, Neo4jError) as exc:
        logger.warning(
            "structural similar-case matching unavailable for case_id=%s — %s",
            case_id, exc,
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
            f"identified for case {case_id}: why each one matched "
            f"(see match_reasons) and how relevant its pattern is. Do not "
            f"add or remove cases; the graph has already decided the matches."
        ),
        scope="SIMILAR_CASES",
    )

    # The authoritative similar_cases section is the DETERMINISTIC
    # structural result, not anything the LLM produced — the LLM
    # explains, it does not decide inclusion.
    sections: Dict[str, Any] = {}
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

    return agent_summary, similar_cases_data, similar_section, merged_provenance


def run_risk_assessment_pipeline(
    case_id: str,
    case_data: Dict[str, Any],
    sections: Dict[str, Any],
    tool_call_log: List[dict],
    new_provenance: List[dict],
    messages,
) -> Tuple[Dict[str, Any], Dict[str, Any], List[dict]]:
    """
    /risk_assessment direct pipeline work (Section 8.4 AI-15) plus
    recommendation-text normalization. Returns (risk_assessment,
    risk_section, merged_provenance).
    """
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
                case_id,
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
                case_id, subject_id, exc,
            )
            risk_assessment["neo4j_signals"] = {"unavailable_reason": str(exc)}
    else:
        logger.warning(
            "graph risk signals skipped for case_id=%s — no subject_primary_id resolved",
            case_id,
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

    return risk_assessment, risk_section, merged_provenance


def prepare_plan_context(
    case_data: Dict[str, Any],
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """
    /plan pre-LLM work (Section 8.5 AI-16). Builds the rule-aware task
    recommendations from the rules_fired block Context Enrichment (AI-13)
    already placed in context, and returns the case context to serialise
    into the plan prompt alongside them.

    No new database connection and no AppWorks call: Section 8.5 requires
    this to work entirely from context. A case with no rules_fired yields
    an empty list, and the plan degrades to generic LLM synthesis exactly
    as it did before.

    Returns (case_data_for_prompt, rule_aware_tasks).
    """
    rule_aware_tasks = build_rule_aware_tasks(
        case_data.get("rules_fired", []),
        case_data.get("graph_context", {}),
    )
    # The LLM selects investigation_steps from BOTH candidate pools: these
    # rule-derived tasks (injected here) and the catalogue tasks it fetches
    # through its own scoped tool.
    case_data_for_prompt = {**case_data, "rule_aware_tasks": rule_aware_tasks}
    return case_data_for_prompt, rule_aware_tasks


def run_plan_pipeline(
    case_id: str,
    case_data: Dict[str, Any],
    sections: Dict[str, Any],
    messages,
    new_provenance: List[dict],
    cache_updated_at_before_call,
    rule_aware_tasks: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[str, Dict[str, Any], Dict[str, Any], List[dict], str, Optional[str], Optional[Any], bool]:
    """
    /plan pipeline work: parse the LLM's markdown into a structured plan,
    validate it, and apply any human override (Section D.6). Returns
    (assistant_text, investigation_plan, plan_section, merged_provenance,
    plan_source, modified_by, modified_on, plan_stale).
    """
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

    # AI-16 / Section 8.5: annotate every step with where it came from — a
    # rule-aware task, a BSI catalogue task, or the agent's own synthesis —
    # so the basis for each recommendation is visible.
    rule_aware_tasks = rule_aware_tasks or []
    catalog_tasks = (sections.get("catalog_tasks") or {}).get("catalog_tasks", [])
    if steps_dicts:
        steps_dicts = tag_step_sources(steps_dicts, rule_aware_tasks, catalog_tasks)
    # Build structured plan from parsed prose
    # Start with metadata from tool result if available
    plan_result = sections.get("investigation_plan", {})

    id_match = re.search(r"Case\s*(?:ID|#)?\s*[:\s]*(\d+)", assistant_text, re.I)
    cid = id_match.group(1) if id_match else case_id
    plan_id = plan_result.get("plan_id") or f"PLAN-{cid}-{datetime.now().strftime('%Y%m%d')}"

    investigation_plan = {
        "plan_id":             plan_id,
        "fraud_types":         plan_result.get("fraud_types", []),
        "risk_tier":           plan_result.get("risk_tier", "UNSPECIFIED"),
        "investigation_steps": steps_dicts,
        "evidence_checklist":  checklist_dicts,
        "escalation_criteria": criteria or None,
        "escalation_required": plan_result.get("escalation_required", False),
        # Section 8.5: carried on the plan and displayed SEPARATELY from the
        # generic investigation steps, so the rule that justifies each
        # recommendation stays visible to the investigator.
        "rule_aware_tasks":    rule_aware_tasks,
        "catalog_tasks":       catalog_tasks,
    }

    try:
        validated_plan = InvestigationPlanContract(**investigation_plan)
        investigation_plan = validated_plan.model_dump(exclude_none=True)
    except Exception as e:
        logger.warning(
            f"Investigation plan schema validation failed — storing unvalidated: {e}"
        )

    # Modify Investigation Steps flow (Section D.6): a saved override
    # replaces investigation_steps only — evidence_checklist,
    # escalation_criteria, fraud_types, and risk_tier stay AI-generated.
    # Read server-side on every /plan call so the override is always
    # reflected regardless of which client called this endpoint.
    override = get_override(case_id)
    if override is not None:
        investigation_plan["investigation_steps"] = override["modified_steps"]
        plan_source = "human_modified"
        modified_by = override["modified_by"]
        modified_on = override["modified_on"]
    else:
        plan_source = "ai_generated"
        modified_by = None
        modified_on = None

    plan_stale = (
        compute_plan_staleness(cache_updated_at_before_call, modified_on)
        if override is not None
        else False
    )

    plan_section = {
        "investigation_plan": investigation_plan
    }

    merged_provenance = merge_provenance(
        case_data.get("provenance_trail", []),
        new_provenance,
    )

    return (
        assistant_text, investigation_plan, plan_section, merged_provenance,
        plan_source, modified_by, modified_on, plan_stale,
    )