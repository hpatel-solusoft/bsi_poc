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

WHAT CHANGED (full case subgraph)
    This module used to return ONLY :FraudNetwork groupings and only
    the three subject-to-subject structural edge types. That answered
    "which fraud networks touch this case?" but not "show me the case."
    An investigator opening the graph saw two floating subject ids and
    no Case, no Allegation, no Employer, no Address, no Alias, no
    Commentary — none of the evidence that made the network in the
    first place.

    get_fraud_network now returns the WHOLE case-scoped subgraph under
    result["graph"]: every node and every relationship reachable from
    the case, typed and labelled, ready to hand straight to Cytoscape.

    The legacy result["networks"] / result["network_count"] keys are
    still present and unchanged in shape, so the existing screen keeps
    working while the frontend migrates. They are now DERIVED from the
    same single subgraph read rather than from a second query, which
    also removes the old two-round-trip inconsistency window.

SCOPE OF THE TRAVERSAL (deliberately bounded, not "everything")
    An unbounded expansion from a case reaches most of the graph within
    three hops — shared employers are hubs. What is collected:

      1. The :Case itself.
      2. Its subjects            (Subject)-[:APPEARS_IN_CASE]->(Case)
      3. Peer subjects ONE hop out, via the inferred/structural
         subject-to-subject types, plus co-members of any fraud network
         a case subject belongs to.
      4. Attribute nodes hanging off the CASE'S OWN subjects only —
         Address, Alias, Employer, FraudNetwork, Commentary.
      5. Case-level children — Allegation, case Commentary, allegation
         Commentary, merged-into cases, prior guilty cases.

    Peer subjects are included as NODES but their own attributes are
    not expanded. This is the important asymmetry: a peer's link to a
    SHARED employer or address is still drawn, because that employer
    node is already in the set (it came in via step 4 from a case
    subject) and step 6 draws every relationship whose two endpoints
    are both in the set. So the investigator sees exactly why the peer
    is connected, without dragging in the peer's unrelated life.

      6. Every relationship, any type, any direction, whose BOTH
         endpoints are in the collected node set.

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
import os
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from reasoning_layer.neo4j_client import get_session
from utils.provenance import graph_provenance

logger = logging.getLogger(__name__)

# Confidence ranking used to pick one representative network-level
# confidence when a network's members disagree (e.g. one membership
# edge active at High, another rejected at Medium) — mirrors
# rules_fired.py's own _CONFIDENCE_ORDER so the two stay consistent.
_CONFIDENCE_ORDER = {"Unresolved": 0, "Medium": 1, "High": 2}

# Subject-to-subject relationship types that justify pulling a peer
# subject into the picture. The three SHARES_* types are rule-inferred
# (Rules 1/3/5); IS_CO_SUBJECT_WITH is ETL-sourced from
# Workfolder_SubjectsRelationship. MEMBER_OF_FRAUD_NETWORK is handled
# separately below because it is a two-hop path through the
# :FraudNetwork node rather than a direct subject-to-subject edge.
_PEER_REL_TYPES: Tuple[str, ...] = (
    "SHARES_EMPLOYER_WITH",
    "SHARES_ADDRESS_WITH",
    "SHARES_ALIAS_PATTERN_WITH",
    "IS_CO_SUBJECT_WITH",
)

# Direct, subject-to-subject structural relationship types the LEGACY
# networks[] blocks draw as edges. MEMBER_OF_FRAUD_NETWORK itself is
# deliberately NOT an edge type there — Section 8.1's contract defines
# those edges as "relationships BETWEEN SUBJECTS"; a subject's
# membership is instead what groups nodes into one network block.
#
# result["graph"] has no such restriction: it carries the membership
# edges too, because a full graph view that hides how a subject joined
# a network is not a full graph view.
_STRUCTURAL_EDGE_TYPES = frozenset({
    "SHARES_EMPLOYER_WITH",
    "SHARES_ADDRESS_WITH",
    "SHARES_ALIAS_PATTERN_WITH",
})

# Statuses an inferred fact can carry. "reverted" exists because
# rejection.revert_rejection writes it; it is included in the graph so
# an investigator can see a rejection that was undone, but it is
# excluded from the legacy networks[] blocks, which have always shown
# only active/rejected.
_LEGACY_MEMBERSHIP_STATUSES = frozenset({"active", "rejected"})

# Safety valve. The traversal is bounded by construction (see the
# module docstring), so these caps should never fire on real case data
# — they exist so one pathological employer hub cannot return a
# 50k-element payload to a browser. Both overridable per deployment.
_MAX_NODES = int(os.getenv("FRAUD_NETWORK_MAX_NODES", "2000"))
_MAX_EDGES = int(os.getenv("FRAUD_NETWORK_MAX_EDGES", "6000"))

# Per-label business key used to build a stable, human-meaningful node
# id. elementId() is NOT usable for this: it changes across a database
# restore, so a frontend that persisted a selection or a saved layout
# against it would silently break. The label prefix matters — in this
# data case_id and subject_id are drawn from the SAME numeric space
# (case 658407433, subject 658636801), so a bare id would collide
# between a :Case and a :Subject.
_LABEL_KEYS: Dict[str, Tuple[str, ...]] = {
    "Case": ("case_id",),
    "Subject": ("subject_id",),
    "Allegation": ("allegation_id",),
    "Address": ("address_key",),
    "Alias": ("alias_value",),
    "Employer": ("employer_key",),
    "Commentary": ("comment_id",),
    "FraudNetwork": ("network_type", "network_key"),
    "Rejection": ("rejection_id",),
}

# Order matters only for nodes carrying more than one label, which this
# graph does not currently produce; it is here so that if ETL ever adds
# a secondary label the id stays deterministic instead of depending on
# whatever order the server happened to return labels() in.
_LABEL_PRIORITY: Tuple[str, ...] = (
    "Case", "Subject", "Allegation", "Employer", "Address", "Alias",
    "FraudNetwork", "Commentary", "Rejection",
)


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
_CASE_SUBGRAPH_QUERY = """
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


# ---------------------------------------------------------------------
# Value coercion
# ---------------------------------------------------------------------

def _to_jsonable(value: Any) -> Any:
    """
    Coerce a Neo4j property value into something json.dumps and Pydantic
    both accept.

    The old implementation never needed this because it hand-picked
    scalar string properties. properties(n) returns whatever is actually
    stored, and a graph loaded by a future ETL revision may hold
    neo4j.time.Date / DateTime / Duration or a spatial Point — none of
    which are JSON-serialisable, and all of which would surface as a
    500 from the route rather than as anything an investigator could act
    on. Converting here, once, is cheaper than auditing every writer.

    Unknown types degrade to str() rather than raising: a slightly ugly
    value on one property is a far better failure mode for a read-only
    visualisation than losing the entire graph.
    """
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    # neo4j.time.* and datetime.* all implement isoformat().
    iso = getattr(value, "isoformat", None)
    if callable(iso):
        try:
            return iso()
        except Exception:  # pragma: no cover - defensive
            pass
    return str(value)


def _clean_properties(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return {str(k): _to_jsonable(v) for k, v in (raw or {}).items()}


# ---------------------------------------------------------------------
# Node identity and presentation
# ---------------------------------------------------------------------

def _primary_label(labels: Sequence[str]) -> str:
    """The one label a node is presented as. Falls back to whatever the
    server returned first so an unmodelled label still renders."""
    for candidate in _LABEL_PRIORITY:
        if candidate in labels:
            return candidate
    return labels[0] if labels else "Node"


def _business_key(label: str, props: Dict[str, Any]) -> Optional[str]:
    """The stable, ETL-controlled key for a node, or None if the node
    somehow lacks it (a stub written by a partial load)."""
    parts: List[str] = []
    for field in _LABEL_KEYS.get(label, ()):
        value = props.get(field)
        if value in (None, ""):
            return None
        parts.append(str(value))
    return ":".join(parts) if parts else None


def _display_name(label: str, props: Dict[str, Any], key: str) -> str:
    """
    What the investigator reads on the node. Every branch ends at a
    guaranteed-present fallback (the key) so a node with sparse
    properties still renders as something clickable rather than blank.
    """
    if label == "Subject":
        person = " ".join(
            str(props[f]).strip()
            for f in ("first_name", "last_name")
            if props.get(f)
        ).strip()
        return person or props.get("company_name") or key
    if label == "Case":
        number = props.get("complaint_number")
        return f"Case {number}" if number else f"Case {key}"
    if label == "Allegation":
        return props.get("allegation_type") or f"Allegation {key}"
    if label == "Employer":
        return props.get("employer_name") or props.get("fein") or key
    if label == "Address":
        line = ", ".join(
            str(props[f]).strip()
            for f in ("street", "city", "state", "zip")
            if props.get(f)
        )
        return line or key
    if label == "Alias":
        return props.get("alias_value") or key
    if label == "FraudNetwork":
        network_type = props.get("network_type") or "Network"
        network_key = props.get("network_key")
        return f"{network_type}: {network_key}" if network_key else network_type
    if label == "Commentary":
        comment_type = str(props.get("comment_type") or "").strip()
        if comment_type:
            return comment_type
        text = str(props.get("comment_text") or "").strip()
        if text:
            return text[:117] + "..." if len(text) > 120 else text
        return key
    return key


# ---------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------

def _build_nodes(raw_nodes: Iterable[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    """
    Turn raw {ref, labels, properties, is_case_subject} rows into the
    node list the frontend renders, and return the elementId -> node id
    map the edge pass needs to rewrite its endpoints.

    A node whose business key is missing falls back to its elementId,
    prefixed the same way. That is an unstable id, and it is the honest
    one: the alternative is dropping the node and silently rendering a
    graph with a hole in it.
    """
    nodes: List[Dict[str, Any]] = []
    ref_to_id: Dict[str, str] = {}

    for row in raw_nodes:
        ref = row.get("ref")
        labels = list(row.get("labels") or [])
        props = _clean_properties(row.get("properties"))
        label = _primary_label(labels)

        key = _business_key(label, props)
        stable = key is not None
        if not stable:
            key = f"ref/{ref}"
        node_id = f"{label}:{key}"

        display_label = label

        ref_to_id[ref] = node_id
        nodes.append({
            "id": node_id,
            "ref": ref,
            "label": display_label,
            "labels": labels,
            "key": key,
            "display_name": _display_name(label, props, key),
            "is_case_subject": bool(row.get("is_case_subject")),
            "stable_id": stable,
            "properties": props,
        })

    nodes.sort(key=lambda n: (_LABEL_PRIORITY.index(n["label"])
                              if n["label"] in _LABEL_PRIORITY else 99,
                              n["id"]))
    return nodes, ref_to_id


def _build_edges(
    raw_edges: Iterable[Dict[str, Any]],
    ref_to_id: Dict[str, str],
    node_by_id: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Turn raw relationship rows into rendered edges.

    Every subject-to-subject edge also carries the four parameters POST
    /reject_inference wants (subject_id_a, subject_id_b,
    relationship_type, rule_id) pre-resolved onto the edge, so the UI's
    Reject button can read them straight off the clicked edge instead of
    reverse-engineering them from node ids. `rejectable` is the flag
    that tells the frontend whether to render the button at all: an
    ETL-sourced fact (APPEARS_IN_CASE, EMPLOYED_BY) is not an inference
    and there is nothing to reject about it.
    """
    edges: List[Dict[str, Any]] = []

    for row in raw_edges:
        source_id = ref_to_id.get(row.get("source_ref"))
        target_id = ref_to_id.get(row.get("target_ref"))
        # Both endpoints were required to be in the node set by the
        # query itself; a miss here would mean the driver returned a
        # relationship whose endpoints it did not return. Skip rather
        # than emit an edge pointing at nothing, which is what breaks a
        # Cytoscape render.
        if not source_id or not target_id:
            continue

        props = _clean_properties(row.get("properties"))
        rel_type = row.get("type")
        source_node = node_by_id.get(source_id, {})
        target_node = node_by_id.get(target_id, {})
        subject_to_subject = (
            source_node.get("label") == "Subject"
            and target_node.get("label") == "Subject"
        )
        rule_id = props.get("source_rule")

        edge: Dict[str, Any] = {
            "id": row.get("ref"),
            "source": source_id,
            "target": target_id,
            "relationship_type": rel_type,
            "confidence": props.get("confidence"),
            # ETL-sourced relationships carry no status. Reporting them
            # as "active" is correct and keeps the frontend from having
            # to special-case a null when it picks a line style.
            "status": props.get("status") or "active",
            "source_rule": rule_id,
            "inferred": bool(rule_id),
            "rejectable": bool(subject_to_subject and rule_id),
            "properties": props,
        }
        if subject_to_subject:
            edge["subject_id_a"] = source_node.get("key")
            edge["subject_id_b"] = target_node.get("key")
            edge["rule_id"] = rule_id
        edges.append(edge)

    edges.sort(key=lambda e: (str(e["relationship_type"]), e["source"], e["target"]))
    return edges


def _network_confidence(confidences: Sequence[Tuple[str, Optional[str]]]) -> str:
    """The network-level confidence Section D3's output contract asks
    for (network.confidence) — the strongest confidence among ACTIVE
    memberships, falling back to the strongest rejected one if every
    membership has been rejected, so a fully-rejected network still
    reports what it used to claim rather than a meaningless default.

    Takes (status, confidence) pairs so the caller does not have to
    build throwaway member dicts just to call it."""
    active = [c for status, c in confidences if status == "active" and c]
    pool = active or [c for _, c in confidences if c]
    if not pool:
        return "Unresolved"
    return max(pool, key=lambda c: _CONFIDENCE_ORDER.get(c, 0))


def _build_networks(
    nodes: Sequence[Dict[str, Any]],
    edges: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Rebuild the LEGACY networks[] blocks from the subgraph, byte-for-byte
    identical in shape to what the previous two-query implementation
    returned, so no existing consumer has to change.

    Semantics preserved exactly:
      * one block per :FraudNetwork the case touches;
      * membership statuses limited to active/rejected;
      * nodes[].is_primary means "this subject is on THIS case", not
        "this subject is central to the network";
      * rejected structural edges are kept, never filtered — the
        frontend renders them dashed (D3 Key Design Rule).
    """
    node_by_id = {n["id"]: n for n in nodes}
    networks: List[Dict[str, Any]] = []

    membership_by_network: Dict[str, List[Dict[str, Any]]] = {}
    for edge in edges:
        if edge["relationship_type"] != "MEMBER_OF_FRAUD_NETWORK":
            continue
        if edge["status"] not in _LEGACY_MEMBERSHIP_STATUSES:
            continue
        subject = node_by_id.get(edge["source"])
        network = node_by_id.get(edge["target"])
        # MEMBER_OF_FRAUD_NETWORK is written (Subject)->(FraudNetwork);
        # tolerate the reverse in case a future rule writes it the other
        # way rather than silently dropping the membership.
        if network and network["label"] == "Subject":
            subject, network = network, subject
        if not subject or not network or network.get("label") != "FraudNetwork":
            continue
        membership_by_network.setdefault(network["id"], []).append({
            "subject": subject,
            "confidence": edge.get("confidence"),
            "status": edge["status"],
        })

    for network_id, members in membership_by_network.items():
        network_node = node_by_id[network_id]
        props = network_node["properties"]
        member_ids = {m["subject"]["id"] for m in members}

        block_nodes = [
            {
                "id": m["subject"]["key"],
                "display_name": m["subject"]["display_name"],
                "is_primary": bool(m["subject"]["is_case_subject"]),
            }
            for m in members
        ]
        block_edges = [
            {
                "source": node_by_id[e["source"]]["key"],
                "target": node_by_id[e["target"]]["key"],
                "relationship_type": e["relationship_type"],
                "confidence": e["confidence"],
                "status": e["status"],
                "source_rule": e["source_rule"],
            }
            for e in edges
            if e["relationship_type"] in _STRUCTURAL_EDGE_TYPES
            and e["status"] in _LEGACY_MEMBERSHIP_STATUSES
            and e["source"] in member_ids
            and e["target"] in member_ids
        ]

        networks.append({
            "network_type": props.get("network_type"),
            "network_key": props.get("network_key"),
            "formed_by_rule": props.get("formed_by_rule"),
            "confidence": _network_confidence(
                [(m["status"], m["confidence"]) for m in members]
            ),
            "nodes": block_nodes,
            "edges": block_edges,
        })

    networks.sort(key=lambda n: (str(n["network_type"]), str(n["network_key"])))
    return networks


def _counts(items: Iterable[Dict[str, Any]], field: str) -> Dict[str, int]:
    tally: Dict[str, int] = {}
    for item in items:
        value = str(item.get(field))
        tally[value] = tally.get(value, 0) + 1
    return dict(sorted(tally.items()))


def _envelope(result: Dict[str, Any]) -> dict:
    return {
        "result": result,
        "provenance": graph_provenance("reasoning_layer.fraud_network.get_fraud_network"),
    }


def get_fraud_network(case_id: str) -> dict:
    """
    Assemble the full case subgraph for one case (D3 / Section 8.1).

    Args:
        case_id: required, non-empty.

    Returns (inside the standard {result, provenance} envelope):
        {
          "case_id": ...,
          "case_found": bool,          # False when the case is not in
                                       # the graph at all (not yet
                                       # synced) — distinct from a case
                                       # that is present but isolated

          # --- the full graph, everything related to this case --------
          "graph": {
            "nodes": [
              {
                "id":              "Subject:658636801",  # label-prefixed
                "ref":             "<elementId>",
                "label":           "Subject",
                "labels":          ["Subject"],
                "key":             "658636801",   # bare business key
                "display_name":    "Jane Doe",
                "is_case_subject": true,
                "stable_id":       true,
                "properties":      {...}          # all node properties
              }
            ],
            "edges": [
              {
                "id":                "<elementId>",
                "source":            "Subject:658636801",
                "target":            "Subject:658653191",
                "relationship_type": "SHARES_EMPLOYER_WITH",
                "confidence":        "High",
                "status":            "active" | "rejected" | "reverted",
                "source_rule":       "Rule_01_Shared_Employer",
                "inferred":          true,
                "rejectable":        true,
                # present on subject-to-subject edges only, pre-resolved
                # for the Reject button:
                "subject_id_a": "658636801",
                "subject_id_b": "658653191",
                "rule_id":      "Rule_01_Shared_Employer",
                "properties":   {...}
              }
            ],
            "node_count": int,
            "edge_count": int,
            "node_counts_by_label": {"Subject": 4, "Employer": 2, ...},
            "edge_counts_by_type":  {"SHARES_EMPLOYER_WITH": 1, ...},
            "truncated": bool
          },

          # --- legacy, unchanged in shape ----------------------------
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
    empty while graph still carries the case, its subjects and their
    evidence. Rejected edges are always included with status "rejected"
    — the frontend renders those with dashed styling per D3's Key
    Design Rule; this function never filters them out.

    Raises:
        ValueError: case_id missing or blank.
        GraphUnavailableError / Neo4jError: propagated unchanged.
    """
    if not case_id or not str(case_id).strip():
        raise ValueError("get_fraud_network requires a non-empty case_id")
    case_id = str(case_id).strip()

    with get_session() as session:
        record = session.run(
            _CASE_SUBGRAPH_QUERY,
            case_id=case_id,
            peer_rel_types=list(_PEER_REL_TYPES),
        ).single()

    # record is None only when no :Case node with this case_id exists.
    # That is a legitimate answer ("nothing synced for this case yet"),
    # not an error — the same stance check_network_match takes for an
    # unknown subject.
    raw_nodes = list(record["nodes"]) if record else []
    raw_edges = list(record["relationships"]) if record else []

    nodes, ref_to_id = _build_nodes(raw_nodes)
    node_by_id = {n["id"]: n for n in nodes}
    edges = _build_edges(raw_edges, ref_to_id, node_by_id)

    # Networks are derived BEFORE truncation so the legacy blocks stay
    # complete even in the pathological case where the raw graph had to
    # be capped for the renderer.
    networks = _build_networks(nodes, edges)

    truncated = len(nodes) > _MAX_NODES or len(edges) > _MAX_EDGES
    if truncated:
        logger.warning(
            "get_fraud_network: case_id=%s truncated (nodes=%d/%d edges=%d/%d)",
            case_id, len(nodes), _MAX_NODES, len(edges), _MAX_EDGES,
        )
        nodes = nodes[:_MAX_NODES]
        kept = {n["id"] for n in nodes}
        edges = [e for e in edges
                 if e["source"] in kept and e["target"] in kept][:_MAX_EDGES]

    result = {
        "case_id": case_id,
        "case_found": bool(record),
        "graph": {
            "nodes": nodes,
            "edges": edges,
            "node_count": len(nodes),
            "edge_count": len(edges),
            "node_counts_by_label": _counts(nodes, "label"),
            "edge_counts_by_type": _counts(edges, "relationship_type"),
            "truncated": truncated,
        },
        "networks": networks,
        "network_count": len(networks),
    }
    logger.info(
        "get_fraud_network: case_id=%s found=%s nodes=%d edges=%d networks=%d",
        case_id, result["case_found"], len(nodes), len(edges), len(networks),
    )
    return _envelope(result)