# config/settings.py
# ----------------------------------------------------------------
# BSI POC - Centralized Service Configuration
# ----------------------------------------------------------------

SIMILAR_CASES_MAX_PER_TYPE    = 5
SIMILAR_CASES_MAX_TOTAL       = 5
SIMILAR_CASES_REQUIRED_STATUS = "Closed"
SIMILAR_CASES_LOOKBACK_YEARS  = 4
SIMILAR_CASES_BROAD_FETCH     = True
SIMILAR_CASES_FALLBACK_RAW    = True
# config/settings.py

# Entities that should be surfaced in the UI Provenance citations.
# Ignores noisy lookup tables like AddressType, StateCityZip, etc.
ALLOWED_ENTITIES = frozenset([
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
    "SystemMemory"
])

TOP_LEVEL_SECTIONS = frozenset({
    "investigation",
    "similar_cases",
    "risk_assessment",
    "investigation_plan",
    "provenance_trail",
})

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