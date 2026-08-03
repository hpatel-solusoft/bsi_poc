# config/settings.py
# ----------------------------------------------------------------
# BSI POC - Centralized Service Configuration
# ----------------------------------------------------------------

SIMILAR_CASES_MAX_PER_TYPE = 5
SIMILAR_CASES_MAX_TOTAL = 5
SIMILAR_CASES_REQUIRED_STATUS = "Closed"
SIMILAR_CASES_LOOKBACK_YEARS = 4
SIMILAR_CASES_BROAD_FETCH = True
SIMILAR_CASES_FALLBACK_RAW = True
# config/settings.py

# Entities that should be surfaced in the UI Provenance citations.
# Ignores noisy lookup tables like AddressType, StateCityZip, etc.
ALLOWED_ENTITIES = frozenset(
    [
        "Workfolder",
        "Subject",
        "SubjectDetail",
        "Allegation",
        "Financial",
        "Agency",
        "FraudRiskRule",
        "Subject_SubjectWorkfolderMapping",
        "AllegationType_ManageAllegationType",
        "AllegationTypeTask",
        "SystemMemory",
    ]
)

TOP_LEVEL_SECTIONS = frozenset(
    {
        "investigation",
        "similar_cases",
        "risk_assessment",
        "investigation_plan",
        "provenance_trail",
    }
)

# ----------------------------------------------------------------
# Neo4j-derived fields that must NEVER be persisted to PostgreSQL
# (case_ai_summary_store) or reused across requests without a fresh
# Neo4j read. Case Summary, Similar Cases, Risk Assessment, and
# Investigation Plan all recompute these live on every request (see
# api.pipeline_execution.fetch_live_graph_findings /
# fetch_live_similar_cases / fetch_live_risk_signals /
# fetch_live_rule_aware_tasks); only the LLM-authored narrative text
# for each tab (core.case_store.AGENT_SUMMARY_CACHE_KEY) is cached.
# Enforced centrally by core.persistence_filters.strip_graph_derived_fields.
# ----------------------------------------------------------------

# investigation.* keys — Case Summary tab's graph findings (AI-12/AI-13).
GRAPH_DERIVED_INVESTIGATION_KEYS = frozenset(
    {
        "network_match_flag",
        "graph_context",
        "graph_signals",
        "rules_fired",
    }
)

# Top-level sections that are entirely Neo4j-derived — Similar Cases
# tab's structural graph matches (AI-14).
GRAPH_DERIVED_TOP_LEVEL_SECTIONS = frozenset({"similar_cases"})

# risk_assessment.* keys — Risk Assessment tab's graph signal add-ons
# (AI-15). risk_score/risk_tier are handled separately (see
# core.persistence_filters.strip_graph_derived_fields) since they are
# augmented in place rather than added under a new key.
GRAPH_DERIVED_RISK_ASSESSMENT_KEYS = frozenset({"neo4j_signals"})

# investigation_plan.* keys — Investigation Plan tab's rule-derived
# task recommendations (AI-16).
GRAPH_DERIVED_PLAN_KEYS = frozenset({"rule_aware_tasks"})

# ----------------------------------------------------------------
# Agent Operational Store (PostgreSQL) — Data Persistence and
# Synchronisation Specification v1.0, Section D.
# Connection details (POSTGRES_HOST/PORT/DB/USER/PASSWORD or
# DATABASE_URL) are read from the environment in core/db.py, not here —
# this file holds pure constants, not secrets.
# ----------------------------------------------------------------

DB_POOL_MIN_CONN = 1
DB_POOL_MAX_CONN = 10

# D.2: conversation_history retains a rolling window of the most recent
# turns per case. A "turn" is one message (user or assistant), so 20
# turns is 10 question/answer exchanges.
CONVERSATION_HISTORY_MAX_TURNS = 20