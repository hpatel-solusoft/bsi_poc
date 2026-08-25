"""
Owns: the Cypher query text reasoning_layer/rule_audit.py's get_rule_audit
uses to answer GET /rule_audit/{case_id} — PRIMARY_SUBJECT_QUERY (which
subject in scope is the case's primary), REL_QUERIES (one query per
relationship-writing rule), and PROP_QUERIES (one query per
property-writing rule, Rules 8/11/12/13).

Split out of rule_audit.py verbatim, same rationale as
reasoning_layer/rules_fired_queries.py: this file owns query text only,
not what an audit row IS or how it is shaped into the D4 response
contract — that logic stays in rule_audit.py, which imports
REL_QUERIES/PROP_QUERIES/PRIMARY_SUBJECT_QUERY from here.

Does NOT own: rule execution (reasoning_layer/rule_engine.py) or rule
content (reasoning_layer/rule_registry.py). This module only holds the
read-side query text used to report on what rule_engine.py already wrote.
"""

from typing import Dict

PRIMARY_SUBJECT_QUERY = """
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
REL_QUERIES: Dict[str, str] = {
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
               r.status AS status,
               // AI-30/AI-31: cascade attribution parity with the
               // case-flag family below (Rule_08/Rule_13) — see
               // reasoning_layer/cascade.py's _AUTO_INVALIDATE_MEMBERSHIP/
               // _MARK_REINSTATED_MEMBERSHIP for what writes these.
               // invalidated_at doubles as the relationship's
               // rejected_at, shared with a MANUAL rejection
               // (reasoning_layer/rejection.py) — auto_invalidated is
               // what tells the two apart, exactly as
               // reasoning_layer/cascade.py's own module docstring
               // ("AUTO-INVALIDATION IS ALWAYS DISTINGUISHABLE...")
               // already documents for this relationship.
               r.auto_invalidated AS auto_invalidated,
               r.invalidated_by_rule_id AS invalidated_by_rule_id,
               r.invalidated_reason AS invalidated_reason,
               r.invalidated_by_investigator_id AS invalidated_by_investigator,
               (CASE WHEN r.auto_invalidated = true THEN toString(r.rejected_at) ELSE null END) AS invalidated_at,
               r.reinstated_by_rule_id AS reinstated_by_rule_id,
               r.reinstated_reason AS reinstated_reason,
               r.reinstated_by_investigator_id AS reinstated_by_investigator,
               toString(r.reinstated_at) AS reinstated_at
    """,
    "Rule_04_Address_Fraud_Network": """
        MATCH (a:Subject)-[r:MEMBER_OF_FRAUD_NETWORK]->(n:FraudNetwork)
        WHERE a.subject_id IN $scope_subject_ids AND r.source_rule = "Rule_04_Address_Fraud_Network"
        RETURN a.subject_id AS subject_id_a, (n.network_type + ":" + n.network_key) AS subject_id_b,
               "MEMBER_OF_FRAUD_NETWORK" AS relationship_type, r.confidence AS confidence,
               toString(r.asserted_at) AS asserted_at, coalesce(r.corroborated, false) AS corroborated,
               r.status AS status,
               r.auto_invalidated AS auto_invalidated,
               r.invalidated_by_rule_id AS invalidated_by_rule_id,
               r.invalidated_reason AS invalidated_reason,
               r.invalidated_by_investigator_id AS invalidated_by_investigator,
               (CASE WHEN r.auto_invalidated = true THEN toString(r.rejected_at) ELSE null END) AS invalidated_at,
               r.reinstated_by_rule_id AS reinstated_by_rule_id,
               r.reinstated_reason AS reinstated_reason,
               r.reinstated_by_investigator_id AS reinstated_by_investigator,
               toString(r.reinstated_at) AS reinstated_at
    """,
    "Rule_06_Identity_Fraud_Network": """
        MATCH (a:Subject)-[r:MEMBER_OF_FRAUD_NETWORK]->(n:FraudNetwork)
        WHERE a.subject_id IN $scope_subject_ids AND r.source_rule = "Rule_06_Identity_Fraud_Network"
        RETURN a.subject_id AS subject_id_a, (n.network_type + ":" + n.network_key) AS subject_id_b,
               "MEMBER_OF_FRAUD_NETWORK" AS relationship_type, r.confidence AS confidence,
               toString(r.asserted_at) AS asserted_at, coalesce(r.corroborated, false) AS corroborated,
               r.status AS status,
               r.auto_invalidated AS auto_invalidated,
               r.invalidated_by_rule_id AS invalidated_by_rule_id,
               r.invalidated_reason AS invalidated_reason,
               r.invalidated_by_investigator_id AS invalidated_by_investigator,
               (CASE WHEN r.auto_invalidated = true THEN toString(r.rejected_at) ELSE null END) AS invalidated_at,
               r.reinstated_by_rule_id AS reinstated_by_rule_id,
               r.reinstated_reason AS reinstated_reason,
               r.reinstated_by_investigator_id AS reinstated_by_investigator,
               toString(r.reinstated_at) AS reinstated_at
    """,
    "Rule_09_PCA_CheckSplit": """
        MATCH (a:Subject)-[r:MEMBER_OF_FRAUD_NETWORK]->(n:FraudNetwork)
        WHERE a.subject_id IN $scope_subject_ids AND r.source_rule = "Rule_09_PCA_CheckSplit"
        RETURN a.subject_id AS subject_id_a, (n.network_type + ":" + n.network_key) AS subject_id_b,
               "MEMBER_OF_FRAUD_NETWORK" AS relationship_type, r.confidence AS confidence,
               toString(r.asserted_at) AS asserted_at, coalesce(r.corroborated, false) AS corroborated,
               r.status AS status,
               // Rule_09 is deliberately absent from
               // reasoning_layer.cascade._NETWORK_TYPE_BY_RULE_ID (see
               // that module's own comment: it never appears as a
               // DOWNSTREAM_DEPENDENTS value, only ever as an upstream
               // key for Rule_08) — so it can never itself be
               // auto-invalidated by this mechanism. These columns are
               // still selected, for output-shape symmetry with the
               // other three MEMBER_OF_FRAUD_NETWORK rules, and will
               // simply read null/false since cascade.py never writes
               // them for a Rule_09 edge.
               r.auto_invalidated AS auto_invalidated,
               r.invalidated_by_rule_id AS invalidated_by_rule_id,
               r.invalidated_reason AS invalidated_reason,
               r.invalidated_by_investigator_id AS invalidated_by_investigator,
               (CASE WHEN r.auto_invalidated = true THEN toString(r.rejected_at) ELSE null END) AS invalidated_at,
               r.reinstated_by_rule_id AS reinstated_by_rule_id,
               r.reinstated_reason AS reinstated_reason,
               r.reinstated_by_investigator_id AS reinstated_by_investigator,
               toString(r.reinstated_at) AS reinstated_at
    """,
}

# Property-writing rules (Rules 8, 11, 12, 13) have no relationship
# instance to list — each asserts onto one node. Represented with the
# same column shape so the API contract stays uniform; subject_id_b
# carries the counterpart the property refers to (a case, an allegation,
# or None for the subject-only flag), exactly mirroring what
# rejection.py's from_key/to_key convention already uses for these
# rule_ids, so a UI Reject button can be wired identically either way.
PROP_QUERIES: Dict[str, str] = {
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
               c.risk_escalation_status AS status,
               // AI-30/AI-31: rules_fired.py already surfaces this pair
               // in its own aggregate output; /rule_audit was the one
               // consumer still missing it — added here for parity, pure
               // field pass-through (no cascade.py change needed).
               c.risk_escalation_auto_invalidated AS auto_invalidated,
               c.risk_escalation_invalidated_by_rule_id AS invalidated_by_rule_id,
               c.risk_escalation_invalidated_reason AS invalidated_reason,
               c.risk_escalation_invalidated_by_investigator_id AS invalidated_by_investigator,
               toString(c.risk_escalation_invalidated_at) AS invalidated_at,
               c.risk_escalation_reinstated_by_rule_id AS reinstated_by_rule_id,
               c.risk_escalation_reinstated_reason AS reinstated_reason,
               c.risk_escalation_reinstated_by_investigator_id AS reinstated_by_investigator,
               toString(c.risk_escalation_reinstated_at) AS reinstated_at
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
               c.fasttrack_recommendation_status AS status,
               // AI-30/AI-31: same parity fix as Rule_08 above.
               c.fasttrack_recommendation_auto_invalidated AS auto_invalidated,
               c.fasttrack_recommendation_invalidated_by_rule_id AS invalidated_by_rule_id,
               c.fasttrack_recommendation_invalidated_reason AS invalidated_reason,
               c.fasttrack_recommendation_invalidated_by_investigator_id AS invalidated_by_investigator,
               toString(c.fasttrack_recommendation_invalidated_at) AS invalidated_at,
               c.fasttrack_recommendation_reinstated_by_rule_id AS reinstated_by_rule_id,
               c.fasttrack_recommendation_reinstated_reason AS reinstated_reason,
               c.fasttrack_recommendation_reinstated_by_investigator_id AS reinstated_by_investigator,
               toString(c.fasttrack_recommendation_reinstated_at) AS reinstated_at
    """,
}

