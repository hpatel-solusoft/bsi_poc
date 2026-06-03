# appworks/investigation_strategy.py
# ----------------------------------------------------------------
# Agent 5: Investigation Plan Context
# ----------------------------------------------------------------
# investigation_strategy does not generate plan content. It only returns the tool inputs
# so the LLM can generate the investigation strategy from the prompt and
# verified case context.
# ----------------------------------------------------------------

import logging
from datetime import datetime, timezone

from semantic_layer.entity_contracts import InvestigationPlan

logger = logging.getLogger(__name__)

def _normalize_list(values):
    if isinstance(values, str):
        return [values]
    if isinstance(values, list):
        return [str(item) for item in values if item is not None]
    return []


def get_investigation_plan(fraud_types: list, risk_tier: str, ai_summary=None, **kwargs) -> dict:
    """Return the plan context skeleton only. The LLM generates the actual analysis."""
    if isinstance(fraud_types, str):
        fraud_types = [fraud_types]

    fraud_types = _normalize_list(fraud_types)
    logger.info("Received request for investigation plan with fraud_types: %s and risk_tier: %s", fraud_types, risk_tier)
    risk_tier = risk_tier or ""
    ai_summary = kwargs.get("ai_summary")
    logger.info("AI summary in investigation_strategy context: %s", ai_summary.length() if ai_summary else "None")
    type_slug = "-".join(str(item).replace(" ", "_") for item in fraud_types[:2]) or "UNSPECIFIED"

    result_data = {
        "plan_id": f"PLAN-{type_slug}-{risk_tier or 'UNSPECIFIED'}-{datetime.now().strftime('%Y%m%d')}",
        "fraud_types": fraud_types,
        "risk_tier": risk_tier,
    }

    logger.info("investigation_strategy plan context returned: %s", result_data)
    validated = InvestigationPlan(**result_data)

    return {
        "result": validated.model_dump(exclude_none=True),
        "provenance": {
            "sources": ["Verified route1 investigation context"],
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "computed_by": "Investigation Strategy agent context passthrough",
        },
    }
