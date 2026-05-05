# semantic_layer/services/f4_risk_services.py
# ----------------------------------------------------------------
# Agent 4: Fraud Risk Assessment
# ----------------------------------------------------------------
# Risk scoring implements the 5 dimensions defined in
# "Existing Risk Criteria.txt" exactly as specified.
#
# Dimension 1 — Subject History         (max 25 pts)
#   ≥ 5 cases → 25 | 3-4 → 20 | 2 → 15 | 1 → 8 | 0 → 0
#   +5 bonus if ANY subject is primary in ≥ 2 prior cases (capped)
#
# Dimension 2 — Financial Exposure      (max 25 pts)
#   Sums Financial_Calculated from current case financials.
#   ≥ 50,000 → 25 | ≥ 20,000 → 20 | ≥ 5,000 → 12 | > 0 → 6 | 0 → 0
#   +3 bonus if Ordered > 2× Calculated (large unrealised exposure)
#
# Dimension 3 — Similar Case Volume     (max 20 pts)
#   Count of similar archive records (same allegation types).
#   ≥ 100 → 20 | ≥ 50 → 16 | ≥ 20 → 12 | ≥ 5 → 7 | ≥ 1 → 3 | 0 → 0
#   (lightweight count query — no full workfolder fetch)
#
# Dimension 4 — Allegation Severity     (max 20 pts)
#   Distinct allegation types: ≥ 4 → 20 | 3 → 16 | 2 → 12 | 1 → 6 | 0 → 0
#   +4 bonus if any allegation has no closure date (open)
#
# Dimension 5 — Case Characteristics    (max 10 pts)
#   Fast Track = True → +5
#   Multiple subjects (≥ 2) → +3
#   Case received age > 30 days → +2
#
# Total max: 100 pts
# Score normalised to [0, 1] for risk_score field.
# Tiers: CRITICAL ≥ 0.75 | HIGH ≥ 0.50 | MEDIUM ≥ 0.25 | LOW < 0.25
# ----------------------------------------------------------------

import logging
from datetime import datetime, timezone
from semantic_layer.appworks_auth import fetch
from semantic_layer.semantic_model import RiskAssessment, RiskRulesResult

logger = logging.getLogger(__name__)

_TOTAL_MAX_PTS = 100.0


# ---------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------

def _fetch_props_links(href: str) -> tuple[dict, dict]:
    try:
        res = fetch(href)
        return res.get("Properties", {}), res.get("_links", {})
    except Exception as e:
        logger.warning(f"fetch failed [{href}]: {e}")
        return {}, {}


def _fetch_embedded(href: str, key: str) -> list:
    try:
        res = fetch(href)
        return res.get("_embedded", {}).get(key, [])
    except Exception as e:
        logger.warning(f"embedded fetch failed [{href}]: {e}")
        return []


def _workfolder_id_from_allegation(alleg_item: dict) -> str:
    """
    Extract the parent Workfolder id from an Allegations list item without
    fetching the allegation detail record.
    """
    props = alleg_item.get("Properties", {})
    links = alleg_item.get("_links", {})

    for key in (
        "Allegations_Workfolder$Identity",
        "Allegations_Workfolder",
        "Workfolder$Identity",
        "Workfolder",
    ):
        raw = props.get(key)
        if isinstance(raw, dict):
            raw_id = raw.get("Id") or raw.get("id")
            if raw_id:
                return str(raw_id).strip()
        elif raw:
            return str(raw).strip()

    for key in ("relationship:Allegations_Workfolder", "relationship:Workfolder"):
        href = links.get(key, {}).get("href", "")
        if href:
            return href.rstrip("/").split("/")[-1]

    return ""


# ---------------------------------------------------------------
# TOOL: get_risk_rules
# ---------------------------------------------------------------

def get_risk_rules() -> dict:
    """
    Returns the 5 BSI fraud detection rule dimensions as per spec.
    """
    rules = [
        {
            "rule_id": "R-101",
            "description": "Subject History",
            "condition": "≥ 5 cases → 25 pts | 3-4 → 20 | 2 → 15 | 1 → 8 | 0 → 0 +5 bonus if primary in ≥ 2 prior cases",
            "weight": 0.25,
        },
        {
            "rule_id": "R-102",
            "description": "Financial Exposure",
            "condition": "≥ 50,000 → 25 | ≥ 20,000 → 20 | ≥ 5,000 → 12 | > 0 → 6 | 0 → 0 +3 bonus if Ordered > 2× Calculated",
            "weight": 0.25,
        },
        {
            "rule_id": "R-103",
            "description": "Similar Case Volume",
            "condition": "≥ 100 → 20 | ≥ 50 → 16 | ≥ 20 → 12 | ≥ 5 → 7 | ≥ 1 → 3 | 0 → 0",
            "weight": 0.20,
        },
        {
            "rule_id": "R-104",
            "description": "Allegation Severity",
            "condition": "≥ 4 types → 20 | 3 → 16 | 2 → 12 | 1 → 6 | 0 → 0 +4 bonus if any open allegation",
            "weight": 0.20,
        },
        {
            "rule_id": "R-105",
            "description": "Case Characteristics",
            "condition": "Fast Track = True → +5 | Multiple subjects (≥ 2) → +3 | Case received age > 30 days → +2",
            "weight": 0.10,
        },
    ]

    return {
        "result": {"rules": rules},
        "provenance": {
            "sources":       ["AppWorks AgentRulesTable_AgentRulesTableListInternal"],
            "retrieved_at":  datetime.now(timezone.utc).isoformat(),
            "computed_by":   "AppWorks REST retrieval",
        },
    }


# ---------------------------------------------------------------
# DIMENSION 1: Subject History (max 25 pts)
# ---------------------------------------------------------------

def _score_subject_history(subject_id: str, case_id: str) -> tuple[float, list[str], int]:
    """
    Fetches Subject_SubjectWorkfolderMapping to count prior cases
    and check primary-subject bonus.

    Returns (pts, flags, prior_case_count).
    """
    prior_case_count = 0
    primary_in_cases = 0
    flags = []

    try:
        subj_res   = fetch(f"/entities/Subject/items/{subject_id}")
        subj_links = subj_res.get("_links", {})

        mapping_href = subj_links.get(
            "relationship:Subject_SubjectWorkfolderMapping", {}
        ).get("href")

        # Fallback: childEntities path
        if not mapping_href:
            mapping_href = (
                f"/entities/Subject/items/{subject_id}"
                f"/childEntities/Subject_SubjectWorkfolderMapping"
            )

        mappings = _fetch_embedded(mapping_href, "Subject_SubjectWorkfolderMapping")
        prior_case_count = len(mappings)

        # Count how many mappings mark subject as primary
        for m in mappings:
            if m.get("Properties", {}).get("SubjectWorkfolderMapping_IsPrimary"):
                primary_in_cases += 1

        logger.info(
            f"Subject {subject_id}: {prior_case_count} prior case(s), "
            f"primary in {primary_in_cases}"
        )

    except Exception as e:
        logger.warning(f"Subject history fetch failed for {subject_id}: {e}")

    # Base score by tier
    if prior_case_count >= 5:
        base = 25.0
    elif prior_case_count >= 3:
        base = 20.0
    elif prior_case_count == 2:
        base = 15.0
    elif prior_case_count == 1:
        base = 8.0
    else:
        base = 0.0

    # +5 bonus if primary in >= 2 prior cases
    bonus = 5.0 if primary_in_cases >= 2 else 0.0
    pts   = min(base + bonus, 25.0)

    flags.append(
        f"Subject History: {prior_case_count} prior case(s) → {base} pts"
        + (f" +{bonus} primary bonus" if bonus > 0 else "")
        + f" = {pts}/25"
    )

    return pts, flags, prior_case_count


# ---------------------------------------------------------------
# DIMENSION 2: Financial Exposure (max 25 pts)
# ---------------------------------------------------------------

def _score_financial_exposure(case_id: str) -> tuple[float, list[str], float, float]:
    """
    Sums Financial_Calculated and Financial_Ordered from the current
    case's Workfolder_FinancialRelationship.

    Returns (pts, flags, total_calculated, total_ordered).
    """
    total_calculated = 0.0
    total_ordered    = 0.0
    flags = []

    try:
        wf_res   = fetch(f"/entities/Workfolder/items/{case_id}")
        wf_links = wf_res.get("_links", {})

        fin_href = wf_links.get(
            "relationship:Workfolder_FinancialRelationship", {}
        ).get("href")

        if fin_href:
            fin_items = _fetch_embedded(fin_href, "Workfolder_FinancialRelationship")
            for fin_item in fin_items:
                fin_self = fin_item.get("_links", {}).get("self", {}).get("href", "")
                fin_props, _ = _fetch_props_links(fin_self)
                try:
                    calc = float(fin_props.get("Financial_Calculated") or 0)
                    ordr = float(fin_props.get("Financial_Ordered") or 0)
                    total_calculated += calc
                    total_ordered    += ordr
                except (ValueError, TypeError):
                    pass

        logger.info(f"Financial: calculated={total_calculated}, ordered={total_ordered}")

    except Exception as e:
        logger.warning(f"Financial fetch failed for {case_id}: {e}")

    # Tier-based score on calculated amount
    if total_calculated >= 50000:
        base = 25.0
    elif total_calculated >= 20000:
        base = 20.0
    elif total_calculated >= 5000:
        base = 12.0
    elif total_calculated > 0:
        base = 6.0
    else:
        base = 0.0

    # +3 bonus if Ordered > 2x Calculated (unrealised exposure)
    bonus = 3.0 if (total_ordered > 0 and total_calculated > 0 and
                    total_ordered > 2 * total_calculated) else 0.0
    pts   = min(base + bonus, 25.0)

    flags.append(
        f"Financial Exposure: calculated={total_calculated}, ordered={total_ordered} "
        f"→ {base} pts" + (f" +{bonus} unrealised bonus" if bonus > 0 else "")
        + f" = {pts}/25"
    )

    return pts, flags, total_calculated, total_ordered


# ---------------------------------------------------------------
# DIMENSION 3: Similar Case Volume (max 20 pts)
# ---------------------------------------------------------------

def _score_similar_case_volume(case_id: str, fraud_types: list) -> tuple[float, list[str], int]:
    """
    Counts distinct workfolders with matching allegation types by querying
    Allegations_All. This intentionally avoids fetching every allegation or
    workfolder detail. This is the same lightweight archive source used
    by search_similar_cases.

    Returns (pts, flags, total_count).
    """
    flags  = []
    total  = 0
    seen   = set()
    raw_match_count = 0
    unresolved_match_count = 0

    # First resolve allegation type IDs from the current workfolder
    try:
        wf_res   = fetch(f"/entities/Workfolder/items/{case_id}")
        wf_links = wf_res.get("_links", {})

        alleg_href = wf_links.get(
            "relationship:Workfolder_AllegationsRelationship", {}
        ).get("href")

        if alleg_href:
            alleg_items = _fetch_embedded(alleg_href, "Workfolder_AllegationsRelationship")
            for alleg_item in alleg_items:
                type_href = alleg_item.get("_links", {}).get(
                    "relationship:Allegations_AllegationsType", {}
                ).get("href", "")
                if not type_href:
                    a_self = alleg_item.get("_links", {}).get("self", {}).get("href", "")
                    if a_self:
                        _, a_links = _fetch_props_links(a_self)
                        type_href = a_links.get(
                            "relationship:Allegations_AllegationsType", {}
                        ).get("href", "")

                if not type_href:
                    continue

                type_id = type_href.rstrip("/").split("/")[-1]
                if not type_id:
                    continue

                # Count all allegations of this type (lightweight — list endpoint only)
                list_res = fetch(
                    f"/entities/Allegations/lists/Allegations_All"
                    f"?Allegations_AllegationsType$Identity.Id={type_id}"
                )
                matched = list_res.get("_embedded", {}).get("Allegations_All", [])
                raw_match_count += len(matched)

                for alleg in matched:
                    wf_id = _workfolder_id_from_allegation(alleg)
                    if not wf_id:
                        unresolved_match_count += 1
                        continue
                    if wf_id == str(case_id) or wf_id in seen:
                        continue
                    seen.add(wf_id)
                    total += 1

    except Exception as e:
        logger.warning(f"Similar case volume count failed: {e}")

    if unresolved_match_count > 0 and raw_match_count > total:
        # Some AppWorks list rows do not expose the Workfolder relationship.
        # Preserve the lightweight D3 query by falling back to allegation-row
        # volume instead of fetching every allegation detail record.
        total = max(total, raw_match_count - 1)
        logger.info(
            "Similar case volume used raw Allegations_All count fallback: "
            f"raw={raw_match_count}, unresolved={unresolved_match_count}"
        )

    # Tier scoring
    if total >= 100:
        pts = 20.0
    elif total >= 50:
        pts = 16.0
    elif total >= 20:
        pts = 12.0
    elif total >= 5:
        pts = 7.0
    elif total >= 1:
        pts = 3.0
    else:
        pts = 0.0

    flags.append(f"Similar Case Volume: {total} cases found → {pts}/20")
    return pts, flags, total


# ---------------------------------------------------------------
# DIMENSION 4: Allegation Severity (max 20 pts)
# ---------------------------------------------------------------

def _score_allegation_severity(case_id: str, fraud_types: list) -> tuple[float, list[str]]:
    """
    Counts distinct allegation types and checks for open allegations.

    Returns (pts, flags).
    """
    flags = []
    distinct_types  = len(set(fraud_types)) if fraud_types else 0
    has_open_alleg  = False

    try:
        wf_res   = fetch(f"/entities/Workfolder/items/{case_id}")
        wf_links = wf_res.get("_links", {})

        alleg_href = wf_links.get(
            "relationship:Workfolder_AllegationsRelationship", {}
        ).get("href")

        if alleg_href:
            alleg_items = _fetch_embedded(alleg_href, "Workfolder_AllegationsRelationship")
            seen_types = set()

            for alleg_item in alleg_items:
                # Fetch allegation entity to check closure date and type
                a_self = alleg_item.get("_links", {}).get("self", {}).get("href", "")
                if a_self:
                    a_props, a_links = _fetch_props_links(a_self)
                    date_closed = a_props.get("Allegations_DateClosed")
                    status      = (a_props.get("Allegations_AllegationStatus") or "").lower()

                    # Open allegation: no closure date AND status is Open
                    if not date_closed and status in ("open", "active", ""):
                        has_open_alleg = True

                    # Gather type IDs for distinct count
                    type_href = a_links.get(
                        "relationship:Allegations_AllegationsType", {}
                    ).get("href", "")
                    if type_href:
                        seen_types.add(type_href.rstrip("/").split("/")[-1])

            if seen_types:
                distinct_types = len(seen_types)

    except Exception as e:
        logger.warning(f"Allegation severity fetch failed: {e}")

    # Base score by distinct type count
    if distinct_types >= 4:
        base = 20.0
    elif distinct_types == 3:
        base = 16.0
    elif distinct_types == 2:
        base = 12.0
    elif distinct_types == 1:
        base = 6.0
    else:
        base = 0.0

    # +4 bonus if any open allegation (no closure date)
    bonus = 4.0 if has_open_alleg else 0.0
    pts   = min(base + bonus, 20.0)

    flags.append(
        f"Allegation Severity: {distinct_types} distinct type(s) → {base} pts"
        + (f" +{bonus} open-allegation bonus" if bonus > 0 else "")
        + f" = {pts}/20"
    )

    return pts, flags


# ---------------------------------------------------------------
# DIMENSION 5: Case Characteristics (max 10 pts)
# ---------------------------------------------------------------

def _score_case_characteristics(case_id: str) -> tuple[float, list[str]]:
    """
    Scores Fast Track flag, multiple subjects, and case received age.

    Returns (pts, flags).
    """
    flags        = []
    pts          = 0.0
    fast_track   = False
    subject_count = 0
    received_age  = 0

    try:
        wf_res   = fetch(f"/entities/Workfolder/items/{case_id}")
        wf_props = wf_res.get("Properties", {})
        wf_links = wf_res.get("_links", {})

        # Fast Track (property on Workfolder — use FAST_TRACK or similar field)
        fast_track = bool(
            wf_props.get("WorkfolderFastTrack")
            or wf_props.get("FAST_TRACK")
            or wf_props.get("FastTrack")
        )

        # Case received age
        age_raw = wf_props.get("WorkfolderDateReceivedAge")
        if age_raw is not None:
            try:
                received_age = int(float(age_raw))
            except (ValueError, TypeError):
                pass

        # Subject count
        subj_href = wf_links.get(
            "relationship:Workfolder_SubjectsRelationship", {}
        ).get("href")
        if subj_href:
            subj_items    = _fetch_embedded(subj_href, "Workfolder_SubjectsRelationship")
            subject_count = len(subj_items)

    except Exception as e:
        logger.warning(f"Case characteristics fetch failed for {case_id}: {e}")

    # Fast Track → +5
    if fast_track:
        pts += 5.0
        flags.append("Case Characteristics: Fast Track=True → +5")

    # Multiple subjects (≥ 2) → +3
    if subject_count >= 2:
        pts += 3.0
        flags.append(f"Case Characteristics: {subject_count} subjects → +3")

    # Case received age > 30 days → +2
    if received_age > 30:
        pts += 2.0
        flags.append(f"Case Characteristics: age={received_age} days > 30 → +2")

    pts = min(pts, 10.0)
    if not flags:
        flags.append("Case Characteristics: no conditions met → 0/10")
    else:
        flags.append(f"Case Characteristics total: {pts}/10")

    return pts, flags


# ---------------------------------------------------------------
# TOOL: calculate_risk_metrics
# ---------------------------------------------------------------

def calculate_risk_metrics(case_id: str, subject_id: str, fraud_types: list) -> dict:
    """
    Deterministic BSI risk scoring across 5 dimensions per
    Existing Risk Criteria.txt.

    Total max: 100 pts.
    Score = earned / 100 → normalised [0, 1].
    Tiers: CRITICAL ≥ 0.75 | HIGH ≥ 0.50 | MEDIUM ≥ 0.25 | LOW < 0.25
    """
    logger.info(f"Calculating risk — Case: {case_id}  Subject: {subject_id}")

    if isinstance(fraud_types, str):
        fraud_types = [fraud_types]

    all_triggered = []
    total_earned  = 0.0

    # ── Dimension 1: Subject History ─────────────────────────────
    d1_pts, d1_flags, prior_case_count = _score_subject_history(subject_id, case_id)
    total_earned += d1_pts
    if d1_pts > 0:
        all_triggered.append({
            "rule_id":           "Subject History",
            "rule_name":         "Subject History",
            "weight":            d1_pts,
            "max_weight":        25.0,
            "display":           f"{d1_pts} / 25",
            "condition_matched": f"{prior_case_count} prior case(s)",
            "flags":             d1_flags,
        })
    logger.info(f"  D1 Subject History: {d1_pts}/25 — {d1_flags}")

    # ── Dimension 2: Financial Exposure ──────────────────────────
    d2_pts, d2_flags, total_calc, total_ord = _score_financial_exposure(case_id)
    total_earned += d2_pts
    if d2_pts > 0:
        all_triggered.append({
            "rule_id":           "Financial Exposure",
            "rule_name":         "Financial Exposure",
            "weight":            d2_pts,
            "max_weight":        25.0,
            "display":           f"{d2_pts} / 25",
            "condition_matched": f"calculated={total_calc}, ordered={total_ord}",
            "flags":             d2_flags,
        })
    logger.info(f"  D2 Financial Exposure: {d2_pts}/25 — {d2_flags}")

    # ── Dimension 3: Similar Case Volume ─────────────────────────
    d3_pts, d3_flags, sim_count = _score_similar_case_volume(case_id, fraud_types)
    total_earned += d3_pts
    if d3_pts > 0:
        all_triggered.append({
            "rule_id":           "Similar Case Volume",
            "rule_name":         "Similar Case Volume",
            "weight":            d3_pts,
            "max_weight":        20.0,
            "display":           f"{d3_pts} / 20",
            "condition_matched": f"{sim_count} similar cases found",
            "flags":             d3_flags,
        })
    logger.info(f"  D3 Similar Case Volume: {d3_pts}/20 — {d3_flags}")

    # ── Dimension 4: Allegation Severity ─────────────────────────
    d4_pts, d4_flags = _score_allegation_severity(case_id, fraud_types)
    total_earned += d4_pts
    if d4_pts > 0:
        all_triggered.append({
            "rule_id":           "Allegation Severity",
            "rule_name":         "Allegation Severity",
            "weight":            d4_pts,
            "max_weight":        20.0,
            "display":           f"{d4_pts} / 20",
            "condition_matched": f"{len(set(fraud_types))} distinct type(s)",
            "flags":             d4_flags,
        })
    logger.info(f"  D4 Allegation Severity: {d4_pts}/20 — {d4_flags}")

    # ── Dimension 5: Case Characteristics ────────────────────────
    d5_pts, d5_flags = _score_case_characteristics(case_id)
    total_earned += d5_pts
    if d5_pts > 0:
        all_triggered.append({
            "rule_id":           "Case Characteristics",
            "rule_name":         "Case Characteristics",
            "weight":            d5_pts,
            "max_weight":        10.0,
            "display":           f"{d5_pts} / 10",
            "condition_matched": "; ".join(d5_flags),
            "flags":             d5_flags,
        })
    logger.info(f"  D5 Case Characteristics: {d5_pts}/10 — {d5_flags}")

    # ── Normalise & tier ─────────────────────────────────────────
    risk_score = round(total_earned / _TOTAL_MAX_PTS, 4)

    if risk_score >= 0.75:
        tier = "CRITICAL"
    elif risk_score >= 0.50:
        tier = "HIGH"
    elif risk_score >= 0.25:
        tier = "MEDIUM"
    else:
        tier = "LOW"

    recommendations = {
        "CRITICAL": (
            "Immediate field investigation and evidence preservation required. "
            "Notify Director of Special Investigations without delay."
        ),
        "HIGH": (
            "Prioritise for comprehensive audit and subject interview. "
            "Escalate to supervisor within 24 hours."
        ),
        "MEDIUM": (
            "Desk audit and prior-case history verification recommended. "
            "Monitor for additional activity."
        ),
        "LOW": (
            "Routine monitoring — no immediate field action required. "
            "Document and schedule standard review."
        ),
    }

    logger.info(
        f"Risk result: {total_earned}/{_TOTAL_MAX_PTS} pts = {risk_score} ({tier}), "
        f"triggered: {[r['rule_id'] for r in all_triggered]}"
    )

    result_data = {
        "case_id":              case_id,
        "subject_id":           subject_id,
        "risk_score":           risk_score,
        "risk_tier":            tier,
        # Full dimension objects — the LLM receives rule_id, display (pts/max),
        # and condition_matched so it can explain exactly why the score was reached.
        "triggered_rules":      all_triggered,
        "total_points":         round(total_earned, 1),
        "max_points":           100,
        "billing_anomaly_flag": any("BILLING" in str(f).upper() for f in fraud_types),
        "prior_case_count":     prior_case_count,
        "recommendation":       recommendations[tier],
    }

    # Pass the full result directly (validated.model_dump() would strip extra fields
    # from TriggeredRule objects; we pass the full dict so the LLM sees all dimension detail)
    return {
        "result": result_data,
        "provenance": {
            "sources": [
                f"AppWorks case record {case_id}",
                f"AppWorks subject record {subject_id}",
                "BSI Risk Criteria (Existing Risk Criteria.txt)",
            ],
            "retrieved_at":  datetime.now(timezone.utc).isoformat(),
            "computed_by": "BSI configured rules evaluation",
        },
    }
