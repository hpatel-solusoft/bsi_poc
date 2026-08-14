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
from typing import Any, Dict, List, Optional

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
_RELATED_NETWORK_QUERY = """
MATCH (s:Subject {subject_id: $subject_id})-[r:SHARES_EMPLOYER_WITH]-(o:Subject)
WHERE r.status IN ["active", "rejected"]
RETURN "SHARES_EMPLOYER_WITH" AS relationship_type,
       o.subject_id AS counterpart_id, "Subject" AS counterpart_type,
       coalesce(o.full_name, o.name, o.subject_id) AS counterpart_label,
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
       coalesce(o.full_name, o.name, o.subject_id) AS counterpart_label,
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
       coalesce(o.full_name, o.name, o.subject_id) AS counterpart_label,
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
       c.case_id AS counterpart_label,
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
       c.case_id AS counterpart_label,
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
       c.case_id AS counterpart_label,
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
       c.case_id AS counterpart_label,
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


def _envelope(result: Dict[str, Any]) -> dict:
    """Standard {result, provenance} envelope (Principle 8) — identical in
    shape to reasoning_layer.graph_queries and reasoning_layer.similar_cases,
    so /generate_report can merge this into `sections` the same way every
    other direct-call graph read is merged."""
    return {
        "result": result,
        "provenance": graph_provenance("reasoning_layer.report_generation.assemble_related_network"),
    }


# resolve_reviewer_notation lives in reasoning_layer/rules_fired_view.py
# (next to build_rejection, the function that produces the SAME raw
# shape from a different call path) rather than here, so
# reasoning_layer/report_llm_context.py's flag-family rule notation
# (Rule 8/11/12/13 — never a "connection" this module's own
# _RELATED_NETWORK_QUERY can represent, see that module's own docstring)
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
        raw_rows = session.run(_RELATED_NETWORK_QUERY, subject_id=subject_id, case_id=case_id).data()

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