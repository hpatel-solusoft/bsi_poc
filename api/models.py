
from pydantic import BaseModel
from typing import Dict, List, Optional, Any

# -----------------------------------------------------------------------
# Request / response models
# -----------------------------------------------------------------------

class InvestigateRequest(BaseModel):
    case_id: str


class SimilarCasesRequest(BaseModel):
    case_id: str
    # ai_summary is REQUIRED per v6 spec — frontend always sends it.
    # Contains: { "investigation": { ...sections... }, "provenance_trail": [...] }
    ai_summary: Dict[str, Any]


class PlanRequest(BaseModel):
    case_id: str
    # ai_summary is REQUIRED per v6 spec — frontend always sends it.
    # Contains: { "investigation": { ...sections... }, "provenance_trail": [...] }
    ai_summary: Dict[str, Any]

class CopilotRequest(BaseModel):
    case_id: str
    question: str
    # ai_summary is REQUIRED per v6 spec — frontend always sends it.
    # Contains: { "investigation": { ...sections... }, "provenance_trail": [...] }
    ai_summary: Dict[str, Any]
    conversation_history: Optional[List[Dict[str, Any]]] = None
    # Human-approved investigation plan, written by an analyst via the Modify Strategy flow.
    # When present, the copilot prompt treats these steps as authoritative over the AI-generated ones.
    # Schema: { "source": "human_approved", "steps": [...], "comment": "...", "modified_on": "...", "modified_by": "..." }
    modified_ai_investigation_plan: Optional[Dict[str, Any]] = None
