"""
Owns: assembling the Rule Audit inventory (Functional Specification D4,
GET /rule_audit/{case_id}) — a complete, per-rule listing of every
inferred fact for a case with full provenance, so an investigator can
review what the system found and why BEFORE deciding what to reject
through POST /reject_inference (rejection.py). D4's own "Why This
Endpoint Is Necessary" note is explicit: "The Rejection Handler only
works well if investigators can first see all inferred facts in one
place. Without this view, rejection decisions are made blind." This
module is that view — the UI's Reject buttons read their
subject_id_a/subject_id_b/rule_id/relationship_type parameters straight
off entries returned here (or off fraud_network.py's edges, for the
network-membership rules; both surfaces use the same field names on
purpose so the UI never has to translate between them).

WHY THIS IS A SEPARATE MODULE FROM rules_fired.py, NOT A REUSE OF IT:
Same reasoning report_generation.py's own docstring gives for the same
question. rules_fired.py assembles a fixed 14-entry, per-RULE
AGGREGATE (count + one summarised confidence) for the pipeline's own
run-scoped output contract (Functional Spec A.4) — it is explicitly
"assembled in exactly one place and never reconstructed by a caller,"
and this module does not touch it. D4 needs the opposite grain:
per-INSTANCE detail (which specific subject pair, which specific
timestamp) across the case's full subject scope, standalone — callable
any time an investigator opens the review panel, not only inside a
pipeline run. So this module runs its own read.

Does NOT own: rule execution (rule_engine.py), rule content
(rules/*.cypher), the rules_fired aggregate (rules_fired.py), or any
write.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

from reasoning_layer import rule_registry
from reasoning_layer.neo4j_client import get_session
from reasoning_layer.scope import resolve_scope

logger = logging.getLogger(__name__)

_PRIMARY_SUBJECT_QUERY = """
MATCH (s:Subject)-[r:APPEARS_IN_CASE]->(:Case {case_id: $case_id})
WHERE r.is_primary = true
RETURN s.subject_id AS primary_subject_id
LIMIT 1
"""

# One query per relationship-writing rule, scoped to every subject in
# this case's reasoning scope (primary + one hop — the same population
# the rules themselves were allowed to match against, so this audit
# shows exactly what could have fired, not an arbitrarily wider read).
# Every branch returns the same column set the D4 output contract asks
# for: subject pair, relationship_type, confidence, asserted_at,
# corroborated, status.
_REL_QUERIES: Dict[str, str] = {
    "Rule_01_Shared_Employer": """
        MATCH (a:Subject)-[r:SHARES_EMPLOYER_WITH]-(b:Subject)
        WHERE a.subject_id IN $scope_subject_ids AND a.subject_id < b.subject_id
          AND r.source_rule = "Rule_01_Shared_Employer"
        RETURN a.subject_id AS subject_id_a, b.subject_id AS subject_id_b,
               "SHARES_EMPLOYER_WITH" AS relationship_type, r.confidence AS confidence,
               toString(r.asserted_at) AS asserted_at, coalesce(r.corroborated, false) AS corroborated,
               r.status AS status
    """,
    "Rule_03_Shared_Address": """
        MATCH (a:Subject)-[r:SHARES_ADDRESS_WITH]-(b:Subject)
        WHERE a.subject_id IN $scope_subject_ids AND a.subject_id < b.subject_id
          AND r.source_rule = "Rule_03_Shared_Address"
        RETURN a.subject_id AS subject_id_a, b.subject_id AS subject_id_b,
               "SHARES_ADDRESS_WITH" AS relationship_type, r.confidence AS confidence,
               toString(r.asserted_at) AS asserted_at, coalesce(r.corroborated, false) AS corroborated,
               r.status AS status
    """,
    "Rule_05_Alias_Identity": """
        MATCH (a:Subject)-[r:SHARES_ALIAS_PATTERN_WITH]-(b:Subject)
        WHERE a.subject_id IN $scope_subject_ids AND a.subject_id < b.subject_id
          AND r.source_rule = "Rule_05_Alias_Identity"
        RETURN a.subject_id AS subject_id_a, b.subject_id AS subject_id_b,
               "SHARES_ALIAS_PATTERN_WITH" AS relationship_type, r.confidence AS confidence,
               toString(r.asserted_at) AS asserted_at, coalesce(r.corroborated, false) AS corroborated,
               r.status AS status
    """,
    "Rule_10_Merged_Case_Propagation": """
        MATCH (a:Subject)-[r:APPEARS_IN_CASE]->(c:Case)
        WHERE a.subject_id IN $scope_subject_ids
          AND r.source_rule = "Rule_10_Merged_Case_Propagation"
        RETURN a.subject_id AS subject_id_a, c.case_id AS subject_id_b,
               "APPEARS_IN_CASE" AS relationship_type, r.confidence AS confidence,
               toString(r.asserted_at) AS asserted_at, coalesce(r.corroborated, false) AS corroborated,
               r.status AS status
    """,
    "Rule_07_Prior_Guilty": """
        MATCH (a:Subject)-[r:HAS_PRIOR_GUILTY_CASE]->(c:Case)
        WHERE a.subject_id IN $scope_subject_ids
          AND r.source_rule = "Rule_07_Prior_Guilty"
        RETURN a.subject_id AS subject_id_a, c.case_id AS subject_id_b,
               "HAS_PRIOR_GUILTY_CASE" AS relationship_type, r.confidence AS confidence,
               toString(r.asserted_at) AS asserted_at, coalesce(r.corroborated, false) AS corroborated,
               r.status AS status
    """,
    "Rule_02_Employer_Fraud_Network": """
        MATCH (a:Subject)-[r:MEMBER_OF_FRAUD_NETWORK]->(n:FraudNetwork)
        WHERE a.subject_id IN $scope_subject_ids AND r.source_rule = "Rule_02_Employer_Fraud_Network"
        RETURN a.subject_id AS subject_id_a, (n.network_type + ":" + n.network_key) AS subject_id_b,
               "MEMBER_OF_FRAUD_NETWORK" AS relationship_type, r.confidence AS confidence,
               toString(r.asserted_at) AS asserted_at, coalesce(r.corroborated, false) AS corroborated,
               r.status AS status
    """,
    "Rule_04_Address_Fraud_Network": """
        MATCH (a:Subject)-[r:MEMBER_OF_FRAUD_NETWORK]->(n:FraudNetwork)
        WHERE a.subject_id IN $scope_subject_ids AND r.source_rule = "Rule_04_Address_Fraud_Network"
        RETURN a.subject_id AS subject_id_a, (n.network_type + ":" + n.network_key) AS subject_id_b,
               "MEMBER_OF_FRAUD_NETWORK" AS relationship_type, r.confidence AS confidence,
               toString(r.asserted_at) AS asserted_at, coalesce(r.corroborated, false) AS corroborated,
               r.status AS status
    """,
    "Rule_06_Identity_Fraud_Network": """
        MATCH (a:Subject)-[r:MEMBER_OF_FRAUD_NETWORK]->(n:FraudNetwork)
        WHERE a.subject_id IN $scope_subject_ids AND r.source_rule = "Rule_06_Identity_Fraud_Network"
        RETURN a.subject_id AS subject_id_a, (n.network_type + ":" + n.network_key) AS subject_id_b,
               "MEMBER_OF_FRAUD_NETWORK" AS relationship_type, r.confidence AS confidence,
               toString(r.asserted_at) AS asserted_at, coalesce(r.corroborated, false) AS corroborated,
               r.status AS status
    """,
    "Rule_09_PCA_CheckSplit": """
        MATCH (a:Subject)-[r:MEMBER_OF_FRAUD_NETWORK]->(n:FraudNetwork)
        WHERE a.subject_id IN $scope_subject_ids AND r.source_rule = "Rule_09_PCA_CheckSplit"
        RETURN a.subject_id AS subject_id_a, (n.network_type + ":" + n.network_key) AS subject_id_b,
               "MEMBER_OF_FRAUD_NETWORK" AS relationship_type, r.confidence AS confidence,
               toString(r.asserted_at) AS asserted_at, coalesce(r.corroborated, false) AS corroborated,
               r.status AS status
    """,
}

# Property-writing rules (Rules 8, 11, 12, 13) have no relationship
# instance to list — each asserts onto one node. Represented with the
# same column shape so the API contract stays uniform; subject_id_b
# carries the counterpart the property refers to (a case, an allegation,
# or None for the subject-only flag), exactly mirroring what
# rejection.py's from_key/to_key convention already uses for these
# rule_ids, so a UI Reject button can be wired identically either way.
_PROP_QUERIES: Dict[str, str] = {
    "Rule_11_Cross_Case_Hub": """
        MATCH (a:Subject)
        WHERE a.subject_id IN $scope_subject_ids AND a.cross_case_source_rule = "Rule_11_Cross_Case_Hub"
        RETURN a.subject_id AS subject_id_a, null AS subject_id_b,
               "CROSS_CASE_HUB" AS relationship_type, a.cross_case_confidence AS confidence,
               toString(a.cross_case_asserted_at) AS asserted_at, false AS corroborated,
               (CASE WHEN a.is_cross_case = true THEN "active" ELSE "rejected" END) AS status
    """,
    "Rule_08_Recidivist_Escalation": """
        MATCH (c:Case)
        WHERE c.case_id IN $scope_case_ids AND c.risk_escalation_source_rule = "Rule_08_Recidivist_Escalation"
        RETURN c.risk_escalation_subject_id AS subject_id_a, c.case_id AS subject_id_b,
               "CASE_RISK_ESCALATION" AS relationship_type, c.risk_escalation_confidence AS confidence,
               toString(c.risk_escalation_asserted_at) AS asserted_at, false AS corroborated,
               c.risk_escalation_status AS status
    """,
    "Rule_12_SLAM_Wage_Corroboration": """
        MATCH (c:Case)-[:HAS_ALLEGATION]->(al:Allegation)-[:ALLEGATION_LIKELY_AGAINST_SUBJECT]->(a:Subject)
        WHERE a.subject_id IN $scope_subject_ids AND al.wage_corroboration_rule = "Rule_12_SLAM_Wage_Corroboration"
        RETURN a.subject_id AS subject_id_a, al.allegation_id AS subject_id_b,
               "WAGE_CORROBORATION" AS relationship_type, al.wage_corroboration_confidence AS confidence,
               toString(al.wage_corroboration_asserted_at) AS asserted_at,
               al.wage_corroboration_verified AS corroborated, al.wage_corroboration_status AS status
    """,
    "Rule_13_FastTrack_Escalation": """
        MATCH (c:Case {case_id: $case_id})
        WHERE c.fasttrack_recommendation_rule = "Rule_13_FastTrack_Escalation"
        RETURN $subject_id AS subject_id_a, c.case_id AS subject_id_b,
               "FASTTRACK_RECOMMENDATION" AS relationship_type,
               c.fasttrack_recommendation_confidence AS confidence,
               toString(c.fasttrack_recommendation_asserted_at) AS asserted_at, false AS corroborated,
               c.fasttrack_recommendation_status AS status
    """,
}


def _envelope(result: Dict[str, Any]) -> dict:
    return {
        "result": result,
        "provenance": {
            "sources": ["Neo4j graph query"],
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "computed_by": "reasoning_layer.rule_audit.get_rule_audit",
        },
    }


def get_rule_audit(case_id: str) -> dict:
    """
    Assemble the complete rule-by-rule inference inventory for a case
    (D4). Standalone GET — resolves its own primary subject and scope
    from the graph rather than depending on a live pipeline run.

    Args:
        case_id: required, non-empty.

    Returns (inside the standard {result, provenance} envelope):
        {
          "case_id": ..., "primary_subject_id": ...,
          "rules": [
            {
              "rule_id": ..., "rule_description": ...,
              "fired": bool,
              "inferred_relationships": [
                {subject_id_a, subject_id_b, relationship_type,
                 confidence, asserted_at, corroborated,
                 status: "active" | "rejected"}
              ],
            }
          ],
        }

    Every rejectable rule_id from rejection.RULE_IDS_REJECTABLE is
    always present, fired or not — the same "fixed-shape contract, not
    a list of hits" discipline rules_fired.py documents, so a consumer
    iterating this can rely on every rule being present.

    A case whose Subject has not appeared in the graph yet (ETL/pipeline
    never ran) is not an error: primary_subject_id is None and every
    rule reports fired=false with an empty inferred_relationships list.

    Raises:
        ValueError: case_id missing or blank.
        GraphUnavailableError / Neo4jError: propagated unchanged.
    """
    if not case_id or not str(case_id).strip():
        raise ValueError("get_rule_audit requires a non-empty case_id")
    case_id = str(case_id).strip()

    rule_names = rule_registry.get_rule_names()

    with get_session() as session:
        primary_record = session.run(_PRIMARY_SUBJECT_QUERY, case_id=case_id).single()

    primary_subject_id = primary_record["primary_subject_id"] if primary_record else None

    if primary_subject_id:
        scope = resolve_scope(case_id=case_id, subject_id=primary_subject_id)
    else:
        logger.warning(
            "get_rule_audit: case_id=%s has no Subject flagged is_primary — "
            "has ETL run for this case? Returning an empty audit.", case_id,
        )
        scope = {"scope_subject_ids": [], "scope_case_ids": [case_id]}

    rules: List[Dict[str, Any]] = []
    with get_session() as session:
        for rule_id in rule_registry.ALL_RULE_IDS:
            if rule_id == rule_registry.MODIFIER_RULE_ID:
                # Rule 14 is a confidence modifier on existing edges, not
                # an independently rejectable/auditable fact — see
                # rejection.py's module docstring for the same exclusion.
                continue

            query = _REL_QUERIES.get(rule_id) or _PROP_QUERIES.get(rule_id)
            rows = session.run(
                query,
                scope_subject_ids=scope["scope_subject_ids"],
                scope_case_ids=scope.get("scope_case_ids", [case_id]),
                case_id=case_id,
                subject_id=primary_subject_id,
            ).data()

            inferred_relationships = [
                {
                    "subject_id_a": row["subject_id_a"],
                    "subject_id_b": row["subject_id_b"],
                    "relationship_type": row["relationship_type"],
                    "confidence": row["confidence"] or "Unresolved",
                    "asserted_at": row["asserted_at"],
                    "corroborated": bool(row["corroborated"]),
                    "status": row["status"] or "active",
                }
                for row in rows
                if row["subject_id_a"] is not None
            ]
            rules.append({
                "rule_id": rule_id,
                "rule_description": rule_names.get(rule_id, rule_id),
                "fired": len(inferred_relationships) > 0,
                "inferred_relationships": inferred_relationships,
            })

    result = {
        "case_id": case_id,
        "primary_subject_id": primary_subject_id,
        "rules": rules,
    }
    logger.info(
        "get_rule_audit: case_id=%s primary_subject_id=%s rules_fired=%d/%d",
        case_id, primary_subject_id,
        sum(1 for r in rules if r["fired"]), len(rules),
    )
    return _envelope(result)
