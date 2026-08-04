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
this fire" panel possible without a second round of queries. Each entry
in `instances` additionally carries asserted_at, subject_id_a,
subject_id_b, relationship_type and match_id (frontend follow-up to
AI-28/AI-33) — the same instance-targeting fields rule_audit.py already
puts on every row it returns — so a caller can POST /reject_inference
or /revert_rejection straight off an /intake response without a second
call to GET /rule_audit first.

Does NOT own: rule execution (rule_engine.py) or rule content.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from reasoning_layer import rejection, rule_inference, rule_registry
from reasoning_layer.neo4j_client import get_session
from reasoning_layer.rejection import build_match_id

logger = logging.getLogger(__name__)

_CONFIDENCE_ORDER = {"Unresolved": 0, "Medium": 1, "High": 2}

# Relationship-writing rules: read back the edges they wrote, filtered to
# this run's scope and to status "active" (a rejected fact is suppressed
# from the block, per Principle 14 — the rejection itself is surfaced
# separately by /rule_audit, never silently dropped).
_REL_RULES: Dict[str, str] = {
    "Rule_01_Shared_Employer": """
        MATCH (a:Subject)-[r:SHARES_EMPLOYER_WITH]-(b:Subject)
        WHERE a.subject_id IN $scope_subject_ids AND a.subject_id < b.subject_id
          AND r.source_rule = "Rule_01_Shared_Employer"
          AND coalesce(r.status, "active") IN ["active", "rejected"]
        OPTIONAL MATCH (a)-[:EMPLOYED_BY]->(e:Employer)<-[:EMPLOYED_BY]-(b)
        WITH a, b, r, head(collect({name: e.name, fein: e.fein})) AS emp
        RETURN a.subject_id AS subject_id, a.first_name AS first_name, a.last_name AS last_name,
               b.subject_id AS related_subject_id, b.first_name AS related_first_name,
               b.last_name AS related_last_name,
               r.confidence AS confidence, coalesce(r.corroborated, false) AS corroborated,
               coalesce(r.status, "active") AS status, toString(r.asserted_at) AS asserted_at,
               {rejected_by: r.rejected_by, rejected_at: r.rejected_at,
                reason: r.rejection_reason, reverted_by: r.reverted_by,
                reverted_at: r.reverted_at, revert_reason: r.revert_reason} AS rejection,
               {employer_name: emp.name, fein: coalesce(emp.fein, r.fein)} AS detail
        ORDER BY subject_id, related_subject_id
""",
    "Rule_03_Shared_Address": """
        MATCH (a:Subject)-[r:SHARES_ADDRESS_WITH]-(b:Subject)
        WHERE a.subject_id IN $scope_subject_ids
          AND r.source_rule = "Rule_03_Shared_Address"
          AND coalesce(r.status, "active") IN ["active", "rejected"]
        OPTIONAL MATCH (a)-[:HAS_ADDRESS]->(addr:Address)<-[:HAS_ADDRESS]-(b)
        WITH a, b, r, head(collect({street: addr.street, city: addr.city,
                                    state: addr.state, zip: addr.zip,
                                    address_key: addr.address_key})) AS ad
        RETURN a.subject_id AS subject_id, a.first_name AS first_name, a.last_name AS last_name,
               b.subject_id AS related_subject_id, b.first_name AS related_first_name,
               b.last_name AS related_last_name,
               r.confidence AS confidence, coalesce(r.corroborated, false) AS corroborated,
               coalesce(r.status, "active") AS status, toString(r.asserted_at) AS asserted_at,
               {rejected_by: r.rejected_by, rejected_at: r.rejected_at,
                reason: r.rejection_reason, reverted_by: r.reverted_by,
                reverted_at: r.reverted_at, revert_reason: r.revert_reason} AS rejection,
               {street: ad.street, city: ad.city, state: ad.state, zip: ad.zip,
                address_key: coalesce(ad.address_key, r.address_key)} AS detail
        ORDER BY subject_id, related_subject_id
""",
    "Rule_05_Alias_Identity": """
        MATCH (a:Subject)-[r:SHARES_ALIAS_PATTERN_WITH]-(b:Subject)
        WHERE a.subject_id IN $scope_subject_ids
          AND r.source_rule = "Rule_05_Alias_Identity"
          AND coalesce(r.status, "active") IN ["active", "rejected"]
        RETURN a.subject_id AS subject_id, a.first_name AS first_name, a.last_name AS last_name,
               b.subject_id AS related_subject_id, b.first_name AS related_first_name,
               b.last_name AS related_last_name,
               r.confidence AS confidence, coalesce(r.corroborated, false) AS corroborated,
               coalesce(r.status, "active") AS status, toString(r.asserted_at) AS asserted_at,
               {rejected_by: r.rejected_by, rejected_at: r.rejected_at,
                reason: r.rejection_reason, reverted_by: r.reverted_by,
                reverted_at: r.reverted_at, revert_reason: r.revert_reason} AS rejection,
               {alias_pattern: coalesce(r.alias_pattern, r.match_basis)} AS detail
        ORDER BY subject_id, related_subject_id
""",
    "Rule_10_Merged_Case_Propagation": """
        MATCH (a:Subject)-[r:APPEARS_IN_CASE]->(c:Case)
        WHERE a.subject_id IN $scope_subject_ids
          AND r.source_rule = "Rule_10_Merged_Case_Propagation"
          AND coalesce(r.status, "active") IN ["active", "rejected"]
        RETURN a.subject_id AS subject_id, a.first_name AS first_name, a.last_name AS last_name,
               c.case_id AS related_case_id,
               r.confidence AS confidence, coalesce(r.corroborated, false) AS corroborated,
               coalesce(r.status, "active") AS status, toString(r.asserted_at) AS asserted_at,
               {rejected_by: r.rejected_by, rejected_at: r.rejected_at,
                reason: r.rejection_reason, reverted_by: r.reverted_by,
                reverted_at: r.reverted_at, revert_reason: r.revert_reason} AS rejection,
               {complaint_no: c.complaint_number, case_status: c.status} AS detail
        ORDER BY subject_id, related_case_id
""",
    "Rule_02_Employer_Fraud_Network": """
        MATCH (a:Subject)-[r:MEMBER_OF_FRAUD_NETWORK]->(n:FraudNetwork)
        WHERE a.subject_id IN $scope_subject_ids
          AND r.source_rule = "Rule_02_Employer_Fraud_Network"
          AND coalesce(r.status, "active") IN ["active", "rejected"]
        // Collapse to ONE row per network, even when several scope subjects
        // are members of it (the normal case — Rule 2 always writes BOTH
        // endpoints). The rendered inference line lists every member by
        // name and does not vary by which scope subject anchored the
        // match, so matching per-`a` produced the identical line once per
        // scope member (e.g. twice when both subjects on the case belong
        // to the same network) instead of once per network.
        WITH n, collect(DISTINCT a) AS scope_members, collect(r) AS scope_rels
        WITH n, head(scope_members) AS a,
             reduce(best = "Unresolved", rel IN scope_rels |
                 CASE WHEN best = "High" OR rel.confidence = "High" THEN "High"
                      WHEN best = "Medium" OR rel.confidence = "Medium" THEN "Medium"
                      ELSE best END) AS confidence,
             any(rel IN scope_rels WHERE rel.corroborated = true) AS corroborated,
             // The network is live while ANY in-scope membership edge is
             // still active. Rejection is a bulk case+rule operation so in
             // practice they flip together, but deriving it rather than
             // reading one edge means a partially-reverted network reads as
             // active — which it is — instead of inheriting whichever edge
             // the planner happened to put first.
             CASE WHEN any(rel IN scope_rels
                           WHERE coalesce(rel.status, "active") = "active")
                  THEN "active" ELSE "rejected" END AS status,
             head([rel IN scope_rels | rel.asserted_at]) AS asserted_at_raw,
             head([rel IN scope_rels
                   WHERE coalesce(rel.status, "active") = "rejected" |
                   {rejected_by: rel.rejected_by, rejected_at: rel.rejected_at,
                    reason: rel.rejection_reason, reverted_by: rel.reverted_by,
                    reverted_at: rel.reverted_at, revert_reason: rel.revert_reason,
                    auto_invalidated: rel.auto_invalidated,
                    invalidated_by_rule_id: rel.invalidated_by_rule_id,
                    invalidated_reason: rel.invalidated_reason,
                    reinstated_by_rule_id: rel.reinstated_by_rule_id,
                    reinstated_reason: rel.reinstated_reason,
                    reinstated_at: rel.reinstated_at}]) AS rejection
        // Rejected members are kept in the member list, carrying their own
        // status. Dropping them emptied the list for a rejected network and
        // left the investigator a revert button with no names next to it.
        OPTIONAL MATCH (m:Subject)-[mm:MEMBER_OF_FRAUD_NETWORK]->(n)
        WHERE coalesce(mm.status, "active") IN ["active", "rejected"]
        OPTIONAL MATCH (m)-[:APPEARS_IN_CASE]->(mc:Case)-[:HAS_ALLEGATION]->(mal:Allegation)
        WITH a, n, confidence, corroborated, status, asserted_at_raw, rejection, m, mm,
             head(collect({complaint_no: mc.complaint_number,
                           allegation_type: mal.allegation_type})) AS mctx
        WITH a, n, confidence, corroborated, status, asserted_at_raw, rejection, collect(DISTINCT {
                 subject_id: m.subject_id, first_name: m.first_name, last_name: m.last_name,
                 complaint_no: mctx.complaint_no, allegation_type: mctx.allegation_type,
                 status: coalesce(mm.status, "active")
             }) AS members_raw
        RETURN a.subject_id AS subject_id, a.first_name AS first_name, a.last_name AS last_name,
               n.network_key AS related_network_key,
               confidence AS confidence, corroborated AS corroborated,
               status AS status, toString(asserted_at_raw) AS asserted_at, rejection AS rejection,
               {network_type: n.network_type, network_key: n.network_key,
                formed_by_rule: n.formed_by_rule,
                members: [x IN members_raw WHERE x.subject_id IS NOT NULL]} AS detail
        ORDER BY related_network_key
""",
    "Rule_04_Address_Fraud_Network": """
        MATCH (a:Subject)-[r:MEMBER_OF_FRAUD_NETWORK]->(n:FraudNetwork)
        WHERE a.subject_id IN $scope_subject_ids
          AND r.source_rule = "Rule_04_Address_Fraud_Network"
          AND coalesce(r.status, "active") IN ["active", "rejected"]
        // Collapse to ONE row per network — see Rule_02's comment above for
        // why matching per scope-subject `a` produced duplicate lines.
        WITH n, collect(DISTINCT a) AS scope_members, collect(r) AS scope_rels
        WITH n, head(scope_members) AS a,
             reduce(best = "Unresolved", rel IN scope_rels |
                 CASE WHEN best = "High" OR rel.confidence = "High" THEN "High"
                      WHEN best = "Medium" OR rel.confidence = "Medium" THEN "Medium"
                      ELSE best END) AS confidence,
             any(rel IN scope_rels WHERE rel.corroborated = true) AS corroborated,
             // The network is live while ANY in-scope membership edge is
             // still active. Rejection is a bulk case+rule operation so in
             // practice they flip together, but deriving it rather than
             // reading one edge means a partially-reverted network reads as
             // active — which it is — instead of inheriting whichever edge
             // the planner happened to put first.
             CASE WHEN any(rel IN scope_rels
                           WHERE coalesce(rel.status, "active") = "active")
                  THEN "active" ELSE "rejected" END AS status,
             head([rel IN scope_rels | rel.asserted_at]) AS asserted_at_raw,
             head([rel IN scope_rels
                   WHERE coalesce(rel.status, "active") = "rejected" |
                   {rejected_by: rel.rejected_by, rejected_at: rel.rejected_at,
                    reason: rel.rejection_reason, reverted_by: rel.reverted_by,
                    reverted_at: rel.reverted_at, revert_reason: rel.revert_reason,
                    auto_invalidated: rel.auto_invalidated,
                    invalidated_by_rule_id: rel.invalidated_by_rule_id,
                    invalidated_reason: rel.invalidated_reason,
                    reinstated_by_rule_id: rel.reinstated_by_rule_id,
                    reinstated_reason: rel.reinstated_reason,
                    reinstated_at: rel.reinstated_at}]) AS rejection
        // Rejected members are kept in the member list, carrying their own
        // status. Dropping them emptied the list for a rejected network and
        // left the investigator a revert button with no names next to it.
        OPTIONAL MATCH (m:Subject)-[mm:MEMBER_OF_FRAUD_NETWORK]->(n)
        WHERE coalesce(mm.status, "active") IN ["active", "rejected"]
        OPTIONAL MATCH (m)-[:APPEARS_IN_CASE]->(mc:Case)-[:HAS_ALLEGATION]->(mal:Allegation)
        WITH a, n, confidence, corroborated, status, asserted_at_raw, rejection, m, mm,
             head(collect({complaint_no: mc.complaint_number,
                           allegation_type: mal.allegation_type})) AS mctx
        WITH a, n, confidence, corroborated, status, asserted_at_raw, rejection, collect(DISTINCT {
                 subject_id: m.subject_id, first_name: m.first_name, last_name: m.last_name,
                 complaint_no: mctx.complaint_no, allegation_type: mctx.allegation_type,
                 status: coalesce(mm.status, "active")
             }) AS members_raw
        RETURN a.subject_id AS subject_id, a.first_name AS first_name, a.last_name AS last_name,
               n.network_key AS related_network_key,
               confidence AS confidence, corroborated AS corroborated,
               status AS status, toString(asserted_at_raw) AS asserted_at, rejection AS rejection,
               {network_type: n.network_type, network_key: n.network_key,
                formed_by_rule: n.formed_by_rule,
                members: [x IN members_raw WHERE x.subject_id IS NOT NULL]} AS detail
        ORDER BY related_network_key
""",
    "Rule_06_Identity_Fraud_Network": """
        MATCH (a:Subject)-[r:MEMBER_OF_FRAUD_NETWORK]->(n:FraudNetwork)
        WHERE a.subject_id IN $scope_subject_ids
          AND r.source_rule = "Rule_06_Identity_Fraud_Network"
          AND coalesce(r.status, "active") IN ["active", "rejected"]
        // Collapse to ONE row per network — see Rule_02's comment above for
        // why matching per scope-subject `a` produced duplicate lines.
        WITH n, collect(DISTINCT a) AS scope_members, collect(r) AS scope_rels
        WITH n, head(scope_members) AS a,
             reduce(best = "Unresolved", rel IN scope_rels |
                 CASE WHEN best = "High" OR rel.confidence = "High" THEN "High"
                      WHEN best = "Medium" OR rel.confidence = "Medium" THEN "Medium"
                      ELSE best END) AS confidence,
             any(rel IN scope_rels WHERE rel.corroborated = true) AS corroborated,
             // The network is live while ANY in-scope membership edge is
             // still active. Rejection is a bulk case+rule operation so in
             // practice they flip together, but deriving it rather than
             // reading one edge means a partially-reverted network reads as
             // active — which it is — instead of inheriting whichever edge
             // the planner happened to put first.
             CASE WHEN any(rel IN scope_rels
                           WHERE coalesce(rel.status, "active") = "active")
                  THEN "active" ELSE "rejected" END AS status,
             head([rel IN scope_rels | rel.asserted_at]) AS asserted_at_raw,
             head([rel IN scope_rels
                   WHERE coalesce(rel.status, "active") = "rejected" |
                   {rejected_by: rel.rejected_by, rejected_at: rel.rejected_at,
                    reason: rel.rejection_reason, reverted_by: rel.reverted_by,
                    reverted_at: rel.reverted_at, revert_reason: rel.revert_reason,
                    auto_invalidated: rel.auto_invalidated,
                    invalidated_by_rule_id: rel.invalidated_by_rule_id,
                    invalidated_reason: rel.invalidated_reason,
                    reinstated_by_rule_id: rel.reinstated_by_rule_id,
                    reinstated_reason: rel.reinstated_reason,
                    reinstated_at: rel.reinstated_at}]) AS rejection
        // Rejected members are kept in the member list, carrying their own
        // status. Dropping them emptied the list for a rejected network and
        // left the investigator a revert button with no names next to it.
        OPTIONAL MATCH (m:Subject)-[mm:MEMBER_OF_FRAUD_NETWORK]->(n)
        WHERE coalesce(mm.status, "active") IN ["active", "rejected"]
        OPTIONAL MATCH (m)-[:APPEARS_IN_CASE]->(mc:Case)-[:HAS_ALLEGATION]->(mal:Allegation)
        WITH a, n, confidence, corroborated, status, asserted_at_raw, rejection, m, mm,
             head(collect({complaint_no: mc.complaint_number,
                           allegation_type: mal.allegation_type})) AS mctx
        WITH a, n, confidence, corroborated, status, asserted_at_raw, rejection, collect(DISTINCT {
                 subject_id: m.subject_id, first_name: m.first_name, last_name: m.last_name,
                 complaint_no: mctx.complaint_no, allegation_type: mctx.allegation_type,
                 status: coalesce(mm.status, "active")
             }) AS members_raw
        RETURN a.subject_id AS subject_id, a.first_name AS first_name, a.last_name AS last_name,
               n.network_key AS related_network_key,
               confidence AS confidence, corroborated AS corroborated,
               status AS status, toString(asserted_at_raw) AS asserted_at, rejection AS rejection,
               {network_type: n.network_type, network_key: n.network_key,
                formed_by_rule: n.formed_by_rule,
                members: [x IN members_raw WHERE x.subject_id IS NOT NULL]} AS detail
        ORDER BY related_network_key
""",
    "Rule_09_PCA_CheckSplit": """
        MATCH (a:Subject)-[r:MEMBER_OF_FRAUD_NETWORK]->(n:FraudNetwork)
        WHERE a.subject_id IN $scope_subject_ids
          AND r.source_rule = "Rule_09_PCA_CheckSplit"
          AND coalesce(r.status, "active") IN ["active", "rejected"]
        // Collapse to ONE row per network — see Rule_02's comment above for
        // why matching per scope-subject `a` produced duplicate lines.
        WITH n, collect(DISTINCT a) AS scope_members, collect(r) AS scope_rels
        WITH n, head(scope_members) AS a,
             reduce(best = "Unresolved", rel IN scope_rels |
                 CASE WHEN best = "High" OR rel.confidence = "High" THEN "High"
                      WHEN best = "Medium" OR rel.confidence = "Medium" THEN "Medium"
                      ELSE best END) AS confidence,
             any(rel IN scope_rels WHERE rel.corroborated = true) AS corroborated,
             // The network is live while ANY in-scope membership edge is
             // still active. Rejection is a bulk case+rule operation so in
             // practice they flip together, but deriving it rather than
             // reading one edge means a partially-reverted network reads as
             // active — which it is — instead of inheriting whichever edge
             // the planner happened to put first.
             CASE WHEN any(rel IN scope_rels
                           WHERE coalesce(rel.status, "active") = "active")
                  THEN "active" ELSE "rejected" END AS status,
             head([rel IN scope_rels | rel.asserted_at]) AS asserted_at_raw,
             head([rel IN scope_rels
                   WHERE coalesce(rel.status, "active") = "rejected" |
                   {rejected_by: rel.rejected_by, rejected_at: rel.rejected_at,
                    reason: rel.rejection_reason, reverted_by: rel.reverted_by,
                    reverted_at: rel.reverted_at, revert_reason: rel.revert_reason,
                    auto_invalidated: rel.auto_invalidated,
                    invalidated_by_rule_id: rel.invalidated_by_rule_id,
                    invalidated_reason: rel.invalidated_reason,
                    reinstated_by_rule_id: rel.reinstated_by_rule_id,
                    reinstated_reason: rel.reinstated_reason,
                    reinstated_at: rel.reinstated_at}]) AS rejection
        // Rejected members are kept in the member list, carrying their own
        // status. Dropping them emptied the list for a rejected network and
        // left the investigator a revert button with no names next to it.
        OPTIONAL MATCH (m:Subject)-[mm:MEMBER_OF_FRAUD_NETWORK]->(n)
        WHERE coalesce(mm.status, "active") IN ["active", "rejected"]
        OPTIONAL MATCH (m)-[:APPEARS_IN_CASE]->(mc:Case)-[:HAS_ALLEGATION]->(mal:Allegation)
        WITH a, n, confidence, corroborated, status, asserted_at_raw, rejection, m, mm,
             head(collect({complaint_no: mc.complaint_number,
                           allegation_type: mal.allegation_type})) AS mctx
        WITH a, n, confidence, corroborated, status, asserted_at_raw, rejection, collect(DISTINCT {
                 subject_id: m.subject_id, first_name: m.first_name, last_name: m.last_name,
                 complaint_no: mctx.complaint_no, allegation_type: mctx.allegation_type,
                 status: coalesce(mm.status, "active")
             }) AS members_raw
        RETURN a.subject_id AS subject_id, a.first_name AS first_name, a.last_name AS last_name,
               n.network_key AS related_network_key,
               confidence AS confidence, corroborated AS corroborated,
               status AS status, toString(asserted_at_raw) AS asserted_at, rejection AS rejection,
               {network_type: n.network_type, network_key: n.network_key,
                formed_by_rule: n.formed_by_rule,
                members: [x IN members_raw WHERE x.subject_id IS NOT NULL]} AS detail
        ORDER BY related_network_key
""",
    "Rule_07_Prior_Guilty": """
        MATCH (a:Subject)-[r:HAS_PRIOR_GUILTY_CASE]->(c:Case)
        WHERE a.subject_id IN $scope_subject_ids
          AND r.source_rule = "Rule_07_Prior_Guilty"
          AND coalesce(r.status, "active") IN ["active", "rejected"]
        RETURN a.subject_id AS subject_id, a.first_name AS first_name, a.last_name AS last_name,
               c.case_id AS related_case_id,
               r.confidence AS confidence, coalesce(r.corroborated, false) AS corroborated,
               coalesce(r.status, "active") AS status, toString(r.asserted_at) AS asserted_at,
               {rejected_by: r.rejected_by, rejected_at: r.rejected_at,
                reason: r.rejection_reason, reverted_by: r.reverted_by,
                reverted_at: r.reverted_at, revert_reason: r.revert_reason} AS rejection,
               {complaint_no: c.complaint_number, outcome: r.outcome,
                date_closed: r.date_closed} AS detail
        ORDER BY subject_id, related_case_id
""",
    "Rule_14_Confirmation_Elevation": """
        MATCH (a:Subject)-[r]-(other)
        WHERE a.subject_id IN $scope_subject_ids
          AND r.corroborated_by = "Rule_14_Confirmation_Elevation"
          AND coalesce(r.status, "active") IN ["active", "rejected"]
        RETURN a.subject_id AS subject_id, a.first_name AS first_name, a.last_name AS last_name,
               other.subject_id AS related_subject_id,
               other.first_name AS related_first_name, other.last_name AS related_last_name,
               "High" AS confidence, true AS corroborated,
               coalesce(r.status, "active") AS status, toString(r.asserted_at) AS asserted_at,
               {rejected_by: r.rejected_by, rejected_at: r.rejected_at,
                reason: r.rejection_reason, reverted_by: r.reverted_by,
                reverted_at: r.reverted_at, revert_reason: r.revert_reason} AS rejection,
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
          // Rejection sets is_cross_case=false and cross_case_rejected=true
          // (rejection.py's _BULK_REJECT_SUBJECT_FLAG), so matching only on
          // is_cross_case=true is what made a rejected hub disappear from
          // the block — and with it the row an investigator would revert
          // from. Both states are matched; the status says which.
          AND (a.is_cross_case = true OR a.cross_case_rejected = true)
        RETURN a.subject_id AS subject_id, a.first_name AS first_name, a.last_name AS last_name,
               a.cross_case_confidence AS confidence, false AS corroborated,
               CASE WHEN a.cross_case_rejected = true THEN "rejected" ELSE "active" END AS status,
               toString(a.cross_case_asserted_at) AS asserted_at,
               {rejected_by: a.cross_case_rejected_by, rejected_at: a.cross_case_rejected_at,
                reason: a.cross_case_rejection_reason, reverted_by: a.cross_case_reverted_by,
                reverted_at: a.cross_case_reverted_at,
                revert_reason: a.cross_case_revert_reason} AS rejection,
               {hub_case_ids: coalesce(a.hub_case_ids, [])} AS detail
        ORDER BY subject_id
""",
    "Rule_08_Recidivist_Escalation": """
        MATCH (c:Case)
        WHERE c.case_id = $case_id
          AND c.risk_escalation_source_rule = "Rule_08_Recidivist_Escalation"
          AND coalesce(c.risk_escalation_status, "active") IN ["active", "rejected"]
        RETURN c.risk_escalation_subject_id AS subject_id, c.case_id AS related_case_id,
               c.risk_escalation_confidence AS confidence, false AS corroborated,
               coalesce(c.risk_escalation_status, "active") AS status,
               toString(c.risk_escalation_asserted_at) AS asserted_at,
               {rejected_by: c.risk_escalation_rejected_by,
                rejected_at: c.risk_escalation_rejected_at,
                reason: c.risk_escalation_rejection_reason,
                reverted_by: c.risk_escalation_reverted_by,
                reverted_at: c.risk_escalation_reverted_at,
                revert_reason: c.risk_escalation_revert_reason,
                auto_invalidated: c.risk_escalation_auto_invalidated,
                invalidated_by_rule_id: c.risk_escalation_invalidated_by_rule_id,
                invalidated_reason: c.risk_escalation_invalidated_reason,
                reinstated_by_rule_id: c.risk_escalation_reinstated_by_rule_id,
                reinstated_reason: c.risk_escalation_reinstated_reason,
                reinstated_at: c.risk_escalation_reinstated_at} AS rejection,
               {complaint_no: c.complaint_number, fraud_amount: c.fraud_amount} AS detail
        ORDER BY related_case_id
""",
    "Rule_12_SLAM_Wage_Corroboration": """
        MATCH (c:Case)-[:HAS_ALLEGATION]->(al:Allegation)-[att:ALLEGATION_LIKELY_AGAINST_SUBJECT]->(a:Subject)
        WHERE a.subject_id IN $scope_subject_ids
          AND al.wage_corroboration_rule = "Rule_12_SLAM_Wage_Corroboration"
          AND coalesce(al.wage_corroboration_status, "active") IN ["active", "rejected"]
        OPTIONAL MATCH (a)-[:HAS_WAGE_RECORD_WITH]->(e:Employer)
        WITH a, c, al, head(collect(e.name)) AS employer_name
        RETURN a.subject_id AS subject_id, a.first_name AS first_name, a.last_name AS last_name,
               c.case_id AS related_case_id, al.allegation_type AS allegation_type,
               al.allegation_id AS allegation_id,
               al.wage_corroboration_confidence AS confidence,
               coalesce(al.wage_corroboration_verified, false) AS corroborated,
               coalesce(al.wage_corroboration_status, "active") AS status,
               toString(al.wage_corroboration_asserted_at) AS asserted_at,
               {rejected_by: al.wage_corroboration_rejected_by,
                rejected_at: al.wage_corroboration_rejected_at,
                reason: al.wage_corroboration_rejection_reason,
                reverted_by: al.wage_corroboration_reverted_by,
                reverted_at: al.wage_corroboration_reverted_at,
                revert_reason: al.wage_corroboration_revert_reason} AS rejection,
               {complaint_no: c.complaint_number, employer_name: employer_name,
                allegation_type: al.allegation_type,
                fraud_start_date: c.fraud_start_date, fraud_end_date: c.fraud_end_date} AS detail
        ORDER BY subject_id, related_case_id
""",
    "Rule_13_FastTrack_Escalation": """
        MATCH (c:Case {case_id: $case_id})
        WHERE c.fasttrack_recommendation_rule = "Rule_13_FastTrack_Escalation"
          AND coalesce(c.fasttrack_recommendation_status, "active") IN ["active", "rejected"]
        RETURN $subject_id AS subject_id, c.case_id AS related_case_id,
               c.fasttrack_recommendation_confidence AS confidence, false AS corroborated,
               coalesce(c.fasttrack_recommendation_status, "active") AS status,
               toString(c.fasttrack_recommendation_asserted_at) AS asserted_at,
               {rejected_by: c.fasttrack_recommendation_rejected_by,
                rejected_at: c.fasttrack_recommendation_rejected_at,
                reason: c.fasttrack_recommendation_rejection_reason,
                reverted_by: c.fasttrack_recommendation_reverted_by,
                reverted_at: c.fasttrack_recommendation_reverted_at,
                revert_reason: c.fasttrack_recommendation_revert_reason,
                auto_invalidated: c.fasttrack_recommendation_auto_invalidated,
                invalidated_by_rule_id: c.fasttrack_recommendation_invalidated_by_rule_id,
                invalidated_reason: c.fasttrack_recommendation_invalidated_reason,
                reinstated_by_rule_id: c.fasttrack_recommendation_reinstated_by_rule_id,
                reinstated_reason: c.fasttrack_recommendation_reinstated_reason,
                reinstated_at: c.fasttrack_recommendation_reinstated_at} AS rejection,
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
    "subject_id",
    "related_subject_id",
    "related_case_id",
    "related_network_key",
    "allegation_type",
    "allegation_id",
)


def _stamp_member_match_ids(rule_id: str, detail: Dict[str, Any]) -> Dict[str, Any]:
    """
    AI-28 completeness gap: the network family (Rule_02/04/06/09)
    collapses every member into ONE instance row for one readable
    narrative line (see the _REL_RULES network queries' own "Collapse
    to ONE row per network" comment) — but that meant the row's single
    top-level match_id could only ever target the ANCHOR member
    (row["subject_id"]), unlike reasoning_layer/rule_audit.py and
    reasoning_layer/fraud_network.py, which return one row PER member
    and so already let an investigator reject/revert ANY specific
    member directly. This stamps a member-specific match_id onto every
    entry in detail["members"], decodable back to (rule_id, that
    member's own subject_id, the network composite key) — byte-
    identical to what rule_audit.py would build for that same member.

    Degrades to leaving a member's match_id as None (never raises)
    when network_type or network_key is missing or a member has no
    subject_id — a display concern must never crash the whole
    rules_fired build over one incomplete row. A no-op, returning
    `detail` completely unchanged, when there is no "members" list at
    all (every non-network-family rule's detail, and a malformed
    network-family row).

    Returns a NEW detail dict with a NEW members list of NEW member
    dicts — the row/detail this module was handed, and its own
    "members" list and dicts, are never mutated in place.
    """
    members = detail.get("members")
    if not members:
        return detail

    network_type = detail.get("network_type")
    network_key = detail.get("network_key")
    network_composite = f"{network_type}:{network_key}" if network_type and network_key else None

    stamped_members = []
    for member in members:
        member_copy = dict(member)
        member_subject_id = member_copy.get("subject_id")
        member_copy["match_id"] = (
            build_match_id(rule_id, member_subject_id, network_composite)
            if member_subject_id and network_composite
            else None
        )
        stamped_members.append(member_copy)

    new_detail = dict(detail)
    new_detail["members"] = stamped_members
    return new_detail


def _instance(rule_id: str, row: Dict[str, Any]) -> Dict[str, Any]:
    """
    One concrete match: WHICH subjects/records this rule fired on, with the
    entity and field detail behind it and a readable inference line.

    `detail` carries the fields the rule actually matched on — the address,
    the employer FEIN, the network members. Without it "Rule 3 fired" tells
    an investigator that something matched but not what, which is not
    enough to accept or reject the inference.

    AI-30 / frontend follow-up: also carries `asserted_at`, `subject_id_a`,
    `subject_id_b`, `relationship_type` and `match_id` — exactly the fields
    reasoning_layer/rule_audit.py already stamps onto every row it returns
    — so the frontend can POST /reject_inference or /revert_rejection
    straight off an /intake response's rules_fired.instances without a
    second round trip to GET /rule_audit first just to obtain a match_id.
    subject_id_a/subject_id_b/match_id are computed via
    reasoning_layer.rejection.instance_endpoints/build_match_id, the same
    per-rule-family encoding rule_audit.py and fraud_network.py already
    use — see that function's docstring for the family-by-family mapping.
    Omitted entirely for rule_ids rejection.py does not track as
    rejectable (Rule 14, a confidence modifier with no independent
    instance of its own), rather than emitting a match_id that would
    always 404 if a caller tried to use it.

    For the case-flag family (Rule 8/13), subject_id_a comes straight
    off row["subject_id"] — Rule 8's query reads its own
    risk_escalation_subject_id property; Rule 13's query returns the
    $subject_id query parameter build_rules_fired binds from
    scope["primary_subject_id"] (Rule 13 stamps no escalating-subject
    id onto :Case itself — see that query's own comment). Either way,
    no separate parameter is needed here: row["subject_id"] is already
    whatever the right value is, or None if a primary subject was never
    resolved, in which case match_id degrades to None rather than
    building a token nobody could ever act on.

    For the network family (Rule 2/4/6/9), every member in
    detail["members"] additionally gets its OWN match_id via
    _stamp_member_match_ids — see that function's docstring.
    """
    instance = {key: row[key] for key in _INSTANCE_KEYS if row.get(key) is not None}
    detail = {k: v for k, v in (row.get("detail") or {}).items() if v is not None and v != []}
    if detail:
        detail = _stamp_member_match_ids(rule_id, detail)
        instance["detail"] = detail
    instance["confidence"] = row.get("confidence") or "Unresolved"
    instance["corroborated"] = bool(row.get("corroborated", False))
    if row.get("asserted_at"):
        instance["asserted_at"] = row["asserted_at"]

    # --- rejection state (Human-in-the-Loop, Section 5.2) ---
    # A rejected instance STAYS in the block. It used to be filtered out of
    # the query entirely, which meant the investigator who rejected it had
    # nothing left on screen to revert from — the only way back was
    # /rule_audit, a different endpoint with a different shape. Keeping the
    # row and flipping a status is what makes reject and revert two
    # directions of one control rather than a one-way door.
    status = row.get("status") or "active"
    instance["status"] = status
    instance["revertable"] = status == "rejected"
    audit = {k: v for k, v in (row.get("rejection") or {}).items() if v is not None and v != ""}
    if audit:
        # Who rejected it, when, and why — and the same for a previous
        # revert. An investigator deciding whether to revert someone else's
        # rejection needs the reason, not just the fact of it.
        instance["rejection"] = audit
    # Names + the "why it fired" line are a presentation concern, owned by
    # rule_inference so rewording never touches this query module.
    for name_key in ("first_name", "last_name", "related_first_name", "related_last_name"):
        if row.get(name_key) is not None:
            instance[name_key] = row[name_key]

    # --- reject/revert targeting fields (v3 contract, AI-28/AI-33) ---
    subject_id_a, subject_id_b = rejection.instance_endpoints(
        rule_id,
        case_id=row.get("related_case_id"),
        subject_id=row.get("subject_id"),
        related_subject_id=row.get("related_subject_id"),
        related_case_id=row.get("related_case_id"),
        network_type=detail.get("network_type"),
        network_key=detail.get("network_key"),
        allegation_id=row.get("allegation_id"),
    )
    if subject_id_a:
        instance["subject_id_a"] = subject_id_a
        instance["subject_id_b"] = subject_id_b
        instance["relationship_type"] = rule_inference.rule_label(rule_id)
        instance["match_id"] = build_match_id(rule_id, subject_id_a, subject_id_b)

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
    active = [i for i in instances if i["status"] == "active"]
    rejected = [i for i in instances if i["status"] == "rejected"]
    count = len(active)

    # EVERY rolled-up figure below is computed from ACTIVE instances only,
    # and that is the whole safety property of this change. `instances` now
    # carries rejected findings so the UI can show and revert them — but
    # rules_fired also feeds the Copilot's context, Investigation Plan and
    # Report Generation, and a fact an investigator has explicitly rejected
    # must never be handed to any of them as live evidence. Visible in the
    # payload, absent from the counts.
    confidences = [i["confidence"] for i in active if i["confidence"]]
    confidence = max(confidences, key=lambda c: _CONFIDENCE_ORDER.get(c, 0)) if confidences else "Unresolved"

    if count and rejected:
        rule_status = "partially_rejected"
    elif rejected:
        rule_status = "rejected"
    elif count:
        rule_status = "active"
    else:
        rule_status = "not_fired"

    return {
        # Unchanged meaning: is there a LIVE finding here. A rule whose only
        # findings were rejected reports fired=false, exactly as it did when
        # those rows were dropped from the query — downstream consumers see
        # no behaviour change from this work.
        "fired": count > 0,
        # A rule that did not fire has no confidence to report. "Unresolved"
        # is the correct value here (A.4's own enum) — not None, and not a
        # cheerful "High" inherited from a previous run.
        "confidence": confidence if count > 0 else "Unresolved",
        "corroborated": any(i["corroborated"] for i in active),
        "evidence_count": count,
        # `matched` is the flag a UI renders the row on: this rule produced
        # something, whether or not it is currently accepted. `fired` alone
        # cannot serve that purpose without either hiding rejected rows or
        # misreporting rejected facts as live to the LLM consumers.
        "matched": len(instances) > 0,
        "status": rule_status,
        "rejected_count": len(rejected),
        "revertable": len(rejected) > 0,
        "instances": instances,
    }


def _dedupe_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Collapse rows describing the SAME logical instance down to one,
    keyed on the same identity fields _instance() itself keys an
    instance on (_INSTANCE_KEYS: subject_id / related_subject_id /
    related_case_id / related_network_key / allegation_type).

    Defense in depth against duplicate PHYSICAL relationships in the
    graph for the same logical fact. The known way this happens: MERGE
    on an undirected relationship pattern — `MERGE (a)-[r:TYPE]-(b)` —
    is not guaranteed idempotent (Cypher's own docs flag undirected
    MERGE as unreliable); under some execution-plan orderings across
    repeated pipeline runs it can create a second parallel relationship
    for the same pair instead of matching the one already there. The
    three symmetric-edge write rules (reasoning_layer/rules/wave1/
    rule_01_shared_employer.cypher, rule_03_shared_address.cypher,
    rule_05_alias_identity.cypher) now MERGE in a fixed, deterministic
    direction (already enforced by their own `a.subject_id < b.subject_id`
    guard) to stop NEW duplicates from forming — but that does nothing
    for a duplicate a graph already has, so every _REL_RULES/_PROP_RULES
    query is deduped here regardless of family, rather than trusting
    each Cypher file to never produce one.

    Keeps the FIRST row for a given key. Every query in _REL_RULES and
    _PROP_RULES orders its results (ORDER BY subject_id, related_subject_id
    or equivalent), so "first" is deterministic across repeated calls,
    not an arbitrary pick.
    """
    seen = set()
    deduped: List[Dict[str, Any]] = []
    for row in rows:
        key = tuple(row.get(k) for k in _INSTANCE_KEYS)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def build_rules_fired(scope: Dict[str, Any], execution_records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
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
        # Rule 13 stamps no escalating-subject id onto :Case itself (see
        # that query's own comment) — its RETURN reads this bound
        # $subject_id parameter instead, so row["subject_id"] is
        # populated for it exactly like every other rule, and
        # _instance() needs no separate case_id/primary_subject_id
        # threading to build its match_id.
        "subject_id": scope.get("primary_subject_id"),
    }

    block: List[Dict[str, Any]] = []
    with get_session() as session:
        for rule_id in rule_registry.ALL_RULE_IDS:
            query = _REL_RULES.get(rule_id) or _PROP_RULES.get(rule_id)
            rows = session.run(query, **params).data()
            rows = _dedupe_rows(rows)
            summary = _summarise(rule_id, rows)
            execution = executed_by_id.get(rule_id, {})
            block.append(
                {
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
                    # --- rejection / revert state (Human-in-the-Loop) ---
                    # `status` is the rule-level roll-up: active, rejected,
                    # partially_rejected, or not_fired. `revertable` tells the UI
                    # whether POST /revert_rejection has anything to act on for
                    # this case_id + rule_id, so it can enable the control without
                    # a second call to /rule_audit.
                    "matched": summary["matched"],
                    "status": summary["status"],
                    "rejected_count": summary["rejected_count"],
                    "revertable": summary["revertable"],
                    # Which concrete subjects/records this rule fired on. Without
                    # it, "Rule 3 fired, evidence_count 2" tells an investigator
                    # something happened but not to whom — and the co-subject
                    # pipeline runs below make multi-instance results the norm.
                    "instances": summary["instances"],
                    "wave": (
                        1
                        if rule_id in rule_registry.WAVE_1_RULE_IDS
                        else 2 if rule_id in rule_registry.WAVE_2_RULE_IDS else 0
                    ),
                    "writes_this_run": execution.get("writes", 0),
                    "skipped_reason": execution.get("skipped_reason"),
                }
            )

    # Second pass: re-render every narrative with the whole block visible.
    # Rule 8's line cites Rule 7's and Rule 2's findings by name and number,
    # and Rule 1's closing clause depends on whether Rule 2 formed a network
    # from that same pair — none of which exists while the block is still
    # being assembled in rule-number order. rule_inference.render_block does
    # that entirely in memory over rows already fetched: no extra queries, no
    # change to any .cypher file, and rewording stays a one-file concern.
    rule_inference.render_block(block)

    fired_count = sum(1 for entry in block if entry["fired"])
    rejected_count = sum(entry["rejected_count"] for entry in block)
    logger.info(
        "rules_fired: case_id=%s subject_id=%s %d/%d rules fired, "
        "%d rejected instance(s) retained for revert",
        scope["case_id"],
        scope["primary_subject_id"],
        fired_count,
        len(block),
        rejected_count,
    )
    return block