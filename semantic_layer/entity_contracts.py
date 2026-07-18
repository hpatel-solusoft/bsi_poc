#  semantic_layer/entity_contracts.py
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




class InvestigationStep(BaseModel):
    """
    A single investigation step in the LLM-generated plan.
    'action' carries the plain instruction sentence from the LLM.
    'owner' and 'deadline_days' are Optional — populated during
    human review in the subsequent analyst step.
    extra='allow' — human review may add additional metadata fields.
    """
    step:          Optional[int] = None
    action:        str
    owner:         Optional[str] = None
    deadline_days: Optional[int] = None
    # AI-16 / Section 8.5: where this step came from — "catalog" (BSI
    # allegation-type task catalogue), "rule_aware" (derived from a fired
    # inference rule), or "llm_generated" (synthesised by the agent). Makes
    # the basis for every step visible to the investigator.
    source:        Optional[str] = None
    source_rule:   Optional[str] = None
    priority:      Optional[str] = None
    model_config = {"extra": "allow"}


class EvidenceItem(BaseModel):
    """
    A single item in the LLM-generated evidence checklist.
    'mandatory' is Optional — the LLM may not always specify it.
    extra='allow' — LLM output shape may vary.
    """
    item:      str
    mandatory: Optional[bool] = None
    model_config = {"extra": "allow"}

class SimilarCaseMatch(BaseModel):
    """A single similar case match from the archive search."""
    case_id:              Optional[str]
    complaint_no:         str
    allegation_type:      str                   # Renamed from fraud_type
    summary:              str
    date_received:        Optional[str] = None
    date_closed:          Optional[str] = None  # Replaces 'status'
    fraud_amount:         Optional[float] = None # Replaces 'financial_calculated'
    similarity_score:     float
    match_reasons:        list[str]             # Replaces 'outcome'
    model_config = {"extra": "forbid"}


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
    source:                 Optional[str] = None
    identifier_name:        Optional[str] = None
    date_reported:          Optional[str] = None
    date_reported_age:      Optional[int] = None
    date_received:          Optional[str] = None
    date_received_age:      Optional[int] = None
    date_entered_age:       Optional[int] = None
    workfolder_allegation:  Optional[str] = None
    co_subject_name:        Optional[str] = None
    subject_city:           Optional[str] = None
    model_config = {"extra": "allow"}

class AllegationType(BaseModel):
    """
    Allegation type classification nested within AllegationHeader.
    Sourced from AppWorks AllegationType entity via relationship:Allegations_AllegationsType.
    extra='allow' — AppWorks may return additional type fields.
    """
    id:          Optional[str] = None
    description: Optional[str] = None
    short_desc:  Optional[str] = None
    defaults:    Optional[str] = None
    model_config = {"extra": "allow"}


class SourceAgency(BaseModel):
    """
    Referring agency nested within AllegationHeader.
    Sourced from AppWorks Agency entity via relationship:Allegations_Source.
    extra='allow' — AppWorks may return additional agency fields.
    """
    name:              Optional[str] = None
    short_description: Optional[str] = None
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
    # Confirmed on the Allegations_All list payload, not captured before
    # the single-call /lists/ endpoint migration.
    agency_referral_number:  Optional[str] = None
    completed_date:          Optional[str] = None
    norris_code:             Optional[str] = None
    source_agency:           Optional[SourceAgency] = None
    allegation_type:         AllegationType
    model_config = {"extra": "allow"}

class SubjectDetails(BaseModel):
    """
    Personal and identity fields from the AppWorks Subject entity
    (fetched via relationship:Subjects_Subject — separate endpoint from SubjectHeader).
    extra='allow' — AppWorks may add subject attributes without breaking the contract.
    """
    identifier:             Optional[str] = None
    first_name:             Optional[str] = None
    middle_initial:         Optional[str] = None
    last_name:              Optional[str] = None
    gender:                 Optional[str] = None
    dob:                    Optional[str] = None
    dod:                    Optional[str] = None
    phone_number:           Optional[str] = None
    # SSN / Driving License intentionally never declared here — Tier 1 PII,
    # reference doc Section 3.5. The real guard is upstream: entity_mappers
    # .map_subjects() never reads Subject_SSN / Subject_DrivingLicenseNumber
    # into the dict this model validates.
    subject_type:           Optional[str] = None
    company_name:           Optional[str] = None
    provider_number:        Optional[str] = None
    pob:                    Optional[str] = None
    comment:                Optional[str] = None
    destination:            Optional[str] = None
    date_entered:           Optional[str] = None
    aliases:                Optional[str] = None
    model_config = {"extra": "allow"}
    
class SubjectHeader(BaseModel):
    subject_id:         str
    subject_type:       Optional[str] = None
    is_primary_subject: Optional[bool] = None
    role:               Optional[str] = None
    details:            SubjectDetails
    addresses:          list[AddressEntry]
    alias_records:      list[str]
    model_config = {"extra": "allow"}


class FinancialRecord(BaseModel):
    """
    A single Financial record. fraud_type / fraud_type_id come from the
    per-record Financial_PrimaryFraudTypeRelationShip embedded on the
    Financial_All list payload — previously fetched but discarded, kept
    only as part of the case-level aggregate.
    """
    calculated:    Optional[float] = None
    ordered:       Optional[float] = None
    comment:       Optional[str] = None
    start_date:    Optional[str] = None
    end_date:      Optional[str] = None
    date:          Optional[str] = None
    fraud_type:    Optional[str] = None
    fraud_type_id: Optional[str] = None
    model_config = {"extra": "allow"}


class FinancialsBlock(BaseModel):
    """
    Financial summary attached to the Workfolder (AppWorks Workfolder_FinancialRelationship).
    Typed explicitly so schema changes in AppWorks are caught at validation time
    rather than silently passed through as untyped extras (Issue #11).
    extra='allow' preserved so future AppWorks financial fields are not stripped.
    """
    records:          Optional[list[FinancialRecord]] = None
    total_calculated: Optional[float] = None
    total_ordered:    Optional[float] = None
    model_config = {"extra": "allow"}


class CaseHeader(BaseModel):
    """Matches nested output of f1_intake_services.py"""
    case_id:            str
    summary:            CaseSummary
    details:            CaseDetails
    allegations:        list[AllegationHeader]
    subjects:           list[SubjectHeader]
    subject_ids:        list[str]   # populated by case_intake for LLM convenience
    subject_primary_id: Optional[str] = None
    fraud_types:        Optional[list[str]] = None
    # Typed financial block — previously arrived as an unvalidated extra (Issue #11)
    financials:         Optional[FinancialsBlock] = None
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
    allegation_comment:   Optional[str] = None
    analyst_comment:     Optional[str] = None
    reviewer_comment:    Optional[str] = None
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


class SubjectHistory(BaseModel):
    profiles:         list[SubjectProfile]
    total_prior_case_count: int
    model_config = {"extra": "allow"}


# ================================================================
# TOOL 3 — search_similar_cases
# ================================================================


class SimilarCasesResult(BaseModel):
    matches:                   list[SimilarCaseMatch]
    relevant_fraud_types:      list[str] # Renamed from allegation_types
    top_n_returned:            int
    total_candidates_scored:   Optional[int] = None # Replaces raw_matches_found
    model_config = {"extra": "forbid"}
# ================================================================
# TOOL 4a — get_risk_rules
# ================================================================
class TriggeredRule(BaseModel):
    """
    A single BSI business rule activated during risk assessment.
    Maintains 'weight' terminology to preserve frontend/LLM contracts.
    """
    rule_id:    str
    rule_name:  Optional[str] = Field(default=None, alias="description")
    weight:     float = 0.0          
    max_weight: Optional[float] = None
    display:    Optional[str] = None
    findings:   Optional[str] = None
    
    # Strictly typed to strings, adhering to Rule 2 (defaults to None)
    flags:      Optional[list[str]] = None 
    triggered:   bool = False
    model_config = {"populate_by_name": True, "extra": "allow"}

class RiskRuleThreshold(BaseModel):
    """Strictly typed breakpoint sub-model to replace untyped lists."""
    condition: Optional[str] = None
    min_value: Optional[float] = None
    points:    float
    model_config = {"extra": "allow"}



class RiskRuleDef(BaseModel):
    """
    A single active BSI fraud-detection rule dimension from AppWorks.
    """
    rule_id:             str
    dimension_key:       str
    description:         Optional[str] = None
    
    # Explicitly typed to inform the Strategy Engine how to process it
    evaluation_strategy: Optional[str] = None 
    
    # Strictly typed to the new sub-model, adhering to Rule 2 (defaults to None)
    thresholds:          Optional[list[RiskRuleThreshold]] = None
    
    # Required for 'fraud_type_match' strategy
    target_fraud_types:  Optional[list[str]] = None
    
    max_pts:             float = 0.0
    bonus_condition:     Optional[str] = None
    bonus_pts:           float = 0.0
    weight:              float = 0.0
    
    # Safely typed from Any to dict
    tier_thresholds:     Optional[dict] = None 
    recommendations:     Optional[dict] = None
    
    active:              bool = True
    model_config = {"extra": "allow"}


class RiskRulesResult(BaseModel):
    """Envelope for get_risk_rules."""
    active_rules: list[RiskRuleDef]
    model_config = {"extra": "forbid"}

# ================================================================
# TOOL 4b — calculate_risk_metrics
# ================================================================

class RiskAssessment(BaseModel):
    """
    The final deterministic risk evaluation.
    Lists are strictly typed, and the mutable default anti-pattern 
    (list = []) is fixed using default_factory for thread safety.
    """
    case_id:              str
    subject_id:           str
    risk_score:           float
    risk_tier:            str
    
    # Strictly typed; prevents LLM hallucination of schema and memory leaks
    fraud_types:          list[str] = Field(default_factory=list)
    risk_indicators:      list[TriggeredRule] = Field(default_factory=list)
    
    total_points:         Optional[float] = None
    max_points:           Optional[float] = None
    prior_case_count:     Optional[int] = None
    recommendation:       Optional[str] = None
    
    model_config = {"extra": "allow"}

# ================================================================
# TOOL 5 — get_investigation_plan
# ================================================================

class AllegationTypeTask(BaseModel):
    """A single BSI configured investigative task (AI-16 / Section 8.5)."""
    task_id:         Optional[str] = None
    task_type:       str
    is_default_task: bool = False
    source:          str = "catalog"
    model_config = {"extra": "allow"}


class AllegationTypeTasksResult(BaseModel):
    """Output contract for get_allegation_type_tasks."""
    catalog_tasks:   list[AllegationTypeTask] = Field(default_factory=list)
    default_tasks:   list[str] = Field(default_factory=list)
    requested_types: list[str] = Field(default_factory=list)
    total_tasks:     int = 0
    model_config = {"extra": "allow"}


class RuleAwareTask(BaseModel):
    """A task recommended because a specific inference rule fired
    (AI-16 / Section 8.5). Displayed separately from generic LLM steps."""
    source_rule:  str
    task_type:    str
    priority:     str
    detects:      Optional[str] = None
    confidence:   Optional[str] = None
    corroborated: bool = False
    model_config = {"extra": "allow"}


class InvestigationPlan(BaseModel):
    plan_id: str
    fraud_types: list[str]
    risk_tier: str
    investigation_steps: Optional[list[InvestigationStep]] = None
    evidence_checklist:  Optional[list[EvidenceItem]] = None
    escalation_criteria: Optional[list[str]] = None    # plain strings — no typed model yet
    escalation_required: Optional[bool] = None
    data_sources: Optional[list[str]] = None
    plan_narrative: Optional[str] = None
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
    model_config = {"extra": "forbid"}


# ================================================================
# AI-18 — /generate_report (Functional Spec Section 8.7, Developer
# Spec Section 7.5). Deliberately NOT a reuse of FinalReport above:
# that contract is the eliminated Phase 1 single-flow report agent
# (Section 7 — "different contract"). This one adds related_network
# and confidence_summary, sourced from reasoning_layer.report_generation,
# never from the LLM (Section 8.7: graph data assembly is deterministic).
# ================================================================

class RejectionNotation(BaseModel):
    investigator_id: Optional[str] = None
    rejected_at:      Optional[str] = None
    reason:           Optional[str] = None
    rule_id:          Optional[str] = None
    model_config = {"extra": "allow"}


class RelatedNetworkFact(BaseModel):
    relationship_type: str
    counterpart_id:    Optional[str] = None
    counterpart_type:  Optional[str] = None
    counterpart_label: Optional[str] = None
    source_rule:       Optional[str] = None
    confidence:        str
    corroborated:      bool = False
    status:             str   # "active" | "rejected" — never silently omitted
    asserted_at:        Optional[str] = None
    rejection:           Optional[RejectionNotation] = None
    model_config = {"extra": "allow"}


class ConfidenceSummary(BaseModel):
    high:       int = 0
    medium:     int = 0
    unresolved: int = 0
    model_config = {"extra": "allow"}


class GeneratedReport(BaseModel):
    report_id:         str
    case_id:            str
    generated_at:        str
    status:              str          # "draft" | "saved_to_appworks"
    standard_sections:   dict         # unchanged Phase 1 sections (narrative)
    related_network:     list[RelatedNetworkFact]
    confidence_summary:  ConfidenceSummary
    model_config = {"extra": "forbid"}



# ================================================================

# TOOL — get_allegation_types

# ================================================================
 
class AllegationTypeDefinition(BaseModel):
    """
    A single distinct allegation type definition from AppWorks.
    Deduplicated from the Allegations_All list by type_id.
    """
    type_id:      str       # AllegationType_AllegationTypeID — unique numeric ID for the type definition
    short_code:   str        # AllegationType_AllegationTypeShortDesc — e.g. "ATS", "CHK"
    description:  str        # AllegationType_AllegationTypeDescription — e.g. "Assets"
    default_text: str        # AllegationType_AllegationTypeDefaults — plain language
                             # definition of what conduct this type covers.
                             # This is the field the LLM uses to match against the
                             # current case allegation comment.
    model_config = {"extra": "forbid"}
 
class AllegationTypesResult(BaseModel):
    """
    Deduplicated list of all active allegation type definitions from AppWorks.
    Used by the LLM to identify all type IDs relevant to the current case scheme
    before calling search_similar_cases.
    """
    allegation_types: list[AllegationTypeDefinition]
    total_types:      int    # count of distinct types returned — for traceability
    relevant_type_ids: Optional[list[int]] = None #Populated by the LLM after reasoning over allegation_types.
    model_config = {"extra": "forbid"}


# ================================================================
# REASONING LAYER — Phase 5: Extraction Stage candidate facts
# (Python Implementation Reference, Section 5.3 Step 3/4; Section 3.2's
#  ALLEGATION_LIKELY_AGAINST_SUBJECT row; Section 3.3's confidence enum)
# ================================================================

class AttributionCandidate(BaseModel):
    """
    A single candidate allegation-to-subject attribution produced by the
    LLM-based Extraction Stage from narrative text. Written into Neo4j
    as an ALLEGATION_LIKELY_AGAINST_SUBJECT relationship (Section 3.2)
    by reasoning_layer/graph_load.py — this model is what stands between
    raw LLM JSON and that write, so a malformed or hallucinated LLM
    response is caught here rather than silently reaching the graph.
    extra='forbid' — the Extraction Stage's whole job is to conform to
    this exact shape; unrecognized keys indicate the LLM drifted from
    the prompt's output contract and should fail validation, not pass
    through silently (Issue #11's lesson, applied to a new tool).
    """
    allegation_id:      str
    subject_id:         str
    confidence:         str  # "High" | "Medium" | "Unresolved" — Section 3.3's enum, shared with every other inferred relationship
    rationale:          str
    source_comment_ids: list[str] = Field(default_factory=list)

    @field_validator("confidence")
    @classmethod
    def _confidence_in_enum(cls, v: str) -> str:
        allowed = {"High", "Medium", "Unresolved"}
        if v not in allowed:
            raise ValueError(f"confidence must be one of {sorted(allowed)}, got {v!r}")
        return v

    model_config = {"extra": "forbid"}


class CorroborationCandidate(BaseModel):
    """
    A narrative confirmation of a structural relationship Wave 1 already
    asserted — the input Rule 14 (Extraction-Confirmed Relationship
    Elevation) reads to elevate Medium confidence to High.

    Section 6.2's Rule 14 worked example calls this
    comm.confirms_relationship_id, "set by the Extraction Stage". Without
    a candidate type for it, nothing in the pipeline ever populated that
    field, and Rule 14 would have had an input no code ever wrote —
    firing zero times forever while appearing to be implemented.

    relationship_ref is the elementId of the relationship being confirmed;
    comment_ref is the comment_id of the :Commentary node doing the
    confirming. Both are echoed back from what the Extraction Stage was
    shown, never invented — reasoning_layer/graph_load.py verifies each
    against the live graph before writing, because a valid SHAPE is not a
    valid VALUE.
    """
    relationship_ref: str
    comment_ref:      str
    rationale:        str
    model_config = {"extra": "forbid"}


class ExtractionResult(BaseModel):
    """
    Full validated output of one Extraction Stage run for one subject
    (dispatched from reasoning_layer.extraction_stage.run_extraction).
    'unresolved_allegation_ids' carries allegations the LLM explicitly
    could not attribute — kept distinct from an empty 'attributions'
    list so a downstream reader can tell "nothing to extract" apart
    from "extraction ran and found no confident attribution".
    """
    subject_id:                str
    attributions:              list[AttributionCandidate] = Field(default_factory=list)
    unresolved_allegation_ids: list[str] = Field(default_factory=list)
    corroborations:            list[CorroborationCandidate] = Field(default_factory=list)
    model_config = {"extra": "forbid"}