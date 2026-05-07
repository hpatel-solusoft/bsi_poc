# semantic_layer/semantic_model.py
# ----------------------------------------------------------------
# BSI Fraud Investigation Platform — Canonical Entity Model
#
# SINGLE SOURCE OF TRUTH for every data entity that flows
# through the system.
#
# Design rules:
#   1. model_config extra="allow" on any model that has AppWorks
#      fields that may grow or vary — this ensures _validate() never
#      silently strips fields the LLM needs to reason over.
#   2. All Optional fields default to None — AppWorks omits many
#      fields when they have no value.
#   3. No hardcoded lists or enums — AppWorks is the authority.
# ----------------------------------------------------------------

from pydantic import BaseModel, Field, field_validator
from typing   import Optional, Any
import re

# ================================================================
# SHARED / PRIMITIVE ENTITIES
# ================================================================

class AddressEntry(BaseModel):
    """A single address record from subject history."""
    address:   Optional[str] = None
    apt_suite: Optional[str] = None
    zipcode:   Optional[str] = None
    address_type: Optional[str] = None
    city:      Optional[str] = None
    state:     Optional[str] = None
    county:    Optional[str] = None
    from_date: Optional[str] = Field(default=None, alias="from")
    to_date:   Optional[str] = Field(default=None, alias="to")
    model_config = {"populate_by_name": True, "extra": "allow"}


class TriggeredRule(BaseModel):
    """A single BSI business rule triggered during risk assessment."""
    rule_id:           str
    rule_name:         Optional[str] = Field(default=None, alias="description")
    weight:            float = 0.0
    max_weight:        Optional[float] = None
    display:           Optional[str] = None
    condition_matched: Optional[str] = None
    flags:             Optional[list] = None
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
    model_config = {"extra": "allow"}


class EvidenceItem(BaseModel):
    """A single item in the evidence checklist."""
    item:      str
    mandatory: Optional[bool] = True
    model_config = {"extra": "allow"}


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
    model_config = {"extra": "allow"}


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
    model_config = {"extra": "allow"}


class CaseClassification(BaseModel):
    entity_text:   Optional[str] = None
    entity_code:   Optional[str] = None
    category_text: Optional[str] = None
    category_code: Optional[str] = None
    request_type:  Optional[str] = None
    model_config = {"extra": "allow"}


class CaseDetails(BaseModel):
    intake_referral_no:     Optional[str] = None
    source:                 Optional[str] = None
    identifier_name:        Optional[str] = None
    identifier_ssn_or_ein:  Optional[str] = None
    date_reported:          Optional[str] = None
    date_reported_age:      Optional[int] = None
    date_received:          Optional[str] = None
    date_received_age:      Optional[int] = None
    date_entered_age:       Optional[int] = None
    workfolder_allegation:  Optional[str] = None
    co_subject_name:        Optional[str] = None
    subject_city:           Optional[str] = None
    model_config = {"extra": "allow"}


class AllegationHeader(BaseModel):
    status:                  Optional[str] = None
    allegation_status:       Optional[str] = None
    date_received:           Optional[str] = None
    date_reported:           Optional[str] = None
    date_closed:             Optional[str] = None
    closure_date_reported:   Optional[str] = None
    close_comment:           Optional[str] = None
    comment:                 Optional[str] = None
    agency_referral_no:      Optional[str] = None
    is_intake:               Optional[bool] = None
    disposition_norris_code: Optional[str] = None
    dta_closure_report:      Optional[bool] = None
    allegation_type:         dict
    source_agency:           dict
    model_config = {"extra": "allow"}


class SubjectHeader(BaseModel):
    subject_id:         str
    subject_type:       Optional[str] = None
    is_primary_subject: Optional[bool] = None
    role:               Optional[str] = None
    details:            dict
    addresses:          list[dict]
    alias_records:      list[str]
    model_config = {"extra": "allow"}


class CaseHeader(BaseModel):
    """Matches nested output of f1_intake_services.py"""
    case_id:            str
    summary:            CaseSummary
    classification:     CaseClassification
    details:            CaseDetails
    allegations:        list[AllegationHeader]
    subjects:           list[SubjectHeader]
    subject_primary_id: Optional[str] = None
    fraud_types:        Optional[list[str]] = None
    model_config = {"extra": "allow"}


# ================================================================
# TOOL 2 — fetch_subject_history
# ================================================================

class PriorCaseHeader(BaseModel):
    workfolder_id:      str
    complaint_no:       Optional[int] = None
    status:             Optional[str] = None
    description:        Optional[str] = None
    case_description:   Optional[str] = None
    destination:        Optional[str] = None
    is_primary_subject: Optional[bool] = None
    date_received:      Optional[str] = None
    date_reported:      Optional[str] = None
    team:               Optional[str] = None
    allegation:         Optional[str] = None
    mapping_title:      Optional[str] = None
    model_config = {"extra": "allow"}


class SubjectProfile(BaseModel):
    subject_id:       str
    first_name:       Optional[str] = None
    last_name:        Optional[str] = None
    dob:              Optional[str] = None
    prior_cases:      list[PriorCaseHeader]
    prior_case_count: int
    model_config = {"extra": "allow"}


# ================================================================
# TOOL 3 — search_similar_cases
# ================================================================

class SimilarCasesResult(BaseModel):
    query_summary:  str
    matches:        list[SimilarCaseMatch]
    top_n_returned: int
    model_config = {"extra": "allow"}


# ================================================================
# TOOL 4a — get_risk_rules
# ================================================================

class RiskRuleDef(BaseModel):
    """
    A single active BSI fraud-detection rule dimension from AppWorks.
    extra="allow" ensures that weight, tier_thresholds, recommendations,
    and any future AppWorks fields are preserved through _validate()
    and delivered intact to the LLM.
    """
    rule_id:          str
    description:      Optional[str] = None
    dimension_key:    str
    thresholds:       list                      # parsed breakpoints list
    bonus_condition:  Optional[str] = None
    bonus_pts:        float = 0.0
    max_pts:          float = 0.0
    weight:           float = 0.0              # AppWorks rule weight (informational)
    tier_thresholds:  Optional[Any] = None     # optional tier config from AppWorks
    recommendations:  Optional[Any] = None     # optional recommendation text from AppWorks
    active:           bool = True
    model_config = {"extra": "allow"}          # preserve any additional AppWorks fields


class RiskRulesResult(BaseModel):
    rules: list[RiskRuleDef]
    model_config = {"extra": "allow"}


# ================================================================
# TOOL 4b — calculate_risk_metrics
# ================================================================

class RiskAssessment(BaseModel):
    case_id:              str
    subject_id:           str
    risk_score:           float
    risk_tier:            str
    triggered_rules:      list               # list of TriggeredRule-compatible dicts
    total_points:         Optional[float] = None
    max_points:           Optional[float] = None
    billing_anomaly_flag: bool = False
    prior_case_count:     int = 0
    recommendation:       str
    model_config = {"extra": "allow"}


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
    model_config = {"extra": "allow"}


# ================================================================
# TOOL 6 — generate_final_report
# ================================================================

class FinalReport(BaseModel):
    report_id:    str
    case_id:      str
    generated_at: str
    sections:     dict
    status:       str
    model_config = {"extra": "allow"}
