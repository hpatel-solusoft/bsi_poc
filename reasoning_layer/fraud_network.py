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
from reasoning_layer.rule_inference import display_name as _subject_display_name
from reasoning_layer.rule_inference import format_address
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
#
# IS_CO_SUBJECT_WITH is included alongside the three SHARES_* types: it is
# the structural edge Rule 9 (CheckSplit) networks are built on
# (graph_sync._Q_CO_SUBJECTS / rule_09's own MATCH), exactly the same way
# SHARES_EMPLOYER_WITH underlies Rule 2. Leaving it out meant a CheckSplit
# network rendered two nodes with no line between them — the investigator
# sees a "network" that visually isn't one. It carries no r.status
# property (it is an asserted fact from the Workfolder's Subject Role
# field, never an inference an investigator rejects), so the query below
# coalesces to "active" rather than requiring the property to exist.
_STRUCTURAL_EDGE_TYPES = (
    "SHARES_EMPLOYER_WITH|SHARES_ADDRESS_WITH|SHARES_ALIAS_PATTERN_WITH|IS_CO_SUBJECT_WITH"
)

# Friendly, investigator-facing text for each structural edge type — same
# spirit as rule_inference._RULE_LABELS/_RULE_DISPLAY_NAMES (machine key
# kept on the contract for the reject flow, human label added alongside
# it rather than replacing it).
_EDGE_RELATIONSHIP_LABELS: Dict[str, str] = {
    "SHARES_EMPLOYER_WITH": "Shares employer with",
    "SHARES_ADDRESS_WITH": "Shares address with",
    "SHARES_ALIAS_PATTERN_WITH": "Shares alias pattern with",
    "IS_CO_SUBJECT_WITH": "Co-subject on this case with",
}

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
       // Type-specific descriptive properties each network-forming rule
       // (02/04/06/09) writes onto its :FraudNetwork node — the raw
       // material for a human-readable network label/reason, since
       // network_key alone (a FEIN, an address_key, ...) is exactly the
       // kind of internal identifier an investigator should not have to
       // read. network_key itself is left untouched: /reject_inference
       // builds its to_key from "<network_type>:<network_key>" verbatim.
       n.employer_name AS employer_name,
       n.employer_fein AS employer_fein,
       n.address_street AS address_street,
       n.address_city AS address_city,
       n.alias_value AS alias_value,
       n.case_id AS network_case_id,
       n.wage_link_verified AS wage_link_verified,
       n.evidence_basis AS evidence_basis,
       collect({
           subject_id: member.subject_id,
           first_name: member.first_name,
           last_name: member.last_name,
           confidence: mm.confidence,
           status: mm.status,
           source_rule: mm.source_rule,
           // Rule 2 records WHICH shared allegation type put this pair
           // together (matched_allegation_type); Rule 9 records whether
           // the wage-record leg was confirmed. Both are null for the
           // rules that don't set them — surfaced only when present.
           allegation_type: mm.allegation_type,
           wage_link_verified: mm.wage_link_verified,
           is_primary: member.subject_id IN case_subject_ids
       }) AS members
"""

_STRUCTURAL_EDGES_QUERY = f"""
MATCH (a:Subject)-[r:{_STRUCTURAL_EDGE_TYPES}]-(b:Subject)
WHERE a.subject_id IN $member_ids AND b.subject_id IN $member_ids
  AND a.subject_id < b.subject_id
  AND coalesce(r.status, "active") IN ["active", "rejected"]
RETURN a.subject_id AS source, b.subject_id AS target,
       type(r) AS relationship_type, r.confidence AS confidence,
       coalesce(r.status, "active") AS status, r.source_rule AS source_rule
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


def _network_label(row: Dict[str, Any]) -> str:
    """The name an investigator should actually read at the top of the
    graph — an employer's name, an address, the alias in question, or
    the originating case — never the raw network_key (a FEIN, an
    address_key, ...) that key exists purely for /reject_inference."""
    network_type = row.get("network_type")
    network_key = row.get("network_key")
    if network_type == "Employer":
        return row.get("employer_name") or (network_key and f"Employer {network_key}") or "Employer network"
    if network_type == "Address":
        addr = format_address({"street": row.get("address_street"), "city": row.get("address_city")})
        return addr or (network_key and f"Address {network_key}") or "Address network"
    if network_type == "Identity":
        alias = row.get("alias_value") or network_key
        return f'Alias "{alias}"' if alias else "Identity network"
    if network_type == "CheckSplit":
        case_ref = row.get("network_case_id") or network_key
        return f"Check-Split network (Case {case_ref})" if case_ref else "Check-Split network"
    return network_key or network_type or "Fraud network"


def _network_reason(row: Dict[str, Any], members: List[Dict[str, Any]]) -> Optional[str]:
    """One plain-English line explaining WHY this group of subjects was
    flagged — the fact that formed the network, not just the network's
    name. Section 6.2's rules each pair a structural fact (shared
    employer/address/alias) with a second, corroborating condition
    (matching allegation type, cross-case, wage records); the network
    name alone shows the first half. Investigators need the second half
    too, or the graph reads as "these people share an employer" — true,
    but not itself suspicious, and not what actually put them here."""
    network_type = row.get("network_type")
    if network_type == "Employer":
        employer = row.get("employer_name") or "the same employer"
        shared_type = next((m.get("allegation_type") for m in members if m.get("allegation_type")), None)
        if shared_type:
            return (
                f"Both subjects are linked to {employer} and each carries an active "
                f'"{shared_type}" allegation.'
            )
        return f"Both subjects are linked to {employer}."
    if network_type == "Address":
        return (
            "Both subjects share this address and each has an active allegation "
            "on a separate case."
        )
    if network_type == "Identity":
        return (
            "These subjects share a matching alias pattern, and at least one carries "
            "an active false-identity allegation."
        )
    if network_type == "CheckSplit":
        verified = row.get("wage_link_verified")
        if verified is True:
            return (
                "Co-subjects on this case with a check-splitting allegation, and "
                "confirmed to share the same employer's wage records."
            )
        if verified is False:
            return (
                "Co-subjects on this case with a check-splitting allegation; shared "
                "wage records could not be verified, so this network is capped at "
                "Medium confidence."
            )
        return "Co-subjects on this case with a check-splitting allegation."
    return None


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
              "network_key": ...,        # internal key — reject flow only, never render this
              "network_label": ...,      # investigator-facing name, e.g. "Sunrise Staffing LLC"
              "network_reason": ...,     # one line on WHY this group was flagged
              "formed_by_rule": ...,
              "confidence": "High" | "Medium" | "Unresolved",
              "nodes": [{"id": subject_id,        # internal — reject flow only, never render this
                         "display_name": ...,     # "Maria Williams" — this is what the UI shows
                         "allegation_type": ...,   # present only when the membership recorded one
                         "is_primary": bool}],
              "edges": [{"source": ..., "target": ..., "relationship_type": ...,
                         "relationship_label": ...,  # "Shares employer with" — render this, not the enum
                         "confidence": ..., "status": "active" | "rejected",
                         "source_rule": ...}],
            }
          ],
          "network_count": int,
        }

    display_name/network_label/relationship_label are additive — every
    field the previous contract exposed is still present, unchanged, so
    an existing caller keying off network_key or relationship_type does
    not break. They exist because subject_id and network_key are internal
    identifiers /reject_inference needs, not what an investigator should
    be shown; a UI built against this contract should render the *_label/
    *_name/display_name fields and treat id/network_key/relationship_type
    as opaque keys carried only for the per-edge Reject action.

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
                "display_name": _subject_display_name(
                    m.get("first_name"), m.get("last_name"), m["subject_id"]
                ),
                "is_primary": bool(m["is_primary"]),
                **({"allegation_type": m["allegation_type"]} if m.get("allegation_type") else {}),
            }
            for m in members
        ]
        edges = [
            {
                "source": e["source"],
                "target": e["target"],
                "relationship_type": e["relationship_type"],
                "relationship_label": _EDGE_RELATIONSHIP_LABELS.get(
                    e["relationship_type"], e["relationship_type"]
                ),
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
            "network_label": _network_label(row),
            "network_reason": _network_reason(row, members),
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