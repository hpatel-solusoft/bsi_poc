"""
Owns: assembling the "Related Network" section for Report Generation
(AI-18 / Functional Specification Section 8.7, Developer Specification
Section 7.7) — every ACTIVE High/Medium-confidence inferred relationship
touching the case's Primary Subject, plus every REJECTED one, each
carrying its rejection notation (investigator, date, reason) when one
exists.

Why this is a separate module from reasoning_layer/rules_fired.py, not a
reuse of it: rules_fired.py assembles a fixed 14-entry, per-RULE
aggregate ("did Rule_01 fire, at what confidence, across the run's
scope") — Functional Specification A.4 is explicit that block is
assembled in exactly one place and never reconstructed by a caller, and
this module does not touch it. A report reader needs the opposite grain:
per-FACT detail scoped to one subject ("who, specifically, is the
Primary Subject connected to, and why") — rules_fired's aggregate counts
cannot answer that, and scope_subject_ids there covers the whole
investigation scope (co-subjects included), not just the Primary
Subject Section 8.7 asks for. So this module runs its own read, and
leaves rules_fired's contract untouched.

Same governance as reasoning_layer/graph_queries.py and
reasoning_layer/similar_cases.py: a direct, unconditional Python call
made by api/server.py's /generate_report route — never an LLM tool,
never dispatcher-routed, never registered in manifest.yaml (it is a
Neo4j read, not an AppWorks call — the manifest governs the latter
only). The LLM's role downstream is to narrate what this module found,
never to decide what belongs in the Related Network section (guideline
Section 2 / Functional Spec 8.7: "LLM used for narrative prose only,
not graph data assembly").

Does NOT own: the reasoning pipeline (pipeline.py), rule content
(rules/*.cypher), rules_fired assembly (rules_fired.py), report
persistence (core/report_artifacts_repository.py), or report narrative
generation (agent_service/prompt_builders.py).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from reasoning_layer.neo4j_client import get_session
from utils.provenance import graph_provenance

logger = logging.getLogger(__name__)

# Confidence tiers the Related Network section lists individually for
# ACTIVE facts (Section 8.7: "active High/Medium facts"). "Unresolved"
# active facts are real inferred facts and are still counted in
# confidence_summary — they are just not detailed line items, matching
# the same tiering rules_fired.py already uses for reporting quality.
_LISTED_ACTIVE_CONFIDENCE = {"High", "Medium"}

# One UNION ALL block per relationship-writing rule shape (mirrors the
# rule set reasoning_layer/rules_fired.py._REL_RULES enumerates, minus
# Rule_14 — a corroboration modifier on an existing edge, not a distinct
# network connection). Each branch returns an identical column set so the
# UNION is valid, and each is filtered to relationships that actually
# touch $subject_id — this is the Primary-Subject-scoped read Section
# 8.7 asks for, deliberately narrower than rules_fired's whole-scope
# aggregate.
#
# from_key/to_key are carried through unevaluated so Python can correlate
# a rejected instance to its :Rejection record without this module having
# to re-derive each rule's key encoding — that encoding belongs to the
# rule file that wrote it (rules/*.cypher), not here.
_RELATED_NETWORK_QUERY = """
MATCH (s:Subject {subject_id: $subject_id})-[r:SHARES_EMPLOYER_WITH]-(o:Subject)
WHERE r.status IN ["active", "rejected"]
RETURN "SHARES_EMPLOYER_WITH" AS relationship_type,
       o.subject_id AS counterpart_id, "Subject" AS counterpart_type,
       coalesce(o.full_name, o.name, o.subject_id) AS counterpart_label,
       r.source_rule AS source_rule, r.confidence AS confidence,
       coalesce(r.corroborated, false) AS corroborated, r.status AS status,
       toString(r.asserted_at) AS asserted_at,
       s.subject_id AS from_key, o.subject_id AS to_key

UNION ALL
MATCH (s:Subject {subject_id: $subject_id})-[r:SHARES_ADDRESS_WITH]-(o:Subject)
WHERE r.status IN ["active", "rejected"]
RETURN "SHARES_ADDRESS_WITH" AS relationship_type,
       o.subject_id AS counterpart_id, "Subject" AS counterpart_type,
       coalesce(o.full_name, o.name, o.subject_id) AS counterpart_label,
       r.source_rule AS source_rule, r.confidence AS confidence,
       coalesce(r.corroborated, false) AS corroborated, r.status AS status,
       toString(r.asserted_at) AS asserted_at,
       s.subject_id AS from_key, o.subject_id AS to_key

UNION ALL
MATCH (s:Subject {subject_id: $subject_id})-[r:SHARES_ALIAS_PATTERN_WITH]-(o:Subject)
WHERE r.status IN ["active", "rejected"]
RETURN "SHARES_ALIAS_PATTERN_WITH" AS relationship_type,
       o.subject_id AS counterpart_id, "Subject" AS counterpart_type,
       coalesce(o.full_name, o.name, o.subject_id) AS counterpart_label,
       r.source_rule AS source_rule, r.confidence AS confidence,
       coalesce(r.corroborated, false) AS corroborated, r.status AS status,
       toString(r.asserted_at) AS asserted_at,
       s.subject_id AS from_key, o.subject_id AS to_key

UNION ALL
MATCH (s:Subject {subject_id: $subject_id})-[r:MEMBER_OF_FRAUD_NETWORK]->(n:FraudNetwork)
WHERE r.status IN ["active", "rejected"]
RETURN "MEMBER_OF_FRAUD_NETWORK" AS relationship_type,
       n.network_key AS counterpart_id, "FraudNetwork" AS counterpart_type,
       (n.network_type + " fraud network") AS counterpart_label,
       r.source_rule AS source_rule, r.confidence AS confidence,
       coalesce(r.corroborated, false) AS corroborated, r.status AS status,
       toString(r.asserted_at) AS asserted_at,
       s.subject_id AS from_key, (n.network_type + ":" + n.network_key) AS to_key

UNION ALL
MATCH (s:Subject {subject_id: $subject_id})-[r:HAS_PRIOR_GUILTY_CASE]->(c:Case)
WHERE r.status IN ["active", "rejected"]
RETURN "HAS_PRIOR_GUILTY_CASE" AS relationship_type,
       c.case_id AS counterpart_id, "Case" AS counterpart_type,
       c.case_id AS counterpart_label,
       r.source_rule AS source_rule, r.confidence AS confidence,
       coalesce(r.corroborated, false) AS corroborated, r.status AS status,
       toString(r.asserted_at) AS asserted_at,
       s.subject_id AS from_key, c.case_id AS to_key

UNION ALL
MATCH (s:Subject {subject_id: $subject_id})-[r:APPEARS_IN_CASE]->(c:Case)
WHERE r.source_rule = "Rule_10_Merged_Case_Propagation"
  AND r.status IN ["active", "rejected"]
RETURN "APPEARS_IN_CASE" AS relationship_type,
       c.case_id AS counterpart_id, "Case" AS counterpart_type,
       c.case_id AS counterpart_label,
       r.source_rule AS source_rule, r.confidence AS confidence,
       coalesce(r.corroborated, false) AS corroborated, r.status AS status,
       toString(r.asserted_at) AS asserted_at,
       s.subject_id AS from_key, c.case_id AS to_key
"""

# Every :Rejection currently in force (status "active" on the Rejection
# node itself — see reasoning_layer/schema.cypher) that names this
# subject on either side. relationship_type + from_key/to_key is the
# only correlation key available (Data Persistence C.1) — there is no
# foreign key from a Rejection to the specific relationship instance it
# suppressed, by design (a Rejection is written before re-assertion is
# attempted again, per Section 9.4).
_REJECTIONS_QUERY = """
MATCH (rej:Rejection)
WHERE rej.status = "active"
  AND (rej.from_key = $subject_id OR rej.to_key = $subject_id)
RETURN rej.relationship_type AS relationship_type,
       rej.from_key AS from_key, rej.to_key AS to_key,
       rej.rejected_by AS investigator_id, rej.rejected_at AS rejected_at,
       rej.reason AS reason, rej.rule_id AS rule_id
"""


def _envelope(result: Dict[str, Any]) -> dict:
    """Standard {result, provenance} envelope (Principle 8) — identical in
    shape to reasoning_layer.graph_queries and reasoning_layer.similar_cases,
    so /generate_report can merge this into `sections` the same way every
    other direct-call graph read is merged."""
    return {
        "result": result,
        "provenance": graph_provenance("reasoning_layer.report_generation.assemble_related_network"),
    }


def _find_rejection(
    entry: Dict[str, Any], rejections: List[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    """Best-effort correlation of one rejected relationship instance to
    its :Rejection notation, by relationship_type plus order-insensitive
    from_key/to_key membership. Rule guard queries (rules/*.cypher) key a
    Rejection by an unordered pair for every symmetric subject-subject
    type and by an exact directed pair for subject->case / subject->
    network types — checking membership both ways covers both shapes
    without this module re-deriving each rule's own encoding."""
    pair = {entry["from_key"], entry["to_key"]}
    for rej in rejections:
        if rej["relationship_type"] != entry["relationship_type"]:
            continue
        if {rej["from_key"], rej["to_key"]} == pair:
            return rej
    return None


def assemble_related_network(case_id: str, subject_id: str) -> dict:
    """
    Assemble the Related Network section for one Primary Subject
    (Section 8.7 / D1): every currently-active High/Medium-confidence
    inferred relationship touching them, plus every rejected one, each
    rejected entry carrying investigator/date/reason notation when a
    matching :Rejection record exists. A rejected fact is never silently
    omitted, regardless of confidence — Principle 14.

    Args:
        case_id: the case this report is being generated for. Used only
            for logging/traceability; the graph read itself is scoped by
            subject_id, since a relationship touching the Primary
            Subject is relevant to their file regardless of which case
            it was inferred from.
        subject_id: the case's Primary Subject. Required and non-empty.

    Returns (inside the standard {result, provenance} envelope):
        {
          "subject_id": ...,
          "related_network": [
            {relationship_type, counterpart_id, counterpart_type,
             counterpart_label, source_rule, confidence, corroborated,
             status, asserted_at,
             rejection: {investigator_id, rejected_at, reason, rule_id} | None}
          ],
          "confidence_summary": {"high": int, "medium": int, "unresolved": int},
          "rejected_count": int,
        }

    A subject absent from the graph, or with no relationships of the
    types above, is not an error: related_network is empty and every
    confidence_summary count is 0 — the honest answer to "what is this
    subject connected to" when nothing is known, not a fabricated blank
    network.

    Raises:
        ValueError: subject_id missing or blank.
        GraphUnavailableError / Neo4jError: propagated unchanged, exactly
            as reasoning_layer.graph_queries.check_network_match does —
            this read has no fallback data source, and the route decides
            how a graph outage degrades for display.
    """
    if not subject_id or not str(subject_id).strip():
        raise ValueError("assemble_related_network requires a non-empty subject_id")
    subject_id = str(subject_id).strip()

    with get_session() as session:
        raw_rows = session.run(_RELATED_NETWORK_QUERY, subject_id=subject_id).data()
        rejection_rows = session.run(_REJECTIONS_QUERY, subject_id=subject_id).data()

    counts = {"high": 0, "medium": 0, "unresolved": 0}
    related_network: List[Dict[str, Any]] = []

    for row in raw_rows:
        status = row.get("status") or "active"
        confidence = row.get("confidence") or "Unresolved"

        if status == "active":
            counts[confidence.lower()] = counts.get(confidence.lower(), 0) + 1
            if confidence not in _LISTED_ACTIVE_CONFIDENCE:
                # Real active fact, counted above, just not itemised —
                # Unresolved-confidence facts are not yet reportable
                # findings (same tiering rules_fired.py applies).
                continue

        entry: Dict[str, Any] = {
            "relationship_type": row["relationship_type"],
            "counterpart_id": row["counterpart_id"],
            "counterpart_type": row["counterpart_type"],
            "counterpart_label": row["counterpart_label"],
            "source_rule": row["source_rule"],
            "confidence": confidence,
            "corroborated": bool(row.get("corroborated")),
            "status": status,
            "asserted_at": row.get("asserted_at"),
            "rejection": None,
        }

        if status == "rejected":
            match = _find_rejection(row, rejection_rows)
            entry["rejection"] = (
                {
                    "investigator_id": match["investigator_id"],
                    "rejected_at": match["rejected_at"],
                    "reason": match["reason"],
                    "rule_id": match["rule_id"],
                }
                if match
                else {
                    "investigator_id": None,
                    "rejected_at": None,
                    "reason": None,
                    "rule_id": None,
                }
            )
            # Never silently omitted: a rejected fact is listed with a
            # blank notation rather than dropped, if no :Rejection record
            # correlates — the gap itself is worth surfacing, not hiding.

        related_network.append(entry)

    rejected_count = sum(1 for e in related_network if e["status"] == "rejected")

    result = {
        "subject_id": subject_id,
        "related_network": related_network,
        "confidence_summary": counts,
        "rejected_count": rejected_count,
    }
    logger.info(
        "assemble_related_network: case_id=%s subject_id=%s entries=%d "
        "(high=%d medium=%d unresolved=%d) rejected=%d",
        case_id, subject_id, len(related_network),
        counts["high"], counts["medium"], counts["unresolved"], rejected_count,
    )
    return _envelope(result)