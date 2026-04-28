# semantic_layer/semantic_model.py
# ----------------------------------------------------------------
# BSI Fraud Investigation Platform — Canonical Entity Model
#
# SINGLE SOURCE OF TRUTH for every data entity that flows
# through the system.
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
# ADDING A NEW TOOL — THREE FILES ONLY:
#   1. Add a new Pydantic class here
#   2. Add a new service function in appworks_services.py
#      calling _validate(YourNewClass, raw, fn_name)
#   3. Add the tool entry in manifest.yaml
#
#   No registry to update. No second place to maintain tool names.
#   manifest.yaml is the single authority on what tools exist.
#   Each entity class is referenced only inside its own service
#   function — the only place that ever needs to know it.
#
# WHY THERE IS NO ENTITY_REGISTRY:
#   An ENTITY_REGISTRY dict was considered and removed. It would
#   have mapped tool names to entity classes, duplicating a
#   relationship already expressed by each service function
#   calling _validate() with its own class directly. Maintaining
#   it in sync with manifest.yaml would create extra overhead
#   with no architectural benefit. Tool names have one authority:
#   manifest.yaml. Entity classes have one reference point: the
#   service function that uses them.
# ----------------------------------------------------------------

from pydantic import BaseModel, Field
from typing   import Optional


# ================================================================
# SHARED / PRIMITIVE ENTITIES
# Nested structures that appear inside tool result entities.
# Define these first — tool entities reference them below.
# ================================================================

class AddressEntry(BaseModel):
    """
    A single address record from subject history.

    NOTE on field aliases:
      AppWorks uses 'from' and 'to' as field names.
      Both are reserved Python keywords and cannot be used
      as attribute names. Field aliases handle the translation
      transparently — the rest of the system uses from_date
      and to_date and never sees the keyword conflict.
      populate_by_name=True allows either name during construction
      so both the AppWorks format and the internal format work.
    """
    address:   str
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
    """A single similar case match from the vector search."""
    case_id:          str
    similarity_score: float
    fraud_type:       str
    outcome:          str
    summary:          str


# ================================================================
# TOOL 1 — verify_case_intake
# Agent:    Complaint Intelligence Agent
# Produced: appworks_services.get_case_header()
# Consumed: LLM context (CS-2), CS-4 complaint_intelligence tab
#
# Critical fields used downstream by other tools:
#   subject_primary_id    → input to Tool 2 and Tool 4
#   fraud_type_classified → input to Tool 5
#   complaint_description → input to Tool 3
# ================================================================

class CaseHeader(BaseModel):
    case_id:               str
    complainant_name:      str
    subject_primary:       str
    subject_primary_id:    str
    subject_secondary:     Optional[str] = None   # not always present
    complaint_description: str
    fraud_type_classified: str
    intake_date:           str
    status:                str


# ================================================================
# TOOL 2 — fetch_subject_history
# Agent:    Context Enrichment Agent
# Produced: appworks_services.get_enriched_subject_profile()
# Consumed: LLM context (CS-2), CS-4 context_enrichment tab
# ================================================================

class SubjectProfile(BaseModel):
    subject_id:       str
    full_name:        str
    dob:              str
    address_history:  list[AddressEntry]
    prior_cases:      list[PriorCase]
    known_associates: list[KnownAssociate]
    prior_case_count: int


# ================================================================
# TOOL 3 — search_similar_cases
# Agent:    Similar Case Retrieval Agent
# Produced: appworks_services.vector_search_cases()
# Consumed: LLM context (CS-2), CS-4 similar_cases tab
# ================================================================

class SimilarCasesResult(BaseModel):
    query_summary:  str
    matches:        list[SimilarCaseMatch]
    top_n_returned: int


# ================================================================
# TOOL 4 — calculate_risk_metrics
# Agent:    Fraud Risk Assessment Agent
# Produced: appworks_services.get_risk_measures()
# Consumed: LLM context (CS-2), CS-4 risk_assessment tab
#
# Critical fields used downstream by other tools:
#   risk_tier → input to Tool 5 (get_investigation_playbook)
# ================================================================

class RiskAssessment(BaseModel):
    case_id:              str
    subject_id:           str
    risk_score:           float
    risk_tier:            str          # LOW / MEDIUM / HIGH / CRITICAL
    triggered_rules:      list[TriggeredRule]
    billing_anomaly_flag: bool
    prior_case_count:     int
    recommendation:       str


# ================================================================
# TOOL 5 — get_investigation_playbook
# Agent:    Case Strategy Agent
# Produced: appworks_services.get_playbook_by_type()
# Consumed: LLM context (CS-2), CS-4 investigation_playbook tab
# ================================================================

class InvestigationPlaybook(BaseModel):
    playbook_id:         str
    fraud_type:          str
    risk_level:          str
    investigation_steps: list[InvestigationStep]
    evidence_checklist:  list[EvidenceItem]
    escalation_required: bool


# ================================================================
# TOOL 6 — generate_final_report
# Agent:    Report Generation Agent
# Produced: appworks_services.compile_and_render_report()
# Consumed: LLM context (CS-2), CS-4 final_report tab
# ================================================================

class ReportSections(BaseModel):
    """Named sections inside the final investigation report."""
    case_summary:        str
    subject_history:     str
    similar_cases:       str
    risk_assessment:     str
    recommended_actions: str
    analyst_notes:       str


class FinalReport(BaseModel):
    report_id:    str
    case_id:      str
    generated_at: str
    sections:     ReportSections
    status:       str