"""
Owns: the Cypher query text reasoning_layer/report_generation.py's
assemble_related_network uses to answer the D5 Related Network section
of a generated report — one UNION ALL query covering every
relationship-writing rule shape plus the two case-level escalation
flags (Rules 8/13), scoped to one primary subject.

Split out of report_generation.py verbatim, same rationale as
reasoning_layer/rules_fired_queries.py and rule_audit_queries.py: this
file owns query text only, not what a Related Network entry IS or how
it is shaped/summarized into the D5 report contract — that logic stays
in report_generation.py, which imports RELATED_NETWORK_QUERY from here.

Does NOT own: rule execution (reasoning_layer/rule_engine.py) or rule
content (reasoning_layer/rule_registry.py). This module only holds the
read-side query text used to report on what rule_engine.py already wrote.
"""

# One UNION ALL block per relationship-writing rule shape (mirrors the
# rule set reasoning_layer/rules_fired.py._REL_RULES enumerates, minus
# Rule_14 — a corroboration modifier on an existing edge, not a distinct
# network connection), PLUS one block each for Rule 8 and Rule 13 (see
# the two case-flag branches below their own comment) — the two
# case-level ESCALATION flags an investigator can also reject. Each
# branch returns an identical column set so the UNION is valid, and
# each is filtered to a fact that actually touches $subject_id (or, for
# Rule 13, $case_id — see that branch's own comment) — this is the
# Primary-Subject-scoped read Section 8.7 asks for, deliberately
# narrower than rules_fired's whole-scope aggregate.
#
# Every branch's final RETURN column, `rejection_raw`, exists ONLY so
# Python (resolve_reviewer_notation) can build the "Reviewed and Excluded
# Connections" notation (investigator, when, why) straight off THIS
# relationship's own audit properties — the same properties
# reasoning_layer/rejection.py (manual reject/revert) and
# reasoning_layer/cascade.py (auto-invalidate/auto-reinstate) already
# write there, and the SAME source rules_fired.py's own _REL_RULES
# queries already read for the identical purpose (see e.g. Rule_01's
# `{rejected_by: r.rejected_by, ...} AS rejection`). A Neo4j UNION
# requires identical COLUMN NAMES across branches, not identical map
# SHAPES — MEMBER_OF_FRAUD_NETWORK is the one branch whose
# `rejection_raw` map carries extra invalidate/reinstate keys (see that
# branch's own comment), which is fine.
#
# Previously this notation came from a SEPARATE lookup against
# dedicated :Rejection nodes (a `_REJECTIONS_QUERY` joined in Python via
# a from_key/to_key/relationship_type match — both now removed, along
# with the from_key/to_key columns that existed only to feed that
# match). That mechanism silently under-reported: cascade.py's
# auto-invalidation NEVER creates a :Rejection node (see that module's
# own "AUTO-INVALIDATION IS ALWAYS DISTINGUISHABLE FROM A MANUAL
# REJECTION" docstring section) — it writes invalidated_by_rule_id /
# invalidated_reason / invalidated_by_investigator_id / invalidated_at
# directly onto the relationship (or, for Rule 8/13, the :Case) instead.
# A connection excluded by cascade therefore had no :Rejection node to
# find, and the old lookup's failure-to-match fell through to the "not
# recorded" placeholder for investigator/date — even though cascade.py
# deliberately carries the real upstream investigator_id and the real
# original reason text through every hop (see cascade.py's _walk, "Same
# original reason carries through every hop"). Reading the
# relationship's own properties directly, exactly like rules_fired.py
# already does, fixes this without a second data source that can go
# stale relative to the first.
RELATED_NETWORK_QUERY = """
MATCH (s:Subject {subject_id: $subject_id})-[r:SHARES_EMPLOYER_WITH]-(o:Subject)
WHERE r.status IN ["active", "rejected"]
RETURN "SHARES_EMPLOYER_WITH" AS relationship_type,
       o.subject_id AS counterpart_id, "Subject" AS counterpart_type,
       // Subject nodes carry first_name/last_name (individuals) or
       // company_name (organizations) — never a combined full_name/name
       // property (see etl/graph_sync.py's _Q_SUBJECTS). The old
       // coalesce(o.full_name, o.name, o.subject_id) referenced
       // properties that never existed on any Subject node, so it
       // ALWAYS fell through to the raw subject_id — confirmed in
       // production PDFs showing bare numbers for every counterpart.
       CASE
           WHEN o.first_name IS NOT NULL OR o.last_name IS NOT NULL
               THEN trim(coalesce(o.first_name, "") + " " + coalesce(o.last_name, ""))
           WHEN o.company_name IS NOT NULL THEN o.company_name
           ELSE o.subject_id
       END AS counterpart_label,
       r.source_rule AS source_rule, r.confidence AS confidence,
       coalesce(r.corroborated, false) AS corroborated, r.status AS status,
       toString(r.asserted_at) AS asserted_at,
       {rejected_by: r.rejected_by, rejected_at: r.rejected_at, reason: r.rejection_reason,
        reverted_by: r.reverted_by, reverted_at: r.reverted_at, revert_reason: r.revert_reason} AS rejection_raw

UNION ALL
MATCH (s:Subject {subject_id: $subject_id})-[r:SHARES_ADDRESS_WITH]-(o:Subject)
WHERE r.status IN ["active", "rejected"]
RETURN "SHARES_ADDRESS_WITH" AS relationship_type,
       o.subject_id AS counterpart_id, "Subject" AS counterpart_type,
       // Subject nodes carry first_name/last_name (individuals) or
       // company_name (organizations) — never a combined full_name/name
       // property (see etl/graph_sync.py's _Q_SUBJECTS). The old
       // coalesce(o.full_name, o.name, o.subject_id) referenced
       // properties that never existed on any Subject node, so it
       // ALWAYS fell through to the raw subject_id — confirmed in
       // production PDFs showing bare numbers for every counterpart.
       CASE
           WHEN o.first_name IS NOT NULL OR o.last_name IS NOT NULL
               THEN trim(coalesce(o.first_name, "") + " " + coalesce(o.last_name, ""))
           WHEN o.company_name IS NOT NULL THEN o.company_name
           ELSE o.subject_id
       END AS counterpart_label,
       r.source_rule AS source_rule, r.confidence AS confidence,
       coalesce(r.corroborated, false) AS corroborated, r.status AS status,
       toString(r.asserted_at) AS asserted_at,
       {rejected_by: r.rejected_by, rejected_at: r.rejected_at, reason: r.rejection_reason,
        reverted_by: r.reverted_by, reverted_at: r.reverted_at, revert_reason: r.revert_reason} AS rejection_raw

UNION ALL
MATCH (s:Subject {subject_id: $subject_id})-[r:SHARES_ALIAS_PATTERN_WITH]-(o:Subject)
WHERE r.status IN ["active", "rejected"]
RETURN "SHARES_ALIAS_PATTERN_WITH" AS relationship_type,
       o.subject_id AS counterpart_id, "Subject" AS counterpart_type,
       // Subject nodes carry first_name/last_name (individuals) or
       // company_name (organizations) — never a combined full_name/name
       // property (see etl/graph_sync.py's _Q_SUBJECTS). The old
       // coalesce(o.full_name, o.name, o.subject_id) referenced
       // properties that never existed on any Subject node, so it
       // ALWAYS fell through to the raw subject_id — confirmed in
       // production PDFs showing bare numbers for every counterpart.
       CASE
           WHEN o.first_name IS NOT NULL OR o.last_name IS NOT NULL
               THEN trim(coalesce(o.first_name, "") + " " + coalesce(o.last_name, ""))
           WHEN o.company_name IS NOT NULL THEN o.company_name
           ELSE o.subject_id
       END AS counterpart_label,
       r.source_rule AS source_rule, r.confidence AS confidence,
       coalesce(r.corroborated, false) AS corroborated, r.status AS status,
       toString(r.asserted_at) AS asserted_at,
       {rejected_by: r.rejected_by, rejected_at: r.rejected_at, reason: r.rejection_reason,
        reverted_by: r.reverted_by, reverted_at: r.reverted_at, revert_reason: r.revert_reason} AS rejection_raw

UNION ALL
MATCH (s:Subject {subject_id: $subject_id})-[r:MEMBER_OF_FRAUD_NETWORK]->(n:FraudNetwork)
WHERE r.status IN ["active", "rejected"]
RETURN "MEMBER_OF_FRAUD_NETWORK" AS relationship_type,
       n.network_key AS counterpart_id, "FraudNetwork" AS counterpart_type,
       (n.network_type + " fraud network") AS counterpart_label,
       r.source_rule AS source_rule, r.confidence AS confidence,
       coalesce(r.corroborated, false) AS corroborated, r.status AS status,
       toString(r.asserted_at) AS asserted_at,
       // The one relationship type a cascade auto-invalidation (never a
       // manual reject) can land on — see reasoning_layer/cascade.py's
       // _AUTO_INVALIDATE_MEMBERSHIP, which SETs exactly these fields
       // directly on this same MEMBER_OF_FRAUD_NETWORK edge. All other
       // branches in this UNION can only ever carry the manual
       // rejected_by/reverted_by pair (per
       // rule_registry.DOWNSTREAM_DEPENDENTS / cascade.py's
       // _NETWORK_TYPE_BY_RULE_ID — only Rule 2/4/6 network membership
       // and the Rule 8/13 case flags are ever cascade targets), so
       // only this branch's map needs the extra keys.
       {rejected_by: r.rejected_by, rejected_at: r.rejected_at, reason: r.rejection_reason,
        reverted_by: r.reverted_by, reverted_at: r.reverted_at, revert_reason: r.revert_reason,
        auto_invalidated: r.auto_invalidated, invalidated_by_rule_id: r.invalidated_by_rule_id,
        invalidated_reason: r.invalidated_reason, invalidated_by_investigator: r.invalidated_by_investigator_id,
        invalidated_at: r.invalidated_at, reinstated_by_rule_id: r.reinstated_by_rule_id,
        reinstated_reason: r.reinstated_reason, reinstated_by_investigator: r.reinstated_by_investigator_id,
        reinstated_at: r.reinstated_at} AS rejection_raw

UNION ALL
MATCH (s:Subject {subject_id: $subject_id})-[r:HAS_PRIOR_GUILTY_CASE]->(c:Case)
WHERE r.status IN ["active", "rejected"]
RETURN "HAS_PRIOR_GUILTY_CASE" AS relationship_type,
       c.case_id AS counterpart_id, "Case" AS counterpart_type,
       // counterpart_label uses the investigator-facing complaint
       // number, not the raw case_id — see core.case_store.
       // get_complaint_number's Python-side equivalent for the same
       // reasoning. Falls back to case_id only if a Case node somehow
       // has no complaint_number (shouldn't happen post-etl/graph_sync.py,
       // but this is read by report generation, so degrade rather than
       // null out the label).
       coalesce(c.complaint_number, c.case_id) AS counterpart_label,
       r.source_rule AS source_rule, r.confidence AS confidence,
       coalesce(r.corroborated, false) AS corroborated, r.status AS status,
       toString(r.asserted_at) AS asserted_at,
       {rejected_by: r.rejected_by, rejected_at: r.rejected_at, reason: r.rejection_reason,
        reverted_by: r.reverted_by, reverted_at: r.reverted_at, revert_reason: r.revert_reason} AS rejection_raw

UNION ALL
MATCH (s:Subject {subject_id: $subject_id})-[r:APPEARS_IN_CASE]->(c:Case)
WHERE r.source_rule = "Rule_10_Merged_Case_Propagation"
  AND r.status IN ["active", "rejected"]
RETURN "APPEARS_IN_CASE" AS relationship_type,
       c.case_id AS counterpart_id, "Case" AS counterpart_type,
       // counterpart_label uses the investigator-facing complaint
       // number, not the raw case_id — see core.case_store.
       // get_complaint_number's Python-side equivalent for the same
       // reasoning. Falls back to case_id only if a Case node somehow
       // has no complaint_number (shouldn't happen post-etl/graph_sync.py,
       // but this is read by report generation, so degrade rather than
       // null out the label).
       coalesce(c.complaint_number, c.case_id) AS counterpart_label,
       r.source_rule AS source_rule, r.confidence AS confidence,
       coalesce(r.corroborated, false) AS corroborated, r.status AS status,
       toString(r.asserted_at) AS asserted_at,
       {rejected_by: r.rejected_by, rejected_at: r.rejected_at, reason: r.rejection_reason,
        reverted_by: r.reverted_by, reverted_at: r.reverted_at, revert_reason: r.revert_reason} AS rejection_raw

// Rule 8 and Rule 13 are the two CASE-FLAG rules (reasoning_layer/
// rejection.py's _FAMILY_CASE_FLAG) — they assert a property directly
// onto the :Case rather than a new edge between two nodes (see
// reasoning_layer/rules_fired.py's own "Property-writing rules"
// comment above its _PROP_RULES). They are included here, alongside
// the six genuine relationship types above, because an investigator
// can reject them exactly like any other finding, and a rejected
// escalation is exactly as reportable a fact as a rejected connection
// — "no representation as a graph edge" is an implementation detail,
// not a reason to hide the fact from Reviewed and Excluded Connections.
// counterpart_type "Case" already exists above (HAS_PRIOR_GUILTY_CASE /
// APPEARS_IN_CASE both use it) — the case itself is the only entity
// either escalation is naturally "about".
UNION ALL
MATCH (c:Case {case_id: $case_id})
WHERE c.risk_escalation_source_rule = "Rule_08_Recidivist_Escalation"
  AND c.risk_escalation_subject_id = $subject_id
  AND coalesce(c.risk_escalation_status, "active") IN ["active", "rejected"]
RETURN "CASE_RISK_ESCALATION" AS relationship_type,
       c.case_id AS counterpart_id, "Case" AS counterpart_type,
       // counterpart_label uses the investigator-facing complaint
       // number, not the raw case_id — see core.case_store.
       // get_complaint_number's Python-side equivalent for the same
       // reasoning. Falls back to case_id only if a Case node somehow
       // has no complaint_number (shouldn't happen post-etl/graph_sync.py,
       // but this is read by report generation, so degrade rather than
       // null out the label).
       coalesce(c.complaint_number, c.case_id) AS counterpart_label,
       c.risk_escalation_source_rule AS source_rule, c.risk_escalation_confidence AS confidence,
       false AS corroborated, coalesce(c.risk_escalation_status, "active") AS status,
       toString(c.risk_escalation_asserted_at) AS asserted_at,
       {rejected_by: c.risk_escalation_rejected_by, rejected_at: c.risk_escalation_rejected_at,
        reason: c.risk_escalation_rejection_reason,
        reverted_by: c.risk_escalation_reverted_by, reverted_at: c.risk_escalation_reverted_at,
        revert_reason: c.risk_escalation_revert_reason,
        auto_invalidated: c.risk_escalation_auto_invalidated,
        invalidated_by_rule_id: c.risk_escalation_invalidated_by_rule_id,
        invalidated_reason: c.risk_escalation_invalidated_reason,
        invalidated_by_investigator: c.risk_escalation_invalidated_by_investigator_id,
        invalidated_at: c.risk_escalation_invalidated_at,
        reinstated_by_rule_id: c.risk_escalation_reinstated_by_rule_id,
        reinstated_reason: c.risk_escalation_reinstated_reason,
        reinstated_by_investigator: c.risk_escalation_reinstated_by_investigator_id,
        reinstated_at: c.risk_escalation_reinstated_at} AS rejection_raw

// Rule 13 stamps no escalating-subject id onto :Case (unlike Rule 8) —
// reasoning_layer/cascade.py's own _CASE_FLAG_FIELDS comment: "it is
// scoped to the case's PRIMARY subject only". The case_id alone
// already disambiguates it, matching reasoning_layer/rules_fired.py's
// own Rule 13 query, which has no subject filter either — so this
// branch correctly surfaces once per case regardless of which
// subject's related_network is being assembled, exactly as it should
// for a case-wide recommendation.
UNION ALL
MATCH (c:Case {case_id: $case_id})
WHERE c.fasttrack_recommendation_rule = "Rule_13_FastTrack_Escalation"
  AND coalesce(c.fasttrack_recommendation_status, "active") IN ["active", "rejected"]
RETURN "FASTTRACK_RECOMMENDATION" AS relationship_type,
       c.case_id AS counterpart_id, "Case" AS counterpart_type,
       // counterpart_label uses the investigator-facing complaint
       // number, not the raw case_id — see core.case_store.
       // get_complaint_number's Python-side equivalent for the same
       // reasoning. Falls back to case_id only if a Case node somehow
       // has no complaint_number (shouldn't happen post-etl/graph_sync.py,
       // but this is read by report generation, so degrade rather than
       // null out the label).
       coalesce(c.complaint_number, c.case_id) AS counterpart_label,
       c.fasttrack_recommendation_rule AS source_rule, c.fasttrack_recommendation_confidence AS confidence,
       false AS corroborated, coalesce(c.fasttrack_recommendation_status, "active") AS status,
       toString(c.fasttrack_recommendation_asserted_at) AS asserted_at,
       {rejected_by: c.fasttrack_recommendation_rejected_by, rejected_at: c.fasttrack_recommendation_rejected_at,
        reason: c.fasttrack_recommendation_rejection_reason,
        reverted_by: c.fasttrack_recommendation_reverted_by, reverted_at: c.fasttrack_recommendation_reverted_at,
        revert_reason: c.fasttrack_recommendation_revert_reason,
        auto_invalidated: c.fasttrack_recommendation_auto_invalidated,
        invalidated_by_rule_id: c.fasttrack_recommendation_invalidated_by_rule_id,
        invalidated_reason: c.fasttrack_recommendation_invalidated_reason,
        invalidated_by_investigator: c.fasttrack_recommendation_invalidated_by_investigator_id,
        invalidated_at: c.fasttrack_recommendation_invalidated_at,
        reinstated_by_rule_id: c.fasttrack_recommendation_reinstated_by_rule_id,
        reinstated_reason: c.fasttrack_recommendation_reinstated_reason,
        reinstated_by_investigator: c.fasttrack_recommendation_reinstated_by_investigator_id,
        reinstated_at: c.fasttrack_recommendation_reinstated_at} AS rejection_raw
"""


