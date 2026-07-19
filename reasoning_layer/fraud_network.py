"""
Owns: assembling the Fraud Network Graph read (Functional Specification
D3, GET /fraud_network/{case_id}) — the structured node/edge JSON the
frontend's D3.js/Cytoscape.js visualisation renders (Developer
Specification Section 8.1), and the same data the UI's per-edge
"Reject" button reads its rule_id/subject_id_a/subject_id_b/
relationship_type parameters from before calling POST /reject_inference.

Read-only. No writes to Neo4j or AppWorks (D3 Key Design Rule). Never
triggers the reasoning pipeline — same decoupling principle
graph_queries.check_network_match already establishes for AI-12.

WHY THIS DOES NOT USE SECTION 8.1'S WORKED CYPHER VERBATIM:
    MATCH (n:FraudNetwork {case_id: $case_id})<-[:MEMBER_OF_FRAUD_NETWORK]-(s:Subject)
That query assumes every :FraudNetwork node carries a case_id property.
It does not: rules/wave2/rule_09_pca_checksplit.cypher sets
network.case_id (CheckSplit is inherently one-case), but
rule_02/rule_04/rule_06 (Employer/Address/Identity) do not — those
networks are keyed on employer_key / address_key / alias_value, which
can span multiple cases by design (that is the point of a fraud
network: it is not case-scoped). Section 6.2 flags its own worked
examples as "first-draft illustrations, not final production code,"
and this is exactly the gap that note anticipates. This module instead
resolves "networks relevant to this case" via the case's actual
Subjects (APPEARS_IN_CASE), which is correct for all four network
types without a schema change.

Does NOT own: the reasoning pipeline, rule content, or any write —
rejection.py is the only module with write access to :Rejection or to
any inferred fact's status field.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from reasoning_layer.neo4j_client import get_session
from utils.provenance import graph_provenance

logger = logging.getLogger(__name__)

# Confidence ranking used to pick one representative network-level
# confidence when a network's members disagree (e.g. one membership
# edge active at High, another rejected at Medium) — mirrors
# rules_fired.py's own _CONFIDENCE_ORDER so the two stay consistent.
_CONFIDENCE_ORDER = {"Unresolved": 0, "Medium": 1, "High": 2}

# Direct, subject-to-subject structural relationship types this screen
# draws as edges. MEMBER_OF_FRAUD_NETWORK itself is deliberately NOT an
# edge type here — Section 8.1's contract defines edges as "relationships
# BETWEEN SUBJECTS"; a subject's membership is instead what groups nodes
# into one network block, matching the output contract's nodes[]/edges[]
# shape (network membership is structure, not a rendered edge).
_STRUCTURAL_EDGE_TYPES = "SHARES_EMPLOYER_WITH|SHARES_ADDRESS_WITH|SHARES_ALIAS_PATTERN_WITH"

_CASE_NETWORKS_QUERY = """
MATCH (cs:Subject)-[:APPEARS_IN_CASE]->(:Case {case_id: $case_id})
WITH collect(DISTINCT cs.subject_id) AS case_subject_ids
UNWIND case_subject_ids AS csid
MATCH (member_of_case:Subject {subject_id: csid})-[:MEMBER_OF_FRAUD_NETWORK]->(n:FraudNetwork)
WITH DISTINCT n, case_subject_ids
MATCH (member:Subject)-[mm:MEMBER_OF_FRAUD_NETWORK]->(n)
WHERE mm.status IN ["active", "rejected"]
RETURN elementId(n) AS network_ref,
       n.network_type AS network_type,
       n.network_key AS network_key,
       n.formed_by_rule AS formed_by_rule,
       collect({
           subject_id: member.subject_id,
           display_name: coalesce(member.full_name, member.name, member.subject_id),
           confidence: mm.confidence,
           status: mm.status,
           source_rule: mm.source_rule,
           is_primary: member.subject_id IN case_subject_ids
       }) AS members
"""

_STRUCTURAL_EDGES_QUERY = f"""
MATCH (a:Subject)-[r:{_STRUCTURAL_EDGE_TYPES}]-(b:Subject)
WHERE a.subject_id IN $member_ids AND b.subject_id IN $member_ids
  AND a.subject_id < b.subject_id
  AND r.status IN ["active", "rejected"]
RETURN a.subject_id AS source, b.subject_id AS target,
       type(r) AS relationship_type, r.confidence AS confidence,
       r.status AS status, r.source_rule AS source_rule
"""


def _envelope(result: Dict[str, Any]) -> dict:
    return {
        "result": result,
        "provenance": graph_provenance("reasoning_layer.fraud_network.get_fraud_network"),
    }


def _network_confidence(members: List[Dict[str, Any]]) -> str:
    """The network-level confidence Section D3's output contract asks
    for (network.confidence) — the strongest confidence among ACTIVE
    memberships, falling back to the strongest rejected one if every
    membership has been rejected, so a fully-rejected network still
    reports what it used to claim rather than a meaningless default."""
    active = [m["confidence"] for m in members if m["status"] == "active" and m["confidence"]]
    pool = active or [m["confidence"] for m in members if m["confidence"]]
    if not pool:
        return "Unresolved"
    return max(pool, key=lambda c: _CONFIDENCE_ORDER.get(c, 0))


def get_fraud_network(case_id: str) -> dict:
    """
    Assemble the Fraud Network Graph for one case (D3 / Section 8.1).

    Args:
        case_id: required, non-empty.

    Returns (inside the standard {result, provenance} envelope):
        {
          "case_id": ...,
          "networks": [
            {
              "network_type": "Employer" | "Address" | "Identity" | "CheckSplit",
              "network_key": ...,
              "formed_by_rule": ...,
              "confidence": "High" | "Medium" | "Unresolved",
              "nodes": [{"id": subject_id, "display_name": ..., "is_primary": bool}],
              "edges": [{"source": ..., "target": ..., "relationship_type": ...,
                         "confidence": ..., "status": "active" | "rejected",
                         "source_rule": ...}],
            }
          ],
          "network_count": int,
        }

    A case with no fraud-network membership at all (the common case —
    most cases never trip Rules 2/4/6/9) is not an error: networks is
    empty. Rejected edges are always included with status "rejected" —
    the frontend renders those with dashed styling per D3's Key Design
    Rule; this function never filters them out.

    Raises:
        ValueError: case_id missing or blank.
        GraphUnavailableError / Neo4jError: propagated unchanged.
    """
    if not case_id or not str(case_id).strip():
        raise ValueError("get_fraud_network requires a non-empty case_id")
    case_id = str(case_id).strip()

    with get_session() as session:
        network_rows = session.run(_CASE_NETWORKS_QUERY, case_id=case_id).data()

        member_ids = sorted({
            member["subject_id"]
            for row in network_rows
            for member in row["members"]
        })
        edge_rows = (
            session.run(_STRUCTURAL_EDGES_QUERY, member_ids=member_ids).data()
            if member_ids else []
        )

    networks: List[Dict[str, Any]] = []
    for row in network_rows:
        members = row["members"]
        member_id_set = {m["subject_id"] for m in members}
        nodes = [
            {
                "id": m["subject_id"],
                "display_name": m["display_name"],
                "is_primary": bool(m["is_primary"]),
            }
            for m in members
        ]
        edges = [
            {
                "source": e["source"],
                "target": e["target"],
                "relationship_type": e["relationship_type"],
                "confidence": e["confidence"],
                "status": e["status"],
                "source_rule": e["source_rule"],
            }
            for e in edge_rows
            if e["source"] in member_id_set and e["target"] in member_id_set
        ]
        networks.append({
            "network_type": row["network_type"],
            "network_key": row["network_key"],
            "formed_by_rule": row["formed_by_rule"],
            "confidence": _network_confidence(members),
            "nodes": nodes,
            "edges": edges,
        })

    result = {
        "case_id": case_id,
        "networks": networks,
        "network_count": len(networks),
    }
    logger.info(
        "get_fraud_network: case_id=%s networks=%d members=%d",
        case_id, len(networks), len(member_ids),
    )
    return _envelope(result)