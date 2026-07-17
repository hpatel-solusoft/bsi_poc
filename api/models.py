from pydantic import BaseModel
from typing import Dict, List, Optional, Any

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


class RiskAssessmentRequest(BaseModel):
    case_id: str
    # ai_summary is optional — see SimilarCasesRequest for the resolution order.
    ai_summary: Optional[Dict[str, Any]] = None
    # Optional. Default False: if a risk_assessment (with a risk_score)
    # already exists for this case_id, skip re-running get_risk_rules /
    # calculate_risk_metrics and return the existing result. True: always
    # re-run and overwrite it.
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