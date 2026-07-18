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
    # No reload_ai_summary field, deliberately: unlike similar_cases/plan/
    # risk_assessment, a generated report is never cached-and-skipped —
    # Data Persistence Spec D.5 exists specifically so a report "can be
    # regenerated, compared across drafts". Every /generate_report call
    # assembles a fresh Related Network read and writes a new draft row;
    # there is no stale-result case to guard against.


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