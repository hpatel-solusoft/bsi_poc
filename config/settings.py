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
    "SystemMemory"
])

TOP_LEVEL_SECTIONS = frozenset({
    "investigation",
    "similar_cases",
    "risk_assessment",
    "investigation_plan",
    "provenance_trail",
})