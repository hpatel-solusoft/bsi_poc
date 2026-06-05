import json
import logging
import copy
from typing import Dict, Any
logger = logging.getLogger(__name__)

from config.prompts import (
    COPILOT_TOOL_PROMPT,
    INVESTIGATE_SYSTEM_PROMPT,
    PLAN_PROMPT,
    RISK_ASSESSMENT_PROMPT,
    SIMILAR_CASES_PROMPT,
)
# -----------------------------------------------------------------------
# PROMPT RENDERING
# -----------------------------------------------------------------------


def _render_prompt(template: str, values: dict) -> str:
    """
    Render a prompt template from config/prompts.py with runtime values.
    config/prompts.py is the single prompt source. agent_runner supplies
    runtime case data and pre-extracted parameters only.
    """
    prompt = template
    for key, value in values.items():
        prompt = prompt.replace("{" + key + "}", value)
    return prompt



# -----------------------------------------------------------------------
# PROMPT BUILDERS — one per ON-DEMAND route
# Each builder reads its template from config/prompts.py and injects
# runtime values. No prompt text lives here.
# -----------------------------------------------------------------------

def build_investigate_system_prompt() -> str:
    """
    SYSTEM prompt for the main investigation agent. 
    This is the only prompt that does not require runtime values, as it contains only static instructions and guidelines for the agent's overall behavior and reasoning style. 
    All tool-specific prompts are ON-DEMAND and receive case data for context injection
    """
    return INVESTIGATE_SYSTEM_PROMPT
    

def build_similar_cases_prompt(case_data: dict) -> str:
    """
    ON-DEMAND /similar_cases prompt.
    Injects full case context so the agent can scope its archive search
    to the conduct described in the active investigation.
    """
    return _render_prompt(
        SIMILAR_CASES_PROMPT,
        {
            "json.dumps(case_data, indent=2)": json.dumps(case_data, indent=2),
        },
    )


def build_risk_assessment_prompt(case_data: dict) -> str:
    """
    ON-DEMAND /risk_assessment prompt.
    Injects full case context for get_risk_rules → calculate_risk_metrics
    two-step sequence.
    """
    return _render_prompt(
        RISK_ASSESSMENT_PROMPT,
        {
            "json.dumps(case_data, indent=2)": json.dumps(case_data, indent=2),
        },
    )


def build_plan_prompt(case_data: dict) -> str:
    """
    ON-DEMAND /plan prompt.
    Extracts fraud_types and risk_tier from case_data for template
    substitution; full context is also injected for strategy generation.
    """
    risk      = case_data.get("risk_assessment") or {}
    complaint = case_data.get("complaint_intelligence") or {}

    fraud_types = complaint.get("fraud_types") or []
    risk_tier   = risk.get("risk_tier") or ""

    return _render_prompt(
        PLAN_PROMPT,
        {
            "json.dumps(case_data, indent=2)":  json.dumps(case_data, indent=2),
            "json.dumps(fraud_types)":           json.dumps(fraud_types),
            "risk_tier":                         risk_tier,
            'case_data.get("case_id")':          str(case_data.get("case_id")),
        },
    )


def build_copilot_prompt(case_id: str, case_data: dict) -> str:
    """
    ON-DEMAND /copilot prompt.

    If a human-approved investigation plan is present in case_data, the
    AI-generated steps are replaced with the human-approved steps BEFORE
    the context is serialised into the prompt. The LLM sees exactly one
    set of steps — no ambiguity, no instruction-following required to
    choose between two competing lists.
    """
    context = copy.deepcopy(case_data)

    human_plan = context.get("modified_ai_investigation_plan")
    if (
        isinstance(human_plan, dict)
        and human_plan.get("source") == "human_approved"
        and isinstance(human_plan.get("steps"), list)
        and len(human_plan["steps"]) > 0
    ):
        if not isinstance(context.get("investigation_plan"), dict):
            context["investigation_plan"] = {}
        context["investigation_plan"]["investigation_steps"]  = human_plan["steps"]
        context["investigation_plan"]["_steps_source"]        = "human_approved"
        context["investigation_plan"]["_approved_by"]         = human_plan.get("modified_by", "")
        context["investigation_plan"]["_approved_on"]         = human_plan.get("modified_on", "")
        context["investigation_plan"]["_approval_comment"]    = human_plan.get("comment", "")

    return _render_prompt(
        COPILOT_TOOL_PROMPT,
        {
            "case_id":                           case_id,
            "json.dumps(case_data, indent=2)":   json.dumps(context, indent=2),
        },
    )
