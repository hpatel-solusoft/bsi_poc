
from pydantic import BaseModel
from typing import Dict, List, Optional, Any

# -----------------------------------------------------------------------
# Request / response models
# -----------------------------------------------------------------------

class intakeRequest(BaseModel):
    case_id: str


class SimilarCasesRequest(BaseModel):
    case_id: str
    # ai_summary is now OPTIONAL (Data Persistence Spec v1.0, Section D.1).
    # AppWorks sends case_id only; the server resolves case_data from
    # CASE_STORE (CS-4) and, on a miss, from the PostgreSQL
    # case_ai_summary_store fallback. ai_summary remains accepted for
    # explicit-override / legacy callers only.
    ai_summary: Optional[Dict[str, Any]] = None


class PlanRequest(BaseModel):
    case_id: str
    # ai_summary is optional — see SimilarCasesRequest for the resolution order.
    ai_summary: Optional[Dict[str, Any]] = None

class RiskAssessmentRequest(BaseModel):
    case_id: str
    # ai_summary is optional — see SimilarCasesRequest for the resolution order.
    ai_summary: Optional[Dict[str, Any]] = None


class CopilotRequest(BaseModel):
    case_id: str
    question: str
    # ai_summary is optional — see SimilarCasesRequest for the resolution order.
    ai_summary: Optional[Dict[str, Any]] = None
    # conversation_history is now server-owned in PostgreSQL (D.2). This
    # field is only used to seed history for a brand-new case_id that has
    # no persisted turns yet; it is otherwise ignored in favor of the
    # server-side transcript.
    conversation_history: Optional[List[Dict[str, Any]]] = None
    # Human-approved investigation plan, written by an analyst via the Modify Strategy flow.
    # When present, the copilot prompt treats these steps as authoritative over the AI-generated ones.
    # Schema: { "source": "human_approved", "steps": [...], "comment": "...", "modified_on": "...", "modified_by": "..." }
    modified_ai_investigation_plan: Optional[Dict[str, Any]] = None
