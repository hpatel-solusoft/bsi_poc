# semantic_layer/semantic_model.py
# ----------------------------------------------------------------
# BSI Fraud Investigation Platform — Canonical Entity Model
#
# This file is the SINGLE SOURCE OF TRUTH for every data entity
# that flows through the system.
#
# DESIGN PRINCIPLE (Anti-Corruption Layer):
#   AppWorks speaks its own language — raw HTTP responses with
#   fields that can change, be null, or be renamed at any time.
#   This file defines YOUR system's language. Every AppWorks
#   response is translated and validated into these models at
#   the service boundary (appworks_services.py) before anything
#   else in the system touches the data.
#
# WHY PYDANTIC AND NOT YAML:
#   YAML tells you a schema exists.
#   Pydantic tells you the data matches it — at the exact moment
#   it enters your system, on every single call. If AppWorks
#   changes a field name or returns a null, a ValidationError
#   is raised here — not silently passed to the LLM as garbage.
#
# ADDING A NEW TOOL:
#   1. Define its return entity here
#   2. Map it in appworks_services.py
#   3. Add to manifest.yaml
#   Nothing else changes.
# ----------------------------------------------------------------

from pydantic import BaseModel, Field
from typing   import Optional


# ================================================================
# SHARED / PRIMITIVE ENTITIES
# ================================================================

class AddressEntry(BaseModel):
    """A single address record from subject history."""
    address: str
    from_date: str = Field(alias="from")
    to_date:   str = Field(alias="to")

    model_config = {"populate_by_name": True}


class PriorCase(BaseModel):
    """A prior BSI case linked to a subject."""
    case_id:    str
    year:       int
    fraud_type: str
    outcome:    str


class KnownAssociate(BaseModel):
    """A known associate of the primary subject."""
    name:         str
    relationship: str
    subject_id:   str


class TriggeredRule(BaseModel):
    """A single BSI business rule triggered during risk assessment."""
    rule_id:   str
    rule_name: str
    weight:    float


class InvestigationStep(BaseModel):
    """A single step in the investigation playbook."""
    step:          int
    action:        str
    owner:         str
    deadline_days: int


class EvidenceItem(BaseModel):
    """A single item in the evidence checklist."""
    item:      str
    mandatory: bool


class SimilarCaseMatch(BaseModel):
    """A single match from the vector similarity search."""
    case_id:          str
    similarity_score: float
    fraud_type:       str
    outcome:          str
    summary:          str


# ================================================================
# TOOL 1 — verify_case_intake
# Agent: Complaint Intelligence Agent
# ================================================================

class CaseHeader(BaseModel):
    """
    Canonical entity for the case header returned by AppWorks.
    Produced by: appworks_services.get_case_header()
    Consumed by: LLM (Tool 1 result), CS-4 complaint_intelligence section
    Fields used downstream:
      - subject_primary_id → required by Tool 2 and Tool 4
      - fraud_type_classified → required by Tool 5
      - complaint_description → required by Tool 3
    """
    case_id:                str
    complainant_name:       str
    subject_primary:        str
    subject_primary_id:     str
    subject_secondary:      Optional[str]  = None
    complaint_description:  str
    fraud_type_classified:  str
    intake_date:            str
    status:                 str


# ================================================================
# TOOL 2 — fetch_subject_history
# Agent: Context Enrichment Agent
# ================================================================

class SubjectProfile(BaseModel):
    """
    Canonical entity for the enriched subject profile from AppWorks.
    Produced by: appworks_services.get_enriched_subject_profile()
    Consumed by: LLM (Tool 2 result), CS-4 context_enrichment section
    """
    subject_id:       str
    full_name:        str
    dob:              str
    address_history:  list[AddressEntry]
    prior_cases:      list[PriorCase]
    known_associates: list[KnownAssociate]
    prior_case_count: int


# ================================================================
# TOOL 3 — search_similar_cases
# Agent: Similar Case Retrieval Agent
# ================================================================

class SimilarCasesResult(BaseModel):
    """
    Canonical entity for the vector similarity search result.
    Produced by: appworks_services.vector_search_cases()
    Consumed by: LLM (Tool 3 result), CS-4 similar_cases section
    """
    query_summary:   str
    matches:         list[SimilarCaseMatch]
    top_n_returned:  int


# ================================================================
# TOOL 4 — calculate_risk_metrics
# Agent: Fraud Risk Assessment Agent
# ================================================================

class RiskAssessment(BaseModel):
    """
    Canonical entity for the risk assessment result.
    Produced by: appworks_services.get_risk_measures()
    Consumed by: LLM (Tool 4 result), CS-4 risk_assessment section
    Fields used downstream:
      - risk_tier → required by Tool 5 (get_investigation_playbook)
    """
    case_id:               str
    subject_id:            str
    risk_score:            float
    risk_tier:             str                  # LOW / MEDIUM / HIGH / CRITICAL
    triggered_rules:       list[TriggeredRule]
    billing_anomaly_flag:  bool
    prior_case_count:      int
    recommendation:        str


# ================================================================
# TOOL 5 — get_investigation_playbook
# Agent: Case Strategy Agent
# ================================================================

class InvestigationPlaybook(BaseModel):
    """
    Canonical entity for the investigation playbook from AppWorks.
    Produced by: appworks_services.get_playbook_by_type()
    Consumed by: LLM (Tool 5 result), CS-4 investigation_playbook section
    """
    playbook_id:           str
    fraud_type:            str
    risk_level:            str
    investigation_steps:   list[InvestigationStep]
    evidence_checklist:    list[EvidenceItem]
    escalation_required:   bool


# ================================================================
# TOOL 6 — generate_final_report
# Agent: Report Generation Agent
# ================================================================

class ReportSections(BaseModel):
    """The named sections inside the final investigation report."""
    case_summary:        str
    subject_history:     str
    similar_cases:       str
    risk_assessment:     str
    recommended_actions: str
    analyst_notes:       str


class FinalReport(BaseModel):
    """
    Canonical entity for the compiled investigation report.
    Produced by: appworks_services.compile_and_render_report()
    Consumed by: LLM (Tool 6 result), CS-4 final_report section
    """
    report_id:    str
    case_id:      str
    generated_at: str
    sections:     ReportSections
    status:       str


# ================================================================
# DISPATCHER ERROR ENTITY
# Canonical shape for all error responses from the dispatcher.
# ================================================================

class DispatchError(BaseModel):
    """
    Canonical error entity returned by the dispatcher when
    a gate fails or a service function raises an exception.
    The LLM receives this and can reason over it — e.g. retry
    with corrected params or report the error in its summary.
    """
    status:  str = "error"
    tool:    Optional[str] = None
    message: str


# ================================================================
# ENTITY REGISTRY
# Maps tool names to their canonical return entity.
# Used by dispatcher.py to know which model to validate against.
# When a new tool is added, register it here.
# ================================================================

ENTITY_REGISTRY: dict[str, type[BaseModel]] = {
    "verify_case_intake":           CaseHeader,
    "fetch_subject_history":        SubjectProfile,
    "search_similar_cases":         SimilarCasesResult,
    "calculate_risk_metrics":       RiskAssessment,
    "get_investigation_playbook":   InvestigationPlaybook,
    "generate_final_report":        FinalReport,
}