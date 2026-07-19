"""
Owns: assembling the `rules_fired` block — the shared output contract of
the Reasoning Pipeline (Functional Specification A.4).

This block is consumed by Context Enrichment, Investigation Plan,
Copilot, Report Generation and Rule Audit. A.4 is blunt about the stakes:
"If it is absent or incorrectly structured, those Phase 2 improvements
fail silently." So it is assembled in exactly one place, from Neo4j,
after the rules have run — never reconstructed by a caller, and never
cached in Postgres (Data Persistence C.2: Neo4j is the system of record
for inferred relationships; Postgres holds no inferred-relationship
state).

Contract, per entry (A.4):
    rule_id      — Rule_01_... through Rule_14_...
    fired        — did this rule match a pattern for this subject
    confidence   — High / Medium / Unresolved
    corroborated — was the inferred fact also confirmed by narrative
                   evidence (Rule 14; Wave 2 and structural rules only)

Everything beyond those four fields (evidence_count, instances, wave,
skipped_reason) is additive and safe for existing consumers to ignore —
but it is what makes /rule_audit and the investigator-facing "why did
this fire" panel possible without a second round of queries.

Does NOT own: rule execution (rule_engine.py) or rule content.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from reasoning_layer import rule_registry
from reasoning_layer.neo4j_client import get_session

logger = logging.getLogger(__name__)

_CONFIDENCE_ORDER = {"Unresolved": 0, "Medium": 1, "High": 2}

# Relationship-writing rules: read back the edges they wrote, filtered to
# this run's scope and to status "active" (a rejected fact is suppressed
# from the block, per Principle 14 — the rejection itself is surfaced
# separately by /rule_audit, never silently dropped).
_REL_RULES: Dict[str, str] = {
    "Rule_01_Shared_Employer": """
        MATCH (a:Subject)-[r:SHARES_EMPLOYER_WITH]-(b:Subject)
        WHERE a.subject_id IN $scope_subject_ids
          AND r.source_rule = "Rule_01_Shared_Employer" AND r.status = "active"
        RETURN a.subject_id AS subject_id, b.subject_id AS related_subject_id,
               r.confidence AS confidence,
               coalesce(r.corroborated, false) AS corroborated
        ORDER BY subject_id, related_subject_id
    """,
    "Rule_03_Shared_Address": """
        MATCH (a:Subject)-[r:SHARES_ADDRESS_WITH]-(b:Subject)
        WHERE a.subject_id IN $scope_subject_ids
          AND r.source_rule = "Rule_03_Shared_Address" AND r.status = "active"
        RETURN a.subject_id AS subject_id, b.subject_id AS related_subject_id,
               r.confidence AS confidence,
               coalesce(r.corroborated, false) AS corroborated
        ORDER BY subject_id, related_subject_id
    """,
    "Rule_05_Alias_Identity": """
        MATCH (a:Subject)-[r:SHARES_ALIAS_PATTERN_WITH]-(b:Subject)
        WHERE a.subject_id IN $scope_subject_ids
          AND r.source_rule = "Rule_05_Alias_Identity" AND r.status = "active"
        RETURN a.subject_id AS subject_id, b.subject_id AS related_subject_id,
               r.confidence AS confidence,
               coalesce(r.corroborated, false) AS corroborated
        ORDER BY subject_id, related_subject_id
    """,
    "Rule_10_Merged_Case_Propagation": """
        MATCH (a:Subject)-[r:APPEARS_IN_CASE]->(c:Case)
        WHERE a.subject_id IN $scope_subject_ids
          AND r.source_rule = "Rule_10_Merged_Case_Propagation" AND r.status = "active"
        RETURN a.subject_id AS subject_id, c.case_id AS related_case_id,
               r.confidence AS confidence,
               coalesce(r.corroborated, false) AS corroborated
        ORDER BY subject_id, related_case_id
    """,
    "Rule_02_Employer_Fraud_Network": """
        MATCH (a:Subject)-[r:MEMBER_OF_FRAUD_NETWORK]->(n:FraudNetwork)
        WHERE a.subject_id IN $scope_subject_ids
          AND r.source_rule = "Rule_02_Employer_Fraud_Network" AND r.status = "active"
        RETURN a.subject_id AS subject_id, n.network_key AS related_network_key,
               r.confidence AS confidence,
               coalesce(r.corroborated, false) AS corroborated
        ORDER BY subject_id, related_network_key
    """,
    "Rule_04_Address_Fraud_Network": """
        MATCH (a:Subject)-[r:MEMBER_OF_FRAUD_NETWORK]->(n:FraudNetwork)
        WHERE a.subject_id IN $scope_subject_ids
          AND r.source_rule = "Rule_04_Address_Fraud_Network" AND r.status = "active"
        RETURN a.subject_id AS subject_id, n.network_key AS related_network_key,
               r.confidence AS confidence,
               coalesce(r.corroborated, false) AS corroborated
        ORDER BY subject_id, related_network_key
    """,
    "Rule_06_Identity_Fraud_Network": """
        MATCH (a:Subject)-[r:MEMBER_OF_FRAUD_NETWORK]->(n:FraudNetwork)
        WHERE a.subject_id IN $scope_subject_ids
          AND r.source_rule = "Rule_06_Identity_Fraud_Network" AND r.status = "active"
        RETURN a.subject_id AS subject_id, n.network_key AS related_network_key,
               r.confidence AS confidence,
               coalesce(r.corroborated, false) AS corroborated
        ORDER BY subject_id, related_network_key
    """,
    "Rule_09_PCA_CheckSplit": """
        MATCH (a:Subject)-[r:MEMBER_OF_FRAUD_NETWORK]->(n:FraudNetwork)
        WHERE a.subject_id IN $scope_subject_ids
          AND r.source_rule = "Rule_09_PCA_CheckSplit" AND r.status = "active"
        RETURN a.subject_id AS subject_id, n.network_key AS related_network_key,
               r.confidence AS confidence,
               coalesce(r.corroborated, false) AS corroborated
        ORDER BY subject_id, related_network_key
    """,
    "Rule_07_Prior_Guilty": """
        MATCH (a:Subject)-[r:HAS_PRIOR_GUILTY_CASE]->(c:Case)
        WHERE a.subject_id IN $scope_subject_ids
          AND r.source_rule = "Rule_07_Prior_Guilty" AND r.status = "active"
        RETURN a.subject_id AS subject_id, c.case_id AS related_case_id,
               r.confidence AS confidence,
               coalesce(r.corroborated, false) AS corroborated
        ORDER BY subject_id, related_case_id
    """,
    "Rule_14_Confirmation_Elevation": """
        MATCH (a:Subject)-[r]-(other)
        WHERE a.subject_id IN $scope_subject_ids
          AND r.corroborated_by = "Rule_14_Confirmation_Elevation"
          AND r.status = "active"
        RETURN a.subject_id AS subject_id,
               coalesce(other.subject_id, other.case_id, other.network_key) AS related_subject_id,
               "High" AS confidence, true AS corroborated
        ORDER BY subject_id, related_subject_id
    """,
}

# Property-writing rules: these assert onto a node rather than creating an
# edge (Rule 8 escalates a Case's risk, Rule 11 flags a Subject as a hub,
# Rule 12 corroborates an Allegation, Rule 13 recommends FastTrack). Same
# contract out; different shape in.
_PROP_RULES: Dict[str, str] = {
    "Rule_11_Cross_Case_Hub": """
        MATCH (a:Subject)
        WHERE a.subject_id IN $scope_subject_ids
          AND a.cross_case_source_rule = "Rule_11_Cross_Case_Hub"
          AND a.is_cross_case = true
        RETURN a.subject_id AS subject_id,
               a.cross_case_confidence AS confidence,
               false AS corroborated
        ORDER BY subject_id
    """,
    "Rule_08_Recidivist_Escalation": """
        MATCH (c:Case)
        WHERE c.case_id IN $scope_case_ids
          AND c.risk_escalation_source_rule = "Rule_08_Recidivist_Escalation"
          AND c.risk_escalation_status = "active"
        RETURN c.case_id AS related_case_id,
               c.risk_escalation_confidence AS confidence,
               false AS corroborated
        ORDER BY related_case_id
    """,
    "Rule_12_SLAM_Wage_Corroboration": """
        MATCH (c:Case)-[:HAS_ALLEGATION]->(al:Allegation)-[att:ALLEGATION_LIKELY_AGAINST_SUBJECT]->(a:Subject)
        WHERE a.subject_id IN $scope_subject_ids
          AND al.wage_corroboration_rule = "Rule_12_SLAM_Wage_Corroboration"
          AND al.wage_corroboration_status = "active"
        RETURN a.subject_id AS subject_id, c.case_id AS related_case_id,
               al.allegation_type AS allegation_type,
               al.wage_corroboration_confidence AS confidence,
               coalesce(al.wage_corroboration_verified, false) AS corroborated
        ORDER BY subject_id, related_case_id
    """,
    "Rule_13_FastTrack_Escalation": """
        MATCH (c:Case {case_id: $case_id})
        WHERE c.fasttrack_recommendation_rule = "Rule_13_FastTrack_Escalation"
          AND c.fasttrack_recommendation_status = "active"
        RETURN c.case_id AS related_case_id,
               c.fasttrack_recommendation_confidence AS confidence,
               false AS corroborated
        ORDER BY related_case_id
    """,
}
# Rule 12's `corroborated` is deliberately wage_corroboration_verified, not
# a Rule 14 flag: for this rule, "corroborated" means the wage period was
# actually checked against the case's fraud date range and overlapped —
# rather than the rule firing on an existing wage record with no dates
# available to verify against. See the rule file.


# Instance keys, in the order they are emitted. Only the ones a given rule
# actually produces appear on its instances — a subject-to-subject rule has
# no related_case_id, and inventing a null one would suggest the rule looked
# for a case and found none.
_INSTANCE_KEYS = (
    "subject_id", "related_subject_id", "related_case_id",
    "related_network_key", "allegation_type",
)


def _instance(row: Dict[str, Any]) -> Dict[str, Any]:
    """One concrete match: WHICH subjects/records this rule fired on."""
    instance = {
        key: row[key] for key in _INSTANCE_KEYS
        if row.get(key) is not None
    }
    instance["confidence"] = row.get("confidence") or "Unresolved"
    instance["corroborated"] = bool(row.get("corroborated", False))
    return instance


def _summarise(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Roll instance rows up into the rule-level summary.

    The rule-level `confidence` is the HIGHEST across instances and
    `corroborated` is true if ANY instance was corroborated. Both are
    deliberately optimistic: the rule-level flags answer "is there anything
    here worth an investigator's attention", and per-instance detail — the
    Medium, uncorroborated match sitting behind a High one — is preserved
    in `instances` rather than averaged away.
    """
    instances = [_instance(row) for row in rows]
    count = len(instances)
    confidences = [i["confidence"] for i in instances if i["confidence"]]
    confidence = (
        max(confidences, key=lambda c: _CONFIDENCE_ORDER.get(c, 0))
        if confidences else "Unresolved"
    )
    return {
        "fired": count > 0,
        # A rule that did not fire has no confidence to report. "Unresolved"
        # is the correct value here (A.4's own enum) — not None, and not a
        # cheerful "High" inherited from a previous run.
        "confidence": confidence if count > 0 else "Unresolved",
        "corroborated": any(i["corroborated"] for i in instances),
        "evidence_count": count,
        "instances": instances,
    }


def build_rules_fired(scope: Dict[str, Any],
                      execution_records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Build the full 14-entry rules_fired block for one pipeline run.

    Always returns 14 entries, in rule-number order, whether or not each
    rule fired — the block is a fixed-shape contract, not a list of hits.
    A consumer iterating it can rely on every rule_id being present.

    `execution_records` (from rule_engine) contributes the skipped_reason,
    so a rule disabled in the registry reads as fired=false +
    skipped_reason="disabled_in_registry" rather than as an ordinary miss.
    """
    executed_by_id = {rec["rule_id"]: rec for rec in execution_records}
    params = {
        "scope_subject_ids": scope["scope_subject_ids"],
        "scope_case_ids": scope["scope_case_ids"],
        "case_id": scope["case_id"],
    }

    block: List[Dict[str, Any]] = []
    with get_session() as session:
        for rule_id in rule_registry.ALL_RULE_IDS:
            query = _REL_RULES.get(rule_id) or _PROP_RULES.get(rule_id)
            rows = session.run(query, **params).data()
            summary = _summarise(rows)
            execution = executed_by_id.get(rule_id, {})
            block.append({
                "rule_id": rule_id,
                "fired": summary["fired"],
                "confidence": summary["confidence"],
                "corroborated": summary["corroborated"],
                # --- additive, beyond A.4's four required fields ---
                "evidence_count": summary["evidence_count"],
                # Which concrete subjects/records this rule fired on. Without
                # it, "Rule 3 fired, evidence_count 2" tells an investigator
                # something happened but not to whom — and the co-subject
                # pipeline runs below make multi-instance results the norm.
                "instances": summary["instances"],
                "wave": (
                    1 if rule_id in rule_registry.WAVE_1_RULE_IDS
                    else 2 if rule_id in rule_registry.WAVE_2_RULE_IDS
                    else 0
                ),
                "writes_this_run": execution.get("writes", 0),
                "skipped_reason": execution.get("skipped_reason"),
            })

    fired_count = sum(1 for entry in block if entry["fired"])
    logger.info(
        "rules_fired: case_id=%s subject_id=%s %d/%d rules fired",
        scope["case_id"], scope["primary_subject_id"], fired_count, len(block),
    )
    return block