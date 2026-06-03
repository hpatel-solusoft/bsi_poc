# appworks/risk_scoring.py
# ----------------------------------------------------------------
# Agent 4: Fraud Risk Assessment (Refactored & Instrumented)
# ----------------------------------------------------------------
# Architecture Notes:
# 1. Zero AppWorks fetches inside the math engine.
# 2. Strategy Pattern evaluation. Pure logic functions.
# 3. Dynamic Additive Parser: Safely evaluates AppWorks expressions.
# 4. Applicability Gate: Rules can be universal or scoped by fraud type.
# 5. Stateful Context Passing: Bypasses REST API when ai_summary is provided.
# 6. Strict Provenance. Output reflects if data came from REST or ai_summary.
# ----------------------------------------------------------------

import logging
import re
from datetime import datetime, timezone
from typing import Any

from appworks.appworks_auth import fetch
from appworks.appworks_paths import AppWorksPaths
from semantic_layer.entity_contracts import RiskRuleDef, TriggeredRule

# Ensure logging is configured to handle DEBUG messages when necessary
logger = logging.getLogger(__name__)

# =======================================================================
# 0. CONSTANTS & FALLBACKS
# =======================================================================

# Used ONLY as fallback because the AppWorks row has no DimensionKey column.
_DESC_TO_DIM: dict = {
    "subject history":     "subject_history",
    "financial exposure":  "financial_exposure",
    "similar case":        "similar_case_volume",
    "allegation severity": "allegation_severity",
    "case characteristic": "case_characteristics",
}


# =======================================================================
# 1. UTILITIES, PARSERS & CONTEXT BUILDER
# =======================================================================

def _safe_float(val: Any, default: float = 0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default

def _parse_pts_string(s) -> float:
    if s is None:
        return 0.0
    if isinstance(s, (int, float)):
        return float(s)
    m = re.search(r'(\d+(?:\.\d+)?)', str(s))
    return float(m.group(1)) if m else 0.0

def _parse_bonus_from_condition(cond_str: str) -> tuple[str, float]:
    """
    Extracts bonus condition key and points from the parent CONDITION field.
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

    return "", 0.0

def _parse_condition_to_threshold(condition_str: str, pts: float, dimension_key: str) -> dict:
    """
    Converts raw AppWorks child text strings (e.g. "≥ 5 cases") into
    structured threshold dictionaries with 'min_value' or 'condition'.
    """
    s = condition_str.strip()
    s_lower = s.lower()

    # Additive named conditions
    if dimension_key == "case_characteristics" or "characteristic" in dimension_key.lower():
        if "fast track" in s_lower:
            return {"condition": "fast_track", "points": pts}
        if "multiple subject" in s_lower or ("subject" in s_lower and "2" in s):
            return {"condition": "multiple_subjects", "points": pts}
        if "received age" in s_lower or "30 day" in s_lower or "> 30" in s_lower:
            return {"condition": "received_age_gt30", "points": pts}
        return {"condition": s_lower, "points": pts}

    # Zero sentinel
    if re.fullmatch(r'\$?0(\.0+)?\s*(cases?|pts?|similar cases?)?', s_lower.strip()):
        return {"condition": "0", "points": 0.0}

    # "≥ N" / ">= N" / "> N"
    m = re.search(r'[≥>]=?\s*\$?\s*(\d[\d,]*(?:\.\d+)?)', s)
    if m:
        num = float(m.group(1).replace(",", ""))
        if num == 0: num = 0.01
        return {"min_value": num, "points": pts}

    # "N – M cases" (lower bound)
    m = re.search(r'(\d+)\s*[–\-]\s*(\d+)', s)
    if m:
        return {"min_value": float(m.group(1)), "points": pts}

    # Bare number: "2 cases", "1 case"
    m = re.match(r'(\d+)', s.strip())
    if m:
        return {"min_value": float(m.group(1)), "points": pts}

    # Suppressed warning as text-based condition failures are expected for fraud match rules
    logger.debug(f"_parse_condition_to_threshold: could not parse {condition_str!r} as a numeric threshold.")
    return {}

def _fetch_child_rules_breakpoints(item_id: str, dimension_key: str, child_href: str | None = None) -> list:
    """
    Fetch childEntities/Rules and convert each child row into a threshold dict.
    """
    thresholds = []
    try:
        endpoint = child_href
        if not endpoint:
            if hasattr(AppWorksPaths, 'FraudRules') and hasattr(AppWorksPaths.FraudRules, 'risk_rules_by_id'):
                endpoint = AppWorksPaths.FraudRules.risk_rules_by_id(item_id)
            else:
                endpoint = f"/OSABSIACM/entities/FraudRiskRules/items/{item_id}/childEntities/Rules"

        res = fetch(endpoint)
        
        embedded = res.get("_embedded", {})
        child_items = embedded.get("Rules", [])
        if not child_items:
            child_items = next((v for v in embedded.values() if isinstance(v, list)), [])

        for child in child_items:
            cp = child.get("Properties", {})
            cond_str = str(cp.get("CONDITION") or cp.get("Condition") or cp.get("AgentRulesTable_Condition") or "")
            wt_str = cp.get("WEIGHT") or cp.get("Weight") or cp.get("POINTS") or cp.get("Points") or cp.get("AgentRulesTable_Weight")
            pts = _parse_pts_string(wt_str)
            
            if not cond_str:
                continue
                
            threshold = _parse_condition_to_threshold(cond_str, pts, dimension_key)
            if threshold:
                thresholds.append(threshold)

        numeric = sorted(
            [t for t in thresholds if "min_value" in t],
            key=lambda t: t["min_value"], reverse=True
        )
        named = [t for t in thresholds if "condition" in t]
        thresholds = numeric + named

    except Exception as e:
        logger.warning(f"Child Rules fetch failed for item {item_id}: {e}")

    return thresholds


def _workfolder_id_from_allegation(alleg_item: dict) -> str:
    """Helper for Similar Case Volume extraction."""
    props = alleg_item.get("Properties", {})
    links = alleg_item.get("_links", {})
    for key in ("Allegations_Workfolder$Identity", "Allegations_Workfolder", "Workfolder$Identity", "Workfolder"):
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


def _fetch_similar_case_volume(case_id: str, wf_res: dict) -> int:
    """Counts distinct workfolders with matching allegation types."""
    total = 0
    seen = set()
    try:
        wf_links = wf_res.get("_links", {})
        alleg_href = wf_links.get("relationship:Workfolder_AllegationsRelationship", {}).get("href")
        if alleg_href:
            alleg_items = fetch(alleg_href).get("_embedded", {}).get("Workfolder_AllegationsRelationship", [])
            for alleg_item in alleg_items:
                type_href = alleg_item.get("_links", {}).get("relationship:Allegations_AllegationsType", {}).get("href", "")
                if not type_href:
                    a_self = alleg_item.get("_links", {}).get("self", {}).get("href", "")
                    if a_self:
                        a_links = fetch(a_self).get("_links", {})
                        type_href = a_links.get("relationship:Allegations_AllegationsType", {}).get("href", "")
                
                if not type_href:
                    continue
                
                type_id = type_href.rstrip("/").split("/")[-1]
                if not type_id:
                    continue
                
                list_res = fetch(AppWorksPaths.Allegations.allegations_by_type(type_id))
                matched = list_res.get("_embedded", {}).get("Allegations_All", [])
                
                for alleg in matched:
                    wf_id = _workfolder_id_from_allegation(alleg)
                    if not wf_id or wf_id == str(case_id) or wf_id in seen:
                        continue
                    seen.add(wf_id)
                    total += 1
    except Exception as e:
        logger.warning(f"Similar case volume count failed: {e}")
    return total


def _build_case_context(case_id: str, subject_id: str, fraud_types: list[str], ai_summary: dict | None = None) -> dict:
    """
    Builds universal case context. 
    Prefers shared ai_summary state to bypass redundant AppWorks fetches.
    Falls back to dynamic AppWorks fetching if ai_summary is missing.
    """
    logger.info(f"Building universal case context for Case {case_id}, Subject {subject_id}")
    context = {
        "case_id": case_id,
        "subject_id": subject_id,
        "fraud_types": [str(ft).lower().strip() for ft in fraud_types]
    }

    # =====================================================================
    # STATEFUL CONTEXT PASSING (Bypass AppWorks REST API)
    # =====================================================================
    
    if ai_summary:
        logger.info("ai_summary detected in payload. Bypassing redundant AppWorks fetches.")
        inv = ai_summary.get("investigation", {})
        comp_intel = inv.get("complaint_intelligence", {})
        enrichment = inv.get("context_enrichment", {})
        
        # 1. Subject History
        context["subject_history"] = int(enrichment.get("total_prior_case_count", 0))
        
        primary_in_prior = 0
        for profile in enrichment.get("profiles", []):
            if str(profile.get("subject_id")) == str(subject_id):
                for prior in profile.get("prior_cases", []):
                    if prior.get("is_primary_subject"):
                        primary_in_prior += 1
        context["primary_in_prior_cases"] = primary_in_prior

        # 2. Financial Exposure
        fins = comp_intel.get("financials", {})
        context["financial_exposure"] = _safe_float(fins.get("total_calculated", 0))
        context["total_ordered"] = _safe_float(fins.get("total_ordered", 0))

        # 3. Similar Case Volume (Use the true scored total, not the truncated UI list)
        sim_cases = ai_summary.get("similar_cases", {})
        context["similar_case_volume"] = int(sim_cases.get("total_candidates_scored", 0))

        # 4. Allegation Severity
        has_open = False
        for alg in comp_intel.get("allegations", []):
            status = str(alg.get("status", "")).lower()
            if status in ("open", "active") and not alg.get("date_closed"):
                has_open = True
        context["has_open_allegation"] = has_open

        # 5. Case Characteristics
        details = comp_intel.get("details", {})
        age = _safe_float(details.get("date_received_age", 0))
        sub_count = len(comp_intel.get("subject_ids", []))
        
        context["workfolder_properties"] = {
            "fast_track": False, 
            "received_age_gt30": age > 30,
            "multiple_subjects": sub_count >= 2
        }

        logger.debug(f"Stateful Context built successfully: {context}")
        return context

    # =====================================================================
    # FALLBACK (Only executes if ai_summary is missing)
    # =====================================================================
    logger.info("No ai_summary provided. Falling back to dynamic AppWorks fetches...")

    # -- Subject History Domain --
    prior_case_count = 0
    primary_in_prior_cases = 0
    try:
        logger.debug(f"Fetching Subject History for subject: {subject_id}")
        subj_res = fetch(AppWorksPaths.Subject.item(subject_id))
        mapping_href = subj_res.get("_links", {}).get("relationship:Subject_SubjectWorkfolderMapping", {}).get("href")
        if mapping_href:
            mappings = fetch(mapping_href).get("_embedded", {}).get("Subject_SubjectWorkfolderMapping", [])
            prior_case_count = len(mappings)
            primary_in_prior_cases = sum(
                1 for m in mappings if m.get("Properties", {}).get("SubjectWorkfolderMapping_IsPrimary")
            )
            logger.debug(f"Subject History found: {prior_case_count} prior cases, {primary_in_prior_cases} as primary.")
    except Exception as e:
        logger.warning(f"Failed to fetch subject history context for {subject_id}: {e}")

    context["subject_history"] = prior_case_count
    context["primary_in_prior_cases"] = primary_in_prior_cases

    # -- Workfolder Root Fetch --
    wf_res = {}
    wf_props = {}
    wf_links = {}
    try:
        logger.debug(f"Fetching Workfolder & Financial properties for case: {case_id}")
        wf_res = fetch(AppWorksPaths.Workfolder.item(case_id))
        wf_props = wf_res.get("Properties", {})
        wf_links = wf_res.get("_links", {})
        logger.debug(f"Cached {len(wf_props)} raw properties from Workfolder.")
    except Exception as e:
        logger.warning(f"Failed to fetch root workfolder context for {case_id}: {e}")

    # -- Financial Exposure Domain --
    total_calculated = 0.0
    total_ordered = 0.0
    try:
        fin_href = wf_links.get("relationship:Workfolder_FinancialRelationship", {}).get("href")
        if fin_href:
            fin_items = fetch(fin_href).get("_embedded", {}).get("Workfolder_FinancialRelationship", [])
            total_calculated = sum(_safe_float(i.get("Properties", {}).get("Financial_Calculated")) for i in fin_items)
            total_ordered = sum(_safe_float(i.get("Properties", {}).get("Financial_Ordered")) for i in fin_items)
            logger.debug(f"Financials found: Calculated=${total_calculated}, Ordered=${total_ordered}")
    except Exception as e:
        logger.warning(f"Failed to fetch financial context for {case_id}: {e}")
        
    context["financial_exposure"] = total_calculated
    context["total_ordered"] = total_ordered

    # -- Similar Case Volume Domain --
    context["similar_case_volume"] = _fetch_similar_case_volume(case_id, wf_res)

    # -- Allegation Severity Domain --
    has_open_allegation = False
    try:
        alleg_href = wf_links.get("relationship:Workfolder_AllegationsRelationship", {}).get("href")
        if alleg_href:
            alleg_items = fetch(alleg_href).get("_embedded", {}).get("Workfolder_AllegationsRelationship", [])
            for alleg_item in alleg_items:
                a_self = alleg_item.get("_links", {}).get("self", {}).get("href", "")
                if a_self:
                    a_res = fetch(a_self)
                    a_props = a_res.get("Properties", {})
                    date_closed = a_props.get("Allegations_DateClosed")
                    status = (a_props.get("Allegations_AllegationStatus") or "").lower()
                    if not date_closed and status in ("open", "active", ""):
                        has_open_allegation = True
                        break
    except Exception as e:
        logger.warning(f"Failed to fetch allegation severity: {e}")
        
    context["has_open_allegation"] = has_open_allegation

    # -- Case Characteristics Dynamic Translation --
    fast_track = bool(wf_props.get("WorkfolderFastTrack") or wf_props.get("FAST_TRACK") or wf_props.get("FastTrack"))
    wf_props["fast_track"] = fast_track

    age_raw = wf_props.get("WorkfolderDateReceivedAge")
    try:
        age = int(float(age_raw)) if age_raw is not None else 0
    except (ValueError, TypeError):
        age = 0
    wf_props["received_age_gt30"] = age > 30

    subj_href = wf_links.get("relationship:Workfolder_SubjectsRelationship", {}).get("href")
    if subj_href:
        try:
            subj_items = fetch(subj_href).get("_embedded", {}).get("Workfolder_SubjectsRelationship", [])
            wf_props["multiple_subjects"] = len(subj_items) >= 2
        except Exception:
            pass

    context["workfolder_properties"] = wf_props

    logger.debug(f"Final Case Context built: keys={list(context.keys())}")
    return context


# =======================================================================
# 2. STRATEGY EVALUATORS (Pure Math & Logic)
# =======================================================================

def _evaluate_numeric(rule: RiskRuleDef, context: dict) -> TriggeredRule:
    """Scores a single numeric value dynamically and applies business bonuses."""
    logger.debug(f"Executing [NUMERIC] strategy for rule '{rule.rule_id}' (dimension: {rule.dimension_key})")
    
    value = float(context.get(rule.dimension_key, 0.0))
    logger.debug(f"Rule {rule.rule_id}: Extracted context value for '{rule.dimension_key}' = {value}")
    
    weight = 0.0
    bonus_applied = 0.0
    finding_msg = f"Value {value} did not meet any thresholds."

    if rule.thresholds:
        numeric_bps = sorted(
            [t for t in rule.thresholds if t.min_value is not None],
            key=lambda x: x.min_value, 
            reverse=True
        )
        logger.debug(f"Rule {rule.rule_id}: Evaluating against {len(numeric_bps)} sorted numeric thresholds.")
        
        for bp in numeric_bps:
            logger.debug(f"Rule {rule.rule_id}: Checking if {value} >= {bp.min_value}")
            if value >= bp.min_value:
                weight = bp.points
                finding_msg = f"{rule.dimension_key} ({value}) >= {bp.min_value}"
                logger.debug(f"Rule {rule.rule_id}: THRESHOLD MET! Awarding {weight} pts.")
                break

    # -- Business Rules Bonuses --
    if rule.bonus_condition == "primary_ge2" and context.get("primary_in_prior_cases", 0) >= 2:
        bonus_applied = rule.bonus_pts
        finding_msg += f" [+ {bonus_applied} pts primary bonus]"
        logger.debug(f"Rule {rule.rule_id}: Applied 'primary_ge2' bonus of {bonus_applied} pts.")
        
    elif rule.bonus_condition == "ordered_gt_2x_calculated":
        calc = context.get("financial_exposure", 0.0)
        ordered = context.get("total_ordered", 0.0)
        if ordered > 0 and calc > 0 and ordered > (2 * calc):
            bonus_applied = rule.bonus_pts
            finding_msg += f" [+ {bonus_applied} pts unrealised bonus]"
            logger.debug(f"Rule {rule.rule_id}: Applied 'ordered_gt_2x_calculated' bonus of {bonus_applied} pts.")
            
    elif rule.bonus_condition == "open_allegation" and context.get("has_open_allegation"):
        bonus_applied = rule.bonus_pts
        finding_msg += f" [+ {bonus_applied} pts open-allegation bonus]"
        logger.debug(f"Rule {rule.rule_id}: Applied 'open_allegation' bonus of {bonus_applied} pts.")

    total_weight = weight + bonus_applied

    return TriggeredRule(
        rule_id=rule.rule_id,
        description=rule.description, 
        weight=min(total_weight, rule.max_pts), # Cap at max
        max_weight=rule.max_pts,
        display=f"{min(total_weight, rule.max_pts)}/{rule.max_pts}",
        findings=finding_msg,
        triggered=(total_weight > 0)
    )


def _evaluate_additive(rule: RiskRuleDef, context: dict) -> TriggeredRule:
    """100% Dynamic Additive Evaluator."""
    logger.debug(f"Executing [ADDITIVE] strategy for rule '{rule.rule_id}'")
    weight = 0.0
    met_conditions = []
    wf_props = context.get("workfolder_properties", {})

    if not rule.thresholds:
        logger.debug(f"Rule {rule.rule_id}: No thresholds provided for additive evaluation.")

    if rule.thresholds:
        for bp in rule.thresholds:
            if not bp.condition:
                continue
                
            cond_str = str(bp.condition).strip()
            bp_pts = bp.points
            logger.debug(f"Rule {rule.rule_id}: Evaluating Additive Condition '{cond_str}' for {bp_pts} pts.")

            if cond_str in wf_props:
                val = wf_props.get(cond_str)
                logger.debug(f"Rule {rule.rule_id}: Found exact property match '{cond_str}' = {val}")
                if str(val).lower() in ("true", "1", "yes", "y") or val is True:
                    weight += bp_pts
                    met_conditions.append(cond_str)
                    logger.debug(f"Rule {rule.rule_id}: Condition met. +{bp_pts} pts.")
                continue

            m = re.match(r'^([a-zA-Z0-9_]+)\s*(>=|<=|>|<|==|!=|=)\s*(.+)$', cond_str)
            if m:
                prop_name, operator, target_val_str = m.groups()
                actual_val = wf_props.get(prop_name)
                logger.debug(f"Rule {rule.rule_id}: Regex parsed '{prop_name}' {operator} '{target_val_str}'. Live value = {actual_val}")
                
                if actual_val is not None:
                    matched = False
                    try:
                        actual_num = float(actual_val)
                        target_num = float(target_val_str)
                        if operator == ">": matched = actual_num > target_num
                        elif operator == ">=": matched = actual_num >= target_num
                        elif operator == "<": matched = actual_num < target_num
                        elif operator == "<=": matched = actual_num <= target_num
                        elif operator in ("==", "="): matched = actual_num == target_num
                        elif operator == "!=": matched = actual_num != target_num
                    except ValueError:
                        target_str = target_val_str.strip("'\"").lower()
                        actual_str = str(actual_val).strip().lower()
                        if operator in ("==", "="): matched = (actual_str == target_str)
                        elif operator == "!=": matched = (actual_str != target_str)

                    if matched:
                        weight += bp_pts
                        met_conditions.append(cond_str)
                        logger.debug(f"Rule {rule.rule_id}: Expression matched! +{bp_pts} pts.")
                    else:
                        logger.debug(f"Rule {rule.rule_id}: Expression did not match.")
            else:
                logger.debug(f"Rule {rule.rule_id}: Condition '{cond_str}' could not be parsed as a valid property or expression.")

    return TriggeredRule(
        rule_id=rule.rule_id,
        description=rule.description,
        weight=min(weight, rule.max_pts),
        max_weight=rule.max_pts,
        display=f"{min(weight, rule.max_pts)}/{rule.max_pts}",
        findings="Matched: " + ", ".join(met_conditions) if met_conditions else "No conditions met",
        triggered=(weight > 0)
    )


def _evaluate_fraud_type(rule: RiskRuleDef, context: dict) -> TriggeredRule:
    """Awards max points if case fraud types intersect with rule targets or description."""
    logger.debug(f"Executing [FRAUD_TYPE_MATCH] strategy for rule '{rule.rule_id}'")
    case_types = context.get("fraud_types", [])
    logger.debug(f"Rule {rule.rule_id}: Case contains fraud types -> {case_types}")
    
    target_labels = [t.lower().strip() for t in (rule.target_fraud_types or [])]
    if rule.description:
        target_labels.append(rule.description.lower().strip())
    if rule.bonus_condition: 
        target_labels.append(rule.bonus_condition.lower().strip())
        
    logger.debug(f"Rule {rule.rule_id}: Scanning for target signals -> {target_labels}")
        
    matched_type = None
    for ft in case_types:
        for label in target_labels:
            if ft in label or label in ft:
                matched_type = ft
                logger.debug(f"Rule {rule.rule_id}: Match found! '{ft}' intersects with target '{label}'")
                break
        if matched_type:
            break

    weight = rule.max_pts if matched_type else 0.0

    return TriggeredRule(
        rule_id=rule.rule_id,
        description=rule.description,
        weight=weight,
        max_weight=rule.max_pts,
        display=f"{weight}/{rule.max_pts}",
        findings=f"Fraud type matched: {matched_type}" if matched_type else "No matching fraud type",
        triggered=(weight > 0)
    )


def _score_rule(rule: RiskRuleDef, context: dict) -> TriggeredRule:
    """Strategy Router: Sends the rule to the correct evaluation logic."""
    strategy = str(rule.evaluation_strategy).lower()
    logger.debug(f"Routing rule '{rule.rule_id}' with strategy '{strategy}'")

    if strategy == "numeric_threshold":
        return _evaluate_numeric(rule, context)
    elif strategy == "additive_conditions":
        return _evaluate_additive(rule, context)
    elif strategy == "fraud_type_match":
        return _evaluate_fraud_type(rule, context)
    
    logger.warning(f"Could not resolve strategy for Rule {rule.rule_id} (dim: {rule.dimension_key})")
    return TriggeredRule(rule_id=rule.rule_id, description=rule.description, max_weight=rule.max_pts)


# =======================================================================
# 3. EXPOSED TOOL FUNCTIONS (LLM & Dispatcher Boundary)
# =======================================================================

def get_risk_rules(**kwargs) -> dict:
    """
    TOOL 1: Fetches ALL active BSI fraud detection rules from AppWorks.
    """
    _RULES_LIST_ENDPOINT = AppWorksPaths.FraudRules.risk_rules_all()
    logger.info(f"TOOL CALL: get_risk_rules -> Fetching from {_RULES_LIST_ENDPOINT}")
    rules_out = []
    
    try:
        res = fetch(_RULES_LIST_ENDPOINT)
        
        items = res.get("_embedded", {}).get("FraudRiskRules_FraudRiskRulesListInternal", [])
        if not items:
            items = res.get("_embedded", {}).get("AgentRulesTable_AgentRulesTableListInternal", [])
            
        logger.debug(f"get_risk_rules: AppWorks returned {len(items)} raw rows.")
        
        for idx, item in enumerate(items):
            props = item.get("Properties", {})
            identity = item.get("Identity", {})
            links = item.get("_links", {})
            
            rule_id = str(identity.get("BusinessId") or props.get("RULE_ID") or "")
            if not rule_id:
                logger.debug(f"Row {idx} skipped: Missing rule_id")
                continue
                
            active_raw = props.get("ACTIVE")
            if active_raw is None:
                active_raw = props.get("IsActive")
            
            is_active = True
            if active_raw is not None:
                is_active = str(active_raw).lower() not in ("false", "0", "no")
                
            if not is_active:
                logger.debug(f"Rule {rule_id} skipped: Marked inactive in AppWorks")
                continue

            dimension_key = str(props.get("DIMENSION_KEY") or props.get("DimensionKey") or "").strip()
            rule_name = str(props.get("RULE_NAME") or props.get("RuleName") or props.get("Name") or "").strip()
            rule_desc = str(props.get("RULE_DESC") or props.get("DESCRIPTION") or props.get("Description") or "").strip()
            
            description = rule_name if rule_name else rule_desc
            if not description:
                description = rule_id
            
            if not dimension_key:
                source_text = f"{rule_name} {rule_desc} {rule_id}".lower()
                for keyword, dk in _DESC_TO_DIM.items():
                    if keyword in source_text:
                        dimension_key = dk
                        logger.debug(f"Rule {rule_id}: Recovered dimension_key '{dk}' via fallback sniffing.")
                        break

            max_pts = _parse_pts_string(props.get("WEIGHT") or props.get("MaxPoints"))

            raw_targets = props.get("TARGETD_FRAUD_TYPE") or props.get("TARGET_FRAUD_TYPES") or props.get("TargetFraudTypes") or ""
            target_types = [t.strip().lower() for t in str(raw_targets).split(",") if t.strip()]

            eval_strat = str(props.get("EVALUATION_STRATEGY") or "").lower()
            if not eval_strat or eval_strat == "null" or eval_strat == "none":
                if dimension_key in ["subject_history", "financial_exposure", "similar_case_volume", "allegation_severity"]:
                    eval_strat = "numeric_threshold"
                elif dimension_key in ["case_characteristics"]:
                    eval_strat = "additive_conditions"
                elif target_types:
                    eval_strat = "fraud_type_match"
                else:
                    eval_strat = "numeric_threshold"
                logger.debug(f"Rule {rule_id}: Auto-detected strategy as '{eval_strat}'")

            raw_condition = str(props.get("CONDITION") or props.get("Condition") or "").strip()
            if props.get("CONDITION") is None and props.get("Condition") is None:
                raw_condition = ""
                
            bonus_cond, bonus_pts = _parse_bonus_from_condition(raw_condition)

            parsed_thresholds = []
            item_self_href = links.get("item", {}).get("href") or links.get("self", {}).get("href") or ""
            item_id = None
            child_href = None

            if item_self_href:
                try:
                    item_res = fetch(item_self_href)
                    item_ident = item_res.get("Identity", {})
                    item_links = item_res.get("_links", {})

                    item_id = str(item_ident.get("Id") or "").strip() or None

                    for lk, lv in item_links.items():
                        if "Rules" in lk and isinstance(lv, dict) and lv.get("href"):
                            child_href = lv["href"]
                            break
                except Exception as e:
                    logger.warning(f"Row {idx} ({rule_id}): item fetch failed: {e}")

            if item_id or child_href:
                parsed_thresholds = _fetch_child_rules_breakpoints(
                    str(item_id) if item_id else "",
                    dimension_key,
                    child_href=child_href,
                )
                if parsed_thresholds:
                    logger.debug(f"Rule {rule_id}: Successfully extracted {len(parsed_thresholds)} child thresholds.")
            
            if not parsed_thresholds:
                inline_thresholds = props.get("Thresholds") or props.get("AgentRulesTable_Thresholds")
                if isinstance(inline_thresholds, list):
                    parsed_thresholds = inline_thresholds
                    logger.debug(f"Rule {rule_id}: Using {len(parsed_thresholds)} inline JSON thresholds.")

            rules_out.append({
                "rule_id": rule_id,
                "dimension_key": dimension_key,
                "evaluation_strategy": eval_strat,
                "description": description,
                "thresholds": parsed_thresholds,
                "target_fraud_types": target_types,
                "bonus_condition": bonus_cond or raw_condition, 
                "bonus_pts": bonus_pts,
                "max_pts": max_pts,
                "weight": max_pts,
                "active": True
            })

    except Exception as e:
        logger.error(f"AgentRulesTable fetch failed: {e}")

    logger.info(f"get_risk_rules: Returning {len(rules_out)} active rules to LLM.")
    return {
        "result": {"active_rules": rules_out},
        "provenance": {
            "sources": [f"AppWorks {_RULES_LIST_ENDPOINT}", "AppWorks relationship child entities"],
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "computed_by": "AppWorks REST retrieval",
        }
    }


def calculate_risk_metrics(
    case_id: str,
    subject_id: str,
    fraud_types: list,
    active_rules: list | None = None,
    **kwargs
) -> dict:
    """
    TOOL 2: Deterministic BSI risk scoring.
    active_rules MUST be passed by the LLM from Turn 1 execution.
    ai_summary is an optional state object passed by the orchestrator.
    """
    logger.info(f"TOOL CALL: calculate_risk_metrics — Case: {case_id} Subject: {subject_id}")

    if not active_rules:
        logger.error("calculate_risk_metrics called without active_rules from the LLM.")
        raise ValueError("Missing active_rules. The agent must call get_risk_rules first.")

    # 1. Build Universal Case State
    context = _build_case_context(
        case_id=case_id, 
        subject_id=subject_id, 
        fraud_types=fraud_types
    )
    
    total_earned = 0.0
    total_max = 0.0
    triggered_indicators = []

    logger.info(f"Beginning evaluation of {len(active_rules)} provided active rules.")

    # 2. Evaluate all rules
    for raw_rule in active_rules:
        try:
            rule = RiskRuleDef(**raw_rule)
            logger.debug(f"Successfully parsed LLM payload for rule {rule.rule_id} into RiskRuleDef schema.")
        except Exception as e:
            logger.warning(f"Failed to parse rule payload into Pydantic schema: {e}")
            continue
            
        if not rule.active:
            continue

        # ==========================================
        # APPLICABILITY GATE
        # ==========================================
        if rule.target_fraud_types:
            case_types = context.get("fraud_types", [])
            has_match = False
            for ct in case_types:
                for target in rule.target_fraud_types:
                    if ct in target or target in ct:
                        has_match = True
                        break
                if has_match: break
            
            if not has_match:
                logger.debug(f"Rule {rule.rule_id} Applicability Gate: FAILED. Rule scoped for {rule.target_fraud_types}, case is {case_types}. Skipping.")
                continue
            else:
                logger.debug(f"Rule {rule.rule_id} Applicability Gate: PASSED. Match found.")
        else:
            logger.debug(f"Rule {rule.rule_id} Applicability Gate: PASSED automatically (Universal Rule).")
        # ==========================================

        # Strategy Execution
        outcome = _score_rule(rule, context)
        
        total_max += (outcome.max_weight or 0.0)
        total_earned += outcome.weight
        
        if outcome.weight > 0:
            logger.info(f"Rule {rule.rule_id} triggered! Earned {outcome.weight}/{outcome.max_weight} pts.")
            triggered_indicators.append(outcome.model_dump(by_alias=True))
        else:
            logger.debug(f"Rule {rule.rule_id} evaluated but scored 0. Not added to final triggered indicators.")

    # 3. Normalization & Tiering
    effective_max = total_max if total_max > 0 else 100.0
    risk_score = round(total_earned / effective_max, 4)

    tier = "LOW"
    if risk_score >= 0.75:
        tier = "CRITICAL"
    elif risk_score >= 0.50:
        tier = "HIGH"
    elif risk_score >= 0.25:
        tier = "MEDIUM"

    logger.info(f"Risk calculation complete: Total Points {total_earned}/{effective_max} = Score {risk_score} (Tier: {tier})")
   
    # 4. Strict Provenance Envelope
    sources = ["AppWorks BSI fraud detection rules table"]
    if ai_summary:
        sources.append("BSI ai_summary (Context Enrichment)")
    else:
        sources.extend([f"AppWorks case record {case_id}", f"AppWorks subject record {subject_id}"])

    return {
        "result": {
            "case_id": case_id,
            "subject_id": subject_id,
            "risk_score": risk_score,
            "risk_tier": tier,
            "fraud_types": context["fraud_types"],
            "risk_indicators": triggered_indicators,
            "total_points": round(total_earned, 1),
            "max_points": round(effective_max, 1),
            "prior_case_count": context.get("subject_history", 0)
        },
        "provenance": {
            "sources": sources,
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "computed_by": "BSI configured rules evaluation",
        }
    }