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
from appworks.appworks_auth import fetch
from typing import Any, Optional, Tuple, Dict, List
from appworks.appworks_utils import extract_workfolder_id_from_allegation
from appworks.appworks_paths import AppWorksPaths
from semantic_layer.entity_contracts import RiskRuleDef, TriggeredRule

# ── NEW: Architecture Standard Imports ───────────────────────
from appworks.appworks_utils import safe_fetch, extract_id_from_href, get_relationship_items
from utils.provenance import ProvenanceTracker

logger = logging.getLogger(__name__)

# =======================================================================
# 0. CONSTANTS & FALLBACKS
# =======================================================================

_DESC_TO_DIM: Dict[str, str] = {
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

def _parse_pts_string(s: Any) -> float:
    if s is None:
        return 0.0
    if isinstance(s, (int, float)):
        return float(s)
    m = re.search(r'(\d+(?:\.\d+)?)', str(s))
    return float(m.group(1)) if m else 0.0

def _parse_bonus_from_condition(cond_str: str) -> Tuple[str, float]:
    """Extracts bonus condition key and points from the parent CONDITION field."""
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

def _parse_condition_to_threshold(condition_str: str, pts: float, dimension_key: str) -> Dict:
    """Converts raw AppWorks child text strings into structured thresholds."""
    s = condition_str.strip()
    s_lower = s.lower()

    if dimension_key == "case_characteristics" or "characteristic" in dimension_key.lower():
        if "fast track" in s_lower: return {"condition": "fast_track", "points": pts}
        if "multiple subject" in s_lower or ("subject" in s_lower and "2" in s): return {"condition": "multiple_subjects", "points": pts}
        if "received age" in s_lower or "30 day" in s_lower or "> 30" in s_lower: return {"condition": "received_age_gt30", "points": pts}
        return {"condition": s_lower, "points": pts}

    if re.fullmatch(r'\$?0(\.0+)?\s*(cases?|pts?|similar cases?)?', s_lower.strip()):
        return {"condition": "0", "points": 0.0}

    m = re.search(r'[≥>]=?\s*\$?\s*(\d[\d,]*(?:\.\d+)?)', s)
    if m:
        num = float(m.group(1).replace(",", ""))
        if num == 0: num = 0.01
        return {"min_value": num, "points": pts}

    m = re.search(r'(\d+)\s*[–\-]\s*(\d+)', s)
    if m: return {"min_value": float(m.group(1)), "points": pts}

    m = re.match(r'(\d+)', s.strip())
    if m: return {"min_value": float(m.group(1)), "points": pts}

    logger.debug(f"_parse_condition_to_threshold: could not parse {condition_str!r} as numeric threshold.")
    return {}

def _fetch_child_rules_breakpoints(item_id: str, dimension_key: str, tracker: ProvenanceTracker, child_href: Optional[str] = None) -> List[Dict]:
    """Fetch childEntities/Rules and convert each child row into a threshold dict."""
    thresholds = []
    try:
        endpoint = child_href
        if not endpoint:
            if hasattr(AppWorksPaths, 'FraudRules') and hasattr(AppWorksPaths.FraudRules, 'risk_rules_by_id'):
                endpoint = AppWorksPaths.FraudRules.risk_rules_by_id(item_id)
            else:
                endpoint = f"/OSABSIACM/entities/FraudRiskRules/items/{item_id}/childEntities/Rules"

        # Using standard utility for list traversal
        child_items = get_relationship_items(endpoint, "Rules")
        
        for child in child_items:
            # Track the child rule if it has a direct link
            child_href = child.get("_links", {}).get("self", {}).get("href", "")
            if child_href:
                tracker.add_source("Rule", extract_id_from_href(child_href))
                
            cp = child.get("Properties", {})
            cond_str = str(cp.get("CONDITION") or cp.get("Condition") or cp.get("AgentRulesTable_Condition") or "")
            wt_str = cp.get("WEIGHT") or cp.get("Weight") or cp.get("POINTS") or cp.get("Points") or cp.get("AgentRulesTable_Weight")
            pts = _parse_pts_string(wt_str)
            
            if not cond_str: continue
                
            threshold = _parse_condition_to_threshold(cond_str, pts, dimension_key)
            if threshold: thresholds.append(threshold)

        numeric = sorted([t for t in thresholds if "min_value" in t], key=lambda t: t["min_value"], reverse=True)
        named = [t for t in thresholds if "condition" in t]
        thresholds = numeric + named

    except Exception as e:
        logger.warning(f"Child Rules fetch failed for item {item_id}: {e}")

    return thresholds

def _fetch_similar_case_volume(case_id: str, wf_res: Dict, tracker: ProvenanceTracker) -> int:
    """Counts distinct workfolders with matching allegation types."""
    total = 0
    seen = set()
    try:
        wf_links = wf_res.get("_links", {})
        alleg_href = wf_links.get("relationship:Workfolder_AllegationsRelationship", {}).get("href")
        
        if alleg_href:
            alleg_items = get_relationship_items(alleg_href, "Workfolder_AllegationsRelationship")
            
            for alleg_item in alleg_items:
                type_href = alleg_item.get("_links", {}).get("relationship:Allegations_AllegationsType", {}).get("href", "")
                if not type_href:
                    a_self = alleg_item.get("_links", {}).get("self", {}).get("href", "")
                    if a_self:
                        _, a_links = safe_fetch(a_self, "Allegation")
                        type_href = a_links.get("relationship:Allegations_AllegationsType", {}).get("href", "")
                
                type_id = extract_id_from_href(type_href)
                if not type_id: continue
                
                # We need to manually fetch the list endpoint since it differs from item payloads
                
                list_res = fetch(AppWorksPaths.Allegations.case_allegations_by_type_id(type_id))
                matched = list_res.get("_embedded", {}).get("Allegations_All", []) if list_res else []
                
                for alleg in matched:
                    wf_id = extract_workfolder_id_from_allegation(alleg)
                    if not wf_id or wf_id == str(case_id) or wf_id in seen: continue
                    seen.add(wf_id)
                    tracker.add_source("Workfolder", wf_id) # Track the discovered historical case
                    total += 1
    except Exception as e:
        logger.warning(f"Similar case volume count failed: {e}")
    return total

def _build_case_context(case_id: str, subject_id: str, fraud_types: List[str], tracker: ProvenanceTracker, ai_summary: Optional[Dict] = None) -> Dict:
    """Builds universal case context. Prefers ai_summary, falls back to dynamic AppWorks fetching."""
    logger.info(f"Building universal case context for Case {case_id}, Subject {subject_id}")
    context = {
        "case_id": case_id,
        "subject_id": subject_id,
        "fraud_types": [str(ft).lower().strip() for ft in fraud_types]
    }
    
    if ai_summary:
        logger.info("ai_summary detected. Bypassing AppWorks fetches for context building.")
        inv = ai_summary.get("investigation", {})
        comp_intel = inv.get("complaint_intelligence", {})
        enrichment = inv.get("context_enrichment", {})
        
        context["subject_history"] = int(enrichment.get("total_prior_case_count", 0))
        
        primary_in_prior = 0
        for profile in enrichment.get("profiles", []):
            if str(profile.get("subject_id")) == str(subject_id):
                for prior in profile.get("prior_cases", []):
                    if prior.get("is_primary_subject"): primary_in_prior += 1
        context["primary_in_prior_cases"] = primary_in_prior

        fins = comp_intel.get("financials", {})
        context["financial_exposure"] = _safe_float(fins.get("total_calculated", 0))
        context["total_ordered"] = _safe_float(fins.get("total_ordered", 0))

        sim_cases = ai_summary.get("similar_cases", {})
        context["similar_case_volume"] = int(sim_cases.get("total_candidates_scored", 0))

        has_open = False
        for alg in comp_intel.get("allegations", []):
            status = str(alg.get("status", "")).lower()
            if status in ("open", "active") and not alg.get("date_closed"): has_open = True
        context["has_open_allegation"] = has_open

        details = comp_intel.get("details", {})
        age = _safe_float(details.get("date_received_age", 0))
        sub_count = len(comp_intel.get("subject_ids", []))
        
        context["workfolder_properties"] = {
            "fast_track": False, 
            "received_age_gt30": age > 30,
            "multiple_subjects": sub_count >= 2
        }
        return context

    # FALLBACK: Execute if ai_summary is missing
    logger.info("No ai_summary provided. Falling back to dynamic AppWorks fetches...")

    prior_case_count, primary_in_prior_cases = 0, 0
    try:
        subj_props, subj_links = safe_fetch(AppWorksPaths.Subject.item(subject_id), "Subject")
        tracker.add_source("Subject", subject_id)
        
        mapping_href = subj_links.get("relationship:Subject_SubjectWorkfolderMapping", {}).get("href")
        mappings = get_relationship_items(mapping_href, "Subject_SubjectWorkfolderMapping")
        prior_case_count = len(mappings)
        primary_in_prior_cases = sum(1 for m in mappings if m.get("Properties", {}).get("SubjectWorkfolderMapping_IsPrimary"))
    except Exception as e:
        logger.warning(f"Failed fallback subject history context: {e}")

    context["subject_history"] = prior_case_count
    context["primary_in_prior_cases"] = primary_in_prior_cases

    wf_props, wf_links = safe_fetch(AppWorksPaths.Workfolder.item(case_id), "Workfolder")
    tracker.add_source("Workfolder", case_id)
    
    # Needs the full raw response dict for the volume helper
    wf_res_raw = fetch(AppWorksPaths.Workfolder.item(case_id)) if wf_props else {}

    total_calculated, total_ordered = 0.0, 0.0
    try:
        fin_href = wf_links.get("relationship:Workfolder_FinancialRelationship", {}).get("href")
        fin_items = get_relationship_items(fin_href, "Workfolder_FinancialRelationship")
        for i in fin_items:
            f_self = i.get("_links", {}).get("self", {}).get("href", "")
            if f_self: tracker.add_source("Financial", extract_id_from_href(f_self))
        total_calculated = sum(_safe_float(i.get("Properties", {}).get("Financial_Calculated")) for i in fin_items)
        total_ordered = sum(_safe_float(i.get("Properties", {}).get("Financial_Ordered")) for i in fin_items)
    except Exception as e:
        logger.warning(f"Failed fallback financial context: {e}")
        
    context["financial_exposure"] = total_calculated
    context["total_ordered"] = total_ordered
    context["similar_case_volume"] = _fetch_similar_case_volume(case_id, wf_res_raw, tracker)

    has_open_allegation = False
    try:
        alleg_href = wf_links.get("relationship:Workfolder_AllegationsRelationship", {}).get("href")
        alleg_items = get_relationship_items(alleg_href, "Workfolder_AllegationsRelationship")
        for alleg_item in alleg_items:
            a_self = alleg_item.get("_links", {}).get("self", {}).get("href", "")
            if a_self:
                a_props, _ = safe_fetch(a_self, "Allegation")
                tracker.add_source("Allegation", extract_id_from_href(a_self))
                date_closed = a_props.get("Allegations_DateClosed")
                status = (a_props.get("Allegations_AllegationStatus") or "").lower()
                if not date_closed and status in ("open", "active", ""):
                    has_open_allegation = True
                    break
    except Exception as e:
        logger.warning(f"Failed fallback allegation severity: {e}")
        
    context["has_open_allegation"] = has_open_allegation

    fast_track = bool(wf_props.get("WorkfolderFastTrack") or wf_props.get("FAST_TRACK") or wf_props.get("FastTrack"))
    wf_props["fast_track"] = fast_track

    age_raw = wf_props.get("WorkfolderDateReceivedAge")
    try: age = int(float(age_raw)) if age_raw is not None else 0
    except (ValueError, TypeError): age = 0
    wf_props["received_age_gt30"] = age > 30

    subj_href = wf_links.get("relationship:Workfolder_SubjectsRelationship", {}).get("href")
    subj_items = get_relationship_items(subj_href, "Workfolder_SubjectsRelationship")
    wf_props["multiple_subjects"] = len(subj_items) >= 2
    context["workfolder_properties"] = wf_props

    return context


# =======================================================================
# 2. STRATEGY EVALUATORS (Pure Math & Logic) - [No changes needed]
# =======================================================================

def _evaluate_numeric(rule: RiskRuleDef, context: Dict) -> TriggeredRule:
    """Scores a single numeric value dynamically and applies business bonuses."""
    logger.debug(f"Executing [NUMERIC] strategy for rule '{rule.rule_id}' (dimension: {rule.dimension_key})")
    value = float(context.get(rule.dimension_key, 0.0))
    weight, bonus_applied = 0.0, 0.0
    finding_msg = f"Value {value} did not meet any thresholds."

    if rule.thresholds:
        numeric_bps = sorted([t for t in rule.thresholds if t.min_value is not None], key=lambda x: x.min_value, reverse=True)
        for bp in numeric_bps:
            if value >= bp.min_value:
                weight = bp.points
                finding_msg = f"{rule.dimension_key} ({value}) >= {bp.min_value}"
                break

    if rule.bonus_condition == "primary_ge2" and context.get("primary_in_prior_cases", 0) >= 2:
        bonus_applied = rule.bonus_pts
        finding_msg += f" [+ {bonus_applied} pts primary bonus]"
    elif rule.bonus_condition == "ordered_gt_2x_calculated":
        calc, ordered = context.get("financial_exposure", 0.0), context.get("total_ordered", 0.0)
        if ordered > 0 and calc > 0 and ordered > (2 * calc):
            bonus_applied = rule.bonus_pts
            finding_msg += f" [+ {bonus_applied} pts unrealised bonus]"
    elif rule.bonus_condition == "open_allegation" and context.get("has_open_allegation"):
        bonus_applied = rule.bonus_pts
        finding_msg += f" [+ {bonus_applied} pts open-allegation bonus]"

    total_weight = weight + bonus_applied
    return TriggeredRule(rule_id=rule.rule_id, description=rule.description, weight=min(total_weight, rule.max_pts), max_weight=rule.max_pts, display=f"{min(total_weight, rule.max_pts)}/{rule.max_pts}", findings=finding_msg, triggered=(total_weight > 0))

def _evaluate_additive(rule: RiskRuleDef, context: Dict) -> TriggeredRule:
    """100% Dynamic Additive Evaluator."""
    logger.debug(f"Executing [ADDITIVE] strategy for rule '{rule.rule_id}'")
    weight = 0.0
    met_conditions = []
    wf_props = context.get("workfolder_properties", {})

    if rule.thresholds:
        for bp in rule.thresholds:
            if not bp.condition: continue
            cond_str, bp_pts = str(bp.condition).strip(), bp.points
            
            if cond_str in wf_props:
                val = wf_props.get(cond_str)
                if str(val).lower() in ("true", "1", "yes", "y") or val is True:
                    weight += bp_pts
                    met_conditions.append(cond_str)
                continue

            m = re.match(r'^([a-zA-Z0-9_]+)\s*(>=|<=|>|<|==|!=|=)\s*(.+)$', cond_str)
            if m:
                prop_name, operator, target_val_str = m.groups()
                actual_val = wf_props.get(prop_name)
                if actual_val is not None:
                    matched = False
                    try:
                        actual_num, target_num = float(actual_val), float(target_val_str)
                        if operator == ">": matched = actual_num > target_num
                        elif operator == ">=": matched = actual_num >= target_num
                        elif operator == "<": matched = actual_num < target_num
                        elif operator == "<=": matched = actual_num <= target_num
                        elif operator in ("==", "="): matched = actual_num == target_num
                        elif operator == "!=": matched = actual_num != target_num
                    except ValueError:
                        target_str, actual_str = target_val_str.strip("'\"").lower(), str(actual_val).strip().lower()
                        if operator in ("==", "="): matched = (actual_str == target_str)
                        elif operator == "!=": matched = (actual_str != target_str)

                    if matched:
                        weight += bp_pts
                        met_conditions.append(cond_str)

    return TriggeredRule(rule_id=rule.rule_id, description=rule.description, weight=min(weight, rule.max_pts), max_weight=rule.max_pts, display=f"{min(weight, rule.max_pts)}/{rule.max_pts}", findings="Matched: " + ", ".join(met_conditions) if met_conditions else "No conditions met", triggered=(weight > 0))

def _evaluate_fraud_type(rule: RiskRuleDef, context: Dict) -> TriggeredRule:
    """Awards max points if case fraud types intersect with rule targets or description."""
    logger.debug(f"Executing [FRAUD_TYPE_MATCH] strategy for rule '{rule.rule_id}'")
    case_types = context.get("fraud_types", [])
    
    target_labels = [t.lower().strip() for t in (rule.target_fraud_types or [])]
    if rule.description: target_labels.append(rule.description.lower().strip())
    if rule.bonus_condition: target_labels.append(rule.bonus_condition.lower().strip())
        
    matched_type = None
    for ft in case_types:
        for label in target_labels:
            if ft in label or label in ft:
                matched_type = ft
                break
        if matched_type: break

    weight = rule.max_pts if matched_type else 0.0
    return TriggeredRule(rule_id=rule.rule_id, description=rule.description, weight=weight, max_weight=rule.max_pts, display=f"{weight}/{rule.max_pts}", findings=f"Fraud type matched: {matched_type}" if matched_type else "No matching fraud type", triggered=(weight > 0))

def _score_rule(rule: RiskRuleDef, context: Dict) -> TriggeredRule:
    """Strategy Router: Sends the rule to the correct evaluation logic."""
    strategy = str(rule.evaluation_strategy).lower()
    if strategy == "numeric_threshold": return _evaluate_numeric(rule, context)
    elif strategy == "additive_conditions": return _evaluate_additive(rule, context)
    elif strategy == "fraud_type_match": return _evaluate_fraud_type(rule, context)
    return TriggeredRule(rule_id=rule.rule_id, description=rule.description, max_weight=rule.max_pts)


# =======================================================================
# 3. EXPOSED TOOL FUNCTIONS (LLM & Dispatcher Boundary)
# =======================================================================

def get_risk_rules(**kwargs) -> Dict:
    """TOOL 1: Fetches ALL active BSI fraud detection rules from AppWorks."""
    
    logger.info(f"TOOL CALL: get_risk_rules -> Fetching from AppWorks...")
    

    tracker = ProvenanceTracker("Catalog", "FraudRiskRules")
    rules_out = []
    
    try:
        rules_out = _fetch_risk_rules(tracker=tracker)

    except Exception as e:
        logger.error(f"fetch_risk_rules failed: {e}")

    logger.info(f"TOOL CALL: get_risk_rules -> Returning {len(rules_out)} active rules to LLM.")
    return {
        "result": {"active_rules": rules_out},
        "provenance": tracker.get_provenance_block()
    }

def _fetch_risk_rules(tracker: Optional[ProvenanceTracker] = None) -> list[Dict]:
    """TOOL 1: Fetches ALL active BSI fraud detection rules from AppWorks."""
    _RULES_LIST_ENDPOINT = AppWorksPaths.FraudRules.risk_rules_all()
    logger.info(f"_fetch_risk_rules -> Fetching from {_RULES_LIST_ENDPOINT}")
    
    if tracker is None:
        tracker = ProvenanceTracker("Catalog", "FraudRiskRules")

    rules_out = []
    
    try:
        res = fetch(_RULES_LIST_ENDPOINT)
        
        items = res.get("_embedded", {}).get("FraudRiskRules_FraudRiskRulesListInternal", [])
        if not items:
            items = res.get("_embedded", {}).get("AgentRulesTable_AgentRulesTableListInternal", [])
            
        for idx, item in enumerate(items):
            props = item.get("Properties", {})
            identity = item.get("Identity", {})
            links = item.get("_links", {})
            
            rule_id = str(identity.get("BusinessId") or props.get("RULE_ID") or "")
            if not rule_id: continue
                
            active_raw = props.get("ACTIVE") if props.get("ACTIVE") is not None else props.get("IsActive")
            is_active = True
            if active_raw is not None:
                is_active = str(active_raw).lower() not in ("false", "0", "no")
                
            if not is_active: continue

            # Track successfully active rule
            #tracker.add_source("FraudRiskRule", rule_id)

            dimension_key = str(props.get("DIMENSION_KEY") or props.get("DimensionKey") or "").strip()
            rule_name = str(props.get("RULE_NAME") or props.get("RuleName") or props.get("Name") or "").strip()
            rule_desc = str(props.get("RULE_DESC") or props.get("DESCRIPTION") or props.get("Description") or "").strip()
            
            description = rule_name if rule_name else rule_desc
            if not description: description = rule_id
            
            if not dimension_key:
                source_text = f"{rule_name} {rule_desc} {rule_id}".lower()
                for keyword, dk in _DESC_TO_DIM.items():
                    if keyword in source_text:
                        dimension_key = dk
                        break

            max_pts = _parse_pts_string(props.get("WEIGHT") or props.get("MaxPoints"))

            raw_targets = props.get("TARGETD_FRAUD_TYPE") or props.get("TARGET_FRAUD_TYPES") or props.get("TargetFraudTypes") or ""
            target_types = [t.strip().lower() for t in str(raw_targets).split(",") if t.strip()]

            eval_strat = str(props.get("EVALUATION_STRATEGY") or "").lower()
            if not eval_strat or eval_strat == "null" or eval_strat == "none":
                if dimension_key in ["subject_history", "financial_exposure", "similar_case_volume", "allegation_severity"]: eval_strat = "numeric_threshold"
                elif dimension_key in ["case_characteristics"]: eval_strat = "additive_conditions"
                elif target_types: eval_strat = "fraud_type_match"
                else: eval_strat = "numeric_threshold"

            raw_condition = str(props.get("CONDITION") or props.get("Condition") or "").strip()
            if props.get("CONDITION") is None and props.get("Condition") is None: raw_condition = ""
                
            bonus_cond, bonus_pts = _parse_bonus_from_condition(raw_condition)

            parsed_thresholds = []
            item_self_href = links.get("item", {}).get("href") or links.get("self", {}).get("href") or ""
            item_id, child_href = None, None

            if item_self_href:
                try:
                    item_props, item_links = safe_fetch(item_self_href, "FraudRiskRule")
                    item_id = extract_id_from_href(item_self_href)

                    for lk, lv in item_links.items():
                        if "Rules" in lk and isinstance(lv, dict) and lv.get("href"):
                            child_href = lv["href"]
                            break
                except Exception as e:
                    logger.warning(f"Row {idx} ({rule_id}): item fetch failed: {e}")

            if item_id or child_href:
                parsed_thresholds = _fetch_child_rules_breakpoints(str(item_id) if item_id else "", dimension_key, tracker, child_href=child_href)
            
            if not parsed_thresholds:
                inline_thresholds = props.get("Thresholds") or props.get("AgentRulesTable_Thresholds")
                if isinstance(inline_thresholds, list):
                    parsed_thresholds = inline_thresholds

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
        logger.error(f"FraudRiskRules fetch failed: {e}")

    logger.info(f"_fetch_risk_rules: Returning {len(rules_out)} active rules to LLM.")
    tracker.add_source("FraudRiskRulesCatalog", f"{len(rules_out)} active rules loaded")
    return rules_out

def calculate_risk_metrics(case_id: str, subject_id: str, fraud_types: List,  **kwargs) -> Dict:
    """TOOL 1: Deterministic BSI risk scoring."""
    logger.info(f"TOOL CALL: calculate_risk_metrics — Case: {case_id} Subject: {subject_id}")
    ai_summary = kwargs.get("ai_summary")
    
    tracker = ProvenanceTracker("RiskCalculation", case_id)
    logger.info("Fetching ground-truth active rules directly from AppWorks/Cache...")
    rule_payload = _fetch_risk_rules(tracker = tracker)

    logger.info(f"Loaded {len(rule_payload)} active rules. Starting risk calculation...")
    
    # Extract the full list of rules from the payload
    active_rules =rule_payload

    if not active_rules:
        logger.error("No active rules found. Risk calculation cannot proceed.")

    

    # 1. Build Universal Case State
    context = _build_case_context(case_id, subject_id, fraud_types, tracker, ai_summary=ai_summary)
    
    total_earned, total_max = 0.0, 0.0
    triggered_indicators = []

    # 2. Evaluate all rules
    for raw_rule in active_rules:
        try: rule = RiskRuleDef(**raw_rule)
        except Exception: continue
            
        if not rule.active: continue

        # APPLICABILITY GATE
        if rule.target_fraud_types:
            case_types = context.get("fraud_types", [])
            has_match = any(ct in target or target in ct for ct in case_types for target in rule.target_fraud_types)
            if not has_match: continue

        # Strategy Execution
        outcome = _score_rule(rule, context)
        
        total_max += (outcome.max_weight or 0.0)
        total_earned += outcome.weight
        
        if outcome.weight > 0:
            triggered_indicators.append(outcome.model_dump(by_alias=True))
            tracker.add_source("FraudRiskRule", rule.rule_id)  

    # 3. Normalization & Tiering
    effective_max = total_max if total_max > 0 else 100.0
    risk_score = round(total_earned / effective_max, 4)

    tier = "LOW"
    if risk_score >= 0.75: tier = "CRITICAL"
    elif risk_score >= 0.50: tier = "HIGH"
    elif risk_score >= 0.25: tier = "MEDIUM"

    # 4. Strict Provenance Envelope
    if ai_summary:
        # If ai_summary is present, it means we bypassed network calls. 
        # So we just log that we used the internal system memory.
        tracker.add_source("SystemMemory", "ai_summary")

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
        },
        "provenance": tracker.get_provenance_block(computed_by="BSI deterministic rules engine")
    }