"""
Owns: assembling the "Related Network" section for Report Generation
(AI-18 / Functional Specification Section 8.7, Developer Specification
Section 7.7) — every ACTIVE High/Medium-confidence inferred relationship
touching the case's Primary Subject, plus the case's Rule 8 / Rule 13
escalation flags, plus every REJECTED one of either kind, each carrying
its rejection notation (investigator, date, reason) when one exists.

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
from typing import Any, Dict, List

from reasoning_layer.neo4j_client import get_session
from reasoning_layer.rules_fired_view import resolve_reviewer_notation
from utils.provenance import graph_provenance

logger = logging.getLogger(__name__)

# Confidence tiers the Related Network section lists individually for
# ACTIVE facts (Section 8.7: "active High/Medium facts"). "Unresolved"
# active facts are real inferred facts and are still counted in
# confidence_summary — they are just not detailed line items, matching
# the same tiering rules_fired.py already uses for reporting quality.
_LISTED_ACTIVE_CONFIDENCE = {"High", "Medium"}

from reasoning_layer.queries.report_generation_queries import RELATED_NETWORK_QUERY

def _envelope(result: Dict[str, Any]) -> dict:
    """Standard {result, provenance} envelope (Principle 8) — identical in
    shape to reasoning_layer.graph_queries and reasoning_layer.similar_cases,
    so /generate_report can merge this into `sections` the same way every
    other direct-call graph read is merged."""
    return {
        "result": result,
        "provenance": graph_provenance("reasoning_layer.reports.report_generation.assemble_related_network"),
    }


# resolve_reviewer_notation lives in reasoning_layer/rules_fired_view.py
# (next to build_rejection, the function that produces the SAME raw
# shape from a different call path) rather than here, so
# reasoning_layer/report_llm_context.py's flag-family rule notation
# (Rule 8/11/12/13 — never a "connection" this module's own
# RELATED_NETWORK_QUERY can represent, see that module's own docstring)
# and this module's related_network notation resolve a rejected fact's
# reviewer/date/reason the exact same way, from one place, rather than
# two independent copies of the same "manual reject vs cascade
# invalidate, most recent wins" logic silently drifting apart.


def assemble_related_network(case_id: str, subject_id: str) -> dict:
    """
    Assemble the Related Network section for one Primary Subject
    (Section 8.7 / D1): every currently-active High/Medium-confidence
    inferred relationship touching them, plus every rejected one, each
    rejected entry carrying investigator/date/reason notation resolved
    from that relationship's OWN audit properties — see
    resolve_reviewer_notation's docstring for exactly how a manual
    reject and a cascade auto-invalidation are each recognised. A
    rejected fact is never silently omitted, regardless of confidence
    — Principle 14.

    Args:
        case_id: the case this report is being generated for. Required
            and non-empty — used both for logging/traceability AND, as
            of the Rule 8/13 branches above, to scope those two
            case-level escalation flags (the other six branches remain
            scoped purely by subject_id, since a relationship touching
            the Primary Subject is relevant to their file regardless of
            which case it was inferred from — only the two case-flag
            branches are case_id-scoped, matching what they actually
            assert onto).
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
        ValueError: case_id or subject_id missing or blank.
        GraphUnavailableError / Neo4jError: propagated unchanged, exactly
            as reasoning_layer.graph_queries.check_network_match does —
            this read has no fallback data source, and the route decides
            how a graph outage degrades for display.
    """
    if not subject_id or not str(subject_id).strip():
        raise ValueError("assemble_related_network requires a non-empty subject_id")
    if not case_id or not str(case_id).strip():
        raise ValueError("assemble_related_network requires a non-empty case_id")
    subject_id = str(subject_id).strip()
    case_id = str(case_id).strip()

    with get_session() as session:
        raw_rows = session.run(RELATED_NETWORK_QUERY, subject_id=subject_id, case_id=case_id).data()

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
            entry["rejection"] = resolve_reviewer_notation(row.get("rejection_raw"), row.get("source_rule"))
            # Never silently omitted: a rejected fact is listed with
            # whatever notation resolves — including an all-None one, if
            # neither a reject nor an invalidate candidate has an actor —
            # rather than dropped. See resolve_reviewer_notation's own
            # docstring for how a manual reject and a cascade
            # auto-invalidation are each recognised.

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
        case_id,
        subject_id,
        len(related_network),
        counts["high"],
        counts["medium"],
        counts["unresolved"],
        rejected_count,
    )
    return _envelope(result)
