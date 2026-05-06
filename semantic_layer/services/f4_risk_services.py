# semantic_layer/services/f4_risk_services.py
# ----------------------------------------------------------------
# Agent 4: Fraud Risk Assessment
# ----------------------------------------------------------------
# ALL rules and thresholds are fetched from AppWorks at runtime.
# Zero hardcoded scoring logic.
#
# get_risk_rules():
#   Fetches active rule dimensions from AgentRulesTable in AppWorks.
#   Each rule carries: rule_id, description, dimension_key, thresholds
#   (breakpoints), bonus_condition, bonus_pts, max_pts.
#   BSI can add/modify/deactivate rules in AppWorks without any code change.
#
# calculate_risk_metrics():
#   Accepts active_rules (passed by LLM from get_risk_rules output).
#   Fetches live case/subject data from AppWorks per dimension.
#   Applies AppWorks-defined breakpoints — no if/elif chains in code.
#   Returns risk_score, risk_tier, triggered_rules, recommendation.
#
# Total max points = sum of all active rule max_pts (from AppWorks).
# Score normalised to [0,1]. Tiers from AppWorks tier config or defaults.
# ----------------------------------------------------------------

import json
import logging
from datetime import datetime, timezone
from semantic_layer.appworks_auth import fetch

logger = logging.getLogger(__name__)

_RULES_LIST_ENDPOINT = "/entities/AgentRulesTable/lists/AgentRulesTable_AgentRulesTableListInternal"


# -----------------------------------------------------------------------
# HELPERS
# -----------------------------------------------------------------------

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


def _safe_float(val, default=0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _workfolder_id_from_allegation(alleg_item: dict) -> str:
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


# -----------------------------------------------------------------------
# THRESHOLD PARSING — parse AppWorks breakpoints into scoring rules
# -----------------------------------------------------------------------

def _parse_thresholds(raw) -> list:
    """
    Parse the Thresholds field from AppWorks.
    Supports:
      - JSON array: [{"min_value": 5, "points": 25}, ...]
      - Additive JSON array: [{"condition": "fast_track", "points": 5}, ...]
      - Compact string: ">=5:25,>=3:20,>=2:15,>=1:8,0:0"
    Returns a list of dicts.
    """
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        return [raw]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return parsed
            if isinstance(parsed, dict):
                return [parsed]
        except (json.JSONDecodeError, ValueError):
            pass
        # Compact format ">=N:pts,..."
        breakpoints = []
        for segment in raw.split(","):
            segment = segment.strip()
            if ":" in segment:
                cond, pts_str = segment.rsplit(":", 1)
                cond = cond.strip()
                try:
                    pts = float(pts_str.strip())
                except ValueError:
                    continue
                breakpoints.append({"condition": cond, "points": pts})
        return breakpoints
    return []


def _apply_thresholds(value: float, breakpoints: list) -> float:
    """
    Evaluate value against AppWorks breakpoints. First match wins.
    Supports {min_value: N, points: P} and {condition: ">=N", points: P}.
    """
    for bp in breakpoints:
        pts = _safe_float(bp.get("points", bp.get("pts", 0)))
        min_val = bp.get("min_value")
        if min_val is not None:
            if value >= _safe_float(min_val):
                return pts
        else:
            cond = str(bp.get("condition", "")).strip()
            if cond.startswith(">="):
                if value >= _safe_float(cond[2:]):
                    return pts
            elif cond.startswith(">"):
                if value > _safe_float(cond[1:]):
                    return pts
            elif cond.startswith("==") or cond.startswith("="):
                if value == _safe_float(cond.lstrip("=")):
                    return pts
            elif cond in ("0", "0.0"):
                return pts
    return 0.0


# -----------------------------------------------------------------------
# APPWORKS RULE-TABLE HELPERS
# -----------------------------------------------------------------------

def _get_prop(props: dict, keys: list):
    """Return first non-None value from props using multiple key candidates."""
    for key in keys:
        val = props.get(key)
        if val is not None:
            return val
    return None


def _parse_pts_string(s) -> float:
    """
    Parse a points string from AppWorks child Rules WEIGHT field.
    Examples: "25 pts", "max 25 pts", "25", 25
    Returns the numeric value or 0.0.
    """
    import re
    if s is None:
        return 0.0
    if isinstance(s, (int, float)):
        return float(s)
    m = re.search(r'(\d+(?:\.\d+)?)', str(s))
    return float(m.group(1)) if m else 0.0


def _parse_condition_to_threshold(condition_str: str, pts: float, dimension_key: str) -> dict:
    """
    Convert one child Rules CONDITION string → a threshold dict for _apply_thresholds.

    Numeric dimensions (subject_history, financial_exposure, similar_case_volume,
    allegation_severity):
      "≥ 5 cases"      → {"min_value": 5,   "points": pts}
      "3 – 4 cases"    → {"min_value": 3,   "points": pts}  (lower bound of range)
      "> $0"           → {"min_value": 0.01,"points": pts}
      "$0" / "0 cases" → {"condition": "0", "points": 0}

    Additive dimension (case_characteristics):
      "Fast Track flag = True"   → {"condition": "fast_track",        "points": pts}
      "Multiple subjects (≥ 2)"  → {"condition": "multiple_subjects", "points": pts}
      "Case received age > 30"   → {"condition": "received_age_gt30", "points": pts}
    """
    import re
    s = condition_str.strip()
    s_lower = s.lower()

    # ── case_characteristics: named additive conditions ──────────────────
    if dimension_key == "case_characteristics":
        if "fast track" in s_lower:
            return {"condition": "fast_track", "points": pts}
        if "multiple subject" in s_lower or ("subject" in s_lower and "2" in s):
            return {"condition": "multiple_subjects", "points": pts}
        if "received age" in s_lower or "30 day" in s_lower or "> 30" in s_lower:
            return {"condition": "received_age_gt30", "points": pts}

    # ── "$0" / "0 cases" / bare zero → zero-pts sentinel ────────────────
    if re.fullmatch(r'\$?0(\.0+)?\s*(cases?|pts?|similar cases?)?', s_lower.strip()):
        return {"condition": "0", "points": 0.0}

    # ── "≥ N" / ">= N" / "> N" ─────────────────────────────────────────
    m = re.search(r'[≥>]=?\s*\$?\s*(\d[\d,]*(?:\.\d+)?)', s)
    if m:
        num = float(m.group(1).replace(",", ""))
        if num == 0:
            num = 0.01  # "> 0" means any positive value
        return {"min_value": num, "points": pts}

    # ── "N – M cases" or "N-M cases" → lower bound ──────────────────────
    m = re.search(r'(\d+)\s*[–\-]\s*(\d+)', s)
    if m:
        return {"min_value": float(m.group(1)), "points": pts}

    # ── bare number at start: "2 cases", "1 case", "4 types" ────────────
    m = re.match(r'(\d+)', s.strip())
    if m:
        return {"min_value": float(m.group(1)), "points": pts}

    logger.warning(f"    _parse_condition_to_threshold: could not parse {condition_str!r}")
    return {}


def _fetch_child_rules_breakpoints(item_id: str, dimension_key: str, child_href: str = None) -> list:
    """
    Fetch /entities/AgentRulesTable/items/{item_id}/childEntities/Rules and
    convert each child row into a threshold dict for _apply_thresholds().

    Child rows have: CONDITION (breakpoint description), WEIGHT (points string).
    Returns breakpoints sorted highest-min_value first (numeric), then named.
    """
    thresholds = []
    try:
        endpoint = child_href or f"/entities/AgentRulesTable/items/{item_id}/childEntities/Rules"
        res = fetch(endpoint)
        child_items = res.get("_embedded", {}).get("Rules", [])
        logger.info(
            f"  AgentRulesTable/{item_id}/childEntities/Rules: "
            f"{len(child_items)} child row(s)"
        )
        for child in child_items:
            cp = child.get("Properties", {})
            cond_str = str(_get_prop(cp, ["CONDITION", "Condition", "condition", "AgentRulesTable_Condition"]) or "")
            wt_str   = _get_prop(cp, ["WEIGHT", "Weight", "weight", "POINTS", "Points", "points", "AgentRulesTable_Weight"])
            pts      = _parse_pts_string(wt_str)
            if not cond_str:
                continue
            threshold = _parse_condition_to_threshold(cond_str, pts, dimension_key)
            if threshold:
                thresholds.append(threshold)

        # Sort: numeric descending, then named conditions
        numeric = sorted(
            [t for t in thresholds if "min_value" in t],
            key=lambda t: t["min_value"], reverse=True
        )
        named = [t for t in thresholds if "condition" in t]
        thresholds = numeric + named

    except Exception as e:
        logger.warning(f"  Child Rules fetch failed for item {item_id}: {e}")

    return thresholds


def _parse_bonus_from_condition(cond_str: str, dimension_key: str) -> tuple:
    """
    Extract bonus condition key and points from the parent AgentRulesTable
    CONDITION field (e.g. '+5 bonus if ANY subject is primary in ≥ 2 prior cases').
    Returns (bonus_condition_key: str, bonus_pts: float).
    """
    import re
    s = cond_str.strip().lower()

    # Extract "+N bonus" points
    m = re.search(r'\+\s*(\d+(?:\.\d+)?)\s*bonus', s)
    bonus_pts = float(m.group(1)) if m else 0.0

    if bonus_pts > 0:
        if "primary" in s and ("prior" in s or "case" in s):
            return "primary_ge2", bonus_pts
        if "ordered" in s or "unrealised" in s or "2x" in s or "2×" in s:
            return "ordered_gt_2x_calculated", bonus_pts
        if "open" in s and "allegation" in s:
            return "open_allegation", bonus_pts

    return "", 0.0
# -----------------------------------------------------------------------

def _fetch_subject_history(subject_id: str, case_id: str) -> tuple[int, bool]:
    """Returns (prior_case_count, is_primary_in_ge2_cases)."""
    prior_case_count = 0
    primary_in_cases = 0
    try:
        subj_res   = fetch(f"/entities/Subject/items/{subject_id}")
        subj_links = subj_res.get("_links", {})
        mapping_href = subj_links.get(
            "relationship:Subject_SubjectWorkfolderMapping", {}
        ).get("href")
        if not mapping_href:
            mapping_href = (
                f"/entities/Subject/items/{subject_id}"
                f"/childEntities/Subject_SubjectWorkfolderMapping"
            )
        mappings = _fetch_embedded(mapping_href, "Subject_SubjectWorkfolderMapping")
        prior_case_count = len(mappings)
        for m in mappings:
            if m.get("Properties", {}).get("SubjectWorkfolderMapping_IsPrimary"):
                primary_in_cases += 1
    except Exception as e:
        logger.warning(f"Subject history fetch failed for {subject_id}: {e}")
    return prior_case_count, (primary_in_cases >= 2)


def _fetch_financial_exposure(case_id: str) -> tuple[float, float]:
    """Returns (total_calculated, total_ordered)."""
    total_calculated = 0.0
    total_ordered    = 0.0
    try:
        wf_res   = fetch(f"/entities/Workfolder/items/{case_id}")
        wf_links = wf_res.get("_links", {})
        fin_href = wf_links.get("relationship:Workfolder_FinancialRelationship", {}).get("href")
        if fin_href:
            fin_items = _fetch_embedded(fin_href, "Workfolder_FinancialRelationship")
            for fin_item in fin_items:
                fin_self  = fin_item.get("_links", {}).get("self", {}).get("href", "")
                fin_props, _ = _fetch_props_links(fin_self)
                total_calculated += _safe_float(fin_props.get("Financial_Calculated"))
                total_ordered    += _safe_float(fin_props.get("Financial_Ordered"))
    except Exception as e:
        logger.warning(f"Financial fetch failed for {case_id}: {e}")
    return total_calculated, total_ordered


def _fetch_similar_case_volume(case_id: str) -> int:
    """Counts distinct workfolders with matching allegation types via Allegations_All."""
    total           = 0
    seen            = set()
    raw_match_count = 0
    unresolved      = 0
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
                list_res = fetch(
                    f"/entities/Allegations/lists/Allegations_All"
                    f"?Allegations_AllegationsType$Identity.Id={type_id}"
                )
                matched = list_res.get("_embedded", {}).get("Allegations_All", [])
                raw_match_count += len(matched)
                for alleg in matched:
                    wf_id = _workfolder_id_from_allegation(alleg)
                    if not wf_id:
                        unresolved += 1
                        continue
                    if wf_id == str(case_id) or wf_id in seen:
                        continue
                    seen.add(wf_id)
                    total += 1
    except Exception as e:
        logger.warning(f"Similar case volume count failed: {e}")
    if unresolved > 0 and raw_match_count > total:
        total = max(total, raw_match_count - 1)
    return total


def _fetch_allegation_severity(case_id: str) -> tuple[int, bool]:
    """Returns (distinct_type_count, has_open_allegation)."""
    distinct_types = 0
    has_open       = False
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
                a_self = alleg_item.get("_links", {}).get("self", {}).get("href", "")
                if a_self:
                    a_props, a_links = _fetch_props_links(a_self)
                    date_closed = a_props.get("Allegations_DateClosed")
                    status      = (a_props.get("Allegations_AllegationStatus") or "").lower()
                    if not date_closed and status in ("open", "active", ""):
                        has_open = True
                    type_href = a_links.get(
                        "relationship:Allegations_AllegationsType", {}
                    ).get("href", "")
                    if type_href:
                        seen_types.add(type_href.rstrip("/").split("/")[-1])
            distinct_types = len(seen_types)
    except Exception as e:
        logger.warning(f"Allegation severity fetch failed: {e}")
    return distinct_types, has_open


def _fetch_case_characteristics(case_id: str) -> tuple[bool, int, int]:
    """Returns (is_fast_track, subject_count, received_age_days)."""
    fast_track    = False
    subject_count = 0
    received_age  = 0
    try:
        wf_res   = fetch(f"/entities/Workfolder/items/{case_id}")
        wf_props = wf_res.get("Properties", {})
        wf_links = wf_res.get("_links", {})
        fast_track = bool(
            wf_props.get("WorkfolderFastTrack")
            or wf_props.get("FAST_TRACK")
            or wf_props.get("FastTrack")
        )
        age_raw = wf_props.get("WorkfolderDateReceivedAge")
        if age_raw is not None:
            try:
                received_age = int(float(age_raw))
            except (ValueError, TypeError):
                pass
        subj_href = wf_links.get(
            "relationship:Workfolder_SubjectsRelationship", {}
        ).get("href")
        if subj_href:
            subj_items    = _fetch_embedded(subj_href, "Workfolder_SubjectsRelationship")
            subject_count = len(subj_items)
    except Exception as e:
        logger.warning(f"Case characteristics fetch failed for {case_id}: {e}")
    return fast_track, subject_count, received_age


# -----------------------------------------------------------------------
# DIMENSION SCORER — evaluates one AppWorks rule against live data
# -----------------------------------------------------------------------

def _score_dimension(
    rule: dict,
    case_id: str,
    subject_id: str,
    prior_case_count: int = None,
    primary_in_prior_cases: int = None,
    total_calculated: float = None,
    total_ordered: float = None,
    similar_case_volume: int = None,
    distinct_types: int = None,
    has_open_allegation: bool = None,
    fast_track: bool = None,
    subject_count: int = None,
    received_age: int = None
) -> tuple[float, dict]:
    """
    Evaluates a single rule dimension against live AppWorks data.
    All thresholds, bonus conditions, and max_pts come from the rule dict
    as returned by AppWorks — nothing is hardcoded here.

    If optional context parameters are provided, they are used instead of
    re-fetching the data from AppWorks.
    """
    dimension_key   = rule.get("dimension_key", "")
    thresholds      = _parse_thresholds(rule.get("thresholds") or "")
    bonus_condition = rule.get("bonus_condition", "")
    bonus_pts       = _safe_float(rule.get("bonus_pts", 0))
    max_pts         = _safe_float(rule.get("max_pts", 0))
    rule_id         = rule.get("rule_id", dimension_key)
    description     = rule.get("description", rule_id)

    pts           = 0.0
    bonus_applied = 0.0
    flags         = []
    condition_matched = ""

    if dimension_key == "subject_history":
        if prior_case_count is None:
            p_count, is_p_ge2 = _fetch_subject_history(subject_id, case_id)
        else:
            p_count = prior_case_count
            is_p_ge2 = bool(primary_in_prior_cases and primary_in_prior_cases >= 2)
        
        pts = _apply_thresholds(float(p_count), thresholds)
        condition_matched = f"{p_count} prior case(s)"
        if bonus_condition == "primary_ge2" and is_p_ge2:
            bonus_applied = bonus_pts
        flags.append(
            f"Subject History: {p_count} cases found → {pts} pts"
            + (f" +{bonus_applied} primary bonus" if bonus_applied > 0 else "")
            + f" = {min(pts + bonus_applied, max_pts)}/{max_pts}"
        )

    elif dimension_key == "financial_exposure":
        if total_calculated is None or total_ordered is None:
            total_calculated, total_ordered = _fetch_financial_exposure(case_id)
        
        pts = _apply_thresholds(total_calculated, thresholds)
        condition_matched = f"calculated={total_calculated}, ordered={total_ordered}"
        if (
            bonus_condition == "ordered_gt_2x_calculated"
            and total_ordered > 0 and total_calculated > 0
            and total_ordered > 2 * total_calculated
        ):
            bonus_applied = bonus_pts
        flags.append(
            f"Financial Exposure: calculated={total_calculated}, ordered={total_ordered} → {pts} pts"
            + (f" +{bonus_applied} unrealised bonus" if bonus_applied > 0 else "")
            + f" = {min(pts + bonus_applied, max_pts)}/{max_pts}"
        )

    elif dimension_key == "similar_case_volume":
        if similar_case_volume is None:
            similar_case_volume = _fetch_similar_case_volume(case_id)
        
        pts = _apply_thresholds(float(similar_case_volume), thresholds)
        condition_matched = f"{similar_case_volume} similar cases found"
        flags.append(f"Similar Case Volume: {similar_case_volume} cases found → {pts}/{max_pts}")

    elif dimension_key == "allegation_severity":
        if distinct_types is None or has_open_allegation is None:
            distinct_types, has_open_allegation = _fetch_allegation_severity(case_id)
        
        pts = _apply_thresholds(float(distinct_types), thresholds)
        condition_matched = f"{distinct_types} distinct type(s)"
        if bonus_condition == "open_allegation" and has_open_allegation:
            bonus_applied = bonus_pts
        flags.append(
            f"Allegation Severity: {distinct_types} distinct type(s) → {pts} pts"
            + (f" +{bonus_applied} open-allegation bonus" if bonus_applied > 0 else "")
            + f" = {min(pts + bonus_applied, max_pts)}/{max_pts}"
        )

    elif dimension_key == "case_characteristics":
        if fast_track is None or subject_count is None or received_age is None:
            fast_track, subject_count, received_age = _fetch_case_characteristics(case_id)
        
        # Additive: each breakpoint has a named condition string
        for bp in thresholds:
            cond_name = str(bp.get("condition", "")).strip()
            bp_pts    = _safe_float(bp.get("points", 0))
            if cond_name == "fast_track" and fast_track:
                pts += bp_pts
                flags.append(f"Case Characteristics: Fast Track=True → +{bp_pts}")
            elif cond_name == "multiple_subjects" and subject_count >= 2:
                pts += bp_pts
                flags.append(f"Case Characteristics: {subject_count} subjects → +{bp_pts}")
            elif cond_name == "received_age_gt30" and received_age > 30:
                pts += bp_pts
                flags.append(f"Case Characteristics: age={received_age} days > 30 → +{bp_pts}")
        condition_matched = "; ".join(flags) if flags else "no conditions met"
        flags.append(f"Case Characteristics total: {min(pts, max_pts)}/{max_pts}")


    else:
        logger.warning(f"Unknown dimension_key '{dimension_key}' for rule {rule_id}")
        return 0.0, {}

    total_pts = min(pts + bonus_applied, max_pts)
    if total_pts <= 0:
        return 0.0, {}

    return total_pts, {
        "rule_id":           rule_id,
        "rule_name":         description,
        "weight":            total_pts,
        "max_weight":        max_pts,
        "display":           f"{total_pts} / {max_pts}",
        "condition_matched": condition_matched,
        "flags":             flags,
    }


# -----------------------------------------------------------------------
# TOOL: get_risk_rules — fetches from AppWorks AgentRulesTable at runtime
# -----------------------------------------------------------------------

def get_risk_rules() -> dict:
    """
    Fetches the active BSI fraud detection rule dimensions from AppWorks
    AgentRulesTable. Returns each active rule with dimension_key, thresholds
    (already parsed to list), bonus_condition, bonus_pts, and max_pts.

    The LLM receives this result and passes active_rules to calculate_risk_metrics.
    """
    rules = []
    try:
        res   = fetch(_RULES_LIST_ENDPOINT)
        items = res.get("_embedded", {}).get(
            "AgentRulesTable_AgentRulesTableListInternal", []
        )
        logger.info(f"AgentRulesTable: {len(items)} row(s) returned")

        for idx, item in enumerate(items):
            props = item.get("Properties", {})
            links = item.get("_links", {})

            # ── is_active ────────────────────────────────────────────────
            active_raw = (
                props.get("ACTIVE")
                or props.get("AgentRulesTable_IsActive")
                or props.get("IsActive")
                or props.get("Active")
                or props.get("AgentRulesTable_Active")
                or props.get("Enabled")
                or True
            )
            if str(active_raw).strip().lower() in ("false", "0", "no", "inactive", "disabled"):
                logger.info(f"  Row {idx}: skipped — IsActive={active_raw!r}")
                continue

            # ── rule_id ──────────────────────────────────────────────────
            rule_id = str(
                props.get("RULE_ID")
                or props.get("AgentRulesTable_RuleId")
                or props.get("RuleId")
                or props.get("AgentRulesTable_Name")
                or props.get("Name")
                or ""
            )
            if not rule_id:
                self_href = links.get("self", {}).get("href", "")
                if self_href:
                    rule_id = f"R-{self_href.rstrip('/').split('/')[-1]}"

            # ── description ──────────────────────────────────────────────
            description = str(
                props.get("DESCRIPTION")
                or props.get("AgentRulesTable_Description")
                or props.get("Description")
                or rule_id
            )

            # ── dimension_key ────────────────────────────────────────────
            # Priority: 1. DIMENSION field, 2. Derived from RULE_ID
            dimension_raw = str(
                props.get("DIMENSION")
                or props.get("DIMENSION_KEY")
                or props.get("DimensionKey")
                or props.get("AgentRulesTable_Dimension")
                or props.get("AgentRulesTable_DimensionKey")
                or rule_id
                or ""
            )
            dimension_key = dimension_raw.strip().lower().replace(" ", "_")
            if not dimension_key:
                logger.warning(f"  Row {idx}: skipped — dimension_key could not be derived")
                continue

            # ── max_pts: AppWorks value ──────────────────────────────────
            max_pts = _safe_float(
                props.get("WEIGHT")
                or props.get("AgentRulesTable_MaxPoints")
                or props.get("MaxPoints")
                or 0
            )
            if isinstance(props.get("WEIGHT"), str):
                max_pts = _parse_pts_string(props.get("WEIGHT"))

            # ── thresholds: AppWorks value ──────────────────────────────
            thresholds_raw = (
                props.get("CONDITION")
                or props.get("AgentRulesTable_Thresholds")
                or props.get("Thresholds")
                or ""
            )
            # Try parsing thresholds directly if they are in JSON/compact format
            thresholds = _parse_thresholds(thresholds_raw)
            
            # If no thresholds found in parent, fetch from child 'Rules'
            if not thresholds:
                # Extract item_id from self or item link
                item_href = (
                    links.get("self", {}).get("href", "")
                    or links.get("item", {}).get("href", "")
                    or ""
                )
                item_id = None
                if item_href:
                    item_id = item_href.rstrip("/").split("/")[-1]
                
                if not item_id:
                    item_id = (
                        props.get("AgentRulesTable_Id")
                        or props.get("Id")
                        or props.get("Identity", {}).get("Id")
                    )

                if item_id:
                    try:
                        child_href = None
                        for lk in links.keys():
                            if "Rules" in lk and "relationship" in lk:
                                child_href = links[lk].get("href")
                                break
                        
                        child_thresholds = _fetch_child_rules_breakpoints(
                            str(item_id), dimension_key, child_href=child_href
                        )
                        if child_thresholds:
                            thresholds = child_thresholds
                            logger.info(
                                f"  Row {idx} ({dimension_key}): loaded thresholds "
                                f"from child Rules entity ({len(thresholds)} breakpoint(s))"
                            )
                    except Exception as e:
                        logger.warning(
                            f"  Row {idx}: failed to load child rule breakpoints: {e}"
                        )

            # ── bonus: AppWorks value ──────────────────────────────────
            bonus_raw = str(props.get("CONDITION") or "")
            bonus_cond, bonus_pts = _parse_bonus_from_condition(bonus_raw, dimension_key)

            # ── optional metadata fields ──────────────────────────────────
            weight = _safe_float(props.get("WEIGHT") or 0)
            tier_thresholds_raw = (
                props.get("TIER_THRESHOLDS")
                or props.get("AgentRulesTable_TierThresholds")
                or props.get("TierThresholds")
                or ""
            )
            recommendations_raw = (
                props.get("RECOMMENDATIONS")
                or props.get("AgentRulesTable_Recommendations")
                or props.get("Recommendations")
                or ""
            )

            rules.append({
                "rule_id":         rule_id,
                "description":     description,
                "dimension_key":   dimension_key,
                "thresholds":      thresholds,
                "bonus_condition": bonus_cond,
                "bonus_pts":       bonus_pts,
                "max_pts":         max_pts,
                "weight":          weight,
                "tier_thresholds": tier_thresholds_raw,
                "recommendations": recommendations_raw,
                "active":          True,
            })
            logger.info(
                f"  Row {idx}: loaded rule '{rule_id}' "
                f"dim='{dimension_key}' max={max_pts} "
                f"thresholds={len(thresholds)} bonus='{bonus_cond}'+{bonus_pts}"
            )

    except Exception as e:
        logger.error(f"Critical error in get_risk_rules: {e}")

    logger.info(f"get_risk_rules: {len(rules)} active rule(s) loaded from AppWorks")

    return {
        "result": {"rules": rules},
        "provenance": {
            "sources":      [f"AppWorks {_RULES_LIST_ENDPOINT}"],
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "computed_by":  "AppWorks REST retrieval",
        },
    }


# -----------------------------------------------------------------------
# TOOL: calculate_risk_metrics
# -----------------------------------------------------------------------

def calculate_risk_metrics(
    case_id: str,
    subject_id: str,
    fraud_types: list,
    active_rules: list = None,
    prior_case_count: int = None,
    primary_in_prior_cases: int = None,
    total_calculated: float = None,
    total_ordered: float = None,
    similar_case_volume: int = None,
    distinct_types: int = None,
    has_open_allegation: bool = None,
    fast_track: bool = None,
    subject_count: int = None,
    received_age: int = None
) -> dict:
    """
    Deterministic BSI risk scoring using active_rules from AppWorks.
    """
    logger.info(f"calculate_risk_metrics — Case: {case_id}  Subject: {subject_id}")

    if isinstance(fraud_types, str):
        fraud_types = [fraud_types]

    if active_rules is None:
        logger.info("active_rules not provided — fetching from AppWorks directly")
        rules_envelope = get_risk_rules()
        active_rules   = rules_envelope["result"].get("rules", [])

    if not active_rules:
        logger.warning("No active rules — risk score will be 0")

    total_earned     = 0.0
    total_max        = 0.0
    all_triggered    = []
    final_prior_count = 0

    for rule in active_rules:
        if not rule.get("active", True):
            continue

        max_pts    = _safe_float(rule.get("max_pts", 0))
        total_max += max_pts

        pts, triggered_dict = _score_dimension(
            rule, case_id, subject_id,
            prior_case_count=prior_case_count,
            primary_in_prior_cases=primary_in_prior_cases,
            total_calculated=total_calculated,
            total_ordered=total_ordered,
            similar_case_volume=similar_case_volume,
            distinct_types=distinct_types,
            has_open_allegation=has_open_allegation,
            fast_track=fast_track,
            subject_count=subject_count,
            received_age=received_age
        )
        total_earned += pts

        if rule.get("dimension_key") == "subject_history" and triggered_dict:
            cm = triggered_dict.get("condition_matched", "")
            try:
                final_prior_count = int(cm.split(" ")[0])
            except (ValueError, IndexError):
                pass

        if pts > 0 and triggered_dict:
            all_triggered.append(triggered_dict)

        logger.info(
            f"  [{rule.get('dimension_key')}] {pts}/{max_pts} pts — "
            f"{triggered_dict.get('condition_matched','') if triggered_dict else 'not triggered'}"
        )

    effective_max = total_max if total_max > 0 else 100.0
    risk_score    = round(total_earned / effective_max, 4)

    tier_thresholds = _load_tier_thresholds(active_rules)
    tier = "LOW"
    for tier_name, min_score in sorted(tier_thresholds.items(), key=lambda x: -x[1]):
        if risk_score >= min_score:
            tier = tier_name
            break

    recommendation = _load_recommendation(tier, active_rules)

    logger.info(
        f"Risk result: {total_earned}/{effective_max} pts = {risk_score} ({tier}), "
        f"triggered: {[r['rule_id'] for r in all_triggered]}"
    )

    return {
        "result": {
            "case_id":              case_id,
            "subject_id":           subject_id,
            "risk_score":           risk_score,
            "risk_tier":            tier,
            "triggered_rules":      all_triggered,
            "total_points":         round(total_earned, 1),
            "max_points":           round(effective_max, 1),
            "billing_anomaly_flag": any("BILLING" in str(f).upper() for f in fraud_types),
            "prior_case_count":     final_prior_count,
            "recommendation":       recommendation,
        },
        "provenance": {
            "sources": [
                f"AppWorks case record {case_id}",
                f"AppWorks subject record {subject_id}",
                "AppWorks BSI fraud detection rules table",
            ],
            "retrieved_at":  datetime.now(timezone.utc).isoformat(),
            "computed_by":   "BSI configured rules evaluation",
        },
    }


# -----------------------------------------------------------------------
# TIER & RECOMMENDATION LOADERS — read from AppWorks rules metadata
# -----------------------------------------------------------------------

def _load_tier_thresholds(active_rules: list) -> dict:
    """
    Read tier min-score thresholds from AppWorks rules metadata.
    """
    for rule in active_rules:
        raw = rule.get("tier_thresholds")
        if raw:
            if isinstance(raw, dict):
                return raw
            try:
                parsed = json.loads(str(raw))
                if isinstance(parsed, dict):
                    return parsed
            except (json.JSONDecodeError, ValueError):
                pass
    return {"HIGH": 0.60, "MEDIUM": 0.30, "LOW": 0.0}


def _load_recommendation(tier: str, active_rules: list) -> str:
    """
    Read recommendation text for a tier from AppWorks rules metadata.
    Returns a generic instruction if AppWorks provides none.
    """
    for rule in active_rules:
        recs = rule.get("recommendations")
        if recs:
            if isinstance(recs, dict) and tier in recs:
                return recs[tier]
            try:
                parsed = json.loads(str(recs))
                if isinstance(parsed, dict) and tier in parsed:
                    return parsed[tier]
            except (json.JSONDecodeError, ValueError):
                pass
    return "Review case data and determine appropriate investigative action."