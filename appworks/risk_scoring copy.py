# # appworks/risk_scoring.py
# # ----------------------------------------------------------------
# # Agent 4: Fraud Risk Assessment
# # ----------------------------------------------------------------
# # ALL rules and thresholds are fetched from AppWorks at runtime.
# # Zero hardcoded scoring logic — no static dimension keys, no
# # static threshold tables, no static fraud-type lists.
# #
# # Architecture:
# #   get_risk_rules()
# #     → Fetches every active rule from AgentRulesTable.
# #     → For each rule, resolves thresholds from:
# #         1. Direct JSON/compact field on the parent row
# #         2. Child Rules entity (breakpoint rows)
# #     → Returns typed rule dicts ready for scoring.
# #
# #   calculate_risk_metrics()
# #     → Accepts active_rules (passed by LLM or self-fetched).
# #     → For each rule, dispatches to _score_dimension() using
# #       the rule's own dimension_key + evaluation_strategy.
# #     → evaluation_strategy is resolved dynamically from the rule
# #       fields — not from a hardcoded if/elif chain.
# #
# # Evaluation strategies (resolved at runtime from rule metadata):
# #   "numeric_threshold"  — score value against sorted breakpoints
# #   "additive_conditions"— sum points from matching named conditions
# #   "fraud_type_match"   — award points when case fraud types match
# #                          the rule's target_fraud_types list
# #
# # Adding a new rule in AppWorks requires zero code changes here.
# # ----------------------------------------------------------------

import json
import logging
import re
from datetime import datetime, timezone
from appworks.appworks_auth import fetch, fetch_list
from appworks.appworks_paths import AppWorksPaths

logger = logging.getLogger(__name__)

_RULES_LIST_ENDPOINT = (
    AppWorksPaths.FraudRules.risk_rules_all()
)


# -----------------------------------------------------------------------
# SPEC DEFAULTS
# Pure safety-net fallbacks used ONLY when AppWorks returns a recognised
# dimension but has no threshold/max_pts data at all for that row.
# These are NOT used for unknown/new dimensions — those score 0 if
# AppWorks provides no breakpoints, which is the correct safe default.
# -----------------------------------------------------------------------

_SPEC_THRESHOLDS: dict = {
    "subject_history": [
        {"min_value": 5,  "points": 25},
        {"min_value": 3,  "points": 20},
        {"min_value": 2,  "points": 15},
        {"min_value": 1,  "points": 8},
    ],
    "financial_exposure": [
        {"min_value": 50000, "points": 25},
        {"min_value": 20000, "points": 20},
        {"min_value": 5000,  "points": 12},
        {"min_value": 0.01,  "points": 6},
    ],
    "similar_case_volume": [
        {"min_value": 100, "points": 20},
        {"min_value": 50,  "points": 16},
        {"min_value": 20,  "points": 12},
        {"min_value": 5,   "points": 7},
        {"min_value": 1,   "points": 3},
    ],
    "allegation_severity": [
        {"min_value": 4, "points": 20},
        {"min_value": 3, "points": 16},
        {"min_value": 2, "points": 12},
        {"min_value": 1, "points": 6},
    ],
    "case_characteristics": [
        {"condition": "fast_track",        "points": 5},
        {"condition": "multiple_subjects", "points": 3},
        {"condition": "received_age_gt30", "points": 2},
    ],
}

_SPEC_MAX_PTS: dict = {
    "subject_history":      25.0,
    "financial_exposure":   25.0,
    "similar_case_volume":  20.0,
    "allegation_severity":  20.0,
    "case_characteristics": 10.0,
}

_SPEC_BONUS: dict = {
    "subject_history":     ("primary_ge2",               5.0),
    "financial_exposure":  ("ordered_gt_2x_calculated",  3.0),
    "allegation_severity": ("open_allegation",            4.0),
}

# Well-known description fragments → stable dimension key
# Used ONLY as fallback when the AppWorks row has no DimensionKey column.
_DESC_TO_DIM: dict = {
    "subject history":     "subject_history",
    "financial exposure":  "financial_exposure",
    "similar case":        "similar_case_volume",
    "allegation severity": "allegation_severity",
    "case characteristic": "case_characteristics",
}

# Known numeric dimensions (score a single value against breakpoints)
_NUMERIC_DIMENSIONS = {
    "subject_history",
    "financial_exposure",
    "similar_case_volume",
    "allegation_severity",
}

# Known additive dimensions (sum points from multiple named condition matches)
_ADDITIVE_DIMENSIONS = {
    "case_characteristics",
}


# -----------------------------------------------------------------------
# HELPERS — generic fetch utilities
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


def _get_prop(props: dict, keys: list):
    for key in keys:
        val = props.get(key)
        if val is not None:
            return val
    return None


def _parse_pts_string(s) -> float:
    if s is None:
        return 0.0
    if isinstance(s, (int, float)):
        return float(s)
    m = re.search(r'(\d+(?:\.\d+)?)', str(s))
    return float(m.group(1)) if m else 0.0


# -----------------------------------------------------------------------
# THRESHOLD PARSING
# -----------------------------------------------------------------------

def _parse_thresholds(raw) -> list:
    """
    Parse the Thresholds field from AppWorks into a list of dicts.
    Supports:
      - Already a list  → returned as-is
      - JSON string     → parsed
      - Compact string  → ">=5:25,>=3:20,..." converted to dicts
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
        breakpoints = []
        for segment in raw.split(","):
            segment = segment.strip()
            if ":" in segment:
                cond, pts_str = segment.rsplit(":", 1)
                try:
                    pts = float(pts_str.strip())
                except ValueError:
                    continue
                breakpoints.append({"condition": cond.strip(), "points": pts})
        return breakpoints
    return []


def _apply_numeric_thresholds(value: float, breakpoints: list) -> float:
    """
    Evaluate value against sorted breakpoints. First match wins.
    Supports {min_value: N, points: P} and {condition: ">=N", points: P}.
    """
    for bp in breakpoints:
        pts     = _safe_float(bp.get("points", bp.get("pts", 0)))
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
# CHILD RULE BREAKPOINTS — load from AgentRulesTable child entity
# -----------------------------------------------------------------------

def _parse_condition_to_threshold(condition_str: str, pts: float, dimension_key: str) -> dict:
    """
    Convert one child Rules CONDITION string → a threshold dict.
    Purely pattern-based — no hardcoded dimension assumptions beyond
    recognising additive vs numeric from the dimension_key passed in.
    """
    s       = condition_str.strip()
    s_lower = s.lower()

    # ── Additive named conditions (any dimension flagged as additive) ──
    if dimension_key in _ADDITIVE_DIMENSIONS or "characteristic" in dimension_key.lower():
        if "fast track" in s_lower:
            return {"condition": "fast_track", "points": pts}
        if "multiple subject" in s_lower or ("subject" in s_lower and "2" in s):
            return {"condition": "multiple_subjects", "points": pts}
        if "received age" in s_lower or "30 day" in s_lower or "> 30" in s_lower:
            return {"condition": "received_age_gt30", "points": pts}
        # Generic named condition — store as-is for dynamic evaluation
        return {"condition": s_lower, "points": pts}

    # ── Zero sentinel ──────────────────────────────────────────────────
    if re.fullmatch(r'\$?0(\.0+)?\s*(cases?|pts?|similar cases?)?', s_lower.strip()):
        return {"condition": "0", "points": 0.0}

    # ── "≥ N" / ">= N" / "> N" ────────────────────────────────────────
    m = re.search(r'[≥>]=?\s*\$?\s*(\d[\d,]*(?:\.\d+)?)', s)
    if m:
        num = float(m.group(1).replace(",", ""))
        if num == 0:
            num = 0.01
        return {"min_value": num, "points": pts}

    # ── "N – M cases" or "N-M cases" → lower bound ────────────────────
    m = re.search(r'(\d+)\s*[–\-]\s*(\d+)', s)
    if m:
        return {"min_value": float(m.group(1)), "points": pts}

    # ── Bare number: "2 cases", "1 case", "4 types" ───────────────────
    m = re.match(r'(\d+)', s.strip())
    if m:
        return {"min_value": float(m.group(1)), "points": pts}

    logger.warning(f"_parse_condition_to_threshold: could not parse {condition_str!r}")
    return {}


def _fetch_child_rules_breakpoints(item_id: str, dimension_key: str, child_href: str = None) -> list:
    """
    Fetch /entities/AgentRulesTable/items/{item_id}/childEntities/Rules
    and convert each child row into a threshold dict.
    """
    thresholds = []
    try:
        endpoint   = child_href or AppWorksPaths.FraudRules.risk_rules_by_id(item_id)

        res        = fetch(endpoint)
        child_items = res.get("_embedded", {}).get("Rules", [])
        logger.info(
            f"  AgentRulesTable/{item_id}/childEntities/Rules: "
            f"{len(child_items)} child row(s)"
        )
        for child in child_items:
            cp       = child.get("Properties", {})
            cond_str = str(_get_prop(cp, ["CONDITION", "Condition", "condition", "AgentRulesTable_Condition"]) or "")
            wt_str   = _get_prop(cp, ["WEIGHT", "Weight", "weight", "POINTS", "Points", "points", "AgentRulesTable_Weight"])
            pts      = _parse_pts_string(wt_str)
            if not cond_str:
                continue
            threshold = _parse_condition_to_threshold(cond_str, pts, dimension_key)
            if threshold:
                thresholds.append(threshold)

        # Sort numeric descending, then named conditions
        numeric = sorted(
            [t for t in thresholds if "min_value" in t],
            key=lambda t: t["min_value"], reverse=True
        )
        named   = [t for t in thresholds if "condition" in t]
        thresholds = numeric + named

    except Exception as e:
        logger.warning(f"  Child Rules fetch failed for item {item_id}: {e}")

    return thresholds


# -----------------------------------------------------------------------
# BONUS PARSING
# -----------------------------------------------------------------------

def _parse_bonus_from_condition(cond_str: str, dimension_key: str) -> tuple:
    """
    Extract bonus condition key and points from the parent CONDITION field.
    Falls back to spec defaults for known dimensions only.
    Returns (bonus_condition_key: str, bonus_pts: float).
    """
    s = (cond_str or "").strip().lower()

    m = re.search(r'\+\s*(\d+(?:\.\d+)?)\s*bonus', s)
    bonus_pts = float(m.group(1)) if m else 0.0

    if bonus_pts > 0:
        if "primary" in s and ("prior" in s or "case" in s):
            return "primary_ge2", bonus_pts
        if "ordered" in s or "unrealised" in s or "2x" in s or "2×" in s:
            return "ordered_gt_2x_calculated", bonus_pts
        if "open" in s and "allegation" in s:
            return "open_allegation", bonus_pts

    # Spec fallback for known dimensions only
    if dimension_key in _SPEC_BONUS:
        return _SPEC_BONUS[dimension_key]
    return "", 0.0


# -----------------------------------------------------------------------
# EVALUATION STRATEGY RESOLUTION
# -----------------------------------------------------------------------

def _resolve_evaluation_strategy(rule: dict) -> str:
    """
    Determines HOW a rule should be evaluated by inspecting its own fields.
    Returns one of:
      "numeric_threshold"   — single value scored against breakpoints
      "additive_conditions" — sum points from multiple named conditions
      "fraud_type_match"    — award points when case fraud types match
      "unknown"             — cannot evaluate; will log and skip
    """
    dim          = (rule.get("dimension_key") or "").lower()
    eval_strat   = (rule.get("evaluation_strategy") or "").lower()
    description  = (rule.get("description") or "").lower()
    rule_id      = (rule.get("rule_id") or "").lower()
    thresholds   = rule.get("thresholds") or []
    target_types = rule.get("target_fraud_types") or []

    # Explicit strategy set on rule (AppWorks can set this directly)
    if eval_strat in ("numeric_threshold", "additive_conditions", "fraud_type_match"):
        return eval_strat

    # Explicit dimension key match
    if dim in _NUMERIC_DIMENSIONS:
        return "numeric_threshold"
    if dim in _ADDITIVE_DIMENSIONS:
        return "additive_conditions"

    # Has fraud type targets → fraud_type_match
    if target_types:
        return "fraud_type_match"

    # Thresholds contain named conditions (not min_value) → additive
    if thresholds and all("condition" in t and "min_value" not in t for t in thresholds):
        named_conds = [str(t.get("condition", "")).lower() for t in thresholds]
        # If conditions look like numeric comparisons, treat as numeric
        numeric_patterns = any(
            re.search(r'[≥><=]|\d+', c) for c in named_conds
        )
        if not numeric_patterns:
            return "additive_conditions"

    # Thresholds contain min_value → numeric
    if thresholds and any("min_value" in t for t in thresholds):
        return "numeric_threshold"

    # Description/rule_id contains fraud type signal words
    fraud_signals = ["fraud type", "fs/snap", "mh/pca", "snap", "snap benefit",
                     "undeclared", "non-disclosure", "billing", "terminated","Personal Care Attendant", "Employment"]
    combined = f"{dim} {description} {rule_id}"
    if any(sig in combined for sig in fraud_signals):
        return "fraud_type_match"

    logger.warning(
        f"_resolve_evaluation_strategy: cannot determine strategy for rule "
        f"'{rule.get('rule_id')}' dim='{dim}'. Will attempt numeric_threshold as last resort."
    )
    return "numeric_threshold"


# -----------------------------------------------------------------------
# LIVE DATA FETCHERS — one per data domain
# -----------------------------------------------------------------------

def _fetch_subject_history(subject_id: str, case_id: str) -> tuple[int, int]:
    """Returns (prior_case_count, primary_in_cases_count)."""
    prior_case_count = 0
    primary_count    = 0
    try:
        subj_res   = fetch(AppWorksPaths.Subject.item(subject_id))
        subj_links = subj_res.get("_links", {})
        mapping_href = (
            subj_links
            .get("relationship:Subject_SubjectWorkfolderMapping", {})
            .get("href")
        )
        if not mapping_href:
            mapping_href = (
               
                AppWorksPaths.Subject.workfolder_mappings(subject_id)
            )
        mappings = _fetch_embedded(mapping_href, "Subject_SubjectWorkfolderMapping")
        prior_case_count = len(mappings)
        for m in mappings:
            if m.get("Properties", {}).get("SubjectWorkfolderMapping_IsPrimary"):
                primary_count += 1
    except Exception as e:
        logger.warning(f"Subject history fetch failed for {subject_id}: {e}")
    return prior_case_count, primary_count


def _fetch_financial_exposure(case_id: str) -> tuple[float, float]:
    """Returns (total_calculated, total_ordered)."""
    total_calculated = 0.0
    total_ordered    = 0.0
    try:
        wf_res   = fetch(AppWorksPaths.Workfolder.item(case_id))
        wf_links = wf_res.get("_links", {})
        fin_href = wf_links.get("relationship:Workfolder_FinancialRelationship", {}).get("href")
        if fin_href:
            fin_items = _fetch_embedded(fin_href, "Workfolder_FinancialRelationship")
            for fin_item in fin_items:
                fin_self = fin_item.get("_links", {}).get("self", {}).get("href", "")
                fin_props, _ = _fetch_props_links(fin_self)
                total_calculated += _safe_float(fin_props.get("Financial_Calculated"))
                total_ordered    += _safe_float(fin_props.get("Financial_Ordered"))
    except Exception as e:
        logger.warning(f"Financial fetch failed for {case_id}: {e}")
    return total_calculated, total_ordered


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


def _fetch_similar_case_volume(case_id: str) -> int:
    """Counts distinct workfolders with matching allegation types via Allegations_All."""
    total           = 0
    seen            = set()
    raw_match_count = 0
    unresolved      = 0
    try:
        wf_res     = fetch(AppWorksPaths.Workfolder.item(case_id))
        wf_links   = wf_res.get("_links", {})
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
                        type_href  = a_links.get(
                            "relationship:Allegations_AllegationsType", {}
                        ).get("href", "")
                if not type_href:
                    continue
                type_id   = type_href.rstrip("/").split("/")[-1]
                if not type_id:
                    continue
                list_res  = fetch_list(
                    AppWorksPaths.Allegations.allegations_by_type(type_id)
                )
                matched        = list_res.get("_embedded", {}).get("Allegations_All", [])
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
        wf_res     = fetch(AppWorksPaths.Workfolder.item(case_id))
        wf_links   = wf_res.get("_links", {})
        alleg_href = wf_links.get(
            "relationship:Workfolder_AllegationsRelationship", {}
        ).get("href")
        if alleg_href:
            alleg_items = _fetch_embedded(alleg_href, "Workfolder_AllegationsRelationship")
            seen_types  = set()
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
        wf_res   = fetch(AppWorksPaths.Workfolder.item(case_id))
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


def _fetch_fraud_types_for_case(case_id: str) -> list[str]:
    """
    Fetches the live fraud/allegation type descriptions for a case from AppWorks.
    Used by fraud_type_match rules to determine what types are present.
    """
    fraud_types = set()
    try:
        wf_res     = fetch(AppWorksPaths.Workfolder.item(case_id))
        wf_links   = wf_res.get("_links", {})
        alleg_href = wf_links.get(
            "relationship:Workfolder_AllegationsRelationship", {}
        ).get("href")
        if alleg_href:
            alleg_items = _fetch_embedded(alleg_href, "Workfolder_AllegationsRelationship")
            for alleg_item in alleg_items:
                a_self = alleg_item.get("_links", {}).get("self", {}).get("href", "")
                if a_self:
                    _, a_links = _fetch_props_links(a_self)
                    type_href  = a_links.get(
                        "relationship:Allegations_AllegationsType", {}
                    ).get("href", "")
                    if type_href:
                        type_props, _ = _fetch_props_links(type_href)
                        desc = (
                            type_props.get("AllegationType_Description")
                            or type_props.get("Description")
                            or type_props.get("AllegationType_ShortDescription")
                            or type_props.get("ShortDescription")
                            or ""
                        )
                        if desc:
                            fraud_types.add(desc.strip())
    except Exception as e:
        logger.warning(f"Fraud types fetch failed for case {case_id}: {e}")
    return list(fraud_types)


def _fetch_live_data_for_dimension(
    dimension_key: str,
    case_id: str,
    subject_id: str,
    # Optional pre-fetched overrides
    prior_case_count:       int   = None,
    primary_in_prior_cases: int   = None,
    total_calculated:       float = None,
    total_ordered:          float = None,
    similar_case_volume:    int   = None,
    distinct_types:         int   = None,
    has_open_allegation:    bool  = None,
    fast_track:             bool  = None,
    subject_count:          int   = None,
    received_age:           int   = None,
    fraud_types:            list  = None,
) -> dict:
    """
    Fetches live AppWorks data for the given dimension_key.
    Returns a context dict that _score_dimension uses for evaluation.
    Pre-fetched override params are merged (live wins when larger/truthy).
    """
    ctx = {}

    if dimension_key == "subject_history":
        p_count, p_primary = _fetch_subject_history(subject_id, case_id)
        if prior_case_count is not None and int(prior_case_count) > p_count:
            p_count = int(prior_case_count)
        if primary_in_prior_cases is not None:
            p_primary = max(p_primary, int(primary_in_prior_cases))
        ctx["prior_case_count"]       = p_count
        ctx["primary_in_prior_cases"] = p_primary

    elif dimension_key == "financial_exposure":
        calc, ordered = _fetch_financial_exposure(case_id)
        if total_calculated is not None and float(total_calculated) > calc:
            calc = float(total_calculated)
        if total_ordered is not None and float(total_ordered) > ordered:
            ordered = float(total_ordered)
        ctx["total_calculated"] = calc
        ctx["total_ordered"]    = ordered

    elif dimension_key == "similar_case_volume":
        s_vol = _fetch_similar_case_volume(case_id)
        if similar_case_volume is not None and int(similar_case_volume) > s_vol:
            s_vol = int(similar_case_volume)
        ctx["similar_case_volume"] = s_vol

    elif dimension_key == "allegation_severity":
        d_types, open_all = _fetch_allegation_severity(case_id)
        if distinct_types is not None and int(distinct_types) > d_types:
            d_types = int(distinct_types)
        if has_open_allegation is not None:
            if isinstance(has_open_allegation, str):
                open_all = open_all or has_open_allegation.lower() in ("true", "1", "yes")
            else:
                open_all = open_all or bool(has_open_allegation)
        ctx["distinct_types"]      = d_types
        ctx["has_open_allegation"] = open_all

    elif dimension_key == "case_characteristics":
        ft, sc, age = _fetch_case_characteristics(case_id)
        if fast_track is not None:
            ft = ft or (str(fast_track).lower() in ("true", "1", "yes"))
        if subject_count is not None and int(subject_count) > sc:
            sc = int(subject_count)
        if received_age is not None and int(received_age) > age:
            age = int(received_age)
        ctx["fast_track"]    = ft
        ctx["subject_count"] = sc
        ctx["received_age"]  = age

    else:
        # Unknown / new dimension — fetch from case workfolder properties
        # and make all fields available so the rule can still evaluate
        # against whatever the AppWorks response contains.
        try:
            wf_res   = fetch(AppWorksPaths.Workfolder.item(case_id))
            wf_props = wf_res.get("Properties", {})
            ctx["workfolder_properties"] = wf_props
            ctx["case_id"]  = case_id
        except Exception as e:
            logger.warning(f"Generic workfolder fetch failed for {case_id}: {e}")

    # Always make live fraud types available (fetched once, cached in caller)
    ctx["fraud_types"] = fraud_types or []
    return ctx


# -----------------------------------------------------------------------
# DIMENSION SCORER — fully dynamic, no hardcoded if/elif per dimension
# -----------------------------------------------------------------------

def _score_dimension(
    rule:        dict,
    case_id:     str,
    subject_id:  str,
    fraud_types: list = None,
    **override_kwargs
) -> tuple[float, dict]:
    """
    Evaluates a single rule against live AppWorks data.
    Strategy is resolved from the rule itself — not a hardcoded chain.
    """
    dimension_key   = (rule.get("dimension_key") or "").strip()
    thresholds      = _parse_thresholds(rule.get("thresholds") or "")
    bonus_condition = rule.get("bonus_condition", "")
    bonus_pts       = _safe_float(rule.get("bonus_pts", 0))
    max_pts         = _safe_float(rule.get("max_pts", 0))
    rule_id         = rule.get("rule_id", dimension_key)
    description     = rule.get("description", rule_id)
    target_types    = rule.get("target_fraud_types") or []

    # ── Recover missing dimension_key from description ──────────────
    if not dimension_key:
        source_text = f"{description} {rule_id}".lower()
        for keyword, dk in _DESC_TO_DIM.items():
            if keyword in source_text:
                dimension_key = dk
                logger.info(f"Recovered dimension_key='{dk}' from rule '{rule_id}'")
                break

    # ── Resolve evaluation strategy ────────────────────────────────
    strategy = _resolve_evaluation_strategy({**rule, "dimension_key": dimension_key})

    # ── Fetch live data ────────────────────────────────────────────
    ctx = _fetch_live_data_for_dimension(
        dimension_key, case_id, subject_id,
        fraud_types=fraud_types,
        **override_kwargs
    )

    pts           = 0.0
    bonus_applied = 0.0
    flags         = []
    condition_matched = ""

    # ════════════════════════════════════════════════════════════════
    # STRATEGY 1: numeric_threshold
    # Score a single numeric value against sorted breakpoints.
    # ════════════════════════════════════════════════════════════════
    if strategy == "numeric_threshold":

        if dimension_key == "subject_history":
            value             = float(ctx.get("prior_case_count", 0))
            condition_matched = f"{int(value)} prior case(s)"
            pts               = _apply_numeric_thresholds(value, thresholds)
            is_primary_ge2    = ctx.get("primary_in_prior_cases", 0) >= 2
            if bonus_condition == "primary_ge2" and is_primary_ge2:
                bonus_applied = bonus_pts
            flags.append(
                f"Subject History: {int(value)} prior case(s) → {pts} pts"
                + (f" +{bonus_applied} primary bonus" if bonus_applied else "")
                + f" = {min(pts + bonus_applied, max_pts)}/{max_pts}"
            )

        elif dimension_key == "financial_exposure":
            calc              = ctx.get("total_calculated", 0.0)
            ordered           = ctx.get("total_ordered",    0.0)
            value             = calc
            condition_matched = f"calculated={calc}, ordered={ordered}"
            pts               = _apply_numeric_thresholds(value, thresholds)
            if (
                bonus_condition == "ordered_gt_2x_calculated"
                and ordered > 0 and calc > 0
                and ordered > 2 * calc
            ):
                bonus_applied = bonus_pts
            flags.append(
                f"Financial Exposure: calc={calc}, ordered={ordered} → {pts} pts"
                + (f" +{bonus_applied} unrealised bonus" if bonus_applied else "")
                + f" = {min(pts + bonus_applied, max_pts)}/{max_pts}"
            )

        elif dimension_key == "similar_case_volume":
            value             = float(ctx.get("similar_case_volume", 0))
            condition_matched = f"{int(value)} similar cases found"
            pts               = _apply_numeric_thresholds(value, thresholds)
            flags.append(f"Similar Case Volume: {int(value)} cases found → {pts}/{max_pts}")

        elif dimension_key == "allegation_severity":
            value             = float(ctx.get("distinct_types", 0))
            open_all          = ctx.get("has_open_allegation", False)
            condition_matched = f"{int(value)} distinct type(s)"
            pts               = _apply_numeric_thresholds(value, thresholds)
            if bonus_condition == "open_allegation" and open_all:
                bonus_applied = bonus_pts
            flags.append(
                f"Allegation Severity: {int(value)} distinct type(s) → {pts} pts"
                + (f" +{bonus_applied} open-allegation bonus" if bonus_applied else "")
                + f" = {min(pts + bonus_applied, max_pts)}/{max_pts}"
            )

        else:
            # ── Generic numeric rule: inspect workfolder properties ────
            # Find the first numeric property that matches a threshold
            wf_props = ctx.get("workfolder_properties", {})
            scored   = False
            for bp in thresholds:
                prop_name = bp.get("property") or bp.get("field")
                if prop_name and prop_name in wf_props:
                    value = _safe_float(wf_props.get(prop_name, 0))
                    pts   = _apply_numeric_thresholds(value, thresholds)
                    condition_matched = f"{prop_name}={value}"
                    flags.append(f"{rule_id}: {prop_name}={value} → {pts}/{max_pts}")
                    scored = True
                    break
            if not scored:
                logger.info(
                    f"Generic numeric rule '{rule_id}' (dim='{dimension_key}'): "
                    f"no matching property found in workfolder — scoring 0"
                )
                return 0.0, {}

    # ════════════════════════════════════════════════════════════════
    # STRATEGY 2: additive_conditions
    # Each threshold is an independent named condition.
    # Points accumulate for each condition that is true.
    # ════════════════════════════════════════════════════════════════
    elif strategy == "additive_conditions":
        ft  = ctx.get("fast_track",    False)
        sc  = ctx.get("subject_count", 0)
        age = ctx.get("received_age",  0)

        for bp in thresholds:
            cond_name = str(bp.get("condition", "")).strip().lower()
            bp_pts    = _safe_float(bp.get("points", 0))

            # Known named conditions
            if cond_name == "fast_track" and ft:
                pts  += bp_pts
                flags.append(f"Case Characteristics: Fast Track=True → +{bp_pts}")
            elif cond_name == "multiple_subjects" and sc >= 2:
                pts  += bp_pts
                flags.append(f"Case Characteristics: {sc} subjects → +{bp_pts}")
            elif cond_name == "received_age_gt30" and age > 30:
                pts  += bp_pts
                flags.append(f"Case Characteristics: age={age} days > 30 → +{bp_pts}")
            else:
                # Dynamic: attempt to evaluate the condition as a numeric expression
                # against workfolder properties if condition contains a number/comparator
                wf_props = ctx.get("workfolder_properties", {})
                m = re.search(r'(\w+)\s*([><=!]+)\s*(\d+(?:\.\d+)?)', cond_name)
                if m and wf_props:
                    prop, op, num_str = m.group(1), m.group(2), m.group(3)
                    prop_val = _safe_float(wf_props.get(prop))
                    num_val  = float(num_str)
                    matched  = (
                        (op in (">",  "gt") and prop_val >  num_val) or
                        (op in (">=", "ge") and prop_val >= num_val) or
                        (op in ("<",  "lt") and prop_val <  num_val) or
                        (op in ("<=", "le") and prop_val <= num_val) or
                        (op in ("=",  "==", "eq") and prop_val == num_val)
                    )
                    if matched:
                        pts  += bp_pts
                        flags.append(f"{rule_id}: {cond_name} → +{bp_pts}")

        condition_matched = "; ".join(
            f for f in flags if "total:" not in f
        ) or "no conditions met"
        flags.append(f"Case Characteristics total: {min(pts, max_pts)}/{max_pts}")

    # ════════════════════════════════════════════════════════════════
    # STRATEGY 3: fraud_type_match
    # Award points when one of the case's fraud types matches the
    # rule's target_fraud_types or description label.
    # ════════════════════════════════════════════════════════════════
    elif strategy == "fraud_type_match":
        case_fraud_types = ctx.get("fraud_types") or []

        # Build the set of label fragments this rule targets.
        # Sources (priority order):
        #   1. target_fraud_types list on the rule (explicit)
        #   2. description field
        #   3. bonus_condition field (stores raw CONDITION from AppWorks)
        target_labels = set()
        for t in target_types:
            target_labels.add(str(t).strip().lower())
        if not target_labels and description:
            target_labels.add(description.strip().lower())
        if not target_labels and bonus_condition:
            target_labels.add(bonus_condition.strip().lower())

        matched_type = None
        for ft in case_fraud_types:
            ft_lower = ft.strip().lower()
            for label in target_labels:
                # Match if either string contains a significant word from the other
                if (
                    ft_lower in label
                    or label in ft_lower
                    or any(
                        word in label
                        for word in ft_lower.split()
                        if len(word) > 3
                    )
                    or any(
                        word in ft_lower
                        for word in label.split()
                        if len(word) > 3
                    )
                ):
                    matched_type = ft
                    break
            if matched_type:
                break

        if matched_type:
            pts               = max_pts  # Full points for a match
            condition_matched = f"fraud type matched: {matched_type}"
            flags.append(f"{rule_id}: fraud type '{matched_type}' matched → {pts}/{max_pts}")
        else:
            # No match — this rule simply does not apply to this case
            return 0.0, {}

    else:
        logger.error(
            f"Cannot evaluate rule '{rule_id}' — unknown strategy '{strategy}', "
            f"dimension_key='{dimension_key}'"
        )
        return 0.0, {}

    total_pts = min(pts + bonus_applied, max_pts)
    if total_pts <= 0:
        return 0.0, {}

    return total_pts, {
        "rule_id":    rule_id,
        "rule_name":  description,
        "weight":     total_pts,
        "max_weight": max_pts,
        "display":    f"{total_pts} / {max_pts}",
        "findings":   condition_matched,
        "flags":      flags,
    }


# -----------------------------------------------------------------------
# TOOL: get_risk_rules
# -----------------------------------------------------------------------

def get_risk_rules() -> dict:
    """
    Fetches ALL active BSI fraud detection rules from AppWorks AgentRulesTable.
    Returns fully resolved rule dicts including thresholds (already parsed),
    bonus conditions, max_pts, and evaluation_strategy.

    Supports any number of rules — 5 or 25+ — without code changes.
    For each rule:
      1. Reads all field variants from AppWorks.
      2. Derives dimension_key from description/rule_id if column is absent.
      3. Detects evaluation_strategy dynamically.
      4. Falls back to child Rules entity for breakpoints if parent has none.
      5. Falls back to spec defaults ONLY for the 5 known core dimensions
         when AppWorks has no breakpoints at all.
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
            identity = item.get("Identity", {})

            if idx == 0:
                logger.info(f"AgentRulesTable first-row prop keys: {sorted(props.keys())}")

            # ── is_active ────────────────────────────────────────────
            active_raw = None
            for ak in (
                "AgentRulesTable_IsActive", "IsActive", "Active", "ACTIVE",
                "AgentRulesTable_Active", "AgentRulesTable_Enabled", "Enabled", "ENABLED"
            ):
                if ak in props:
                    active_raw = props[ak]
                    break
            # Column absent → treat as active
            if active_raw is None:
                is_active = True
            else:
                is_active = str(active_raw).strip().lower() not in (
                    "false", "0", "no", "inactive", "disabled", "none"
                )
            if not is_active:
                logger.info(f"  Row {idx}: skipped — IsActive={active_raw!r}")
                continue

            # ── rule_id ──────────────────────────────────────────────
            # rule_id = str(
            #     props.get("RULE_ID")
            #     or props.get("AgentRulesTable_RuleId")
            #     or props.get("RuleId")
            #     or props.get("AgentRulesTable_Name")
            #     or props.get("Name")
            #     or props.get("AgentRulesTable_Title")
            #     or props.get("Title")
            #     or ""
            # )
            # if not rule_id:
            #     self_href = links.get("self", {}).get("href", "")
            #     if self_href:
            #         rule_id = f"R-{self_href.rstrip('/').split('/')[-1]}"

            rule_id = str(
                identity.get("BusinessId")
                or props.get("RULE_ID")
                or props.get("AgentRulesTable_RuleId")
                or props.get("RuleId")
                or props.get("AgentRulesTable_Name")
                or props.get("Name")
                or props.get("AgentRulesTable_Title")
                or props.get("Title")
                or ""
            )
            if not rule_id:
                self_href = links.get("self", {}).get("href", "")
                if self_href:
                    rule_id = f"R-{self_href.rstrip('/').split('/')[-1]}"
                    
            # ── description ──────────────────────────────────────────
            # description = str(
            #     props.get("DESCRIPTION")
            #     or props.get("AgentRulesTable_Description")
            #     or props.get("Description")
            #     or props.get("AgentRulesTable_Label")
            #     or props.get("Label")
            #     or rule_id
            # )
            description = str(
                props.get("RULE_ID")
                or props.get("DESCRIPTION")
                or props.get("AgentRulesTable_Description")
                or props.get("Description")
                or props.get("AgentRulesTable_Label")
                or props.get("Label")
                or rule_id
            )
            
            # ── dimension_key ─────────────────────────────────────────
            dimension_key = str(
                props.get("DIMENSION_KEY")
                or props.get("DimensionKey")
                or props.get("AgentRulesTable_DimensionKey")
                or props.get("AgentRulesTable_Dimension")
                or props.get("Dimension")
                or props.get("KEY")
                or props.get("Key")
                or props.get("TYPE")
                or props.get("Type")
                or ""
            )
            if not dimension_key:
                source_text = f"{description} {rule_id}".lower()
                for keyword, dk in _DESC_TO_DIM.items():
                    if keyword in source_text:
                        dimension_key = dk
                        logger.info(f"  Row {idx}: derived dimension_key='{dk}' from metadata")
                        break

            # ── evaluation_strategy (AppWorks can set this explicitly) ─
            eval_strat = str(
                props.get("EVALUATION_STRATEGY")
                or props.get("EvaluationStrategy")
                or props.get("AgentRulesTable_EvaluationStrategy")
                or props.get("Strategy")
                or ""
            )

            # ── target_fraud_types (for fraud_type_match rules) ───────
            target_types_raw = (
                props.get("TARGET_FRAUD_TYPES")
                or props.get("TargetFraudTypes")
                or props.get("AgentRulesTable_TargetFraudTypes")
                or props.get("FraudTypes")
                or props.get("FRAUD_TYPES")
                or ""
            )
            target_fraud_types = []
            if target_types_raw:
                if isinstance(target_types_raw, list):
                    target_fraud_types = target_types_raw
                else:
                    try:
                        parsed = json.loads(str(target_types_raw))
                        target_fraud_types = parsed if isinstance(parsed, list) else [str(parsed)]
                    except (json.JSONDecodeError, ValueError):
                        # Comma-separated string
                        target_fraud_types = [
                            t.strip() for t in str(target_types_raw).split(",") if t.strip()
                        ]

            # Skip rows where we can't determine an identifier
            if not rule_id and not dimension_key:
                logger.warning(
                    f"  Row {idx}: skipped — no rule_id or dimension_key. "
                    f"Prop keys: {sorted(props.keys())}"
                )
                continue

            # ── max_pts ───────────────────────────────────────────────
            max_pts = _safe_float(
                props.get("WEIGHT")
                or props.get("AgentRulesTable_MaxPoints")
                or props.get("MaxPoints")
                or props.get("AgentRulesTable_MaxPts")
                or props.get("MaxPts")
                or 0
            )
            if isinstance(props.get("WEIGHT"), str):
                max_pts = _parse_pts_string(props.get("WEIGHT"))

            if max_pts <= 0 and dimension_key in _SPEC_MAX_PTS:
                max_pts = _SPEC_MAX_PTS[dimension_key]
                logger.info(f"  Row {idx} ({dimension_key}): using spec max_pts={max_pts}")

            # ── raw CONDITION field ────────────────────────────────────
            # Used as: bonus condition source, fraud type label for match rules,
            # and fallback threshold data.
            raw_condition = str(
                props.get("CONDITION")
                or props.get("AgentRulesTable_Condition")
                or props.get("Condition")
                or ""
            )

            # ── thresholds ────────────────────────────────────────────
            thresholds_raw = (
                props.get("AgentRulesTable_Thresholds")
                or props.get("Thresholds")
                or props.get("AgentRulesTable_BreakPoints")
                or props.get("BreakPoints")
                or ""
            )
            # Only use CONDITION as thresholds source for known numeric/additive dims
            if not thresholds_raw and dimension_key in (
                list(_NUMERIC_DIMENSIONS) + list(_ADDITIVE_DIMENSIONS)
            ):
                thresholds_raw = raw_condition

            thresholds = _parse_thresholds(thresholds_raw)

            # ── Fetch child Rules breakpoints dynamically ─────────────
            # The list endpoint does NOT return Identity.Id (numeric).
            # Strategy:
            #   1. Use the item's self href to fetch the full item record.
            #   2. From the full item, get Identity.Id (numeric, e.g. "32770").
            #   3. Scan _links for a child Rules link (key contains "Rules").
            #   4. Use that link (or build from item_id) to fetch breakpoints.
            # This works for ANY number of rules without hardcoding.
            item_self_href = links.get("self", {}).get("href") or ""
            item_id   = None
            child_href = None

            if item_self_href:
                try:
                    item_res   = fetch(item_self_href)
                    item_ident = item_res.get("Identity", {})
                    item_links = item_res.get("_links", {})

                    # Numeric Id — safe to use in URL path
                    item_id = str(item_ident.get("Id") or "").strip() or None

                    # Find child Rules link dynamically from item _links
                    for lk, lv in item_links.items():
                        if "Rules" in lk and isinstance(lv, dict) and lv.get("href"):
                            child_href = lv["href"]
                            break

                    logger.debug(
                        f"  Row {idx} ({rule_id}): item_id={item_id} "                        f"child_href={'found' if child_href else 'not found'}"
                    )
                except Exception as e:
                    logger.warning(f"  Row {idx} ({rule_id}): item fetch failed: {e}")

            if item_id or child_href:
                child_thresholds = _fetch_child_rules_breakpoints(
                    str(item_id) if item_id else "",
                    dimension_key,
                    child_href=child_href,
                )
                if child_thresholds:
                    thresholds = child_thresholds
                    logger.info(
                        f"  Row {idx} ({rule_id}): loaded {len(child_thresholds)} "
                        f"threshold(s) from child Rules (item_id={item_id})"
                    )
                else:
                    logger.debug(
                        f"  Row {idx} ({rule_id}): child Rules returned no breakpoints"
                    )
            else:
                logger.warning(
                    f"  Row {idx} ({rule_id}): could not resolve item_id — "
                    f"no self href in list response. Cannot fetch child Rules."
                )

            # Spec default thresholds — known core dimensions only
            if not thresholds and dimension_key in _SPEC_THRESHOLDS:
                thresholds = _SPEC_THRESHOLDS[dimension_key]
                logger.warning(
                    f"  Row {idx} ({dimension_key}): AppWorks has no breakpoints — "
                    f"using spec-default ({len(thresholds)} breakpoints). "
                    f"Populate AgentRulesTable to remove this fallback."
                )

            # ── bonus condition ───────────────────────────────────────
            bonus_raw  = raw_condition  # bonus is encoded in CONDITION for most rules
            bonus_cond, bonus_pts = _parse_bonus_from_condition(bonus_raw, dimension_key)

            # For fraud_type_match rules store the raw condition label as
            # bonus_condition so _score_dimension can match it.
            if not bonus_cond and raw_condition and not thresholds:
                bonus_cond = raw_condition.lower()

            # ── optional metadata ─────────────────────────────────────
            weight = _safe_float(
                props.get("AgentRulesTable_Weight") or props.get("Weight") or 0
            )
            tier_thresholds_raw = (
                props.get("AgentRulesTable_TierThresholds")
                or props.get("TierThresholds")
                or ""
            )
            recommendations_raw = (
                props.get("AgentRulesTable_Recommendations")
                or props.get("Recommendations")
                or ""
            )

            rule_obj = {
                "rule_id":             rule_id,
                "description":         description,
                "dimension_key":       dimension_key,
                "evaluation_strategy": eval_strat,   # may be "" — resolved at score time
                "thresholds":          thresholds,
                "bonus_condition":     bonus_cond,
                "bonus_pts":           bonus_pts,
                "max_pts":             max_pts,
                "weight":              weight,
                "tier_thresholds":     tier_thresholds_raw,
                "recommendations":     recommendations_raw,
                "target_fraud_types":  target_fraud_types,
                "active":              True,
            }

            rules.append(rule_obj)
            logger.info(
                f"  Row {idx}: loaded '{rule_id}' "
                f"dim='{dimension_key}' strategy='{eval_strat or 'auto'}' "
                f"max={max_pts} thresholds={len(thresholds)} "
                f"target_types={target_fraud_types} bonus='{bonus_cond}'+{bonus_pts}"
            )

    except Exception as e:
        logger.error(f"AgentRulesTable fetch failed: {e}. Returning empty rules list.")

    if not rules:
        logger.warning("No active rules loaded from AppWorks — using BSI spec defaults.")
        for dim_key, max_pts in _SPEC_MAX_PTS.items():
            t          = _SPEC_THRESHOLDS.get(dim_key, [])
            bc, bp     = _SPEC_BONUS.get(dim_key, ("", 0.0))
            rules.append({
                "rule_id":             f"SPEC-{dim_key}",
                "description":         dim_key.replace("_", " ").title(),
                "dimension_key":       dim_key,
                "evaluation_strategy": "",
                "thresholds":          t,
                "bonus_condition":     bc,
                "bonus_pts":           bp,
                "max_pts":             max_pts,
                "weight":              0.0,
                "tier_thresholds":     "",
                "recommendations":     "",
                "target_fraud_types":  [],
                "active":              True,
            })
            logger.info(f"Created spec-default rule for '{dim_key}' max={max_pts}")
    else:
        logger.info(f"get_risk_rules: {len(rules)} active rule(s) loaded")

    return {
        "result": {"active_rules": rules},
        "provenance": {
            "sources":      [f"AppWorks {_RULES_LIST_ENDPOINT}"],
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "computed_by":  "AppWorks REST retrieval",
        },
    }


# -----------------------------------------------------------------------
# TIER & RECOMMENDATION LOADERS
# -----------------------------------------------------------------------

def _load_tier_thresholds(active_rules: list) -> dict:
    """Read tier min-score thresholds from AppWorks rules metadata."""
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
    # Spec defaults — only when AppWorks provides no tier config
    return {"CRITICAL": 0.75, "HIGH": 0.50, "MEDIUM": 0.25, "LOW": 0.0}


def _load_recommendation(tier: str, active_rules: list) -> str | None:
    """Read recommendation text for a tier from AppWorks rules metadata."""
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
    return None


# -----------------------------------------------------------------------
# TOOL: calculate_risk_metrics
# -----------------------------------------------------------------------

def calculate_risk_metrics(
    case_id:                str,
    subject_id:             str,
    fraud_types:            list,
    active_rules:           list  = None,
    prior_case_count:       int   = None,
    primary_in_prior_cases: int   = None,
    total_calculated:       float = None,
    total_ordered:          float = None,
    similar_case_volume:    int   = None,
    distinct_types:         int   = None,
    has_open_allegation:    bool  = None,
    fast_track:             bool  = None,
    subject_count:          int   = None,
    received_age:           int   = None,
) -> dict:
    """
    Deterministic BSI risk scoring using active_rules from AppWorks.

    active_rules is passed by the LLM from get_risk_rules output.
    If not provided, rules are fetched from AppWorks directly.

    All rules are evaluated — including fraud-type specific rules,
    custom rules, and any new rules added to AppWorks without code changes.

    Scoring:
      - Each active rule defines its own dimension, strategy, and thresholds.
      - total_max = sum of all active rule max_pts from AppWorks.
      - risk_score = earned / total_max, normalised [0,1].
    """
    logger.info(f"calculate_risk_metrics — Case: {case_id}  Subject: {subject_id}")

    # ── Placeholder protection ─────────────────────────────────────────
    placeholders = {"primary_subject_id", "subject_primary_id", "subject_id", "placeholder"}
    if not subject_id or str(subject_id).lower() in placeholders or not str(subject_id).isdigit():
        logger.warning(f"Invalid/placeholder subject_id '{subject_id}' — resolving from case...")
        try:
            wf_res   = fetch(AppWorksPaths.Workfolder.item(case_id))
            links    = wf_res.get("_links", {})
            subj_href = links.get("relationship:Workfolder_SubjectsRelationship", {}).get("href")
            if subj_href:
                subjects = _fetch_embedded(subj_href, "Workfolder_SubjectsRelationship")
                for s in subjects:
                    if s.get("Properties", {}).get("Workfolder_SubjectsRelationship_IsPrimary"):
                        subject_id = str(
                            s.get("Properties", {}).get("Identity", {}).get("Id", "")
                        )
                        logger.info(f"Resolved primary subject_id: {subject_id}")
                        break
        except Exception as e:
            logger.error(f"Failed to resolve subject_id for case {case_id}: {e}")

    if isinstance(fraud_types, str):
        fraud_types = [fraud_types]
    fraud_types = [
        ft for ft in (fraud_types or [])
        if str(ft).lower() not in placeholders
    ]

    # ── Fetch rules if not provided ────────────────────────────────────
    # if active_rules is None:
    #     logger.info("active_rules not provided — fetching from AppWorks...")
    #     active_rules = get_risk_rules()["result"]["active_rules"]
    
    logger.info("Fetching active rules from AppWorks...")
    active_rules = get_risk_rules()["result"]["active_rules"]   
    
    if not active_rules:
        logger.warning("No active rules — risk score will be 0")

    # ── Fetch live fraud types from AppWorks (merge with passed list) ───
    live_fraud_types = _fetch_fraud_types_for_case(case_id)
    # Union: use both passed fraud_types and live-fetched ones
    combined_fraud_types = list({ft.strip() for ft in (fraud_types + live_fraud_types) if ft})
    logger.info(f"Fraud types for scoring: {combined_fraud_types}")

    total_earned      = 0.0
    total_max         = 0.0
    all_triggered     = []
    final_prior_count = 0

    for rule in active_rules:
        if not rule.get("active", True):
            continue

        max_pts    = _safe_float(rule.get("max_pts", 0))
        total_max += max_pts

        pts, triggered_dict = _score_dimension(
            rule        = rule,
            case_id     = case_id,
            subject_id  = subject_id,
            fraud_types = combined_fraud_types,
            prior_case_count       = prior_case_count,
            primary_in_prior_cases = primary_in_prior_cases,
            total_calculated       = total_calculated,
            total_ordered          = total_ordered,
            similar_case_volume    = similar_case_volume,
            distinct_types         = distinct_types,
            has_open_allegation    = has_open_allegation,
            fast_track             = fast_track,
            subject_count          = subject_count,
            received_age           = received_age,
        )
        total_earned += pts

        # Capture prior_case_count from subject_history dimension result
        if rule.get("dimension_key") == "subject_history" and triggered_dict:
            try:
                final_prior_count = int(
                    triggered_dict.get("findings", "0").split(" ")[0]
                )
            except (ValueError, IndexError):
                final_prior_count = prior_case_count or 0

        if pts > 0 and triggered_dict:
            all_triggered.append(triggered_dict)

        logger.info(
            f"  [{rule.get('rule_id')} / {rule.get('dimension_key')}] "
            f"{pts}/{max_pts} pts — "
            f"{triggered_dict.get('findings', '') if triggered_dict else 'not triggered'}"
        )

    # ── Normalise ───────────────────────────────────────────────────────
    effective_max = total_max if total_max > 0 else 100.0
    risk_score    = round(total_earned / effective_max, 4)

    # ── Tier ────────────────────────────────────────────────────────────
    tier_thresholds = _load_tier_thresholds(active_rules)
    tier = "LOW"
    for tier_name, min_score in sorted(tier_thresholds.items(), key=lambda x: -x[1]):
        if risk_score >= min_score:
            tier = tier_name
            break

    recommendation = _load_recommendation(tier, active_rules)

    logger.info(
        f"Risk result: {total_earned}/{effective_max} pts = {risk_score} ({tier}), "
        f"triggered rules: {[r['rule_id'] for r in all_triggered]}"
    )

    return {
        "result": {
            "case_id":          case_id,
            "subject_id":       subject_id,
            "risk_score":       risk_score,
            "risk_tier":        tier,
            "fraud_types":      combined_fraud_types,
            "risk_indicators":  all_triggered,
            "total_points":     round(total_earned, 1),
            "max_points":       round(effective_max, 1),
            "prior_case_count": final_prior_count,
            "recommendation":   recommendation,
            "active_rules":     active_rules,
        },
        "provenance": {
            "sources": [
                f"AppWorks case record {case_id}",
                f"AppWorks subject record {subject_id}",
                "AppWorks BSI fraud detection rules table",
            ],
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "computed_by":  "BSI configured rules evaluation",
        },
    }