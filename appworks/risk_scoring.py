# appworks/risk_scoring.py
# ----------------------------------------------------------------
# Agent 4: Fraud Risk Assessment (Refactored & Instrumented)
# ----------------------------------------------------------------
# Architecture Notes:
# 1. Zero AppWorks fetches inside the math engine.
# 2. Strategy Pattern evaluation. Pure logic functions.
# 3. Dynamic Additive Parser: Safely evaluates AppWorks expressions.
# 4. Applicability Gate: Rules can be universal or scoped by fraud type.
# 5. LLM Bypass eliminated. active_rules MUST be provided in Turn 2.
# 6. Strict Provenance. BSI configured rules evaluation explicitly cited.
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
# 1. UTILITIES & CONTEXT BUILDER (Data Fetching Boundary)
# =======================================================================

def _safe_float(val: Any, default: float = 0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _build_case_context(case_id: str, subject_id: str, fraud_types: list[str]) -> dict:
    """
    Fetches all live domain data from AppWorks upfront.
    Outputs keys that perfectly match AppWorks dimension_keys to allow 
    for dynamic, blind evaluation downstream.
    """
    logger.info(f"Building universal case context for Case {case_id}, Subject {subject_id}")
    context = {
        "case_id": case_id,
        "subject_id": subject_id,
        "fraud_types": [str(ft).lower().strip() for ft in fraud_types]
    }

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
        else:
            logger.debug("No SubjectWorkfolderMapping href found for subject.")
    except Exception as e:
        logger.warning(f"Failed to fetch subject history context for {subject_id}: {e}")

    # Map directly to expected AppWorks dimension strings
    context["subject_history"] = prior_case_count
    context["primary_in_prior_cases"] = primary_in_prior_cases

    # -- Financial Exposure & Case Characteristics Domain --
    total_calculated = 0.0
    total_ordered = 0.0
    try:
        logger.debug(f"Fetching Workfolder & Financial properties for case: {case_id}")
        wf_res = fetch(AppWorksPaths.Workfolder.item(case_id))
        wf_props = wf_res.get("Properties", {})
        
        # Stash the full properties payload for the dynamic additive evaluator
        context["workfolder_properties"] = wf_props
        logger.debug(f"Cached {len(wf_props)} raw properties from Workfolder.")
        
        fin_href = wf_res.get("_links", {}).get("relationship:Workfolder_FinancialRelationship", {}).get("href")
        if fin_href:
            fin_items = fetch(fin_href).get("_embedded", {}).get("Workfolder_FinancialRelationship", [])
            total_calculated = sum(_safe_float(i.get("Properties", {}).get("Financial_Calculated")) for i in fin_items)
            total_ordered = sum(_safe_float(i.get("Properties", {}).get("Financial_Ordered")) for i in fin_items)
            logger.debug(f"Financials found: Calculated=${total_calculated}, Ordered=${total_ordered}")
        else:
            logger.debug("No FinancialRelationship href found for workfolder.")
    except Exception as e:
        logger.warning(f"Failed to fetch financial/workfolder context for {case_id}: {e}")
        
    # Map directly to expected AppWorks dimension strings
    context["financial_exposure"] = total_calculated
    context["total_ordered"] = total_ordered

    logger.debug(f"Final Case Context built: keys={list(context.keys())}")
    return context


# =======================================================================
# 2. STRATEGY EVALUATORS (Pure Math & Logic)
# =======================================================================

def _evaluate_numeric(rule: RiskRuleDef, context: dict) -> TriggeredRule:
    """Scores a single numeric value dynamically based on its dimension key."""
    logger.debug(f"Executing [NUMERIC] strategy for rule '{rule.rule_id}' (dimension: {rule.dimension_key})")
    
    # 100% DYNAMIC LOOKUP
    value = float(context.get(rule.dimension_key, 0.0))
    logger.debug(f"Rule {rule.rule_id}: Extracted context value for '{rule.dimension_key}' = {value}")
    
    weight = 0.0
    finding_msg = f"Value {value} did not meet any thresholds."

    if rule.thresholds:
        # Sort breakpoints highest to lowest to hit the top tier first
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

    return TriggeredRule(
        rule_id=rule.rule_id,
        description=rule.description, 
        weight=min(weight, rule.max_pts),
        max_weight=rule.max_pts,
        display=f"{min(weight, rule.max_pts)}/{rule.max_pts}",
        findings=finding_msg,
        triggered=(weight > 0)
    )


def _evaluate_additive(rule: RiskRuleDef, context: dict) -> TriggeredRule:
    """
    100% Dynamic Additive Evaluator.
    Evaluates raw AppWorks condition strings against live AppWorks properties.
    """
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

            # 1. Simple boolean flag lookup (e.g., "WorkfolderFastTrack")
            if cond_str in wf_props:
                val = wf_props.get(cond_str)
                logger.debug(f"Rule {rule.rule_id}: Found exact property match '{cond_str}' = {val}")
                if str(val).lower() in ("true", "1", "yes", "y") or val is True:
                    weight += bp_pts
                    met_conditions.append(cond_str)
                    logger.debug(f"Rule {rule.rule_id}: Condition met. +{bp_pts} pts.")
                continue

            # 2. Relational Expression Parser (e.g., "WorkfolderDateReceivedAge > 30")
            m = re.match(r'^([a-zA-Z0-9_]+)\s*(>=|<=|>|<|==|!=|=)\s*(.+)$', cond_str)
            if m:
                prop_name, operator, target_val_str = m.groups()
                actual_val = wf_props.get(prop_name)
                logger.debug(f"Rule {rule.rule_id}: Regex parsed '{prop_name}' {operator} '{target_val_str}'. Live value = {actual_val}")
                
                if actual_val is not None:
                    matched = False
                    try:
                        # Attempt numeric comparison
                        actual_num = float(actual_val)
                        target_num = float(target_val_str)
                        if operator == ">": matched = actual_num > target_num
                        elif operator == ">=": matched = actual_num >= target_num
                        elif operator == "<": matched = actual_num < target_num
                        elif operator == "<=": matched = actual_num <= target_num
                        elif operator in ("==", "="): matched = actual_num == target_num
                        elif operator == "!=": matched = actual_num != target_num
                    except ValueError:
                        # Fallback to string comparison
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

def get_risk_rules() -> dict:
    """
    TOOL 1: Fetches ALL active BSI fraud detection rules from AppWorks.
    Extracts explicit target fraud types and child threshold entities.
    """
    _RULES_LIST_ENDPOINT = AppWorksPaths.FraudRules.risk_rules_all()
    logger.info(f"TOOL CALL: get_risk_rules -> Fetching from {_RULES_LIST_ENDPOINT}")
    rules_out = []
    
    try:
        res = fetch(_RULES_LIST_ENDPOINT)
        items = res.get("_embedded", {}).get("AgentRulesTable_AgentRulesTableListInternal", [])
        logger.debug(f"get_risk_rules: AppWorks returned {len(items)} raw rows.")
        
        for idx, item in enumerate(items):
            props = item.get("Properties", {})
            identity = item.get("Identity", {})
            
            # 1. Identifiers
            rule_id = str(identity.get("BusinessId") or props.get("RULE_ID") or "")
            if not rule_id:
                logger.debug(f"Row {idx} skipped: Missing rule_id")
                continue
                
            is_active = str(props.get("IsActive", "True")).lower() not in ("false", "0", "no")
            if not is_active:
                logger.debug(f"Rule {rule_id} skipped: Marked inactive in AppWorks")
                continue

            dimension_key = str(props.get("DIMENSION_KEY") or props.get("DimensionKey") or "").strip()
            description = str(props.get("DESCRIPTION") or props.get("Description") or rule_id).strip()
            max_pts = _safe_float(props.get("WEIGHT") or props.get("MaxPoints"), 0.0)

            # 2. Native Target Fraud Types Extraction
            raw_targets = props.get("TARGETD_FRAUD_TYPE") or props.get("TARGET_FRAUD_TYPES") or props.get("TargetFraudTypes") or ""
            target_types = [t.strip().lower() for t in str(raw_targets).split(",") if t.strip()]

            # 3. Strategy Resolution (Prioritize known dimensions over target presence)
            eval_strat = str(props.get("EVALUATION_STRATEGY") or "").lower()
            if not eval_strat:
                if dimension_key in ["subject_history", "financial_exposure", "similar_case_volume", "allegation_severity"]:
                    eval_strat = "numeric_threshold"
                elif dimension_key in ["case_characteristics"]:
                    eval_strat = "additive_conditions"
                elif target_types:
                    eval_strat = "fraud_type_match"
                else:
                    eval_strat = "numeric_threshold"
                logger.debug(f"Rule {rule_id}: Auto-detected strategy as '{eval_strat}'")

            # 4. Fetch Child Entities (Thresholds)
            parsed_thresholds = []
            _links = item.get("_links", {})
            
            child_rel_key = next(
                (k for k in _links.keys() if "relationship" in k.lower() and ("rule" in k.lower() or "threshold" in k.lower() or "condition" in k.lower())), 
                None
            )

            if child_rel_key:
                child_href = _links[child_rel_key].get("href")
                if child_href:
                    try:
                        logger.debug(f"Rule {rule_id}: Fetching child thresholds from {child_href}")
                        child_res = fetch(child_href)
                        embedded_children = child_res.get("_embedded", {})
                        
                        child_items = next((v for v in embedded_children.values() if isinstance(v, list)), [])
                        for child in child_items:
                            c_props = child.get("Properties", {})
                            c_cond = c_props.get("CONDITION") or c_props.get("Condition")
                            c_min_str = c_props.get("MIN_VALUE") or c_props.get("MinValue")
                            c_pts = _safe_float(c_props.get("POINTS") or c_props.get("WEIGHT") or c_props.get("Weight"), 0.0)
                            
                            parsed_thresholds.append({
                                "condition": str(c_cond).strip() if c_cond else None,
                                "min_value": _safe_float(c_min_str) if c_min_str is not None else None,
                                "points": c_pts
                            })
                        logger.debug(f"Rule {rule_id}: Successfully extracted {len(parsed_thresholds)} child thresholds.")
                    except Exception as e:
                        logger.warning(f"Failed to fetch child thresholds for {rule_id}: {e}")
            
            if not parsed_thresholds:
                inline_thresholds = props.get("Thresholds") or props.get("AgentRulesTable_Thresholds")
                if isinstance(inline_thresholds, list):
                    parsed_thresholds = inline_thresholds
                    logger.debug(f"Rule {rule_id}: Using {len(parsed_thresholds)} inline JSON thresholds.")

            # 5. Build final payload
            rules_out.append({
                "rule_id": rule_id,
                "dimension_key": dimension_key,
                "evaluation_strategy": eval_strat,
                "description": description,
                "thresholds": parsed_thresholds,
                "target_fraud_types": target_types,
                "bonus_condition": str(props.get("CONDITION") or props.get("Condition") or "").strip(),
                "max_pts": max_pts,
                "weight": max_pts,
                "active": True
            })
            logger.debug(f"Successfully packaged rule {rule_id} for LLM delivery.")

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
    active_rules: list = None,
    **kwargs
) -> dict:
    """
    TOOL 2: Deterministic BSI risk scoring.
    active_rules MUST be passed by the LLM from Turn 1 execution.
    """
    logger.info(f"TOOL CALL: calculate_risk_metrics — Case: {case_id} Subject: {subject_id}")

    # ENFORCED ARCHITECTURE
    if not active_rules:
        logger.error("calculate_risk_metrics called without active_rules from the LLM.")
        raise ValueError("Missing active_rules. The agent must call get_risk_rules first.")

    # 1. Build Universal Case State
    context = _build_case_context(case_id, subject_id, fraud_types)
    
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
            "sources": [
                f"AppWorks case record {case_id}",
                f"AppWorks subject record {subject_id}",
                "AppWorks BSI fraud detection rules table",
            ],
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "computed_by": "BSI configured rules evaluation",
        }
    }