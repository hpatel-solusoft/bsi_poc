from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, field_validator, model_validator

from semantic_layer.entity_contracts import InvestigationStep

# -----------------------------------------------------------------------
# Request / response models
# -----------------------------------------------------------------------
#
# username and token are NOT fields on any request model below — they
# travel as headers instead (X-BSI-Username, Authorization: Bearer),
# extracted and validated by api/auth_headers.py's get_username/get_token
# FastAPI dependencies, which every route in api/server.py and
# api/services/*.py takes as parameters. See api/auth_headers.py's module
# docstring for why headers rather than the request body or query string.


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


class ReloadAllRequest(BaseModel):
    """POST /reload_all — force-refresh every ON-DEMAND tab for case_id
    in one call: /graph/ingest (structural AppWorks -> Neo4j re-sync,
    run_rules=True — the graph is both freshly loaded and freshly
    reasoned before anything else runs), then /intake, /similar_cases,
    /risk_assessment, /plan, each run with reload_ai_summary=True — in
    that exact dependency order. See the reload_all route's own
    docstring for why /graph/ingest has to run first, and why running
    it with run_rules=True means Wave 1/2 reasoning happens twice in
    one call (redundant, not incorrect — see that docstring)."""

    case_id: str


class ReloadStepResult(BaseModel):
    """One tab's outcome within a POST /reload_all run."""

    step: str
    # "success" | "failed" | "skipped". "skipped" only occurs after an
    # earlier step in the sequence failed — a step is never skipped for
    # any other reason.
    status: str
    duration_seconds: float
    # The underlying route's own meta.agent_summary_source /
    # meta.stale — "llm" and False respectively on a genuine reload,
    # since reload_ai_summary=True always forces a fresh LLM run. Only
    # present when status == "success".
    agent_summary_source: Optional[str] = None
    stale: Optional[bool] = None
    # Present only when status == "failed" — the underlying route's own
    # error detail, so a caller can distinguish e.g. a missing
    # OPENAI_API_KEY from an AppWorks fetch failure without re-deriving
    # it from an HTTP status code alone.
    error: Optional[str] = None


class ReloadAllResponse(BaseModel):
    """Response for POST /reload_all."""

    case_id: str
    # "success"  — every step succeeded.
    # "partial"  — at least one step succeeded before the first failure.
    # "failed"   — the first step (intake) itself failed.
    status: str
    duration_seconds: float
    steps: List[ReloadStepResult]


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
    contract (Functional Specification D2 Input Contract, v3 — AI-28/
    AI-33). Mirrors RejectInferenceRequest's contract: case_id + rule_id
    + investigator_id + reason, PLUS identifying the exact instance to
    revert — either match_id, or subject_id_a (+ subject_id_b where the
    rule family has one). Reverting is the exact inverse of POST
    /reject_inference and now targets exactly the ONE instance
    identified, never every currently-rejected fact this rule produced.

    investigator_id and reason are required for the same audit-trail
    reason they're required on /reject_inference: this overrules a
    prior rejection decision, so who did it and why must be recorded.
    """

    case_id: str
    rule_id: str
    investigator_id: str
    reason: str
    match_id: Optional[str] = None
    subject_id_a: Optional[str] = None
    subject_id_b: Optional[str] = None

    @field_validator("case_id", "rule_id", "investigator_id", "reason")
    @classmethod
    def must_be_non_blank(cls, value: str) -> str:
        """Reject empty or whitespace-only strings for these required fields."""
        if not value or not value.strip():
            raise ValueError("must be a non-empty string.")
        return value

    @model_validator(mode="after")
    def must_identify_one_instance(self) -> "RevertRejectionRequest":
        """
        v3 contract (AI-28): there is no more bulk "every instance this
        rule rejected for this case" mode — the caller must identify
        exactly which instance to revert, either by match_id (the
        opaque token rule_audit.py/fraud_network.py stamp onto every
        row/edge) or by subject_id_a (the same field, present on every
        one of those rows/edges directly).
        """
        if not (self.match_id and self.match_id.strip()) and not (
            self.subject_id_a and self.subject_id_a.strip()
        ):
            raise ValueError(
                "must identify the exact instance to revert: provide either "
                "match_id, or subject_id_a (+ subject_id_b where the rule "
                "family has one)."
            )
        return self


class RevertedItem(BaseModel):
    """One instance restored to active by a revert_rejection call."""

    subject_id_a: Optional[str] = None
    subject_id_b: Optional[str] = None
    match_id: Optional[str] = None


class CascadeChange(BaseModel):
    """
    One downstream fact reasoning_layer/cascade.py's DOWNSTREAM_DEPENDENTS
    walk (AI-30) auto-invalidated or reinstated as a side effect of this
    reject/revert. action is "auto_invalidated" (reject direction —
    invalidated_by_rule_id names which upstream rule broke the
    downstream rule's condition) or "reinstated" (revert direction —
    invalidated_by_rule_id is always null here, since a reinstated fact
    is no longer invalidated by anything). reason is the investigator's
    own reason text for the ORIGINAL upstream reject/revert action that
    triggered this cascade hop — the same text all the way down a
    multi-hop chain (Rule 1 -> Rule 2 -> Rule 8 all show the SAME
    reason, the one given for Rule 1), so a caller looking at any one
    downstream fact can see why it changed without a second lookup.
    investigator_id is the investigator who issued the UPSTREAM reject/
    revert that triggered this cascade hop (the same person named on
    the top-level rejected_items/reverted_items entry, threaded down
    every hop of a multi-level chain) — full audit parity with a
    manually-rejected fact's own rejected_by field. changed_at is when
    this specific hop was written (shared across every hop of one
    cascade walk, since the whole walk happens inside the same reject/
    revert call and is timestamped once).
    """

    rule_id: str
    subject_id: str
    action: str
    invalidated_by_rule_id: Optional[str] = None
    reason: Optional[str] = None
    investigator_id: Optional[str] = None
    changed_at: Optional[str] = None


class RevertRejectionResponse(BaseModel):
    """What the UI needs to flip the reverted row back to un-rejected."""

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
    cascade_changes: List[CascadeChange] = []
    # AI-31: the value just written to (:Case).last_inference_change_at
    # by this call — see reasoning_layer/rejection.py's
    # _touch_case_last_inference_change.
    last_inference_change_at: Optional[str] = None
    model_config = {"extra": "allow"}


class RejectInferenceRequest(BaseModel):
    """
    POST /reject_inference — the Human-in-the-Loop "Reject" button's
    HTTP contract (Functional Specification D2 Input Contract, v3 —
    AI-28/AI-33).

    v3 contract: case_id, rule_id, reason, investigator_id — all
    required, as before — PLUS identifying the exact match to act on:
    either match_id, or subject_id_a (+ subject_id_b where the rule
    family has one). A rule can match more than one pair of subjects
    (e.g. A-B share an address, and separately A-C share an address);
    an investigator may agree with one match and disagree with another,
    so this endpoint now rejects exactly the ONE instance identified —
    never every currently-active fact rule_id produced for the case.

    match_id and subject_id_a/subject_id_b are exactly the fields
    reasoning_layer/rule_audit.py and reasoning_layer/fraud_network.py
    already stamp onto every row/edge they return, so the frontend's
    Reject button reads them straight off the clicked row — no
    per-rule-family subject-pairing knowledge required on the client
    side (see reasoning_layer/rejection.py's module docstring for that
    encoding, which stays entirely server-side).

    reason is required so there is always a record of why a specific
    match was overruled. investigator_id is required so the :Rejection
    audit trail records who made that call — see
    reasoning_layer/rejection.py's module docstring ATTRIBUTION NOTE.
    """

    case_id: str
    rule_id: str
    reason: str
    investigator_id: str
    match_id: Optional[str] = None
    subject_id_a: Optional[str] = None
    subject_id_b: Optional[str] = None

    @field_validator("case_id", "rule_id", "reason", "investigator_id")
    @classmethod
    def must_be_non_blank(cls, value: str) -> str:
        """Reject empty or whitespace-only strings for these required fields."""
        if not value or not value.strip():
            raise ValueError("must be a non-empty string.")
        return value

    @model_validator(mode="after")
    def must_identify_one_instance(self) -> "RejectInferenceRequest":
        """
        v3 contract (AI-28): there is no more bulk "every currently-active
        fact this rule produced" mode — the caller must identify exactly
        which match to reject, either by match_id or by subject_id_a
        (+ subject_id_b where the rule family has one). See
        RevertRejectionRequest.must_identify_one_instance — identical
        rule, the exact inverse action.
        """
        if not (self.match_id and self.match_id.strip()) and not (
            self.subject_id_a and self.subject_id_a.strip()
        ):
            raise ValueError(
                "must identify the exact match to reject: provide either "
                "match_id, or subject_id_a (+ subject_id_b where the rule "
                "family has one)."
            )
        return self


class RejectedItem(BaseModel):
    """One instance rejected by a reject_inference call."""

    subject_id_a: Optional[str] = None
    subject_id_b: Optional[str] = None
    match_id: Optional[str] = None


class RejectInferenceResponse(BaseModel):
    """Response for POST /reject_inference (D2 Output Contract, v3)."""

    accepted: bool
    case_id: str
    rule_id: str
    relationship_type: str
    reason: str
    investigator_id: str
    rejected_count: int
    rejected_items: List[RejectedItem] = []
    rejected_at: str
    cascade_changes: List[CascadeChange] = []
    # AI-31: the value just written to (:Case).last_inference_change_at
    # by this call — see reasoning_layer/rejection.py's
    # _touch_case_last_inference_change.
    last_inference_change_at: Optional[str] = None
    model_config = {"extra": "allow"}


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


class GraphNode(BaseModel):
    """One node of the full case subgraph.

    `id` is label-prefixed ("Subject:658636801") because case_id and
    subject_id are drawn from the same numeric space in this data — a
    bare id would collide between a :Case and a :Subject. `key` carries
    the bare business key for callers that need it (the reject flow).
    """

    id: str
    ref: Optional[str] = None
    label: str
    labels: List[str] = []
    key: Optional[str] = None
    display_name: Optional[str] = None
    is_case_subject: bool = False
    stable_id: bool = True
    properties: Dict[str, Any] = {}


class GraphEdge(BaseModel):
    """One relationship of the full case subgraph.

    subject_id_a / subject_id_b / rule_id are populated on
    subject-to-subject edges only; together with relationship_type they
    are exactly the POST /reject_inference parameters, pre-resolved so
    the UI reads them off the clicked edge. match_id (v3 contract,
    AI-28/AI-33) is the same instance wrapped into one opaque token, for
    a caller that would rather send that instead.
    """

    id: Optional[str] = None
    source: str
    target: str
    relationship_type: str
    confidence: Optional[str] = None
    status: str = "active"
    source_rule: Optional[str] = None
    inferred: bool = False
    rejectable: bool = False
    subject_id_a: Optional[str] = None
    subject_id_b: Optional[str] = None
    rule_id: Optional[str] = None
    # v3 instance-level Reject/Revert contract (AI-28/AI-33). Without
    # this field, reasoning_layer.fraud_network._build_edges's match_id
    # is computed correctly but silently dropped here, since a
    # response_model strips any key it doesn't declare.
    match_id: Optional[str] = None
    properties: Dict[str, Any] = {}


class CaseGraph(BaseModel):
    """Everything related to the case: all nodes, all relationships."""

    nodes: List[GraphNode] = []
    edges: List[GraphEdge] = []
    node_count: int = 0
    edge_count: int = 0
    node_counts_by_label: Dict[str, int] = {}
    edge_counts_by_type: Dict[str, int] = {}
    truncated: bool = False


class FraudNetworkResponse(BaseModel):
    """Response for GET /fraud_network/{case_id} (D3 Output Contract).

    `graph` is the full case subgraph. `networks`/`network_count` are
    the original FraudNetwork-only groupings, retained unchanged so the
    current screen keeps working while the frontend migrates.

    AI-31: no dedicated top-level staleness field is added here on
    purpose — reasoning_layer/fraud_network.py's subgraph query already
    returns full properties(case_node) as part of `graph.nodes` (see
    GraphNode.properties), so (:Case).last_inference_change_at (and the
    AI-30 auto_invalidated/invalidated_by_rule_id pair on any
    MEMBER_OF_FRAUD_NETWORK edge) is already present there with no query
    change — find the node with labels containing "Case" and read
    properties.last_inference_change_at off it. Confirmed by
    tests/test_fraud_network_case_staleness.py.
    """

    case_id: str
    case_found: bool = True
    graph: CaseGraph = CaseGraph()
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
    # v3 instance-level Reject/Revert contract (AI-28/AI-33). Without
    # this field, reasoning_layer.rule_audit.get_rule_audit's match_id
    # is computed correctly but silently dropped here, since a
    # response_model strips any key it doesn't declare.
    match_id: Optional[str] = None
    # AI-30/AI-31: cascade attribution (reasoning_layer/cascade.py's
    # DOWNSTREAM_DEPENDENTS walk) — who auto-invalidated or reinstated
    # this fact as a side effect of a DIFFERENT rule's reject/revert,
    # which upstream rule triggered it, when, and why. Populated for
    # Rule_02/04/06/08/09/13 rows (every rule cascade.py can ever
    # auto-invalidate/reinstate); None for every other rule_id, and for
    # Rule_09 specifically even among those six (it can never itself be
    # a cascade target — see reasoning_layer/rule_audit.py's Rule_09
    # query comment). Same reason match_id above needs an explicit
    # field: an undeclared key is silently stripped by this
    # response_model, not merely omitted.
    auto_invalidated: Optional[bool] = None
    invalidated_by_rule_id: Optional[str] = None
    invalidated_reason: Optional[str] = None
    invalidated_by_investigator: Optional[str] = None
    invalidated_at: Optional[str] = None
    reinstated_by_rule_id: Optional[str] = None
    reinstated_reason: Optional[str] = None
    reinstated_by_investigator: Optional[str] = None
    reinstated_at: Optional[str] = None


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
    # AI-31: case-wide graph-change staleness signal — see
    # reasoning_layer/rejection.py's _touch_case_last_inference_change.
    # None until the first reject_inference/revert_rejection call for
    # this case.
    last_inference_change_at: Optional[str] = None