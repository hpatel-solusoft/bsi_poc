"""
Owns: the single Cypher statement reasoning_layer/fraud_network.py's
get_fraud_network uses to pull one case's entire subgraph (subjects,
employers, addresses, allegations, financials, agencies, prior/merged
cases, and every structural/network/flag edge among them) in one round
trip.

Split out of fraud_network.py verbatim, same rationale as
reasoning_layer/rules_fired_queries.py, rule_audit_queries.py, and
report_generation_queries.py: this file owns query text only, not how
the raw rows get built into nodes/edges/networks for the D3 response —
that logic (_build_nodes, _build_edges, _build_networks, etc.) stays in
fraud_network.py, which imports CASE_SUBGRAPH_QUERY from here.

Does NOT own: rule execution (reasoning_layer/rule_engine.py) or rule
content (reasoning_layer/rule_registry.py). This module only holds the
read-side query text used to assemble the graph fraud_network.py shapes.
"""

# ---------------------------------------------------------------------
# The one Cypher statement.
#
# Written to the same rules graph_queries._NETWORK_MATCH_QUERY follows:
#   * No CALL subquery — no dependency on the 5.23+ scoped-CALL syntax
#     and no deprecation warning on older 5.x.
#   * Results are returned as scalar maps built with properties()/
#     labels()/type(), never as raw node or relationship objects with
#     chained property access.
#   * UNWIND (<list> + [null]) rather than UNWIND (<list>): a plain
#     UNWIND of an empty list annihilates the row, which would lose the
#     case_node and turn "case with no subjects" into "case not found".
#     The extra null anchor simply yields nulls through OPTIONAL MATCH.
#   * reduce(...) is used for list de-duplication because Cypher has no
#     list-union operator and collect(DISTINCT) cannot be applied to an
#     already-built list.
# ---------------------------------------------------------------------
CASE_SUBGRAPH_QUERY = """
MATCH (case_node:Case {case_id: $case_id})

// ---- 1. Subjects on this case -------------------------------------
OPTIONAL MATCH (cs:Subject)-[:APPEARS_IN_CASE]->(case_node)
WITH case_node, collect(DISTINCT cs) AS case_subjects

// ---- 2. Peer subjects, exactly one hop out ------------------------
UNWIND (case_subjects + [null]) AS anchor
OPTIONAL MATCH (anchor)-[pr]-(direct_peer:Subject)
    WHERE type(pr) IN $peer_rel_types
OPTIONAL MATCH (anchor)-[:MEMBER_OF_FRAUD_NETWORK]->(:FraudNetwork)
              <-[:MEMBER_OF_FRAUD_NETWORK]-(net_peer:Subject)
// Two collects, then concatenated in a SEPARATE projection. Combining
// them inline (collect(a) + collect(b) AS x) parses, but keeping the
// aggregation and the list arithmetic in different WITH steps is the
// form that behaves identically on every 5.x build — the same caution
// graph_queries.py applies to chained property access.
WITH case_node, case_subjects,
     collect(DISTINCT direct_peer) AS direct_peers,
     collect(DISTINCT net_peer) AS net_peers
WITH case_node, case_subjects, direct_peers + net_peers AS raw_peers
WITH case_node, case_subjects,
     reduce(acc = [], p IN raw_peers |
            CASE WHEN p IN acc OR p IN case_subjects THEN acc ELSE acc + p END
     ) AS peer_subjects
WITH case_node, case_subjects, peer_subjects,
     case_subjects + peer_subjects AS subjects

// ---- 3. Attribute nodes, from the CASE'S OWN subjects only --------
UNWIND (case_subjects + [null]) AS cs_attr
OPTIONAL MATCH (cs_attr)-[]->(attr)
    WHERE attr:Address OR attr:Alias OR attr:Employer
       OR attr:FraudNetwork OR attr:Commentary
WITH case_node, case_subjects, peer_subjects, subjects,
     collect(DISTINCT attr) AS attr_nodes

// ---- 4. Prior guilty cases of the case's own subjects -------------
UNWIND (case_subjects + [null]) AS cs_prior
OPTIONAL MATCH (cs_prior)-[:HAS_PRIOR_GUILTY_CASE]->(prior:Case)
WITH case_node, case_subjects, peer_subjects, subjects, attr_nodes,
     collect(DISTINCT prior) AS prior_cases

// ---- 5. Case-level children ---------------------------------------
OPTIONAL MATCH (case_node)-[:HAS_ALLEGATION]->(al:Allegation)
WITH case_node, case_subjects, peer_subjects, subjects, attr_nodes, prior_cases,
     collect(DISTINCT al) AS allegations

OPTIONAL MATCH (case_node)-[:HAS_COMMENTARY]->(case_comment:Commentary)
WITH case_node, case_subjects, peer_subjects, subjects, attr_nodes, prior_cases,
     allegations, collect(DISTINCT case_comment) AS case_comments

UNWIND (allegations + [null]) AS al_c
OPTIONAL MATCH (al_c)-[:HAS_COMMENTARY]->(alleg_comment:Commentary)
WITH case_node, case_subjects, peer_subjects, subjects, attr_nodes, prior_cases,
     allegations, case_comments, collect(DISTINCT alleg_comment) AS allegation_comments

OPTIONAL MATCH (case_node)-[:MERGED_INTO_CASE]-(merged:Case)
WITH case_node, case_subjects, subjects, attr_nodes, prior_cases,
     allegations, case_comments, allegation_comments,
     collect(DISTINCT merged) AS merged_cases

// ---- 6. Collapse to one de-duplicated node set --------------------
WITH case_subjects,
     [case_node] + subjects + attr_nodes + prior_cases + allegations
     + case_comments + allegation_comments + merged_cases AS raw_nodes
WITH case_subjects,
     reduce(acc = [], n IN raw_nodes |
            CASE WHEN n IN acc THEN acc ELSE acc + n END
     ) AS nodes

// ---- 7. Every relationship internal to that node set --------------
UNWIND nodes AS x
OPTIONAL MATCH (x)-[r]-(y)
    WHERE y IN nodes
WITH case_subjects, nodes, collect(DISTINCT r) AS rels

RETURN
    [n IN nodes | {
        ref:             elementId(n),
        labels:          labels(n),
        properties:      properties(n),
        is_case_subject: n IN case_subjects
    }] AS nodes,
    [r IN rels | {
        ref:        elementId(r),
        type:       type(r),
        source_ref: elementId(startNode(r)),
        target_ref: elementId(endNode(r)),
        properties: properties(r)
    }] AS relationships
"""


