"""
Owns: deterministic structural similar-case matching (AI-14 / Sections
8.3, 9.2) — a single read-only Cypher query that replaces Phase 1's
non-deterministic two-step LLM type-selection (get_allegation_types then
search_similar_cases).

Section 8.3 defines the change precisely:
  * matching is a Cypher property match on allegation_type in Neo4j,
    deterministic — the LLM no longer decides what matches;
  * one query across THREE structural dimensions;
  * similarity is COMPUTED, not the hardcoded 1.0 of Phase 1:
        allegation type exact match  -> 0.5 base (the entry requirement)
        shared Employer FEIN         -> +0.25
        shared FraudNetwork membership -> +0.25
    giving a score in [0.5, 1.0];
  * new output fields: match_reasons (which dimensions matched) and
    source: "structural_graph".

The LLM's role becomes EXPLAINING what the graph found, never selecting it
(Section 8.3, 9.2 Turn 2). This module makes no LLM call and no AppWorks
call — it is a pure Neo4j read.

WHY THIS IS A DIRECT CALL, NOT A MANIFEST TOOL:
Section 9.2 sketches find_structural_similar_cases as a dispatcher-routed
tool, but it resolves to Neo4j, not AppWorks. Per the governance rule that
manifest.yaml holds a tool ONLY IF it is LLM-called AND makes an AppWorks
call, this is invoked directly by the /similar_cases route (the same
pattern as check_network_match and enrich_graph_context), and its result
is injected into the LLM's context so the LLM can explain it.

DETERMINISM (AI-14 todo — "same input must return the same results on
repeated runs"): the query aggregates with count(DISTINCT ...), derives the
score from booleans, and ORDERs BY score DESC, case_id ASC. There is no
LLM, no randomness, and a total order on ties (case_id), so repeated runs
on an unchanged graph return byte-identical output.

Does NOT own: the AppWorks search_similar_cases path (now unused for this
flow), the pipeline, or any write.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from reasoning_layer.neo4j_client import get_session
from utils.provenance import graph_provenance

logger = logging.getLogger(__name__)

# One read-only statement, three matching dimensions.
#
# Dimension 1 (base, required): the candidate case shares at least one
#   allegation_type with the active case. Cases with no shared type are not
#   similar and never appear — this is the +0.5 entry requirement, so every
#   returned case carries "allegation_type" in its reasons.
# Dimension 2 (+0.25): a subject on each case is EMPLOYED_BY the same
#   :Employer that carries a FEIN (the "shared Employer FEIN" signal).
# Dimension 3 (+0.25): a subject on each case is MEMBER_OF_FRAUD_NETWORK of
#   the same :FraudNetwork.
#
# Matches use lower-cased allegation_type so "PCA" and "pca" unify, mirroring
# the case-insensitive CONTAINS the rule library already uses.
_SIMILAR_CASES_QUERY = """
MATCH (c1:Case {case_id: $case_id})-[:HAS_ALLEGATION]->(a1:Allegation)
WITH c1, collect(DISTINCT toLower(a1.allegation_type)) AS c1_types
WHERE size(c1_types) > 0

MATCH (c2:Case)-[:HAS_ALLEGATION]->(a2:Allegation)
WHERE c2.case_id <> $case_id
  AND toLower(a2.allegation_type) IN c1_types
WITH c1, c2, collect(DISTINCT a2.allegation_type) AS shared_types

OPTIONAL MATCH (c1)<-[:APPEARS_IN_CASE]-(:Subject)-[:EMPLOYED_BY]->(e:Employer)
               <-[:EMPLOYED_BY]-(:Subject)-[:APPEARS_IN_CASE]->(c2)
WHERE e.fein IS NOT NULL
WITH c1, c2, shared_types, count(DISTINCT e) AS shared_employer_count

OPTIONAL MATCH (c1)<-[:APPEARS_IN_CASE]-(:Subject)-[:MEMBER_OF_FRAUD_NETWORK]->(fn:FraudNetwork)
               <-[:MEMBER_OF_FRAUD_NETWORK]-(:Subject)-[:APPEARS_IN_CASE]->(c2)
WITH c2, shared_types,
     shared_employer_count,
     count(DISTINCT fn) AS shared_network_count

WITH c2, shared_types,
     (shared_employer_count > 0) AS has_employer,
     (shared_network_count > 0)  AS has_network
WITH c2, shared_types,
     0.5
       + CASE WHEN has_employer THEN 0.25 ELSE 0.0 END
       + CASE WHEN has_network  THEN 0.25 ELSE 0.0 END AS similarity_score,
     [reason IN [
        "allegation_type",
        CASE WHEN has_employer THEN "shared_employer_fein" ELSE null END,
        CASE WHEN has_network  THEN "shared_fraud_network"  ELSE null END
     ] WHERE reason IS NOT NULL] AS match_reasons
RETURN
    c2.case_id           AS case_id,
    c2.complaint_number  AS complaint_no,
    c2.status            AS status,
    c2.fraud_amount      AS fraud_amount,
    c2.opened_date       AS date_opened,
    shared_types         AS matched_allegation_types,
    similarity_score,
    match_reasons
ORDER BY similarity_score DESC, case_id ASC
"""


def find_structural_matches(case_id: str, limit: int = 25) -> dict:
    """
    Return structurally similar cases for `case_id`, scored 0.5–1.0.

    Args:
        case_id: the active case to find matches for. Required, non-empty.
        limit:   maximum matches to return (already ordered strongest-first).

    Returns (inside the standard {result, provenance} envelope):
        {
          "matches": [
            { case_id, complaint_no, status, fraud_amount, date_opened,
              matched_allegation_types, similarity_score, match_reasons }
          ],
          "source": "structural_graph",
          "total_candidates_scored": int
        }

    An active case with no allegations, or one absent from the graph,
    yields an empty match list — not an error. That is the honest answer:
    nothing to match on.

    Raises:
        ValueError: on a missing/blank case_id.
        GraphUnavailableError / Neo4jError: propagated; the /similar_cases
            route degrades to an empty, clearly-unavailable section rather
            than failing.
    """
    if not case_id or not str(case_id).strip():
        raise ValueError("find_structural_matches requires a non-empty case_id")
    case_id = str(case_id).strip()

    with get_session() as session:
        rows = session.run(_SIMILAR_CASES_QUERY, case_id=case_id).data()

    matches: List[Dict[str, Any]] = [
        {
            "case_id": row["case_id"],
            "complaint_no": row.get("complaint_no"),
            "status": row.get("status"),
            "fraud_amount": row.get("fraud_amount"),
            "date_opened": row.get("date_opened"),
            "matched_allegation_types": list(row.get("matched_allegation_types") or []),
            "similarity_score": round(float(row["similarity_score"]), 2),
            "match_reasons": list(row.get("match_reasons") or []),
        }
        for row in rows
    ]
    total_scored = len(matches)
    if limit is not None and limit >= 0:
        matches = matches[:limit]

    logger.info(
        "find_structural_matches: case_id=%s candidates_scored=%d returned=%d",
        case_id, total_scored, len(matches),
    )

    return {
        "result": {
            "matches": matches,
            "source": "structural_graph",
            "total_candidates_scored": total_scored,
        },
        "provenance": graph_provenance("reasoning_layer.similar_cases"),
    }