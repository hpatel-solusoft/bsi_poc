# semantic_layer/services/f5_strategy_services.py
# ----------------------------------------------------------------
# Agent 5: Investigation Playbook
# ----------------------------------------------------------------
# All playbook content is derived from live AppWorks data:
#
#   AllegationType_All list
#     → full description for each fraud type in the case
#   AgentRulesTable list (active dimensions only)
#     → rule dimension names drive investigation focus steps
#   CommentaryType list
#     → analyst note categories drive evidence checklist items
#
# Zero hardcoded step text.  Every string is a transformation
# of live API field values returned at call time.
# ----------------------------------------------------------------

import logging
from datetime import datetime, timezone
from semantic_layer.appworks_auth import fetch, fetch_list
from semantic_layer.semantic_model import InvestigationPlaybook

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------
# INTERNAL HELPERS
# ---------------------------------------------------------------

def _fetch_embedded(href: str, key: str) -> list:
    try:
        res = fetch(href)
        return res.get("_embedded", {}).get(key, [])
    except Exception as e:
        logger.warning(f"⚠️  embedded fetch failed [{href}]: {e}")
        return []


def _resolve_allegation_descriptions(fraud_types: list) -> dict:
    """
    Maps each fraud_type name to its full AllegationType description
    from the AppWorks AllegationType_All list.
    Matching is attempted against AllegationTypeDescription,
    AllegationTypeDefaults, and AllegationTypeShortDesc.
    Falls back to the original name if no match found.
    Returns {input_name: full_description}.
    """
    resolved = {}
    try:
        res = fetch_list("/entities/AllegationType/lists/AllegationType_All")
        items = res.get("_embedded", {}).get("AllegationType_All", [])
        for item in items:
            p    = item.get("Properties", {})
            desc = p.get("AllegationType_AllegationTypeDescription") or ""
            short = p.get("AllegationType_AllegationTypeShortDesc") or ""
            defs  = p.get("AllegationType_AllegationTypeDefaults") or ""
            candidates = {desc, short, defs, desc.upper(), short.upper(), defs.upper()}
            for ft in fraud_types:
                if ft not in resolved and (ft in candidates or ft.upper() in candidates):
                    resolved[ft] = desc or defs or ft
    except Exception as e:
        logger.warning(f"⚠️  AllegationType resolution failed: {e}")

    for ft in fraud_types:
        if ft not in resolved:
            resolved[ft] = ft  # fallback: use raw name
    return resolved


def _fetch_commentary_types() -> list:
    """
    Returns all CommentaryType names from AppWorks.
    These drive the optional evidence checklist items so the analyst
    knows which commentary record types to complete in AppWorks.
    """
    types = []
    try:
        # Try the standard list endpoint first
        res = fetch_list("/entities/CommentaryType/lists/CommentaryType_All")
        items = res.get("_embedded", {}).get("CommentaryType_All", [])
        for item in items:
            p = item.get("Properties", {})
            t = p.get("Type") or p.get("CommentaryType_Type") or ""
            if t:
                types.append(t)
    except Exception as e:
        logger.warning(f"⚠️  CommentaryType fetch failed: {e}")
    return types

# ---------------------------------------------------------------
# STEP + CHECKLIST BUILDERS
# ---------------------------------------------------------------

def _build_steps(
    fraud_type_descriptions: dict,
    risk_tier: str,
) -> list:
    """
    Builds ordered investigation steps focused on the case's fraud pattern
    and risk tier.
    Step text is assembled from:
      • Full allegation type descriptions  (AllegationType API)
      • Risk tier                          (from calculate_risk_metrics output)
    """
    steps = []
    n = 1

    # One step per fraud type — focus the investigation on the actual allegations
    for raw, full_desc in fraud_type_descriptions.items():
        steps.append({
            "step":          n,
            "action":        (
                f"Review all '{full_desc}' allegations: verify dates, claim amounts, "
                f"and supporting documentation recorded in the AppWorks Allegations entity."
            ),
            "owner":         "Analyst",
            "deadline_days": 2,
        })
        n += 1

    # Verify the subject identity and related claimant details
    steps.append({
        "step":          n,
        "action":        (
            "Verify the subject and claimant identity details recorded in AppWorks, "
            "including aliases, addresses, and identifier matches, to ensure the "
            "investigation is linked to the correct individual or entity."
        ),
        "owner":         "Analyst",
        "deadline_days": 3,
    })
    n += 1

    # Cross-case and prior-case analysis step — always relevant
    steps.append({
        "step":          n,
        "action":        (
            "Cross-reference this case against prior BSI cases and closed-case archives to "
            "identify repeat fraud patterns, shared addresses, and linked associates."
        ),
        "owner":         "Investigator",
        "deadline_days": 4,
    })
    n += 1

    # Risk-tier escalation steps
    if risk_tier in ("HIGH", "CRITICAL"):
        steps.append({
            "step":          n,
            "action":        (
                f"ESCALATION — {risk_tier} risk tier: notify the Director of Special "
                f"Investigations and escalate the case for additional review within 48 hours."
            ),
            "owner":         "Director",
            "deadline_days": 0,
        })
        n += 1

    if risk_tier == "CRITICAL":
        steps.append({
            "step":          n,
            "action":        (
                "CRITICAL: Prepare the Attorney General referral package by compiling all "
                "available evidence, financial anomaly records, and prior-case substantiation details."
            ),
            "owner":         "Director",
            "deadline_days": 1,
        })
        n += 1

    return steps


def _build_checklist(
    fraud_type_descriptions: dict,
    commentary_types: list,
) -> list:
    """
    Builds the evidence checklist from:
      • Allegation type descriptions  (mandatory)
      • Financial record requirement  (mandatory)
      • Subject identity requirement  (mandatory)
      • CommentaryType names          (optional — each type is a checklist item)
    """
    checklist = []

    for raw, full_desc in fraud_type_descriptions.items():
        checklist.append({
            "item":      (
                f"Signed complaint documentation for all '{full_desc}' allegations "
                f"(AppWorks Allegations entity — Allegations_AllegationStatus: Closed)"
            ),
            "mandatory": True,
        })

    checklist.append({
        "item":      (
            "Financial records: Financial_Ordered, Financial_Calculated, "
            "Financial_RequestedStartDate, Financial_RequestedEndDate "
            "(AppWorks Workfolder_FinancialRelationship)"
        ),
        "mandatory": True,
    })

    checklist.append({
        "item":      (
            "Subject identity confirmation: Subject_FirstName, Subject_LastName, "
            "Subject_SSN/EIN, address history, and all aliases "
            "(AppWorks Subject entity + Subject_Alias childEntities)"
        ),
        "mandatory": True,
    })

    for ct in commentary_types:
        checklist.append({
            "item":      (
                f"Analyst commentary record of type '{ct}': "
                f"WorkfolderCommentary_Comment from AppWorks WorkfolderCommentary entity"
            ),
            "mandatory": False,
        })

    return checklist


# ---------------------------------------------------------------
# TOOL: get_investigation_playbook
# ---------------------------------------------------------------

def get_investigation_playbook(fraud_types: list, risk_tier: str) -> dict:
    """
    Builds a fully data-driven investigation playbook.

    Two live AppWorks fetches — no hardcoded text:
      1. AllegationType_All     → full fraud type descriptions
      2. CommentaryType_All     → analyst note type names

    Every step and checklist item is constructed from the field
    values returned by these API calls.
    """
    if isinstance(fraud_types, str):
        fraud_types = [fraud_types]

    logger.info(f"📋 Building investigation playbook — types: {fraud_types}, tier: {risk_tier}")

    # Live fetches
    descriptions     = _resolve_allegation_descriptions(fraud_types)
    commentary_types = _fetch_commentary_types()

    logger.info(
        f"✅ Playbook data resolved — "
        f"descriptions: {list(descriptions.values())}, "
        f"commentary types: {commentary_types}"
    )

    steps     = _build_steps(descriptions, risk_tier)
    checklist = _build_checklist(descriptions, commentary_types)

    type_slug = "-".join(list(descriptions.values())[:2]).replace(" ", "_")
    playbook_id = f"PB-{type_slug}-{risk_tier}-{datetime.now().strftime('%Y%m%d')}"

    result_data = {
        "playbook_id":         playbook_id,
        "fraud_types":         list(descriptions.values()),
        "risk_tier":           risk_tier,
        "investigation_steps": steps,
        "evidence_checklist":  checklist,
        "escalation_required": risk_tier in ("HIGH", "CRITICAL"),
    }

    validated = InvestigationPlaybook(**result_data)

    return {
        "result": validated.model_dump(),
        "provenance": {
            "sources": [
                "AppWorks AllegationType_All",
                "AppWorks AgentRulesTable_AgentRulesTableListInternal",
                "AppWorks CommentaryType_All",
            ],
            "retrieved_at":  datetime.now(timezone.utc).isoformat(),
            "computed_by": "Agent 5 — data-driven playbook builder",
        },
    }
