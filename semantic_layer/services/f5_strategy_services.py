# semantic_layer/services/f5_strategy_services.py
# ----------------------------------------------------------------
# Agent 5: Investigation Playbook Context
# ----------------------------------------------------------------
# F5 does not generate plan content. It only returns the tool inputs
# so the LLM can generate the investigation strategy from the prompt and
# verified case context.
# ----------------------------------------------------------------

import logging
from datetime import datetime, timezone

from semantic_layer.semantic_model import InvestigationPlaybook

logger = logging.getLogger(__name__)


def _normalize_case_data(case_data):
    if isinstance(case_data, dict):
        return case_data
    return {}


def _normalize_list(values):
    if isinstance(values, str):
        return [values]
    if isinstance(values, list):
        return [str(item) for item in values if item is not None]
    return []


def get_investigation_playbook(fraud_types: list, risk_tier: str, case_data: dict = None) -> dict:
    """Return the plan context skeleton only. The LLM generates the actual analysis."""
    if isinstance(fraud_types, str):
        fraud_types = [fraud_types]
    fraud_types = _normalize_list(fraud_types)
    risk_tier = risk_tier or ""
    _normalize_case_data(case_data)
    type_slug = "-".join(str(item).replace(" ", "_") for item in fraud_types[:2]) or "UNSPECIFIED"

    result_data = {
        "playbook_id": f"PLAN-{type_slug}-{risk_tier or 'UNSPECIFIED'}-{datetime.now().strftime('%Y%m%d')}",
        "fraud_types": fraud_types,
        "risk_tier": risk_tier,
    }

    logger.info("F5 plan context returned: %s", result_data)
    validated = InvestigationPlaybook(**result_data)

    return {
        "result": validated.model_dump(exclude_none=True),
        "provenance": {
            "sources": ["Verified route1 investigation context"],
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "computed_by": "Agent 5 - plan context passthrough",
        },
    }
