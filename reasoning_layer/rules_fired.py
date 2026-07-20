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

from reasoning_layer import rule_inference, rule_registry
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
        OPTIONAL MATCH (a)-[:EMPLOYED_BY]->(e:Employer)<-[:EMPLOYED_BY]-(b)
        WITH a, b, r, head(collect({name: e.name, fein: e.fein})) AS emp
        RETURN a.subject_id AS subject_id, a.first_name AS first_name, a.last_name AS last_name,
               b.subject_id AS related_subject_id, b.first_name AS related_first_name,
               b.last_name AS related_last_name,
               r.confidence AS confidence, coalesce(r.corroborated, false) AS corroborated,
               {employer_name: emp.name, fein: coalesce(emp.fein, r.fein)} AS detail
        ORDER BY subject_id, related_subject_id
""",
    "Rule_03_Shared_Address": """
        MATCH (a:Subject)-[r:SHARES_ADDRESS_WITH]-(b:Subject)
        WHERE a.subject_id IN $scope_subject_ids
          AND r.source_rule = "Rule_03_Shared_Address" AND r.status = "active"
        OPTIONAL MATCH (a)-[:HAS_ADDRESS]->(addr:Address)<-[:HAS_ADDRESS]-(b)
        WITH a, b, r, head(collect({street: addr.street, city: addr.city,
                                    state: addr.state, zip: addr.zip,
                                    address_key: addr.address_key})) AS ad
        RETURN a.subject_id AS subject_id, a.first_name AS first_name, a.last_name AS last_name,
               b.subject_id AS related_subject_id, b.first_name AS related_first_name,
               b.last_name AS related_last_name,
               r.confidence AS confidence, coalesce(r.corroborated, false) AS corroborated,
               {street: ad.street, city: ad.city, state: ad.state, zip: ad.zip,
                address_key: coalesce(ad.address_key, r.address_key)} AS detail
        ORDER BY subject_id, related_subject_id
""",
    "Rule_05_Alias_Identity": """
        MATCH (a:Subject)-[r:SHARES_ALIAS_PATTERN_WITH]-(b:Subject)
        WHERE a.subject_id IN $scope_subject_ids
          AND r.source_rule = "Rule_05_Alias_Identity" AND r.status = "active"
        RETURN a.subject_id AS subject_id, a.first_name AS first_name, a.last_name AS last_name,
               b.subject_id AS related_subject_id, b.first_name AS related_first_name,
               b.last_name AS related_last_name,
               r.confidence AS confidence, coalesce(r.corroborated, false) AS corroborated,
               {alias_pattern: coalesce(r.alias_pattern, r.match_basis)} AS detail
        ORDER BY subject_id, related_subject_id
""",
    "Rule_10_Merged_Case_Propagation": """
        MATCH (a:Subject)-[r:APPEARS_IN_CASE]->(c:Case)
        WHERE a.subject_id IN $scope_subject_ids
          AND r.source_rule = "Rule_10_Merged_Case_Propagation" AND r.status = "active"
        RETURN a.subject_id AS subject_id, a.first_name AS first_name, a.last_name AS last_name,
               c.case_id AS related_case_id,
               r.confidence AS confidence, coalesce(r.corroborated, false) AS corroborated,
               {complaint_no: c.complaint_number, status: c.status} AS detail
        ORDER BY subject_id, related_case_id
""",
    "Rule_02_Employer_Fraud_Network": """
        MATCH (a:Subject)-[r:MEMBER_OF_FRAUD_NETWORK]->(n:FraudNetwork)
        WHERE a.subject_id IN $scope_subject_ids
          AND r.source_rule = "Rule_02_Employer_Fraud_Network" AND r.status = "active"
        OPTIONAL MATCH (m:Subject)-[mm:MEMBER_OF_FRAUD_NETWORK]->(n)
        WHERE coalesce(mm.status, "active") = "active"
        OPTIONAL MATCH (m)-[:APPEARS_IN_CASE]->(mc:Case)-[:HAS_ALLEGATION]->(mal:Allegation)
        WITH a, r, n, m, head(collect({complaint_no: mc.complaint_number,
                                       allegation_type: mal.allegation_type})) AS mctx
        WITH a, r, n, collect(DISTINCT {
                 subject_id: m.subject_id, first_name: m.first_name, last_name: m.last_name,
                 complaint_no: mctx.complaint_no, allegation_type: mctx.allegation_type
             }) AS members_raw
        RETURN a.subject_id AS subject_id, a.first_name AS first_name, a.last_name AS last_name,
               n.network_key AS related_network_key,
               r.confidence AS confidence, coalesce(r.corroborated, false) AS corroborated,
               {network_type: n.network_type, network_key: n.network_key,
                formed_by_rule: n.formed_by_rule,
                members: [x IN members_raw WHERE x.subject_id IS NOT NULL]} AS detail
        ORDER BY subject_id, related_network_key
""",
    "Rule_04_Address_Fraud_Network": """
        MATCH (a:Subject)-[r:MEMBER_OF_FRAUD_NETWORK]->(n:FraudNetwork)
        WHERE a.subject_id IN $scope_subject_ids
          AND r.source_rule = "Rule_04_Address_Fraud_Network" AND r.status = "active"
        OPTIONAL MATCH (m:Subject)-[mm:MEMBER_OF_FRAUD_NETWORK]->(n)
        WHERE coalesce(mm.status, "active") = "active"
        OPTIONAL MATCH (m)-[:APPEARS_IN_CASE]->(mc:Case)-[:HAS_ALLEGATION]->(mal:Allegation)
        WITH a, r, n, m, head(collect({complaint_no: mc.complaint_number,
                                       allegation_type: mal.allegation_type})) AS mctx
        WITH a, r, n, collect(DISTINCT {
                 subject_id: m.subject_id, first_name: m.first_name, last_name: m.last_name,
                 complaint_no: mctx.complaint_no, allegation_type: mctx.allegation_type
             }) AS members_raw
        RETURN a.subject_id AS subject_id, a.first_name AS first_name, a.last_name AS last_name,
               n.network_key AS related_network_key,
               r.confidence AS confidence, coalesce(r.corroborated, false) AS corroborated,
               {network_type: n.network_type, network_key: n.network_key,
                formed_by_rule: n.formed_by_rule,
                members: [x IN members_raw WHERE x.subject_id IS NOT NULL]} AS detail
        ORDER BY subject_id, related_network_key
""",
    "Rule_06_Identity_Fraud_Network": """
        MATCH (a:Subject)-[r:MEMBER_OF_FRAUD_NETWORK]->(n:FraudNetwork)
        WHERE a.subject_id IN $scope_subject_ids
          AND r.source_rule = "Rule_06_Identity_Fraud_Network" AND r.status = "active"
        OPTIONAL MATCH (m:Subject)-[mm:MEMBER_OF_FRAUD_NETWORK]->(n)
        WHERE coalesce(mm.status, "active") = "active"
        OPTIONAL MATCH (m)-[:APPEARS_IN_CASE]->(mc:Case)-[:HAS_ALLEGATION]->(mal:Allegation)
        WITH a, r, n, m, head(collect({complaint_no: mc.complaint_number,
                                       allegation_type: mal.allegation_type})) AS mctx
        WITH a, r, n, collect(DISTINCT {
                 subject_id: m.subject_id, first_name: m.first_name, last_name: m.last_name,
                 complaint_no: mctx.complaint_no, allegation_type: mctx.allegation_type
             }) AS members_raw
        RETURN a.subject_id AS subject_id, a.first_name AS first_name, a.last_name AS last_name,
               n.network_key AS related_network_key,
               r.confidence AS confidence, coalesce(r.corroborated, false) AS corroborated,
               {network_type: n.network_type, network_key: n.network_key,
                formed_by_rule: n.formed_by_rule,
                members: [x IN members_raw WHERE x.subject_id IS NOT NULL]} AS detail
        ORDER BY subject_id, related_network_key
""",
    "Rule_09_PCA_CheckSplit": """
        MATCH (a:Subject)-[r:MEMBER_OF_FRAUD_NETWORK]->(n:FraudNetwork)
        WHERE a.subject_id IN $scope_subject_ids
          AND r.source_rule = "Rule_09_PCA_CheckSplit" AND r.status = "active"
        OPTIONAL MATCH (m:Subject)-[mm:MEMBER_OF_FRAUD_NETWORK]->(n)
        WHERE coalesce(mm.status, "active") = "active"
        OPTIONAL MATCH (m)-[:APPEARS_IN_CASE]->(mc:Case)-[:HAS_ALLEGATION]->(mal:Allegation)
        WITH a, r, n, m, head(collect({complaint_no: mc.complaint_number,
                                       allegation_type: mal.allegation_type})) AS mctx
        WITH a, r, n, collect(DISTINCT {
                 subject_id: m.subject_id, first_name: m.first_name, last_name: m.last_name,
                 complaint_no: mctx.complaint_no, allegation_type: mctx.allegation_type
             }) AS members_raw
        RETURN a.subject_id AS subject_id, a.first_name AS first_name, a.last_name AS last_name,
               n.network_key AS related_network_key,
               r.confidence AS confidence, coalesce(r.corroborated, false) AS corroborated,
               {network_type: n.network_type, network_key: n.network_key,
                formed_by_rule: n.formed_by_rule,
                members: [x IN members_raw WHERE x.subject_id IS NOT NULL]} AS detail
        ORDER BY subject_id, related_network_key
""",
    "Rule_07_Prior_Guilty": """
        MATCH (a:Subject)-[r:HAS_PRIOR_GUILTY_CASE]->(c:Case)
        WHERE a.subject_id IN $scope_subject_ids
          AND r.source_rule = "Rule_07_Prior_Guilty" AND r.status = "active"
        RETURN a.subject_id AS subject_id, a.first_name AS first_name, a.last_name AS last_name,
               c.case_id AS related_case_id,
               r.confidence AS confidence, coalesce(r.corroborated, false) AS corroborated,
               {complaint_no: c.complaint_number, outcome: r.outcome,
                date_closed: r.date_closed} AS detail
        ORDER BY subject_id, related_case_id
""",
    "Rule_14_Confirmation_Elevation": """
        MATCH (a:Subject)-[r]-(other)
        WHERE a.subject_id IN $scope_subject_ids
          AND r.corroborated_by = "Rule_14_Confirmation_Elevation"
          AND r.status = "active"
        RETURN a.subject_id AS subject_id, a.first_name AS first_name, a.last_name AS last_name,
               other.subject_id AS related_subject_id,
               other.first_name AS related_first_name, other.last_name AS related_last_name,
               "High" AS confidence, true AS corroborated,
               {confirmed_relationship: type(r),
                related_case_id: other.case_id,
                related_network_key: other.network_key} AS detail
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
        RETURN a.subject_id AS subject_id, a.first_name AS first_name, a.last_name AS last_name,
               a.cross_case_confidence AS confidence, false AS corroborated,
               {hub_case_ids: coalesce(a.hub_case_ids, [])} AS detail
        ORDER BY subject_id
""",
    "Rule_08_Recidivist_Escalation": """
        MATCH (c:Case)
        WHERE c.case_id IN $scope_case_ids
          AND c.risk_escalation_source_rule = "Rule_08_Recidivist_Escalation"
          AND c.risk_escalation_status = "active"
        RETURN c.case_id AS related_case_id,
               c.risk_escalation_confidence AS confidence, false AS corroborated,
               {complaint_no: c.complaint_number, fraud_amount: c.fraud_amount} AS detail
        ORDER BY related_case_id
""",
    "Rule_12_SLAM_Wage_Corroboration": """
        MATCH (c:Case)-[:HAS_ALLEGATION]->(al:Allegation)-[att:ALLEGATION_LIKELY_AGAINST_SUBJECT]->(a:Subject)
        WHERE a.subject_id IN $scope_subject_ids
          AND al.wage_corroboration_rule = "Rule_12_SLAM_Wage_Corroboration"
          AND al.wage_corroboration_status = "active"
        OPTIONAL MATCH (a)-[:HAS_WAGE_RECORD_WITH]->(e:Employer)
        WITH a, c, al, head(collect(e.name)) AS employer_name
        RETURN a.subject_id AS subject_id, a.first_name AS first_name, a.last_name AS last_name,
               c.case_id AS related_case_id, al.allegation_type AS allegation_type,
               al.wage_corroboration_confidence AS confidence,
               coalesce(al.wage_corroboration_verified, false) AS corroborated,
               {complaint_no: c.complaint_number, employer_name: employer_name,
                allegation_type: al.allegation_type,
                fraud_start_date: c.fraud_start_date, fraud_end_date: c.fraud_end_date} AS detail
        ORDER BY subject_id, related_case_id
""",
    "Rule_13_FastTrack_Escalation": """
        MATCH (c:Case {case_id: $case_id})
        WHERE c.fasttrack_recommendation_rule = "Rule_13_FastTrack_Escalation"
          AND c.fasttrack_recommendation_status = "active"
        RETURN c.case_id AS related_case_id,
               c.fasttrack_recommendation_confidence AS confidence, false AS corroborated,
               {complaint_no: c.complaint_number, fraud_amount: c.fraud_amount} AS detail
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


def _instance(rule_id: str, row: Dict[str, Any]) -> Dict[str, Any]:
    """
    One concrete match: WHICH subjects/records this rule fired on, with the
    entity and field detail behind it and a readable inference line.

    `detail` carries the fields the rule actually matched on — the address,
    the employer FEIN, the network members. Without it "Rule 3 fired" tells
    an investigator that something matched but not what, which is not
    enough to accept or reject the inference.
    """
    instance = {
        key: row[key] for key in _INSTANCE_KEYS
        if row.get(key) is not None
    }
    detail = {
        k: v for k, v in (row.get("detail") or {}).items()
        if v is not None and v != []
    }
    if detail:
        instance["detail"] = detail
    instance["confidence"] = row.get("confidence") or "Unresolved"
    instance["corroborated"] = bool(row.get("corroborated", False))
    # Names + the "why it fired" line are a presentation concern, owned by
    # rule_inference so rewording never touches this query module.
    for name_key in ("first_name", "last_name", "related_first_name", "related_last_name"):
        if row.get(name_key) is not None:
            instance[name_key] = row[name_key]
    return rule_inference.enrich_instance(rule_id, instance)


def _summarise(rule_id: str, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Roll instance rows up into the rule-level summary.

    The rule-level `confidence` is the HIGHEST across instances and
    `corroborated` is true if ANY instance was corroborated. Both are
    deliberately optimistic: the rule-level flags answer "is there anything
    here worth an investigator's attention", and per-instance detail — the
    Medium, uncorroborated match sitting behind a High one — is preserved
    in `instances` rather than averaged away.
    """
    instances = [_instance(rule_id, row) for row in rows]
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
            summary = _summarise(rule_id, rows)
            execution = executed_by_id.get(rule_id, {})
            block.append({
                "rule_id": rule_id,
                "fired": summary["fired"],
                "confidence": summary["confidence"],
                "corroborated": summary["corroborated"],
                # --- additive, beyond A.4's four required fields ---
                # What this rule looks for, from config/rule.yaml — so the
                # Inference panel can explain the rule itself, not only the match.
                "rule_description": rule_inference.rule_description(rule_id),
                "relationship_type": rule_inference.rule_label(rule_id),
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
