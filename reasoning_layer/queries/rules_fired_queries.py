"""
Owns: the per-rule Cypher query templates that reasoning_layer/rules_fired.py's
build_rules_fired uses to fetch each rule's fired/rejected instances from
Neo4j — REL_RULES for relationship-asserting rules (Rule_01 etc., an edge
between two Subjects), PROP_RULES for property-writing rules (Rule_08/11/12/13,
which assert onto a single node rather than creating an edge between two).

Split out of rules_fired.py verbatim (Section 6D: this file owns one thing —
query text — and nothing here decides what a fired/rejected instance IS or
how it gets shaped into the API contract; that logic stays in
rules_fired.py, which imports REL_RULES/PROP_RULES from here).

Does NOT own: rule EXECUTION (reasoning_layer/rule_engine.py writes these
edges/properties in the first place) or rule CONTENT/business meaning
(reasoning_layer/rule_registry.py). This module only holds the read-side
query text used to report on what rule_engine.py already wrote.
"""

from typing import Dict

REL_RULES: Dict[str, str] = {
    "Rule_01_Shared_Employer": """
        MATCH (a:Subject)-[r:SHARES_EMPLOYER_WITH]-(b:Subject)
        WHERE (a.subject_id = $subject_id OR b.subject_id = $subject_id) AND a.subject_id < b.subject_id
          AND r.source_rule = "Rule_01_Shared_Employer"
          AND coalesce(r.status, "active") IN ["active", "rejected"]
        OPTIONAL MATCH (a)-[:EMPLOYED_BY]->(e:Employer)<-[:EMPLOYED_BY]-(b)
        WITH a, b, r, head(collect({employer_name: e.employer_name, name: e.name, fein: e.fein})) AS emp
        RETURN a.subject_id AS subject_id, a.first_name AS first_name, a.last_name AS last_name,
               b.subject_id AS related_subject_id, b.first_name AS related_first_name,
               b.last_name AS related_last_name,
               r.confidence AS confidence, coalesce(r.corroborated, false) AS corroborated,
               coalesce(r.status, "active") AS status, toString(r.asserted_at) AS asserted_at,
               {rejected_by: r.rejected_by, rejected_at: r.rejected_at,
                reason: r.rejection_reason, reverted_by: r.reverted_by,
                reverted_at: r.reverted_at, revert_reason: r.revert_reason} AS rejection,
               {employer_name: coalesce(emp.employer_name, emp.name), fein: coalesce(emp.fein, r.fein)} AS detail
        ORDER BY subject_id, related_subject_id
""",
    "Rule_03_Shared_Address": """
        MATCH (a:Subject)-[r:SHARES_ADDRESS_WITH]-(b:Subject)
        WHERE (a.subject_id = $subject_id OR b.subject_id = $subject_id) AND a.subject_id < b.subject_id
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
        WHERE (a.subject_id = $subject_id OR b.subject_id = $subject_id) AND a.subject_id < b.subject_id
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
                       {alias_pattern: coalesce(r.alias_pattern, r.match_basis),
                    alias_value: coalesce(r.alias_value, r.alias_pattern, r.match_basis)} AS detail
        ORDER BY subject_id, related_subject_id
""",
    "Rule_10_Merged_Case_Propagation": """
        MATCH (a:Subject)-[r:APPEARS_IN_CASE]->(c:Case)
        WHERE a.subject_id = $subject_id
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
        WHERE a.subject_id = $subject_id
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
             // BUG FIX: this used to filter to
             // `WHERE coalesce(rel.status, "active") = "rejected"` --
             // meaning `rejection` came back null the instant status
             // flipped back to "active" (a revert, or a cascade
             // reinstate), silently discarding the very reverted_by/
             // revert_reason/reverted_at (or reinstated_by_rule_id/
             // reinstated_reason/reinstated_at) fields that same status
             // flip had just written. Filtering on "does this edge have
             // ANY audit trail at all" instead of "is it CURRENTLY
             // rejected" is what makes revert/reinstate history survive
             // being looked at again after the fact, exactly the way
             // Rule_01/03/05's own unconditional per-row `rejection`
             // map already does (those never filtered by status to
             // begin with -- only this aggregated, one-row-per-network
             // shape did).
             head([rel IN scope_rels
                   WHERE rel.rejected_by IS NOT NULL OR rel.reverted_by IS NOT NULL
                      OR rel.invalidated_by_rule_id IS NOT NULL OR rel.reinstated_by_rule_id IS NOT NULL |
                   // rel.rejected_at is ONE physical property shared by a
                   // genuine manual reject AND an auto-invalidate cascade
                   // write (rel.auto_invalidated is what tells them apart --
                   // see rule_audit.py's identical pattern/comment for this
                   // same relationship type). Exposing it unconditionally as
                   // both a bare `rejected_at` and an unconditional
                   // `invalidated_at` let a pure-cascade write (rejected_by
                   // never set) masquerade as a manual reject downstream.
                   // Each is now gated on the field that actually signals
                   // ITS kind of event, matching rule_audit.py's own guard.
                   {rejected_by: rel.rejected_by,
                    rejected_at: (CASE WHEN rel.rejected_by IS NOT NULL THEN rel.rejected_at ELSE null END),
                    reason: rel.rejection_reason, reverted_by: rel.reverted_by,
                    reverted_at: rel.reverted_at, revert_reason: rel.revert_reason,
                    auto_invalidated: rel.auto_invalidated,
                    invalidated_by_rule_id: rel.invalidated_by_rule_id,
                    invalidated_reason: rel.invalidated_reason,
                    invalidated_by_investigator: rel.invalidated_by_investigator_id,
                    invalidated_at: (CASE WHEN rel.auto_invalidated = true THEN rel.rejected_at ELSE null END),
                    reinstated_by_rule_id: rel.reinstated_by_rule_id,
                    reinstated_reason: rel.reinstated_reason,
                    reinstated_by_investigator: rel.reinstated_by_investigator_id,
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
        // Each member carries its OWN reject/revert/cascade audit trail off
        // its own `mm` edge -- this is a per-member fact, distinct from the
        // network-level `rejection` computed above (which only reflects
        // whichever scope-subject's `r` edge happened to carry audit data).
        // Same "any audit field at all, not just current status" test as
        // the network-level `rejection` above, for the same reason: a
        // revert/reinstate on THIS member's edge must survive being looked
        // at again after the member's own status has flipped back to
        // "active". See build_member_view (reasoning_layer/rules_fired_view.py)
        // for where this is consumed.
        WITH a, n, confidence, corroborated, status, asserted_at_raw, rejection, collect(DISTINCT {
                 subject_id: m.subject_id, first_name: m.first_name, last_name: m.last_name,
                 complaint_no: mctx.complaint_no, allegation_type: mctx.allegation_type,
                 status: coalesce(mm.status, "active"),
                 // Same shared-property gating as the network-level
                 // `rejection` map above -- mm.rejected_at is written for
                 // both a manual reject and a cascade auto-invalidate of
                 // THIS member's own edge, so each side is gated on the
                 // field that actually signals its kind of event.
                 rejection: CASE WHEN mm.rejected_by IS NOT NULL OR mm.reverted_by IS NOT NULL
                                    OR mm.invalidated_by_rule_id IS NOT NULL OR mm.reinstated_by_rule_id IS NOT NULL
                              THEN {rejected_by: mm.rejected_by,
                                    rejected_at: (CASE WHEN mm.rejected_by IS NOT NULL THEN mm.rejected_at ELSE null END),
                                    reason: mm.rejection_reason, reverted_by: mm.reverted_by,
                                    reverted_at: mm.reverted_at, revert_reason: mm.revert_reason,
                                    auto_invalidated: mm.auto_invalidated,
                                    invalidated_by_rule_id: mm.invalidated_by_rule_id,
                                    invalidated_reason: mm.invalidated_reason,
                                    invalidated_by_investigator: mm.invalidated_by_investigator_id,
                                    invalidated_at: (CASE WHEN mm.auto_invalidated = true THEN mm.rejected_at ELSE null END),
                                    reinstated_by_rule_id: mm.reinstated_by_rule_id,
                                    reinstated_reason: mm.reinstated_reason,
                                    reinstated_by_investigator: mm.reinstated_by_investigator_id,
                                    reinstated_at: mm.reinstated_at}
                              ELSE null END
             }) AS members_raw
        RETURN a.subject_id AS subject_id, a.first_name AS first_name, a.last_name AS last_name,
               n.network_key AS related_network_key,
               confidence AS confidence, corroborated AS corroborated,
               status AS status, toString(asserted_at_raw) AS asserted_at, rejection AS rejection,
               {network_type: n.network_type, network_key: n.network_key,
                formed_by_rule: n.formed_by_rule,
                // Read straight off the :FraudNetwork node -- rule_02's own
                // Cypher writes this ON CREATE from the :Employer node it
                // matched (e.employer_name), never hardcoded. Lets the
                // grouped-instance title (rules_fired_view.py's
                // build_grouped_instance_view) show the employer's NAME
                // next to the FEIN that network_key already carries,
                // instead of the FEIN standing in alone for the employer.
                employer_name: n.employer_name,
                members: [x IN members_raw WHERE x.subject_id IS NOT NULL]} AS detail
        ORDER BY related_network_key
""",
    # WHERE a.subject_id = $subject_id
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
             // BUG FIX: this used to filter to
             // `WHERE coalesce(rel.status, "active") = "rejected"` --
             // meaning `rejection` came back null the instant status
             // flipped back to "active" (a revert, or a cascade
             // reinstate), silently discarding the very reverted_by/
             // revert_reason/reverted_at (or reinstated_by_rule_id/
             // reinstated_reason/reinstated_at) fields that same status
             // flip had just written. Filtering on "does this edge have
             // ANY audit trail at all" instead of "is it CURRENTLY
             // rejected" is what makes revert/reinstate history survive
             // being looked at again after the fact, exactly the way
             // Rule_01/03/05's own unconditional per-row `rejection`
             // map already does (those never filtered by status to
             // begin with -- only this aggregated, one-row-per-network
             // shape did).
             head([rel IN scope_rels
                   WHERE rel.rejected_by IS NOT NULL OR rel.reverted_by IS NOT NULL
                      OR rel.invalidated_by_rule_id IS NOT NULL OR rel.reinstated_by_rule_id IS NOT NULL |
                   // rel.rejected_at is ONE physical property shared by a
                   // genuine manual reject AND an auto-invalidate cascade
                   // write (rel.auto_invalidated is what tells them apart --
                   // see rule_audit.py's identical pattern/comment for this
                   // same relationship type). Exposing it unconditionally as
                   // both a bare `rejected_at` and an unconditional
                   // `invalidated_at` let a pure-cascade write (rejected_by
                   // never set) masquerade as a manual reject downstream.
                   // Each is now gated on the field that actually signals
                   // ITS kind of event, matching rule_audit.py's own guard.
                   {rejected_by: rel.rejected_by,
                    rejected_at: (CASE WHEN rel.rejected_by IS NOT NULL THEN rel.rejected_at ELSE null END),
                    reason: rel.rejection_reason, reverted_by: rel.reverted_by,
                    reverted_at: rel.reverted_at, revert_reason: rel.revert_reason,
                    auto_invalidated: rel.auto_invalidated,
                    invalidated_by_rule_id: rel.invalidated_by_rule_id,
                    invalidated_reason: rel.invalidated_reason,
                    invalidated_by_investigator: rel.invalidated_by_investigator_id,
                    invalidated_at: (CASE WHEN rel.auto_invalidated = true THEN rel.rejected_at ELSE null END),
                    reinstated_by_rule_id: rel.reinstated_by_rule_id,
                    reinstated_reason: rel.reinstated_reason,
                    reinstated_by_investigator: rel.reinstated_by_investigator_id,
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
        // Each member carries its OWN reject/revert/cascade audit trail off
        // its own `mm` edge -- this is a per-member fact, distinct from the
        // network-level `rejection` computed above (which only reflects
        // whichever scope-subject's `r` edge happened to carry audit data).
        // Same "any audit field at all, not just current status" test as
        // the network-level `rejection` above, for the same reason: a
        // revert/reinstate on THIS member's edge must survive being looked
        // at again after the member's own status has flipped back to
        // "active". See build_member_view (reasoning_layer/rules_fired_view.py)
        // for where this is consumed.
        WITH a, n, confidence, corroborated, status, asserted_at_raw, rejection, collect(DISTINCT {
                 subject_id: m.subject_id, first_name: m.first_name, last_name: m.last_name,
                 complaint_no: mctx.complaint_no, allegation_type: mctx.allegation_type,
                 status: coalesce(mm.status, "active"),
                 // Same shared-property gating as the network-level
                 // `rejection` map above -- mm.rejected_at is written for
                 // both a manual reject and a cascade auto-invalidate of
                 // THIS member's own edge, so each side is gated on the
                 // field that actually signals its kind of event.
                 rejection: CASE WHEN mm.rejected_by IS NOT NULL OR mm.reverted_by IS NOT NULL
                                    OR mm.invalidated_by_rule_id IS NOT NULL OR mm.reinstated_by_rule_id IS NOT NULL
                              THEN {rejected_by: mm.rejected_by,
                                    rejected_at: (CASE WHEN mm.rejected_by IS NOT NULL THEN mm.rejected_at ELSE null END),
                                    reason: mm.rejection_reason, reverted_by: mm.reverted_by,
                                    reverted_at: mm.reverted_at, revert_reason: mm.revert_reason,
                                    auto_invalidated: mm.auto_invalidated,
                                    invalidated_by_rule_id: mm.invalidated_by_rule_id,
                                    invalidated_reason: mm.invalidated_reason,
                                    invalidated_by_investigator: mm.invalidated_by_investigator_id,
                                    invalidated_at: (CASE WHEN mm.auto_invalidated = true THEN mm.rejected_at ELSE null END),
                                    reinstated_by_rule_id: mm.reinstated_by_rule_id,
                                    reinstated_reason: mm.reinstated_reason,
                                    reinstated_by_investigator: mm.reinstated_by_investigator_id,
                                    reinstated_at: mm.reinstated_at}
                              ELSE null END
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
        WHERE a.subject_id = $subject_id
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
             // BUG FIX: this used to filter to
             // `WHERE coalesce(rel.status, "active") = "rejected"` --
             // meaning `rejection` came back null the instant status
             // flipped back to "active" (a revert, or a cascade
             // reinstate), silently discarding the very reverted_by/
             // revert_reason/reverted_at (or reinstated_by_rule_id/
             // reinstated_reason/reinstated_at) fields that same status
             // flip had just written. Filtering on "does this edge have
             // ANY audit trail at all" instead of "is it CURRENTLY
             // rejected" is what makes revert/reinstate history survive
             // being looked at again after the fact, exactly the way
             // Rule_01/03/05's own unconditional per-row `rejection`
             // map already does (those never filtered by status to
             // begin with -- only this aggregated, one-row-per-network
             // shape did).
             head([rel IN scope_rels
                   WHERE rel.rejected_by IS NOT NULL OR rel.reverted_by IS NOT NULL
                      OR rel.invalidated_by_rule_id IS NOT NULL OR rel.reinstated_by_rule_id IS NOT NULL |
                   // rel.rejected_at is ONE physical property shared by a
                   // genuine manual reject AND an auto-invalidate cascade
                   // write (rel.auto_invalidated is what tells them apart --
                   // see rule_audit.py's identical pattern/comment for this
                   // same relationship type). Exposing it unconditionally as
                   // both a bare `rejected_at` and an unconditional
                   // `invalidated_at` let a pure-cascade write (rejected_by
                   // never set) masquerade as a manual reject downstream.
                   // Each is now gated on the field that actually signals
                   // ITS kind of event, matching rule_audit.py's own guard.
                   {rejected_by: rel.rejected_by,
                    rejected_at: (CASE WHEN rel.rejected_by IS NOT NULL THEN rel.rejected_at ELSE null END),
                    reason: rel.rejection_reason, reverted_by: rel.reverted_by,
                    reverted_at: rel.reverted_at, revert_reason: rel.revert_reason,
                    auto_invalidated: rel.auto_invalidated,
                    invalidated_by_rule_id: rel.invalidated_by_rule_id,
                    invalidated_reason: rel.invalidated_reason,
                    invalidated_by_investigator: rel.invalidated_by_investigator_id,
                    invalidated_at: (CASE WHEN rel.auto_invalidated = true THEN rel.rejected_at ELSE null END),
                    reinstated_by_rule_id: rel.reinstated_by_rule_id,
                    reinstated_reason: rel.reinstated_reason,
                    reinstated_by_investigator: rel.reinstated_by_investigator_id,
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
        // Each member carries its OWN reject/revert/cascade audit trail off
        // its own `mm` edge -- this is a per-member fact, distinct from the
        // network-level `rejection` computed above (which only reflects
        // whichever scope-subject's `r` edge happened to carry audit data).
        // Same "any audit field at all, not just current status" test as
        // the network-level `rejection` above, for the same reason: a
        // revert/reinstate on THIS member's edge must survive being looked
        // at again after the member's own status has flipped back to
        // "active". See build_member_view (reasoning_layer/rules_fired_view.py)
        // for where this is consumed.
        WITH a, n, confidence, corroborated, status, asserted_at_raw, rejection, collect(DISTINCT {
                 subject_id: m.subject_id, first_name: m.first_name, last_name: m.last_name,
                 complaint_no: mctx.complaint_no, allegation_type: mctx.allegation_type,
                 status: coalesce(mm.status, "active"),
                 // Same shared-property gating as the network-level
                 // `rejection` map above -- mm.rejected_at is written for
                 // both a manual reject and a cascade auto-invalidate of
                 // THIS member's own edge, so each side is gated on the
                 // field that actually signals its kind of event.
                 rejection: CASE WHEN mm.rejected_by IS NOT NULL OR mm.reverted_by IS NOT NULL
                                    OR mm.invalidated_by_rule_id IS NOT NULL OR mm.reinstated_by_rule_id IS NOT NULL
                              THEN {rejected_by: mm.rejected_by,
                                    rejected_at: (CASE WHEN mm.rejected_by IS NOT NULL THEN mm.rejected_at ELSE null END),
                                    reason: mm.rejection_reason, reverted_by: mm.reverted_by,
                                    reverted_at: mm.reverted_at, revert_reason: mm.revert_reason,
                                    auto_invalidated: mm.auto_invalidated,
                                    invalidated_by_rule_id: mm.invalidated_by_rule_id,
                                    invalidated_reason: mm.invalidated_reason,
                                    invalidated_by_investigator: mm.invalidated_by_investigator_id,
                                    invalidated_at: (CASE WHEN mm.auto_invalidated = true THEN mm.rejected_at ELSE null END),
                                    reinstated_by_rule_id: mm.reinstated_by_rule_id,
                                    reinstated_reason: mm.reinstated_reason,
                                    reinstated_by_investigator: mm.reinstated_by_investigator_id,
                                    reinstated_at: mm.reinstated_at}
                              ELSE null END
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
        WHERE a.subject_id = $subject_id
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
             // BUG FIX: this used to filter to
             // `WHERE coalesce(rel.status, "active") = "rejected"` --
             // meaning `rejection` came back null the instant status
             // flipped back to "active" (a revert, or a cascade
             // reinstate), silently discarding the very reverted_by/
             // revert_reason/reverted_at (or reinstated_by_rule_id/
             // reinstated_reason/reinstated_at) fields that same status
             // flip had just written. Filtering on "does this edge have
             // ANY audit trail at all" instead of "is it CURRENTLY
             // rejected" is what makes revert/reinstate history survive
             // being looked at again after the fact, exactly the way
             // Rule_01/03/05's own unconditional per-row `rejection`
             // map already does (those never filtered by status to
             // begin with -- only this aggregated, one-row-per-network
             // shape did).
             head([rel IN scope_rels
                   WHERE rel.rejected_by IS NOT NULL OR rel.reverted_by IS NOT NULL
                      OR rel.invalidated_by_rule_id IS NOT NULL OR rel.reinstated_by_rule_id IS NOT NULL |
                   // rel.rejected_at is ONE physical property shared by a
                   // genuine manual reject AND an auto-invalidate cascade
                   // write (rel.auto_invalidated is what tells them apart --
                   // see rule_audit.py's identical pattern/comment for this
                   // same relationship type). Exposing it unconditionally as
                   // both a bare `rejected_at` and an unconditional
                   // `invalidated_at` let a pure-cascade write (rejected_by
                   // never set) masquerade as a manual reject downstream.
                   // Each is now gated on the field that actually signals
                   // ITS kind of event, matching rule_audit.py's own guard.
                   {rejected_by: rel.rejected_by,
                    rejected_at: (CASE WHEN rel.rejected_by IS NOT NULL THEN rel.rejected_at ELSE null END),
                    reason: rel.rejection_reason, reverted_by: rel.reverted_by,
                    reverted_at: rel.reverted_at, revert_reason: rel.revert_reason,
                    auto_invalidated: rel.auto_invalidated,
                    invalidated_by_rule_id: rel.invalidated_by_rule_id,
                    invalidated_reason: rel.invalidated_reason,
                    invalidated_by_investigator: rel.invalidated_by_investigator_id,
                    invalidated_at: (CASE WHEN rel.auto_invalidated = true THEN rel.rejected_at ELSE null END),
                    reinstated_by_rule_id: rel.reinstated_by_rule_id,
                    reinstated_reason: rel.reinstated_reason,
                    reinstated_by_investigator: rel.reinstated_by_investigator_id,
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
        // Each member carries its OWN reject/revert/cascade audit trail off
        // its own `mm` edge -- this is a per-member fact, distinct from the
        // network-level `rejection` computed above (which only reflects
        // whichever scope-subject's `r` edge happened to carry audit data).
        // Same "any audit field at all, not just current status" test as
        // the network-level `rejection` above, for the same reason: a
        // revert/reinstate on THIS member's edge must survive being looked
        // at again after the member's own status has flipped back to
        // "active". See build_member_view (reasoning_layer/rules_fired_view.py)
        // for where this is consumed.
        WITH a, n, confidence, corroborated, status, asserted_at_raw, rejection, collect(DISTINCT {
                 subject_id: m.subject_id, first_name: m.first_name, last_name: m.last_name,
                 complaint_no: mctx.complaint_no, allegation_type: mctx.allegation_type,
                 status: coalesce(mm.status, "active"),
                 // Same shared-property gating as the network-level
                 // `rejection` map above -- mm.rejected_at is written for
                 // both a manual reject and a cascade auto-invalidate of
                 // THIS member's own edge, so each side is gated on the
                 // field that actually signals its kind of event.
                 rejection: CASE WHEN mm.rejected_by IS NOT NULL OR mm.reverted_by IS NOT NULL
                                    OR mm.invalidated_by_rule_id IS NOT NULL OR mm.reinstated_by_rule_id IS NOT NULL
                              THEN {rejected_by: mm.rejected_by,
                                    rejected_at: (CASE WHEN mm.rejected_by IS NOT NULL THEN mm.rejected_at ELSE null END),
                                    reason: mm.rejection_reason, reverted_by: mm.reverted_by,
                                    reverted_at: mm.reverted_at, revert_reason: mm.revert_reason,
                                    auto_invalidated: mm.auto_invalidated,
                                    invalidated_by_rule_id: mm.invalidated_by_rule_id,
                                    invalidated_reason: mm.invalidated_reason,
                                    invalidated_by_investigator: mm.invalidated_by_investigator_id,
                                    invalidated_at: (CASE WHEN mm.auto_invalidated = true THEN mm.rejected_at ELSE null END),
                                    reinstated_by_rule_id: mm.reinstated_by_rule_id,
                                    reinstated_reason: mm.reinstated_reason,
                                    reinstated_by_investigator: mm.reinstated_by_investigator_id,
                                    reinstated_at: mm.reinstated_at}
                              ELSE null END
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
        WHERE a.subject_id = $subject_id
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
        WHERE a.subject_id = $subject_id
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
PROP_RULES: Dict[str, str] = {
    "Rule_11_Cross_Case_Hub": """
        MATCH (a:Subject)
        WHERE a.subject_id = $subject_id
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
        // Same first_name/last_name lookup Rule_07's query already does off
        // its own matched Subject node `a` — this rule instead asserts onto
        // the :Case (see the module's "Property-writing rules" comment
        // above _PROP_RULES), so the Subject has to be looked up separately
        // by the id the escalation already carries, rather than falling
        // out of the same MATCH for free. Without this, _instance()/
        // enrich_instance() (reasoning_layer/rule_inference.py) have no
        // first_name/last_name to build subject_name from and display_name
        // falls back to the bare subject_id, so an investigator saw
        // "658636801" as the title where every other single-render rule
        // (7, 11, 12) shows a name.
        OPTIONAL MATCH (a:Subject {subject_id: c.risk_escalation_subject_id})
        RETURN c.risk_escalation_subject_id AS subject_id, a.first_name AS first_name, a.last_name AS last_name,
               c.case_id AS related_case_id,
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
                invalidated_by_investigator: c.risk_escalation_invalidated_by_investigator_id,
                invalidated_at: c.risk_escalation_invalidated_at,
                reinstated_by_rule_id: c.risk_escalation_reinstated_by_rule_id,
                reinstated_reason: c.risk_escalation_reinstated_reason,
                reinstated_by_investigator: c.risk_escalation_reinstated_by_investigator_id,
                reinstated_at: c.risk_escalation_reinstated_at} AS rejection,
               {complaint_no: c.complaint_number, fraud_amount: c.fraud_amount} AS detail
        ORDER BY related_case_id
""",
    "Rule_12_SLAM_Wage_Corroboration": """
        MATCH (c:Case)-[:HAS_ALLEGATION]->(al:Allegation)-[att:ALLEGATION_LIKELY_AGAINST_SUBJECT]->(a:Subject)
        WHERE a.subject_id = $subject_id
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
        // Same reasoning as Rule_08's own comment just above: this rule
        // asserts onto the :Case, not a Subject, so first_name/last_name
        // have to be looked up separately off the bound $subject_id param
        // (scope["primary_subject_id"], per build_rules_fired's own
        // comment on that parameter) rather than falling out of a Subject
        // MATCH this query never has. Without it, display_name() has
        // nothing but the bare id to fall back to.
        OPTIONAL MATCH (a:Subject {subject_id: $subject_id})
        RETURN $subject_id AS subject_id, a.first_name AS first_name, a.last_name AS last_name,
               c.case_id AS related_case_id,
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
                invalidated_by_investigator: c.fasttrack_recommendation_invalidated_by_investigator_id,
                invalidated_at: c.fasttrack_recommendation_invalidated_at,
                reinstated_by_rule_id: c.fasttrack_recommendation_reinstated_by_rule_id,
                reinstated_reason: c.fasttrack_recommendation_reinstated_reason,
                reinstated_by_investigator: c.fasttrack_recommendation_reinstated_by_investigator_id,
                reinstated_at: c.fasttrack_recommendation_reinstated_at} AS rejection,
               {complaint_no: c.complaint_number, fraud_amount: c.fraud_amount} AS detail
        ORDER BY related_case_id
""",
}
