from datetime import datetime

from pydantic import BaseModel, field_validator

from typing import Dict, List, Optional, Any

from semantic_layer.entity_contracts import InvestigationStep

# -----------------------------------------------------------------------
# Request / response models
# -----------------------------------------------------------------------


class intakeRequest(BaseModel):
    case_id: str
    # Optional. Default False: if intake has already run for this case_id
    # (found warm in CS-4 or in the PostgreSQL case_ai_summary_store
    # fallback), skip re-running the intake agent/tools/reasoning pipeline
    # and return the existing result. True: always re-run — the intake
    # agent, its tools, and (via Context Enrichment) the Neo4j reasoning
    # pipeline — and persist the fresh result to PostgreSQL and Neo4j,
    # regardless of whether intake ran before.
    reload_ai_summary: bool = False


class SimilarCasesRequest(BaseModel):
    case_id: str
    # ai_summary is now OPTIONAL (Data Persistence Spec v1.0, Section D.1).
    # AppWorks sends case_id only; the server resolves case_data from
    # CASE_STORE (CS-4) and, on a miss, from the PostgreSQL
    # case_ai_summary_store fallback. ai_summary remains accepted for
    # explicit-override / legacy callers only.
    ai_summary: Optional[Dict[str, Any]] = None
    # Optional. Default False: if a similar_cases result already exists
    # for this case_id, skip re-running search_similar_cases and return
    # the existing result. True: always re-run and overwrite it.
    reload_ai_summary: bool = False


class PlanRequest(BaseModel):
    case_id: str
    # ai_summary is optional — see SimilarCasesRequest for the resolution order.
    ai_summary: Optional[Dict[str, Any]] = None
    # Optional. Default False: if an investigation_plan already exists for
    # this case_id, skip re-running get_investigation_plan and return the
    # existing result. True: always re-run and overwrite it.
    reload_ai_summary: bool = False


class ModifyInvestigationStepsRequest(BaseModel):
    """
    POST /plan/modify_investigation_steps — the Investigation Plan
    "Modify" popup contract (Data Persistence Spec v1.0, Section D.6;
    Modify Investigation Steps flow).

    Overrides investigation_steps only. evidence_checklist,
    escalation_criteria, fraud_types, risk_tier, and the narrative
    summary are never accepted here — they remain AI-generated at all
    times, per the Section D.6 scope rule.
    """
    case_id: str
    # Reuses the same {step, action, owner?, deadline_days?} shape the
    # AI-generated plan already uses (semantic_layer.entity_contracts.
    # InvestigationStep) — the investigator is editing the same list,
    # not authoring a different one.
    steps: List[InvestigationStep]
    comment: Optional[str] = None
    investigator_id: str

    @field_validator("steps")
    @classmethod
    def steps_must_be_non_empty(cls, value: List[InvestigationStep]) -> List[InvestigationStep]:
        """Reject a save with no steps — that is what "Revert to AI Plan" is for."""
        if not value:
            raise ValueError("steps must contain at least one investigation step.")
        return value

    @field_validator("investigator_id")
    @classmethod
    def investigator_id_must_be_non_blank(cls, value: str) -> str:
        """modified_by must be attributable — never store an anonymous override."""
        if not value or not value.strip():
            raise ValueError("investigator_id must be a non-empty string.")
        return value


class ModifyInvestigationStepsResponse(BaseModel):
    """Response for POST /plan/modify_investigation_steps."""
    case_id: str
    status: str
    plan_source: str
    modified_by: str
    modified_on: datetime


class RevertToAiPlanRequest(BaseModel):
    """POST /plan/revert_to_ai — deletes case_id's saved override."""
    case_id: str


class RevertToAiPlanResponse(BaseModel):
    """Response for POST /plan/revert_to_ai."""
    case_id: str
    status: str
    plan_source: str


class InvestigationStepsResponse(BaseModel):
    """
    Response for GET /plan/modify_investigation_steps/{case_id}.

    investigation_steps is always the single, current list — never two
    parallel fields with one left null depending on source. Which
    table it came from is carried entirely by
    is_modify_investigation_steps, so a caller checks one flag rather
    than inspecting which of two fields is populated.
    """
    case_id: str
    investigation_steps: List[InvestigationStep]
    # True  -> investigation_steps came from investigation_plan_overrides
    #          (the investigator's saved edit).
    # False -> investigation_steps came from case_ai_summary_store
    #          (the AI-generated / last-cached plan; no override exists).
    is_modify_investigation_steps: bool


class RiskAssessmentRequest(BaseModel):
    case_id: str
    # ai_summary is optional — see SimilarCasesRequest for the resolution order.
    ai_summary: Optional[Dict[str, Any]] = None
    # Optional. Default False: if a risk_assessment (with a risk_score)
    # already exists for this case_id, skip re-running get_risk_rules /
    # calculate_risk_metrics and return the existing result. True: always
    # re-run and overwrite it.
    reload_ai_summary: bool = False


class ReportGenerationRequest(BaseModel):
    case_id: str
    # ai_summary is optional — see SimilarCasesRequest for the resolution order.
    ai_summary: Optional[Dict[str, Any]] = None
    # Optional. Default False: if a report has already been generated and
    # persisted for this case_id (report_artifacts, D.5), skip re-running
    # the Related Network assembly, Decision Log build, and the LLM
    # narration, and return the latest persisted draft instead. True:
    # always re-run the full pipeline and persist a fresh draft row.
    reload_ai_summary: bool = False


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
    # Optional. Default False: Copilot always answers the question, but by
    # default it does not force the Neo4j reasoning pipeline to re-run for
    # this case's subject before answering — it answers against whatever
    # graph_context is already cached. True: force Context Enrichment to
    # re-run the reasoning pipeline for the subject first (even if it
    # already completed), refresh graph_context/graph_signals/rules_fired
    # in PostgreSQL and Neo4j, then answer using the refreshed context.
    reload_ai_summary: bool = False


class ConversationTurn(BaseModel):
    """One transcript turn in the user/assistant shape /copilot uses."""
    role: str
    content: str


class ConversationHistoryResponse(BaseModel):
    """
    GET /copilot/{case_id} response.

    conversation_history mirrors the field /copilot returns — the ordered
    user/assistant transcript, oldest first. conversation_history_source
    reports where it was resolved from (CS-4 warm store vs the PostgreSQL
    conversation_history table) for support/observability, matching the
    conversation_history_source field on the /copilot response.
    """
    case_id: str
    conversation_history: List[ConversationTurn]
    conversation_history_source: str

class GraphIngestRequest(BaseModel):
    """
    POST /graph/ingest — the AppWorks Lifecycle-event contract.

    Today this endpoint is called by hand (or by etl/run_sync.py, which
    calls the same service function directly). It is shaped for what
    AppWorks will send once the lifecycle event is wired up: the case that
    changed, and nothing else. Everything AppWorks would have to know to
    populate any other field is something the server can work out for
    itself, and every such field would be one more thing to keep in sync
    across two systems.

    case_ids  — one for a lifecycle event, many for a POC/demo backfill.
    run_rules — false loads structural data into Neo4j without reasoning
                over it. Useful when staging a large backfill and running
                the rules as a separate step; never the default, because a
                loaded-but-unreasoned graph looks complete and is not.

    There is no "subjects" selector: reasoning always runs for every subject
    on the case. The pipeline is scoped per (case, subject), and only a
    subject with its own run gets the ALLEGATION_LIKELY_AGAINST_SUBJECT
    attribution edges the Wave 2 network rules need — reasoning only the
    primary would silently starve those rules of their other endpoints.
    """
    case_ids: List[str]
    run_rules: bool = True


# -----------------------------------------------------------------------
# D2 — POST /reject_inference (Functional Specification D2;
# reasoning_layer/rejection.py)
# -----------------------------------------------------------------------

class RevertRejectionRequest(BaseModel):
    """
    POST /revert_rejection — the Case Summary "Revert" button's HTTP
    contract. Mirrors RejectInferenceRequest's contract: case_id +
    rule_id + investigator_id + reason. Reverting is a bulk action, the
    exact inverse of POST /reject_inference — it restores every
    currently-rejected fact this rule produced for this case.
    investigator_id and reason are required for the same audit-trail
    reason they're required on /reject_inference: this overrules a
    prior rejection decision, so who did it and why must be recorded.
    """
    case_id: str
    rule_id: str
    investigator_id: str
    reason: str

    @field_validator("case_id", "rule_id", "investigator_id", "reason")
    @classmethod
    def must_be_non_blank(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("must be a non-empty string.")
        return value


class RevertedItem(BaseModel):
    """One instance restored to active by a bulk revert."""
    subject_id_a: Optional[str] = None
    subject_id_b: Optional[str] = None


class RevertRejectionResponse(BaseModel):
    """What the UI needs to flip the rule's rows back to un-rejected."""
    reverted: bool
    case_id: str
    rule_id: str
    relationship_type: str
    investigator_id: str
    reason: str
    status: str
    reverted_count: int
    reverted_items: List[RevertedItem] = []
    reverted_at: Optional[str] = None
    model_config = {"extra": "allow"}

class RejectInferenceRequest(BaseModel):
    """
    POST /reject_inference — the Human-in-the-Loop "Reject" button's
    HTTP contract (Functional Specification D2 Input Contract, v2).

    v2 contract: exactly the four fields the frontend can actually
    supply for a rule row — case_id, rule_id, reason, investigator_id —
    all required. There is no subject_id_a/subject_id_b/relationship_type
    here any more: the frontend has no reliable way to know the internal
    subject pairing a rule matched on for a given case, so this endpoint
    now rejects every currently-active fact rule_id produced within
    case_id's reasoning scope in one bulk operation, rather than one
    caller-identified edge. See reasoning_layer/rejection.py's module
    docstring for the full per-rule-family breakdown of what "every
    fact this rule produced" means.

    reason is required (not optional) precisely because this is now a
    bulk action — it is the only record of why a rule's entire output
    for a case was overruled. investigator_id is required so the
    :Rejection audit trail records who made that call — see
    reasoning_layer/rejection.py's module docstring ATTRIBUTION NOTE.
    """
    case_id: str
    rule_id: str
    reason: str
    investigator_id: str

    @field_validator("case_id", "rule_id", "reason", "investigator_id")
    @classmethod
    def must_be_non_blank(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("must be a non-empty string.")
        return value


class RejectedItem(BaseModel):
    """One instance rejected by a bulk reject_inference call."""
    subject_id_a: Optional[str] = None
    subject_id_b: Optional[str] = None


class RejectInferenceResponse(BaseModel):
    """Response for POST /reject_inference (D2 Output Contract, v2)."""
    accepted: bool
    case_id: str
    rule_id: str
    relationship_type: str
    reason: str
    investigator_id: str
    rejected_count: int
    rejected_items: List[RejectedItem] = []
    rejected_at: str


# -----------------------------------------------------------------------
# D3 — GET /fraud_network/{case_id} (Functional Specification D3;
# reasoning_layer/fraud_network.py)
# -----------------------------------------------------------------------

class FraudNetworkNode(BaseModel):
    id: str
    display_name: Optional[str] = None
    is_primary: bool = False


class FraudNetworkEdge(BaseModel):
    source: str
    target: str
    relationship_type: str
    confidence: Optional[str] = None
    status: str
    source_rule: Optional[str] = None


class FraudNetworkBlock(BaseModel):
    network_type: str
    network_key: Optional[str] = None
    formed_by_rule: Optional[str] = None
    confidence: str
    nodes: List[FraudNetworkNode]
    edges: List[FraudNetworkEdge]


class FraudNetworkResponse(BaseModel):
    """Response for GET /fraud_network/{case_id} (D3 Output Contract)."""
    case_id: str
    networks: List[FraudNetworkBlock]
    network_count: int


# -----------------------------------------------------------------------
# D4 — GET /rule_audit/{case_id} (Functional Specification D4;
# reasoning_layer/rule_audit.py)
# -----------------------------------------------------------------------

class InferredRelationship(BaseModel):
    subject_id_a: str
    subject_id_b: Optional[str] = None
    relationship_type: str
    confidence: str
    asserted_at: Optional[str] = None
    corroborated: bool = False
    status: str


class RuleAuditEntry(BaseModel):
    rule_id: str
    rule_description: str
    fired: bool
    inferred_relationships: List[InferredRelationship]


class RuleAuditResponse(BaseModel):
    """Response for GET /rule_audit/{case_id} (D4 Output Contract)."""
    case_id: str
    primary_subject_id: Optional[str] = None
    rules: List[RuleAuditEntry]