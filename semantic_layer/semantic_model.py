# semantic_layer/semantic_model.py
# ----------------------------------------------------------------
# BSI Fraud Investigation Platform — Canonical Entity Model
#
# SINGLE SOURCE OF TRUTH for every data entity that flows
# through the system.
# ----------------------------------------------------------------

from pydantic import BaseModel, Field, field_validator
from typing   import Optional, Any
import re

# ================================================================
# SHARED / PRIMITIVE ENTITIES
# ================================================================

class AddressEntry(BaseModel):
    """A single address record from subject history."""
    address:   str
    from_date: Optional[str] = Field(default=None, alias="from")
    to_date:   Optional[str] = Field(default=None, alias="to")
    model_config = {"populate_by_name": True}

class TriggeredRule(BaseModel):
    """A single BSI business rule triggered during risk assessment."""
    rule_id:   str
    rule_name: Optional[str] = Field(default=None, alias="description")
    weight:    float = 0.0
    model_config = {"populate_by_name": True, "extra": "allow"}

    @field_validator("weight", mode="before")
    @classmethod
    def parse_weight(cls, v):
        if isinstance(v, str):
            match = re.search(r"(\d+(\.\d+)?)", v)
            return float(match.group(1)) if match else 0.0
        return float(v) if v is not None else 0.0

class InvestigationStep(BaseModel):
    """A single step in the investigation playbook."""
    step:          int
    action:        str
    owner:         str
    deadline_days: int

class EvidenceItem(BaseModel):
    """A single item in the evidence checklist."""
    item:      str
    mandatory: Optional[bool] = True

class SimilarCaseMatch(BaseModel):
    """A single similar case match from the archive search."""
    case_id:              str
    allegation_id:        Optional[str] = None
    similarity_score:     float
    fraud_type:           str
    outcome:              str
    summary:              str
    estimated_loss:       float = 0.0
    financial_calculated: float = 0.0

# ================================================================
# TOOL 1 — verify_case_intake
# ================================================================

class CaseSummary(BaseModel):
    complaint_no:     Optional[int] = None
    description:      Optional[str] = None
    case_description: Optional[str] = None
    status:           Optional[str] = None
    destination:      Optional[str] = None
    team:             Optional[str] = None
    created:          Optional[str] = None

class CaseClassification(BaseModel):
    entity_text:   Optional[str] = None
    category_text: Optional[str] = None
    request_type:  Optional[str] = None

class CaseDetails(BaseModel):
    intake_referral_no:    Optional[str] = None
    source:                Optional[str] = None
    identifier_name:       Optional[str] = None
    date_received:         Optional[str] = None
    date_received_age:     Optional[int] = None
    subject_city:          Optional[str] = None

class AllegationHeader(BaseModel):
    status:            Optional[str] = None
    allegation_status: Optional[str] = None
    date_received:     Optional[str] = None
    allegation_type:   dict
    source_agency:     dict

class SubjectHeader(BaseModel):
    subject_id:         str
    subject_type:       Optional[str] = None
    is_primary_subject: Optional[bool] = None
    role:               Optional[str] = None
    details:            dict
    addresses:          list[dict]
    alias_records:      list[str]

class CaseHeader(BaseModel):
    """Matches nested output of f1_intake_services.py"""
    case_id:        str
    summary:        CaseSummary
    classification: CaseClassification
    details:        CaseDetails
    allegations:    list[AllegationHeader]
    subjects:       list[SubjectHeader]
    subject_primary_id: Optional[str] = None
    fraud_types:    Optional[list[str]] = None


# ================================================================
# TOOL 2 — fetch_subject_history
# ================================================================

class PriorCaseHeader(BaseModel):
    workfolder_id:      str
    complaint_no:       Optional[int] = None
    status:             Optional[str] = None
    description:        Optional[str] = None
    destination:        Optional[str] = None
    is_primary_subject: Optional[bool] = None
    date_received:      Optional[str] = None

class SubjectProfile(BaseModel):
    subject_id:       str
    first_name:       Optional[str] = None
    last_name:        Optional[str] = None
    dob:              Optional[str] = None
    prior_cases:      list[PriorCaseHeader]
    prior_case_count: int


# ================================================================
# TOOL 3 — search_similar_cases
# ================================================================

class SimilarCasesResult(BaseModel):
    query_summary:  str
    matches:        list[SimilarCaseMatch]
    top_n_returned: int


# ================================================================
# TOOL 4 — calculate_risk_metrics
# ================================================================

class RiskAssessment(BaseModel):
    case_id:              str
    subject_id:           str
    risk_score:           float
    risk_tier:            str
    triggered_rules:      list
    billing_anomaly_flag: bool
    prior_case_count:     int
    recommendation:       str


# ================================================================
# TOOL 5 — get_investigation_playbook
# ================================================================

class InvestigationPlaybook(BaseModel):
    playbook_id:         str
    fraud_types:         list[str]
    risk_tier:           str
    investigation_steps: list[InvestigationStep]
    evidence_checklist:  list[EvidenceItem]
    escalation_required: bool


# ================================================================
# TOOL 6 — generate_final_report
# ================================================================

class FinalReport(BaseModel):
    report_id:       str
    case_id:         str
    generated_at:    str
    sections:        dict
    status:          str

class RiskRuleDef(BaseModel):
    rule_id:         str
    description:     Optional[str] = None
    dimension_key:   str
    thresholds:      list
    bonus_condition: Optional[str] = None
    bonus_pts:       float = 0.0
    max_pts:         float = 0.0
    tier_thresholds: Optional[Any] = None
    recommendations: Optional[Any] = None
    active:          bool = True

class RiskRulesResult(BaseModel):
    rules: list[RiskRuleDef]