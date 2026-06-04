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
from typing import List, Dict, Any, Optional

from semantic_layer.entity_contracts import InvestigationPlan
from appworks.provenance import ProvenanceTracker

logger = logging.getLogger(__name__)

def _normalize_list(values: Any) -> List[str]:
    """Safely coerces input into a flat list of strings."""
    if isinstance(values, str):
        return [values]
    if isinstance(values, list):
        return [str(item) for item in values if item is not None]
    return []


def get_investigation_plan(fraud_types: List[str], risk_tier: str, ai_summary: Optional[Dict] = None, **kwargs) -> Dict:
    """Return the plan context skeleton only. The LLM generates the actual analysis."""
    
    fraud_types = _normalize_list(fraud_types)
    risk_tier = risk_tier or "UNSPECIFIED"
    
    logger.info(f"Received request for investigation plan with fraud_types: {fraud_types} and risk_tier: {risk_tier}")
    
    # 🐛 BUG FIX: Python dicts use len(), not .length()
    ai_summary_len = len(ai_summary) if ai_summary else "None"
    logger.info(f"AI summary in investigation_strategy context length: {ai_summary_len}")
    
    type_slug = "-".join(str(item).replace(" ", "_") for item in fraud_types[:2]) or "UNSPECIFIED"

    result_data = {
        "plan_id": f"PLAN-{type_slug}-{risk_tier}-{datetime.now().strftime('%Y%m%d')}",
        "fraud_types": fraud_types,
        "risk_tier": risk_tier,
    }

    logger.info(f"investigation_strategy plan context returned: {result_data}")
    validated = InvestigationPlan(**result_data)

    # ── NEW: Standardized Provenance Envelope ───────────────────────
    # We track this as coming from internal system memory rather than a REST endpoint
    tracker = ProvenanceTracker("SystemMemory", "ai_summary")
    
    if ai_summary:
        tracker.add_source("SystemMemory", "Verified Investigation Context (CS-4)")
    else:
        tracker.add_source("SystemMemory", "Base Context Payload")

    return {
        "result": validated.model_dump(exclude_none=True),
        "provenance": tracker.get_provenance_block(computed_by="Investigation Strategy context passthrough")
    }